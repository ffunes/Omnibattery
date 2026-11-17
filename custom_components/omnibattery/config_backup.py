"""Side copy of the config entries so a full delete stays recoverable.

The seamless :mod:`.domain_migration` only fires while a *live* legacy config
entry still exists. A HACS rebrand can instead leave a user in a state where
they delete the **integration** (config entry) — e.g. because the broken HACS
state won't let them remove the repository cleanly. That wipes
``core.config_entries`` and the entity registry, so the migration has nothing to
grab and the user must reconfigure every option (PD tuning, slots, thresholds)
from scratch.

This keeps a copy of every config entry's data + options in a Store under a
**fixed, domain-independent** key, written on setup and on every options change.
A config-entry deletion does not touch arbitrary integration Stores, so the copy
survives it. The new-domain config flow reads it to offer a one-click restore
when neither the legacy nor the new domain has any entries left.

The key is the literal legacy domain so the file is byte-identical before and
after the rebrand (the same philosophy that keeps the ``marstek_venus_*``
``entity_id`` literals unchanged for recorder continuity).
"""
from __future__ import annotations

import inspect
import logging
from types import MappingProxyType

from homeassistant.config_entries import ConfigEntry, ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util.json import load_json

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Fixed across the rebrand so the marstek-domain bridge build and the omnibattery
# build share one file. NEVER derive this from DOMAIN. _migrate_storage_files
# skips any ``*.config_backup`` file so the rebrand migration never renames it.
BACKUP_KEY = "marstek_venus_energy_manager.config_backup"
BACKUP_VERSION = 1


def _store(hass: HomeAssistant) -> Store:
    return Store(hass, BACKUP_VERSION, BACKUP_KEY)


def _serialize(entry: ConfigEntry) -> dict:
    return {
        "domain": entry.domain,
        "entry_id": entry.entry_id,
        "version": entry.version,
        "minor_version": entry.minor_version,
        "title": entry.title,
        "data": dict(entry.data),
        "options": dict(entry.options),
        "unique_id": entry.unique_id,
        "source": entry.source,
    }


async def async_save_config_backup(hass: HomeAssistant) -> None:
    """Snapshot all of this domain's config entries to the backup Store."""
    records = [_serialize(e) for e in hass.config_entries.async_entries(DOMAIN)]
    if not records:
        return
    await _store(hass).async_save({"entries": records})


async def async_load_config_backup(hass: HomeAssistant) -> list[dict]:
    """Return the saved entry records (empty list if none)."""
    data = await _store(hass).async_load()
    if not data:
        return []
    return data.get("entries", [])


async def async_has_config_backup(hass: HomeAssistant) -> bool:
    """True if a previous configuration backup is available to restore."""
    return bool(await async_load_config_backup(hass))


async def _async_carry_storage_files(
    hass: HomeAssistant, old_domain: str, old_entry_id: str, new_entry_id: str
) -> None:
    """Copy the deleted entry's Store files into the new entry's namespace.

    Each copy is saved once more through Store. Home Assistant lists the storage
    files at startup and a Store treats a key outside that list as having no
    data, so a file written next to Store stays invisible until the next restart.
    A file that can't be read back or isn't a Store is skipped, so it can't stop
    the restore.
    """
    # Imported lazily: the marstek-domain bridge build ships this module for the
    # *writer* only and has no domain_migration module. Restore is never called
    # on the source domain, so this import only resolves where it exists.
    from .domain_migration import _migrate_storage_files

    written = await hass.async_add_executor_job(
        _migrate_storage_files,
        hass.config.path(".storage"),
        old_domain,
        DOMAIN,
        old_entry_id,
        new_entry_id,
    )
    for name in written:
        try:
            content = await hass.async_add_executor_job(
                load_json, hass.config.path(".storage", name)
            )
            await Store(
                hass,
                content["version"],
                name,
                minor_version=content.get("minor_version", 1),
            ).async_save(content["data"])
        except (HomeAssistantError, OSError, KeyError, TypeError) as err:
            _LOGGER.warning("Skipping storage file %s during restore: %r", name, err)


async def async_restore_config_backup(hass: HomeAssistant) -> list[str]:
    """Recreate config entries from the backup under the current domain.

    Used only when the integration was deleted entirely (no live legacy or
    current entries). The entities re-register with their unchanged
    ``unique_id``, so they reclaim their clean ``entity_id`` — and the recorder
    history keyed by it — with no ``_2`` suffixes (nothing conflicts, the old
    registry entries went with the deleted config entry). The integration's
    ``.storage`` Store files are copied into the new ``entry_id`` namespace so
    daily counters and learned profiles survive too.

    Returns the new entry_ids created.
    """
    records = await async_load_config_backup(hass)
    accepted = inspect.signature(ConfigEntry).parameters
    new_ids: list[str] = []
    for rec in records:
        candidate_kwargs = {
            "version": rec["version"],
            "minor_version": rec["minor_version"],
            "domain": DOMAIN,
            "title": rec["title"],
            "data": dict(rec["data"]),
            "options": dict(rec["options"]),
            "source": rec.get("source", "user"),
            "unique_id": rec.get("unique_id"),
            # Added disabled so it does not load (and save default Stores)
            # before the old Store files are copied into its namespace:
            # the copy skips files that already exist.
            "disabled_by": ConfigEntryDisabler.USER,
            "discovery_keys": MappingProxyType({}),
            "subentries_data": [],
        }
        new_entry = ConfigEntry(
            **{k: v for k, v in candidate_kwargs.items() if k in accepted}
        )
        await hass.config_entries.async_add(new_entry)

        # Carry the persisted Store files (daily energy, accumulators, history)
        # from the deleted entry's namespace into the new one. The .storage files
        # outlive a config-entry deletion just like this backup does.
        await _async_carry_storage_files(
            hass,
            rec.get("domain", DOMAIN),
            rec.get("entry_id", ""),
            new_entry.entry_id,
        )
        # Enable it -> it loads with the restored Store data.
        await hass.config_entries.async_set_disabled_by(new_entry.entry_id, None)
        new_ids.append(new_entry.entry_id)
    return new_ids
