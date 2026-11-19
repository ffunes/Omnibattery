"""Migration 17 -> 18: the backup offgrid threshold and its number are removed.

Needs the real hass: run with ``-o addopts=""``.
"""
from __future__ import annotations


async def test_v18_drops_the_threshold_and_its_number(hass) -> None:
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.omnibattery import async_migrate_entry

    entry = MockConfigEntry(
        domain="omnibattery",
        version=17,
        data={"batteries": [{"name": "Venus", "backup_offgrid_threshold": 2500}]},
    )
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    old = reg.async_get_or_create(
        "number", "omnibattery", "192.0.2.10_502_backup_offgrid_threshold",
        config_entry=entry,
    )
    kept = reg.async_get_or_create(
        "number", "omnibattery", "192.0.2.10_502_max_soc", config_entry=entry
    )

    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 18
    assert entry.data["batteries"] == [{"name": "Venus"}]
    assert reg.async_get(old.entity_id) is None
    assert reg.async_get(kept.entity_id) is not None
