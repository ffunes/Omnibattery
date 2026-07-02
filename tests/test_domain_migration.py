"""Integration tests for the seamless domain-migration helper.

These mount a fake *old* domain integration with two entities and a user
customization, run ``async_migrate_legacy_domain_entries``, and assert the
migration is seamless: identical ``entity_id`` / ``unique_id``, no ``_2``
suffixes, user customizations preserved, old entry gone, new entry LOADED.

Requires the in-process ``hass`` fixture, so the HA test plugin must be active:
run WITHOUT the suite's default ``-p no:homeassistant`` flag, e.g.::

    .venv-test/Scripts/python -m pytest tests/test_domain_migration.py -o addopts=""

The repo's ``tests/conftest.py`` lifts the socket guard at ``pytest_configure``
so the asyncio self-pipe can be built on a Windows dev box too.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest
from homeassistant.config_entries import ConfigEntryState, ConfigFlow
from homeassistant.const import STATE_UNAVAILABLE, Platform
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntity,
    MockModule,
    MockPlatform,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from custom_components.omnibattery import async_migrate_entry
from custom_components.omnibattery.domain_migration import (
    async_migrate_legacy_domain_entries,
)
from custom_components.omnibattery.migration_flow import (
    LegacyDomainMigrationMixin,
    async_has_legacy_entries,
)

OLD_DOMAIN = "marstek_venus_energy_manager"
NEW_DOMAIN = "omnibattery"

# Hardcoded unique ids + display names that survive the rebrand unchanged. The
# entity_id slug HA derives from each name is what the recorder indexes by.
ENTITIES = [
    ("marstek_venus_system_solar_power", "Marstek Venus System Solar Power"),
    ("marstek_venus_system_battery_power", "Marstek Venus System Battery Power"),
]

ENTRY_DATA = {"host": "1.2.3.4", "port": 502, "battery_version": "v2"}
ENTRY_OPTIONS = {"pd_controller_kp": 0.35}
ENTRY_UNIQUE_ID = "1.2.3.4_502"


class _MockFlow(ConfigFlow):
    """Minimal flow handler so config-entry setup/migrate checks pass.

    VERSION/MINOR_VERSION match the MockConfigEntry defaults (1/1) so
    ``ConfigEntry.async_migrate`` short-circuits to True (no migration needed).
    """

    VERSION = 1
    MINOR_VERSION = 1


@pytest.fixture
def _flow_handlers():
    """Register the flow handler for both domains for the whole test."""
    with mock_config_flow(OLD_DOMAIN, _MockFlow), mock_config_flow(
        NEW_DOMAIN, _MockFlow
    ):
        yield


async def _platform_setup_entry(hass, entry, async_add_entities):
    """Sensor platform that adds the same entities for whichever domain owns it."""
    async_add_entities(
        MockEntity(unique_id=uid, name=name) for uid, name in ENTITIES
    )


def _register_domain(hass: HomeAssistant, domain: str) -> None:
    """Register a fake integration + sensor platform under ``domain``."""

    async def async_setup_entry(hass, entry):
        await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])
        return True

    async def async_unload_entry(hass, entry):
        return await hass.config_entries.async_unload_platforms(
            entry, [Platform.SENSOR]
        )

    mock_integration(
        hass,
        MockModule(
            domain,
            async_setup_entry=async_setup_entry,
            async_unload_entry=async_unload_entry,
        ),
    )
    mock_platform(
        hass,
        f"{domain}.sensor",
        MockPlatform(async_setup_entry=_platform_setup_entry),
    )
    # Config-entry setup imports the config_flow platform (version/migrate check).
    mock_platform(hass, f"{domain}.config_flow", Mock())


async def _setup_old_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Mount the old-domain entry with its two entities live."""
    _register_domain(hass, OLD_DOMAIN)
    _register_domain(hass, NEW_DOMAIN)

    old_entry = MockConfigEntry(
        domain=OLD_DOMAIN,
        title="Marstek Venus",
        unique_id=ENTRY_UNIQUE_ID,
        data=ENTRY_DATA,
        options=ENTRY_OPTIONS,
    )
    old_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(old_entry.entry_id)
    await hass.async_block_till_done()
    return old_entry


