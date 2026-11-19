"""Tests for ``_is_backup_function_active``: only a grid outage excludes.

A load on the backup port is the same permanent rack or router with the grid
present as during an outage, so it never decides (#540, #660). The battery is
excluded only while the driver reports the outage itself: the Backup inverter
state, or Huawei's off-grid bit.

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

INVERTER_CHARGE = 2
INVERTER_DISCHARGE = 3
INVERTER_BACKUP = 4
INVERTER_BYPASS = 6


class _Coord:
    """Identity-hashable coordinator stand-in (used as a dict key)."""

    name = "Venus"
    driver = _FakeMarstekDriver()

    def __init__(self, data):
        self.data = data


def _active(coord, controller=None):
    controller = controller or SimpleNamespace(_backup_cooldown_until={})
    return ChargeDischargeController._is_backup_function_active(controller, coord)


def _data(*, offgrid, inverter_state, backup_function=0):
    return {
        "backup_function": backup_function,
        "ac_offgrid_power": offgrid,
        "inverter_state": inverter_state,
    }


@pytest.mark.parametrize(
    "inverter_state",
    [INVERTER_CHARGE, INVERTER_DISCHARGE, INVERTER_BYPASS, None, "Backup"],
)
def test_port_load_with_the_grid_present_is_not_backup(inverter_state):
    """#660: a 122 W rack on the port while the battery charges or discharges."""
    controller = SimpleNamespace(_backup_cooldown_until={})
    coord = _Coord(_data(offgrid=122, inverter_state=inverter_state))
    assert _active(coord, controller) is False
    assert coord not in controller._backup_cooldown_until


@pytest.mark.parametrize("offgrid", [0, 122, None])
def test_backup_state_is_backup_whatever_the_port_load(offgrid):
    controller = SimpleNamespace(_backup_cooldown_until={})
    coord = _Coord(_data(offgrid=offgrid, inverter_state=INVERTER_BACKUP))
    assert _active(coord, controller) is True
    assert coord in controller._backup_cooldown_until


def test_cooldown_after_outage_still_runs():
    coord = _Coord(_data(offgrid=122, inverter_state=INVERTER_BYPASS))
    controller = SimpleNamespace(
        _backup_cooldown_until={coord: dt_util.utcnow() + timedelta(minutes=3)}
    )
    assert _active(coord, controller) is True


def test_expired_cooldown_releases():
    coord = _Coord(_data(offgrid=122, inverter_state=INVERTER_BYPASS))
    controller = SimpleNamespace(
        _backup_cooldown_until={coord: dt_util.utcnow() - timedelta(minutes=1)}
    )
    assert _active(coord, controller) is False
    assert coord not in controller._backup_cooldown_until


@pytest.mark.parametrize("inverter_state", [INVERTER_BYPASS, INVERTER_BACKUP])
def test_switch_off_is_never_backup(inverter_state):
    coord = _Coord(_data(offgrid=500, inverter_state=inverter_state, backup_function=1))
    assert _active(coord) is False


@pytest.mark.parametrize(
    ("backup_function", "expected"),
    [("Off-grid", True), ("Ready", False), ("Disabled", False)],
)
def test_huawei_follows_its_off_grid_bit(backup_function, expected):
    """Huawei's storage status is a label, so the off-grid bit is the signal."""
    driver = SimpleNamespace(
        backup_enabled=lambda data: HuaweiSolarDriver.backup_enabled(None, data),
        on_backup_power=lambda data: HuaweiSolarDriver.on_backup_power(None, data),
    )
    coord = _Coord(
        _data(offgrid=900, inverter_state="Discharge", backup_function=backup_function)
    )
    coord.driver = driver
    assert _active(coord) is expected


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
    assert entry.version == 18
    assert entry.data["batteries"] == [{"name": "Venus"}]
    assert reg.async_get(old.entity_id) is None
    assert reg.async_get(kept.entity_id) is not None
