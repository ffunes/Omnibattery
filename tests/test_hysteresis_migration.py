"""Tests for the v7 -> v8 config-entry migration that makes charge hysteresis
mandatory.

Spec: hysteresis is no longer optional. Per battery:
  * ``enable_charge_hysteresis`` is forced ``True``;
  * a battery that already had it enabled keeps its configured percent;
  * a battery that had it off (or unset) gets the ``MIN_CHARGE_HYSTERESIS_PERCENT``
    floor;
  * any value is clamped up to the floor so SOC drift can't shrink the deadband.

A config entry on version 7 exercises the v8 branch (data-only). The newer v9
branch heals the entity registry, which the light no-``hass``-fixture fakes can't
provide, so we patch the two entity_registry helpers it calls to no-op here (the
v9 heal has its own dedicated registry test). v10 renames the title, handled by
accepting the kwarg in the fake; v11 adds the disabled-by-default phase schema
and v12 adds the persisted Fronius ownership boundary.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.omnibattery import async_migrate_entry
from custom_components.omnibattery.const import (
    MIN_CHARGE_HYSTERESIS_PERCENT,
)


def _no_registry():
    """Patch the entity_registry helpers the v9 heal touches into a no-op."""
    return patch.multiple(
        "homeassistant.helpers.entity_registry",
        async_get=lambda hass: object(),
        async_entries_for_config_entry=lambda reg, entry_id: [],
    )


class _FakeConfigEntries:
    """Captures the data/version handed to ``async_update_entry``."""

    def __init__(self):
        self.updated = None

    def async_update_entry(self, entry, *, data, version, title=None):
        self.updated = {"data": data, "version": version}
        entry.data = data
        entry.version = version


def _migrate(batteries):
    hass = SimpleNamespace(config_entries=_FakeConfigEntries())
    entry = SimpleNamespace(version=7, entry_id="entry", data={"batteries": batteries})
    with _no_registry():
        result = asyncio.run(async_migrate_entry(hass, entry))
    assert result is True
    assert hass.config_entries.updated["version"] == 12
    return hass.config_entries.updated["data"]["batteries"]


def test_enabled_battery_keeps_configured_percent():
    out = _migrate([{"enable_charge_hysteresis": True, "charge_hysteresis_percent": 8}])
    assert out[0]["enable_charge_hysteresis"] is True
    assert out[0]["charge_hysteresis_percent"] == 8


def test_disabled_battery_gets_floor():
    out = _migrate([{"enable_charge_hysteresis": False, "charge_hysteresis_percent": 7}])
    # Was off -> ignore the stale percent, apply the floor and force on.
    assert out[0]["enable_charge_hysteresis"] is True
    assert out[0]["charge_hysteresis_percent"] == MIN_CHARGE_HYSTERESIS_PERCENT


def test_unset_battery_gets_floor():
    out = _migrate([{}])
    assert out[0]["enable_charge_hysteresis"] is True
    assert out[0]["charge_hysteresis_percent"] == MIN_CHARGE_HYSTERESIS_PERCENT


def test_enabled_below_floor_is_clamped_up():
    out = _migrate([{"enable_charge_hysteresis": True, "charge_hysteresis_percent": 1}])
    assert out[0]["charge_hysteresis_percent"] == MIN_CHARGE_HYSTERESIS_PERCENT


def test_v11_adds_safe_fronius_ownership_default():
    hass = SimpleNamespace(config_entries=_FakeConfigEntries())
    entry = SimpleNamespace(
        version=11,
        data={
            "batteries": [
                {"brand": "fronius_gen24"},
                {"brand": "marstek"},
            ]
        },
    )
    assert asyncio.run(async_migrate_entry(hass, entry)) is True
    assert hass.config_entries.updated["version"] == 12
    batteries = hass.config_entries.updated["data"]["batteries"]
    assert batteries[0]["fronius_internal_control_disabled"] is True
    assert "fronius_internal_control_disabled" not in batteries[1]


def test_already_v12_is_noop():
    hass = SimpleNamespace(config_entries=_FakeConfigEntries())
    entry = SimpleNamespace(version=12, data={"batteries": [{}]})
    assert asyncio.run(async_migrate_entry(hass, entry)) is True
    assert hass.config_entries.updated is None
