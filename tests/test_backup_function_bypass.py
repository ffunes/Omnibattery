"""Tests for ``_is_backup_function_active``: only a reported grid outage excludes.

A load on the backup port never decides (#540, #660), and no inverter state
does either: the battery leaves automatic control only while its driver says a
grid outage has it feeding the backup output. Huawei is the one driver that
does (its off-grid bit).

The method is exercised unbound with light stubs for ``self`` and the
coordinator (same pattern as test_charge_hysteresis.py).
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.drivers.huawei import HuaweiSolarDriver
from tests.conftest import _FakeMarstekDriver

_HUAWEI = SimpleNamespace(
    on_backup_power=lambda data: HuaweiSolarDriver.on_backup_power(None, data)
)


class _Coord:
    """Identity-hashable coordinator stand-in (used as a dict key)."""

    name = "Battery"

    def __init__(self, data, driver):
        self.data = data
        self.driver = driver


def _active(coord, controller=None):
    controller = controller or SimpleNamespace(_backup_cooldown_until={})
    return ChargeDischargeController._is_backup_function_active(controller, coord)


@pytest.mark.parametrize("inverter_state", [2, 3, 4, 6, None])
def test_a_marstek_is_never_excluded(inverter_state):
    """#660: a 122 W rack on the port; not even the Backup state (4) excludes."""
    controller = SimpleNamespace(_backup_cooldown_until={})
    coord = _Coord(
        {"backup_function": 0, "ac_offgrid_power": 122, "inverter_state": inverter_state},
        _FakeMarstekDriver(),
    )
    assert _active(coord, controller) is False
    assert coord not in controller._backup_cooldown_until


@pytest.mark.parametrize(
    ("backup_function", "expected"),
    [("Off-grid", True), ("Ready", False), ("Disabled", False)],
)
def test_huawei_is_excluded_only_while_off_grid(backup_function, expected):
    controller = SimpleNamespace(_backup_cooldown_until={})
    coord = _Coord({"backup_function": backup_function, "ac_offgrid_power": 900}, _HUAWEI)
    assert _active(coord, controller) is expected
    assert (coord in controller._backup_cooldown_until) is expected


def test_cooldown_after_the_outage_still_runs():
    coord = _Coord({"backup_function": "Ready"}, _HUAWEI)
    controller = SimpleNamespace(
        _backup_cooldown_until={coord: dt_util.utcnow() + timedelta(minutes=3)}
    )
    assert _active(coord, controller) is True


def test_expired_cooldown_releases():
    coord = _Coord({"backup_function": "Ready"}, _HUAWEI)
    controller = SimpleNamespace(
        _backup_cooldown_until={coord: dt_util.utcnow() - timedelta(minutes=1)}
    )
    assert _active(coord, controller) is False
    assert coord not in controller._backup_cooldown_until


# --- Migration 17 -> 18 (needs the real hass: run with -o addopts="") ----------


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
    assert entry.version == 21
    assert entry.data["batteries"] == [{"name": "Venus"}]
    assert reg.async_get(old.entity_id) is None
    assert reg.async_get(kept.entity_id) is not None