async def test_migration_is_seamless(hass: HomeAssistant, _flow_handlers) -> None:
    """Full recipe: migrate old -> new with zero entity_id/unique_id drift."""
    old_entry = await _setup_old_entry(hass)
    registry = er.async_get(hass)

    reg_entries = er.async_entries_for_config_entry(registry, old_entry.entry_id)
    assert len(reg_entries) == 2

    # --- user customization BEFORE migration: rename one entity_id + assign area.
    area = ar.async_get(hass).async_get_or_create("Living Room")
    target = reg_entries[0]
    registry.async_update_entity(
        target.entity_id,
        new_entity_id="sensor.my_renamed_solar",
        name="My Renamed Solar",
    )
    registry.async_update_entity("sensor.my_renamed_solar", area_id=area.id)
    await hass.async_block_till_done()

    # Snapshot unique_id -> entity_id (post-rename) to prove invariance later.
    pre = er.async_entries_for_config_entry(registry, old_entry.entry_id)
    pre_map = {e.unique_id: e.entity_id for e in pre}
    assert "sensor.my_renamed_solar" in pre_map.values()

    # --- run the migration.
    migrated = await async_migrate_legacy_domain_entries(hass, OLD_DOMAIN, NEW_DOMAIN)
    await hass.async_block_till_done()

    assert len(migrated) == 1
    old_id, new_id = migrated[0]
    assert old_id == old_entry.entry_id

    # --- config entry side: old gone, new present + LOADED, data preserved.
    assert hass.config_entries.async_entries(OLD_DOMAIN) == []
    new_entries = hass.config_entries.async_entries(NEW_DOMAIN)
    assert len(new_entries) == 1
    new_entry = new_entries[0]
    assert new_entry.entry_id == new_id
    assert new_entry.state is ConfigEntryState.LOADED
    assert new_entry.unique_id == ENTRY_UNIQUE_ID
    assert dict(new_entry.data) == ENTRY_DATA
    assert dict(new_entry.options) == ENTRY_OPTIONS

    # --- registry side: same entity_ids + unique_ids, repointed to new platform.
    post = er.async_entries_for_config_entry(registry, new_entry.entry_id)
    post_map = {e.unique_id: e.entity_id for e in post}

    # entity_id identical per unique_id (covers "no _2 suffix").
    assert post_map == pre_map
    assert not any(e.entity_id.endswith("_2") for e in post)
    for e in post:
        assert e.platform == NEW_DOMAIN
        assert e.config_entry_id == new_entry.entry_id

    # --- user customizations survived the migration.
    custom = registry.async_get("sensor.my_renamed_solar")
    assert custom is not None
    assert custom.name == "My Renamed Solar"
    assert custom.area_id == area.id
    # statistic_id == entity_id, and entity_id is unchanged above, so long-term
    # statistics_meta stays attached without simulating the recorder here.


