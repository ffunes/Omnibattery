"""Tests for ``_is_backup_function_active`` while the inverter reports Bypass.

With the grid present a Venus passes it through to the backup port, so a
constant load connected there reads as off-grid power. That must not count as
an outage: only the backup state means the battery feeds the port.

The method is exercised unbound with light stubs for ``self`` and the
coordinator (same pattern as test_charge_hysteresis.py).
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController

INVERTER_BACKUP = 4
INVERTER_BYPASS = 6


class _Coord:
    """Identity-hashable coordinator stand-in (used as a dict key)."""

    name = "Venus"
    backup_offgrid_threshold = 50

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


def test_bypass_with_constant_load_is_not_backup():
    controller = SimpleNamespace(_backup_cooldown_until={})
    coord = _Coord(_data(offgrid=116, inverter_state=INVERTER_BYPASS))
    assert _active(coord, controller) is False
    assert coord not in controller._backup_cooldown_until


def test_backup_state_with_load_is_backup():
    controller = SimpleNamespace(_backup_cooldown_until={})
    coord = _Coord(_data(offgrid=116, inverter_state=INVERTER_BACKUP))
    assert _active(coord, controller) is True
    assert coord in controller._backup_cooldown_until


def test_load_above_threshold_without_inverter_state_is_backup():
    coord = _Coord(_data(offgrid=116, inverter_state=None))
    assert _active(coord) is True


def test_label_state_does_not_count_as_bypass():
    coord = _Coord(_data(offgrid=116, inverter_state="Bypass"))
    assert _active(coord) is True


def test_cooldown_after_outage_still_runs_in_bypass():
    coord = _Coord(_data(offgrid=116, inverter_state=INVERTER_BYPASS))
    controller = SimpleNamespace(
        _backup_cooldown_until={coord: dt_util.utcnow() + timedelta(minutes=3)}
    )
    assert _active(coord, controller) is True


def test_expired_cooldown_releases_in_bypass():
    coord = _Coord(_data(offgrid=116, inverter_state=INVERTER_BYPASS))
    controller = SimpleNamespace(
        _backup_cooldown_until={coord: dt_util.utcnow() - timedelta(minutes=1)}
    )
    assert _active(coord, controller) is False
    assert coord not in controller._backup_cooldown_until


@pytest.mark.parametrize("inverter_state", [INVERTER_BYPASS, INVERTER_BACKUP])
def test_switch_off_is_never_backup(inverter_state):
    coord = _Coord(_data(offgrid=500, inverter_state=inverter_state, backup_function=1))
    assert _active(coord) is False
