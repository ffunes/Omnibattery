"""Characterization tests for ConsumptionTracker.

These pin the *current* behavior so the planned module refactors can be proven
to change nothing. No Home Assistant entities, no Modbus, no battery: the pure
helpers are called directly, and the one instance test uses the in-process
``hass`` fixture plus a stand-in controller object.
"""
from __future__ import annotations

import math
from datetime import date
from types import SimpleNamespace

import pytest

from custom_components.marstek_venus_energy_manager.const import (
    DEFAULT_BASE_CONSUMPTION_KWH,
)
from custom_components.marstek_venus_energy_manager.consumption_tracker import (
    ConsumptionTracker,
)
from tests.conftest import FakeCoordinator


# ----------------------------------------------------------------------
# Pure solar-energy model: get_solar_fraction_done (static, no HA needed)
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "now_h, t_start, t_end, expected",
    [
        (8.0, 8.0, 16.0, 0.0),    # at sunrise -> nothing produced yet
        (16.0, 8.0, 16.0, 1.0),   # at sunset  -> fully produced
        (12.0, 8.0, 16.0, 0.5),   # midpoint   -> half (sinusoid is symmetric)
        (7.0, 8.0, 16.0, 0.0),    # before window -> clamped to 0
        (17.0, 8.0, 16.0, 1.0),   # after window  -> clamped to 1
        (10.0, 8.0, 16.0, (1.0 - math.cos(math.pi * 0.25)) / 2.0),  # quarter way
    ],
)
def test_solar_fraction_curve(now_h, t_start, t_end, expected):
    result = ConsumptionTracker.get_solar_fraction_done(now_h, t_start, t_end)
    assert result == pytest.approx(expected)


def test_solar_fraction_invalid_window_returns_full():
    # t_end <= t_start is treated as "all produced" rather than dividing by zero.
    assert ConsumptionTracker.get_solar_fraction_done(10.0, 12.0, 12.0) == 1.0
    assert ConsumptionTracker.get_solar_fraction_done(10.0, 12.0, 8.0) == 1.0


# ----------------------------------------------------------------------
# Pure formatting helper: h_to_hhmm (static, no HA needed)
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "hours, expected",
    [
        (13.25, "13:15"),
        (7.5, "07:30"),
        (0.0, "00:00"),
        (9.0, "09:00"),
        (None, None),
    ],
)
def test_h_to_hhmm(hours, expected):
    assert ConsumptionTracker.h_to_hhmm(hours) == expected


# ----------------------------------------------------------------------
# Instance method with a mocked controller: get_avg_daily_consumption
# Proves the controller-by-reference pattern is testable without hardware.
# The tracker is built via __new__ so __init__ (which needs a real hass for
# its Store objects) is skipped: this method only reads one controller attr,
# so isolating it that way keeps the test free of the hass fixture.
# ----------------------------------------------------------------------

def _make_tracker(history):
    """Build a tracker wired to a stand-in controller holding `history`."""
    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._controller = SimpleNamespace(_daily_consumption_history=history)
    return tracker


def test_avg_daily_consumption_empty_uses_fallback():
    tracker = _make_tracker([])
    assert tracker.get_avg_daily_consumption() == DEFAULT_BASE_CONSUMPTION_KWH


def test_avg_daily_consumption_averages_history():
    history = [(date(2026, 6, 1), 4.0), (date(2026, 6, 2), 6.0)]
    tracker = _make_tracker(history)
    assert tracker.get_avg_daily_consumption() == pytest.approx(5.0)


def test_avg_daily_consumption_single_day():
    tracker = _make_tracker([(date(2026, 6, 1), 3.0)])
    assert tracker.get_avg_daily_consumption() == pytest.approx(3.0)


# ----------------------------------------------------------------------
# Total solar power: external sensor + Venus DC-coupled PV (MPPT on vA/vD).
# Pins the #354 fix — daily solar must count the battery's own MPPT panels,
# not only the configured external sensor, and survive the external being gone.
# ----------------------------------------------------------------------

class _FakeStates:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, entity_id):
        return self._mapping.get(entity_id)


