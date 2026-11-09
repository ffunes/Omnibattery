"""Seamless migration of config + entity registry between integration domains.

Spike artifact: this is the orchestration that the *new* domain's ``async_setup``
will run once, on first start after the rebrand, to move every config entry and
its entity registry entries from the old domain to the new one **without**
changing any ``entity_id`` or ``unique_id``. Keeping those identical is what
preserves recorder history and long-term statistics (both keyed by ``entity_id``
/ ``statistic_id``), and avoids the ``_2`` suffixes HA would otherwise mint if a
fresh entry re-registered the same unique ids under a new platform.

It is deliberately standalone (not yet wired into ``async_setup``) so it can be
unit-tested in isolation before the rename is committed.

Recipe, per old config entry:
  1. If LOADED, unload it. ``async_update_entity_platform`` refuses to migrate an
     entity that still has a live state, so the entities must be torn down first.
  2. Create a mirror ``ConfigEntry`` on the new domain (same data/options/
     unique_id/source/version) but ``disabled_by`` set, and ``async_add`` it.
     ``async_add`` always calls setup, but a disabled entry returns early and
     loads no platforms — so the new entry exists without grabbing the entities.
  3. Re-point every registry entry of the old entry to the new platform +
     new config entry id, leaving ``entity_id`` and ``unique_id`` untouched.
     This must happen *before* removing the old entry: ``async_remove`` clears
     the registry entries still linked to it.
  4. Remove the old config entry, then copy the integration's ``.storage`` Store
     files into the new ``{domain}.{entry_id}`` key namespace (daily energy,
     accumulators, balance history) so persisted counters survive the rebrand.
  5. Clear ``disabled_by`` on the new entry. That reloads it, and now its
     platforms set up: ``async_get_or_create(new_domain, unique_id)`` re-finds
     the migrated registry entries and reuses their ``entity_id`` verbatim.
"""
from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path
from types import MappingProxyType

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryDisabler,
    ConfigEntryState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)


def _migrate_storage_files(
    storage_dir: str,
    old_domain: str,
    new_domain: str,
    old_entry_id: str,
    new_entry_id: str,
) -> list[str]:
    """Copy the integration's ``.storage`` Store files into the new key namespace.

    Store files are named after their key — ``{domain}.{entry_id}.<name>`` for
    per-entry stores (daily energy, accumulators, balance/hourly history, weekly
    charge state, …) and the domain-only ``{domain}_<name>``. A rebrand changes
    *both* the domain and the entry_id, so the new entry would otherwise look up
    keys that don't exist and silently reset every persisted counter (the daily
    "energy today" totals, etc.). This rewrites each old file to the new name and
    fixes the ``key`` field stored inside it. Recorder history is unaffected (it
    lives in the DB keyed by ``entity_id``), which is why weekly/long-term data
    survives even without this. Blocking IO — run in the executor.

    Returns the list of new filenames written.
    """
    written: list[str] = []
    base = Path(storage_dir)
    if not base.is_dir():
        return written
    for old_path in base.iterdir():
        if not old_path.is_file():
            continue
        name = old_path.name
        # The config backup (config_backup.py) is intentionally domain-stable:
        # its filename is the literal legacy domain so both builds read one file.
        # Never rename it into the new domain or the new build can't find it.
        if name.endswith(".config_backup"):
            continue
        if not (
            name == old_domain
            or name.startswith(f"{old_domain}.")
            or name.startswith(f"{old_domain}_")
        ):
            continue
        new_name = name.replace(old_domain, new_domain, 1)
        if old_entry_id:
            new_name = new_name.replace(old_entry_id, new_entry_id)
        new_path = base / new_name
        if new_path.exists():
            continue
        try:
            content = json.loads(old_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            _LOGGER.warning("Skipping storage file %s during migration: %s", name, err)
            continue
        if isinstance(content, dict) and "key" in content:
            content["key"] = new_name
        new_path.write_text(json.dumps(content), encoding="utf-8")
        written.append(new_name)
    return written


async def async_migrate_legacy_domain_entries(
    hass: HomeAssistant,
    old_domain: str,
    new_domain: str,
) -> list[tuple[str, str]]:
    """Migrate all config entries from ``old_domain`` to ``new_domain``.

    Returns a list of ``(old_entry_id, new_entry_id)`` pairs for the entries
    that were migrated (empty if there was nothing to do).
    """
    registry = er.async_get(hass)
    migrated: list[tuple[str, str]] = []

    for old in list(hass.config_entries.async_entries(old_domain)):
        # 1. Tear down live entities so the registry entries can be migrated.
        if old.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(old.entry_id)

        # 2. Mirror the entry on the new domain, disabled so it doesn't load yet.
        #    ConfigEntry's constructor signature drifts across HA cores
        #    (``discovery_keys`` was added; ``subentries_data`` became required).
        #    Build the full kwarg set, then keep only what this HA accepts so the
        #    migration works on both older and newer cores.
        candidate_kwargs = {
            "version": old.version,
            "minor_version": old.minor_version,
            "domain": new_domain,
            "title": old.title,
            "data": dict(old.data),
            "options": dict(old.options),
            "source": old.source,
            "unique_id": old.unique_id,
            "disabled_by": ConfigEntryDisabler.USER,
            # ``discovery_keys`` arrived in HA 2024.10; hacs.json allows
            # 2024.4.1, where reading it raised AttributeError and aborted
            # the migration. Unsupported kwargs are filtered out below.
            "discovery_keys": getattr(old, "discovery_keys", MappingProxyType({})),
            "subentries_data": [
                {
                    "subentry_id": se.subentry_id,
                    "subentry_type": se.subentry_type,
                    "title": se.title,
                    "unique_id": se.unique_id,
                    "data": dict(se.data),
                }
                for se in getattr(old, "subentries", {}).values()
            ],
        }
        accepted = inspect.signature(ConfigEntry).parameters
        new_entry = ConfigEntry(
            **{k: v for k, v in candidate_kwargs.items() if k in accepted}
        )
        await hass.config_entries.async_add(new_entry)

        # 3. Re-point the registry entries (entity_id + unique_id unchanged).
        for entry in er.async_entries_for_config_entry(registry, old.entry_id):
            # A registered entity whose config entry is unloaded — or whose
            # integration is gone after the rename — gets a restored
            # ``unavailable`` placeholder state from the entity registry.
            # ``async_update_entity_platform`` only migrates entities with no
            # live state (``None`` or ``unknown``), so drop the placeholder
            # first; the new entry recreates a fresh state when it loads.
            if hass.states.get(entry.entity_id) is not None:
                hass.states.async_remove(entry.entity_id)
            registry.async_update_entity_platform(
                entry.entity_id,
                new_domain,
                new_config_entry_id=new_entry.entry_id,
            )

        # 4. Drop the old entry (its registry links are already gone).
        await hass.config_entries.async_remove(old.entry_id)

        # 4b. Carry over the integration's .storage Store files (daily energy,
        #     accumulators, balance/hourly history, weekly charge state). They are
        #     keyed by {domain}.{entry_id}, both of which change in a rebrand, so
        #     without this the new entry starts with every persisted counter reset
        #     to 0 (e.g. the dashboard's "energy today" totals).
        await hass.async_add_executor_job(
            _migrate_storage_files,
            hass.config.path(".storage"),
            old_domain,
            new_domain,
            old.entry_id,
            new_entry.entry_id,
        )

        # 5. Enable the new entry -> it loads and re-adopts the migrated entities.
        await hass.config_entries.async_set_disabled_by(new_entry.entry_id, None)

        migrated.append((old.entry_id, new_entry.entry_id))

    return migrated
