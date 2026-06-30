"""Regression tests for WeeklyFullChargeManager BMS-cutoff detection.

Pins the fix for the "full charge stops at 3.58 V" bug: a battery that is merely
idle (≤10 W + Standby in the taper zone but NOT commanded to charge) must not be
mistaken for a real top-of-charge BMS cutoff. Only a battery we actually commanded
to charge yet refuses counts; once confirmed, the latch must survive the charge
exclusion that follows it.

No hardware, no real Home Assistant. ``Store(hass, ...)`` only stores references at
construction, so a SimpleNamespace hass is enough; ``is_active`` is overridden to
isolate the cutoff counter from day/feature gating.
"""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.omnibattery.const import NORMAL_BALANCE_TAPER_CELL_VOLTAGE
from custom_components.omnibattery.control.weekly_full_charge import (
    WeeklyFullChargeManager,
    _BMS_CUTOFF_REQUIRED_CYCLES,
)

_IN_ZONE = NORMAL_BALANCE_TAPER_CELL_VOLTAGE + 0.05  # cell above taper entry
_STANDBY = 1


class _Coord:
    """Identity-hashable coordinator stand-in (name-keyed in the counter dict)."""

    def __init__(self, name, *, soc, power, commanded, vmax=_IN_ZONE, inv=_STANDBY):
        self.name = name
        self.commanded_charge_power = commanded
        self.data = {
            "battery_soc": soc,
            "battery_power": power,
            "inverter_state": inv,
            "max_cell_voltage": vmax,
        }


def _mgr(coord):
    """Build a manager without its Store (only the cutoff-counter state matters)."""
    ctrl = SimpleNamespace(coordinators=[coord], weekly_full_charge_enabled=True)
    m = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    m._controller = ctrl
    m._bms_cutoff_counts = {}
    m._already_complete_logged = False
    m.is_active = lambda: True  # weekly active; bypass day/feature gating
    return m


def test_idle_battery_never_confirms_cutoff():
    """Idle in the taper zone (not commanded) must NOT accumulate cutoff cycles."""
    coord = _Coord("bat", soc=94, power=0, commanded=0)
    m = _mgr(coord)
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES * 3):
        m.tick_bms_cutoff()
    assert m._bms_cutoff_counts.get("bat", 0) == 0
    assert m.is_battery_full(coord) is False


def test_commanded_refusal_confirms_cutoff():
    """Commanded to charge but refusing (≤10 W + Standby) confirms after N cycles."""
    coord = _Coord("bat", soc=94, power=0, commanded=200)
    m = _mgr(coord)
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
        m.tick_bms_cutoff()
    assert m._bms_cutoff_counts["bat"] >= _BMS_CUTOFF_REQUIRED_CYCLES
    assert m.is_battery_full(coord) is True


def test_confirmed_cutoff_latches_when_battery_goes_idle():
    """Once confirmed, dropping the charge command must freeze (not reset) the count."""
    coord = _Coord("bat", soc=94, power=0, commanded=200)
    m = _mgr(coord)
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
        m.tick_bms_cutoff()
    assert m.is_battery_full(coord) is True
    # Battery is now excluded → no longer commanded. Must stay full, not un-latch.
    coord.commanded_charge_power = 0
    coord.data["battery_power"] = 0
    for _ in range(10):
        m.tick_bms_cutoff()
    assert m.is_battery_full(coord) is True


def test_accepting_charge_resets_counter():
    """A battery taking the charge it was offered is not full → counter resets."""
    coord = _Coord("bat", soc=94, power=0, commanded=200)
    m = _mgr(coord)
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES - 1):
        m.tick_bms_cutoff()
    assert m._bms_cutoff_counts["bat"] == _BMS_CUTOFF_REQUIRED_CYCLES - 1
    # Now it accepts charge.
    coord.data["battery_power"] = 150
    m.tick_bms_cutoff()
    assert m._bms_cutoff_counts["bat"] == 0
    assert m.is_battery_full(coord) is False