def _w(value):
    """A power state in watts."""
    return SimpleNamespace(state=str(value), attributes={"unit_of_measurement": "W"})


def _make_solar_tracker(states, solar_sensor, coordinators):
    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._hass = SimpleNamespace(states=_FakeStates(states))
    tracker._controller = SimpleNamespace(
        solar_production_sensor=solar_sensor,
        coordinators=coordinators,
    )
    return tracker


def _vunit(version, mppt_total, available=True):
    return FakeCoordinator(
        battery_version=version,
        data={"mppt1_power": mppt_total},
        is_available=available,
    )


def test_total_solar_external_only():
    tracker = _make_solar_tracker({"sensor.aps": _w(1500)}, "sensor.aps", [])
    assert tracker._read_total_solar_power_kw() == pytest.approx(1.5)


def test_total_solar_mppt_only_no_external():
    # No external sensor configured, panels on the Venus MPPT inputs.
    tracker = _make_solar_tracker({}, None, [_vunit("vA", 800)])
    assert tracker._read_total_solar_power_kw() == pytest.approx(0.8)


def test_total_solar_external_plus_mppt():
    tracker = _make_solar_tracker(
        {"sensor.aps": _w(1500)}, "sensor.aps", [_vunit("vA", 800), _vunit("vD", 200)]
    )
    assert tracker._read_total_solar_power_kw() == pytest.approx(2.5)


def test_total_solar_ignores_non_pv_versions():
    # v2 has no MPPT registers; it must not contribute.
    tracker = _make_solar_tracker({}, None, [_vunit("v2", 999)])
    assert tracker._read_total_solar_power_kw() is None


def test_total_solar_none_when_no_source():
    tracker = _make_solar_tracker({}, None, [])
    assert tracker._read_total_solar_power_kw() is None


def test_total_solar_skips_disconnected_unit():
    # A disconnected unit keeps its last MPPT reading (coordinator.data is merged,
    # never expired). It must not be counted, or the daily solar total inflates.
    tracker = _make_solar_tracker(
        {}, None, [_vunit("vA", 800, available=False)]
    )
    assert tracker._read_total_solar_power_kw() is None


def test_total_solar_counts_only_connected_units():
    tracker = _make_solar_tracker(
        {}, None, [_vunit("vA", 800), _vunit("vD", 500, available=False)]
    )
    assert tracker._read_total_solar_power_kw() == pytest.approx(0.8)


# ----------------------------------------------------------------------
# Derived home power: home = grid + sum(ac_power) + external_solar.
# Pins the stale-battery fix — a unit that drops mid-discharge keeps a frozen
# ac_power in coordinator.data; counting it double-books the load the grid
# meter already shows, inflating home consumption and its daily integral.
# ----------------------------------------------------------------------

def _battunit(ac_w, available=True):
    return FakeCoordinator(data={"ac_power": ac_w}, is_available=available)


def _make_home_tracker(states, coordinators, grid_sensor="sensor.grid", solar_sensor=None):
    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._hass = SimpleNamespace(states=_FakeStates(states))
    tracker._controller = SimpleNamespace(
        consumption_sensor=grid_sensor,
        solar_production_sensor=solar_sensor,
        coordinators=coordinators,
    )
    return tracker


def test_derive_home_counts_connected_discharge():
    # Grid imports 300 W, battery discharges 2500 W (positive ac_power): the
    # battery covers most of a 2.8 kW house load.
    tracker = _make_home_tracker(
        {"sensor.grid": _w(300)}, [_battunit(2500)]
    )
    assert tracker._derive_home_power_kw() == pytest.approx(2.8)


def test_derive_home_skips_disconnected_stale_discharge():
    # Same battery dropped mid-discharge: ac_power frozen at 2500, but its load
    # has shifted onto the grid meter (now 2800 W). Counting the stale 2500 would
    # report 5.3 kW; skipping it gives the true 2.8 kW.
    tracker = _make_home_tracker(
        {"sensor.grid": _w(2800)}, [_battunit(2500, available=False)]
    )
    assert tracker._derive_home_power_kw() == pytest.approx(2.8)
