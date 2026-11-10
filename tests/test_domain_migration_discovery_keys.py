"""The legacy-domain migration survives a core without ``discovery_keys``.

``ConfigEntry.discovery_keys`` arrived in HA 2024.10, while hacs.json allows
2024.4.1. Reading it directly raised AttributeError and aborted the migration.
"""
from __future__ import annotations

from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import custom_components.omnibattery.domain_migration as domain_migration


class _NewEntry:
    """Records the kwargs the migration builds the new entry from."""

    created: list[dict] = []

    def __init__(
        self,
        *,
        version,
        minor_version,
        domain,
        title,
        data,
        options,
        source,
        unique_id,
        disabled_by,
        discovery_keys,
    ):
        self.entry_id = "new-entry"
        _NewEntry.created.append(
            {"domain": domain, "discovery_keys": discovery_keys, "data": data}
        )


async def test_an_entry_without_discovery_keys_is_still_migrated(monkeypatch):
    _NewEntry.created = []
    monkeypatch.setattr(domain_migration, "ConfigEntry", _NewEntry)
    monkeypatch.setattr(domain_migration.er, "async_get", lambda hass: Mock())
    monkeypatch.setattr(
        domain_migration.er, "async_entries_for_config_entry", lambda reg, eid: []
    )
    # A pre-2024.10 entry: no ``discovery_keys`` and no ``subentries``.
    old = SimpleNamespace(
        entry_id="old-entry",
        state=None,
        version=1,
        minor_version=1,
        title="Marstek Venus",
        data={"host": "1.2.3.4"},
        options={},
        source="user",
        unique_id="1.2.3.4_502",
    )
    config_entries = SimpleNamespace(
        async_entries=lambda domain: [old],
        async_unload=AsyncMock(),
        async_add=AsyncMock(),
        async_remove=AsyncMock(),
        async_set_disabled_by=AsyncMock(),
    )
    hass = SimpleNamespace(
        config_entries=config_entries,
        config=SimpleNamespace(path=lambda name: name),
        async_add_executor_job=AsyncMock(),
        states=Mock(),
    )

    migrated = await domain_migration.async_migrate_legacy_domain_entries(
        hass, "marstek_venus_energy_manager", "omnibattery"
    )

    assert migrated == [("old-entry", "new-entry")]
    assert _NewEntry.created == [
        {
            "domain": "omnibattery",
            "discovery_keys": MappingProxyType({}),
            "data": {"host": "1.2.3.4"},
        }
    ]
    config_entries.async_remove.assert_awaited_once_with("old-entry")