async def test_migration_carries_storage_files(
    hass: HomeAssistant, _flow_handlers
) -> None:
    """The integration's .storage Store files follow the rebrand.

    Daily energy / accumulators / history are persisted in Store files keyed by
    ``{domain}.{entry_id}`` (plus a domain-only consumption history). Both the
    domain and entry_id change, so the helper must copy them — otherwise the new
    entry resets every persisted counter (the dashboard's "energy today" totals).
    """
    import json
    import os

    old_entry = await _setup_old_entry(hass)
    storage_dir = hass.config.path(".storage")
    os.makedirs(storage_dir, exist_ok=True)

    per_entry_old = f"{OLD_DOMAIN}.{old_entry.entry_id}.daily_energy"
    domain_old = f"{OLD_DOMAIN}_consumption_history"
    with open(os.path.join(storage_dir, per_entry_old), "w", encoding="utf-8") as fh:
        json.dump(
            {"version": 1, "key": per_entry_old,
             "data": {"date": "2026-06-21", "home_kwh": 8.0}}, fh
        )
    with open(os.path.join(storage_dir, domain_old), "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "key": domain_old, "data": {"history": [1, 2, 3]}}, fh)

    migrated = await async_migrate_legacy_domain_entries(hass, OLD_DOMAIN, NEW_DOMAIN)
    await hass.async_block_till_done()
    _old_id, new_id = migrated[0]

    per_entry_new = os.path.join(storage_dir, f"{NEW_DOMAIN}.{new_id}.daily_energy")
    domain_new = os.path.join(storage_dir, f"{NEW_DOMAIN}_consumption_history")
    assert os.path.exists(per_entry_new)
    assert os.path.exists(domain_new)

    with open(per_entry_new, encoding="utf-8") as fh:
        moved = json.load(fh)
    # data preserved verbatim; the embedded key is rewritten to the new filename.
    assert moved["data"] == {"date": "2026-06-21", "home_kwh": 8.0}
    assert moved["key"] == f"{NEW_DOMAIN}.{new_id}.daily_energy"


async def test_restored_unavailable_state_blocks_raw_migration(
    hass: HomeAssistant, _flow_handlers
) -> None:
    """Documents the obstacle the helper's state-removal step exists to clear.

    Unloading a config entry (or losing the integration after the rename) does
    not remove the registered entities' states — the entity registry writes a
    restored ``unavailable`` placeholder. ``async_update_entity_platform`` only
    migrates entities with no live state (``None``/``unknown``), so that
    placeholder blocks a naive migration until it is removed.
    """
    old_entry = await _setup_old_entry(hass)
    registry = er.async_get(hass)
    entity_id = er.async_entries_for_config_entry(registry, old_entry.entry_id)[
        0
    ].entity_id

    await hass.config_entries.async_unload(old_entry.entry_id)
    state = hass.states.get(entity_id)
    assert state is not None and state.state == STATE_UNAVAILABLE

    with pytest.raises(ValueError, match="haven't been loaded"):
        registry.async_update_entity_platform(
            entity_id, NEW_DOMAIN, new_config_entry_id="dummy"
        )

    # Removing the placeholder (what the helper does) clears the obstacle.
    hass.states.async_remove(entity_id)
    assert hass.states.get(entity_id) is None


class _SpikeMigrationFlow(LegacyDomainMigrationMixin, ConfigFlow):
    """Stand-in for the real new-domain flow: route to migration when legacy."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if async_has_legacy_entries(self.hass):
            return await self.async_step_migrate_legacy()
        return self.async_abort(reason="no_legacy_entries")


async def test_config_flow_triggers_migration(hass: HomeAssistant) -> None:
    """The trigger: 'Add integration' on the new domain migrates legacy entries.

    This is the only entry point HA always exposes when the new domain has no
    config entries yet (the deadlock that blocks a passive async_setup migration
    after a HACS rename).
    """
    with mock_config_flow(OLD_DOMAIN, _MockFlow), mock_config_flow(
        NEW_DOMAIN, _SpikeMigrationFlow
    ):
        old_entry = await _setup_old_entry(hass)
        registry = er.async_get(hass)
        pre_map = {
            e.unique_id: e.entity_id
            for e in er.async_entries_for_config_entry(registry, old_entry.entry_id)
        }

        # Start "Add Omnibattery" -> detects legacy entries -> confirm form.
        result = await hass.config_entries.flow.async_init(
            NEW_DOMAIN, context={"source": "user"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "migrate_legacy"
        assert result["description_placeholders"]["count"] == "1"

        # Confirm -> runs the helper -> aborts with success (no entry created here).
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "migration_successful"
        await hass.async_block_till_done()

        # Same seamless outcome as the direct-helper test, via the flow trigger.
        assert hass.config_entries.async_entries(OLD_DOMAIN) == []
        new_entries = hass.config_entries.async_entries(NEW_DOMAIN)
        assert len(new_entries) == 1
        assert new_entries[0].state is ConfigEntryState.LOADED
        post = er.async_entries_for_config_entry(registry, new_entries[0].entry_id)
        assert {e.unique_id: e.entity_id for e in post} == pre_map
        assert all(e.platform == NEW_DOMAIN for e in post)


# --- Single-entry, real-heal end-to-end check (issue #31) -------------------
#
# Everything above uses a stable unique_id on both sides (no v9 heal involved).
# System entities are different: pre-rebrand they keyed unique_id on the config
# entry_id, and the real v8->v9 heal (custom_components.omnibattery.async_migrate_entry)
# is what re-keys them onto the literal `marstek_venus_system_` prefix sensor.py
# now uses. This exercises that real function end-to-end, for a SINGLE old entry,
# to check whether the ordinary (non-race) case actually preserves the historical
# entity_id -- i.e. whether issue #31 needs 2 old entries to reproduce at all.

SYSTEM_UID_PREFIX = "marstek_venus_system_"
HISTORICAL_EID = "sensor.marstek_venus_system_discharge_window"


class _OldSystemFlow(ConfigFlow):
    """Stand-in for the pre-rebrand marstek_venus_energy_manager config flow."""

    VERSION = 8
    MINOR_VERSION = 1


class _NewSystemFlow(ConfigFlow):
    """Stand-in for the real Omnibattery config flow (config_flow.py VERSION=10)."""

    VERSION = 10
    MINOR_VERSION = 1


async def _old_system_platform_setup(hass, entry, async_add_entities):
    """Mimic the real pre-rebrand sensor.py: DischargeWindowSensor.

    unique_id = f"{entry.entry_id}_discharge_window" (entry-id-prefixed);
    entity_id explicitly set via english_entity_id(...), which slugifies to the
    same clean id regardless of which entry created it.
    """
    e = MockEntity(
        unique_id=f"{entry.entry_id}_discharge_window",
        entity_id=HISTORICAL_EID,
        name="Marstek Venus System Discharge Window",
    )
    async_add_entities([e])


async def _new_system_platform_setup(hass, entry, async_add_entities):
    """Mimic the real current sensor.py: DischargeWindowSensor.

    unique_id is the literal SYSTEM_UNIQUE_ID_PREFIX (entry-independent);
    entity_id is explicitly suggested via system_entity_id(), i.e. `omnibattery_*`.
    """
    e = MockEntity(
        unique_id=f"{SYSTEM_UID_PREFIX}discharge_window",
        entity_id="sensor.omnibattery_discharge_window",
        name="Discharge Window",
    )
    async_add_entities([e])


def _register_system_domain(hass: HomeAssistant, domain: str, platform_setup) -> None:
    async def async_setup_entry(hass, entry):
        await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])
        return True

    async def async_unload_entry(hass, entry):
        return await hass.config_entries.async_unload_platforms(
            entry, [Platform.SENSOR]
        )

    mock_integration(
        hass,
        MockModule(
            domain,
            async_setup_entry=async_setup_entry,
            async_unload_entry=async_unload_entry,
            # The real heal (v8 -> v9) only runs for the NEW domain, but wiring
            # it for both is harmless -- the OLD domain's entry starts at its own
            # matching version so migrate() short-circuits as "up to date".
            async_migrate_entry=async_migrate_entry,
        ),
    )
    mock_platform(hass, f"{domain}.sensor", MockPlatform(async_setup_entry=platform_setup))
    mock_platform(hass, f"{domain}.config_flow", Mock())


async def test_single_old_entry_system_entity_survives_seamless_migration(
    hass: HomeAssistant,
) -> None:
    """Does issue #31 need 2 old entries, or does a single one already break?

    One old config entry, one system entity, real ``async_migrate_entry`` (the
    v9 heal) wired in exactly as production does. If this passes, the historical
    ``sensor.marstek_venus_system_discharge_window`` id survives on an ordinary
    single-entry install, and issue #31 needs the 2-old-entries race to
    reproduce. If it fails, the bug is universal and the race theory is wrong.
    """
    with mock_config_flow(OLD_DOMAIN, _OldSystemFlow), mock_config_flow(
        NEW_DOMAIN, _NewSystemFlow
    ):
        _register_system_domain(hass, OLD_DOMAIN, _old_system_platform_setup)
        _register_system_domain(hass, NEW_DOMAIN, _new_system_platform_setup)

        old_entry = MockConfigEntry(
            domain=OLD_DOMAIN, title="Marstek Venus", version=8, minor_version=1,
            data={},
        )
        old_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(old_entry.entry_id)
        await hass.async_block_till_done()

        reg = er.async_get(hass)
        pre = reg.async_get(HISTORICAL_EID)
        assert pre is not None
        assert pre.unique_id == f"{old_entry.entry_id}_discharge_window"

        migrated = await async_migrate_legacy_domain_entries(hass, OLD_DOMAIN, NEW_DOMAIN)
        await hass.async_block_till_done()

        assert len(migrated) == 1
        new_entry = hass.config_entries.async_entries(NEW_DOMAIN)[0]
        assert new_entry.state is ConfigEntryState.LOADED
        # Confirms the real async_migrate_entry ran all the way through (proves
        # the v9 heal block actually executed, not skipped).
        assert new_entry.version == 10

        final = reg.async_get(HISTORICAL_EID)
        assert final is not None, "historical entity_id should survive"
        assert final.unique_id == f"{SYSTEM_UID_PREFIX}discharge_window", (
            "should have been healed onto the literal uid"
        )
        # The fresh omnibattery_* id must NOT exist as a second, competing entity.
        assert reg.async_get("sensor.omnibattery_discharge_window") is None
