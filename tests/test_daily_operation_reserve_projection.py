"""The Daily Operation projection keeps what the battery holds for later.

The plan's holds (and a weekly hold, a Time Slot window) keep stored energy for
a later slot and block discharge until then. Without it the projected discharge
and SOC spent that energy in the cheaper hours first, which the battery does
not do.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.pricing.daily_timeline import (
    BatteryProjectionInput,
    ProjectionIntervalInput,
    simulate_battery_projection,
)

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 6, 13, 0, tzinfo=BERLIN)
QUARTER = timedelta(minutes=15)


def _intervals(count: int, consumption: float = 0.5) -> list[ProjectionIntervalInput]:
    return [
        ProjectionIntervalInput(
            NOW + QUARTER * i, NOW + QUARTER * (i + 1), consumption_kwh=consumption
        )
        for i in range(count)
    ]


def _battery(stored: float = 2.0) -> BatteryProjectionInput:
    return BatteryProjectionInput("a", stored, 10.0, 0.0, 100.0, 4000.0, 4000.0)


def _discharged(result) -> list[float]:
    return [round(flow.battery_to_home_kwh, 6) for flow in result.intervals]


def test_without_a_floor_the_battery_covers_the_house_at_once():
    result = simulate_battery_projection(_intervals(6), [_battery()])

    assert _discharged(result) == [0.5, 0.5, 0.5, 0.5, 0.0, 0.0]


def test_the_floor_keeps_its_energy_until_the_held_slots_end():
    """1.5 kWh kept up to the 5th quarter: 0.5 kWh above it is spent at once,
    the rest waits."""
    floor = [1.5, 1.5, 1.5, 1.5, 1.0, 0.5]
    result = simulate_battery_projection(
        _intervals(6), [_battery()],
        discharge_reserve_fn=lambda start, _usable, _free: floor[(start - NOW) // QUARTER],
    )

    assert _discharged(result) == [0.5, 0.0, 0.0, 0.0, 0.5, 0.5]
    assert result.intervals[3].stored_energy_end_kwh == pytest.approx(1.5)


def test_the_floor_is_a_fleet_floor_above_each_battery_s_minimum():
    batteries = [
        BatteryProjectionInput("a", 1.5, 5.0, 10.0, 100.0, 4000.0, 4000.0),
        BatteryProjectionInput("b", 1.5, 5.0, 10.0, 100.0, 4000.0, 4000.0),
    ]
    # 0.5 kWh floor each, 2.0 kWh usable; the floor keeps 1.2 of it.
    result = simulate_battery_projection(
        _intervals(1, consumption=2.0), batteries,
        discharge_reserve_fn=lambda _start, _usable, _free: 1.2,
    )

    assert result.intervals[0].battery_to_home_kwh == pytest.approx(0.8)
    assert result.intervals[0].grid_to_home_kwh == pytest.approx(1.2)
