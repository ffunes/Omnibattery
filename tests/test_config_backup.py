"""Tests for the config-backup recovery path (config_backup.py).

The seamless domain migration only fires while a *live* legacy entry exists. If
the user instead deletes the integration entirely, there is nothing to migrate.
The backup Store is the safety net: written on setup, it survives a config-entry
deletion (HA doesn't touch arbitrary integration Stores), so the new-domain flow
can recreate the entry — data, options and .storage counters — from it.

Run WITHOUT the suite's default ``-p no:homeassistant`` flag, e.g.::

    .venv-test/Scripts/python -m pytest tests/test_config_backup.py -o addopts=""
"""
from __future__ import annotations

import json
import os
from unittest.mock import Mock

from homeassistant.config_entries import ConfigEntryState, ConfigFlow
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntity,
    MockModule,
    MockPlatform,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from custom_components.omnibattery.config_backup import (
    BACKUP_KEY,
    async_has_config_backup,
    async_load_config_backup,
    async_restore_config_backup,
    async_save_config_backup,
)

DOMAIN = "omnibattery"
LEGACY_DOMAIN = "marstek_venus_energy_manager"

ENTITIES = [("marstek_venus_system_solar_power", "Marstek Venus System Solar Power")]
ENTRY_DATA = {"batteries": [{"name": "B1", "host": "1.2.3.4", "port": 502}]}
ENTRY_OPTIONS = {"pd_controller_kp": 0.42}
ENTRY_UNIQUE_ID = "1.2.3.4_502"


class _MockFlow(ConfigFlow):
    VERSION = 1
    MINOR_VERSION = 1


async def _platform_setup_entry(hass, entry, async_add_entities):
    async_add_entities(MockEntity(unique_id=uid, name=name) for uid, name in ENTITIES)


def _register_domain(hass: HomeAssistant) -> None:
    async def async_setup_entry(hass, entry):
        await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])
        return True

    async def async_unload_entry(hass, entry):
        return await hass.config_entries.async_unload_platforms(entry, [Platform.SENSOR])

    mock_integration(
        hass,
        MockModule(
            DOMAIN,
            async_setup_entry=async_setup_entry,
            async_unload_entry=async_unload_entry,
        ),
    )
    mock_platform(
        hass, f"{DOMAIN}.sensor", MockPlatform(async_setup_entry=_platform_setup_entry)
    )
    mock_platform(hass, f"{DOMAIN}.config_flow", Mock())


async def _mount_entry(hass: HomeAssistant) -> MockConfigEntry:
    _register_domain(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Omnibattery",
        unique_id=ENTRY_UNIQUE_ID,
        data=ENTRY_DATA,
        options=ENTRY_OPTIONS,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_save_load_roundtrip(hass: HomeAssistant) -> None:
    """A live entry is snapshot verbatim (data, options, unique_id) to the Store."""
    with mock_config_flow(DOMAIN, _MockFlow):
        entry = await _mount_entry(hass)

    assert not await async_has_config_backup(hass)
    await async_save_config_backup(hass)
    assert await async_has_config_backup(hass)

    records = await async_load_config_backup(hass)
    assert len(records) == 1
    rec = records[0]
    assert rec["domain"] == DOMAIN
    assert rec["entry_id"] == entry.entry_id
    assert rec["data"] == ENTRY_DATA
    assert rec["options"] == ENTRY_OPTIONS
    assert rec["unique_id"] == ENTRY_UNIQUE_ID


async def test_restore_after_full_delete(hass: HomeAssistant) -> None:
    """Restore recreates the entry from the backup once everything was deleted."""
    with mock_config_flow(DOMAIN, _MockFlow):
        entry = await _mount_entry(hass)
        await async_save_config_backup(hass)

        # Full delete: drop the live entry. Backup Store is untouched by this.
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.config_entries.async_entries(DOMAIN) == []
        assert await async_has_config_backup(hass)

        new_ids = await async_restore_config_backup(hass)
        await hass.async_block_till_done()

        assert len(new_ids) == 1
        entries = hass.config_entries.async_entries(DOMAIN)
        assert len(entries) == 1
        restored = entries[0]
        assert restored.entry_id == new_ids[0]
        assert restored.entry_id != entry.entry_id  # fresh entry
        assert restored.state is ConfigEntryState.LOADED
        assert dict(restored.data) == ENTRY_DATA
        assert dict(restored.options) == ENTRY_OPTIONS
        assert restored.unique_id == ENTRY_UNIQUE_ID


async def test_restore_carries_storage_files(hass: HomeAssistant) -> None:
    """Daily-counter Store files follow the restore into the new entry_id."""
    with mock_config_flow(DOMAIN, _MockFlow):
        entry = await _mount_entry(hass)
        await async_save_config_backup(hass)

        storage_dir = hass.config.path(".storage")
        os.makedirs(storage_dir, exist_ok=True)
        old_name = f"{DOMAIN}.{entry.entry_id}.daily_energy"
        with open(os.path.join(storage_dir, old_name), "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "key": old_name, "data": {"home_kwh": 9.0}}, fh)

        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

        new_id = (await async_restore_config_backup(hass))[0]
        await hass.async_block_till_done()

        new_name = os.path.join(storage_dir, f"{DOMAIN}.{new_id}.daily_energy")
        assert os.path.exists(new_name)
        with open(new_name, encoding="utf-8") as fh:
            moved = json.load(fh)
        assert moved["data"] == {"home_kwh": 9.0}
        assert moved["key"] == f"{DOMAIN}.{new_id}.daily_energy"


async def test_backup_key_is_domain_stable() -> None:
    """The Store key must stay the legacy literal so both builds share one file."""
    assert BACKUP_KEY == f"{LEGACY_DOMAIN}.config_backup"
    assert DOMAIN not in BACKUP_KEY


async def test_restore_keeps_store_data_the_entry_writes_on_setup(
    hass: HomeAssistant,
) -> None:
    """A real entry saves its Stores as soon as it loads.

    Adding the restored entry enabled loaded it before the old files were
    copied, so it wrote default Stores first and the copy (which skips
    existing files) lost the user's counters.
    """
    storage_dir = hass.config.path(".storage")
    os.makedirs(storage_dir, exist_ok=True)

    with mock_config_flow(DOMAIN, _MockFlow):
        entry = await _mount_entry(hass)
        await async_save_config_backup(hass)
        old_name = f"{DOMAIN}.{entry.entry_id}.daily_energy"
        with open(os.path.join(storage_dir, old_name), "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "key": old_name, "data": {"home_kwh": 9.0}}, fh)
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

        async def setup_writing_defaults(hass, entry):
            path = os.path.join(storage_dir, f"{DOMAIN}.{entry.entry_id}.daily_energy")
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"version": 1, "key": "x", "data": {"home_kwh": 0.0}}, fh)
            return True

        async def unload(hass, entry):
            return True

        mock_integration(
            hass,
            MockModule(
                DOMAIN,
                async_setup_entry=setup_writing_defaults,
                async_unload_entry=unload,
            ),
        )

        new_id = (await async_restore_config_backup(hass))[0]
        await hass.async_block_till_done()

        restored = hass.config_entries.async_get_entry(new_id)
        assert restored.disabled_by is None
        assert restored.state is ConfigEntryState.LOADED
        with open(
            os.path.join(storage_dir, f"{DOMAIN}.{new_id}.daily_energy"),
            encoding="utf-8",
        ) as fh:
            assert json.load(fh)["data"] == {"home_kwh": 9.0}
