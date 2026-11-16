"""Time Slot: the Daily Operation projection holds a window as the control does.

The plan idles the battery inside a configured window to size the charge.
The control does not: it lets the battery spend what is above the energy
needed after the window and holds only below it. Drawing the plan's idle
battery showed a flat SOC and a grey night for a battery that discharges.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.chronological import (
    EnergyInterval,
    SlotAllocation,
)
from custom_components.omnibattery.pricing.daily_timeline import (
    CONTEXT_BATTERY_HOLD,
    BatteryProjectionInput,
    ProjectionIntervalInput,
)
from custom_components.omnibattery.pricing.plan_inputs import time_slot_window_keeps
from custom_components.omnibattery.tracking.daily_projection import (
    DailyOperationProjectionRequest,
    build_daily_operation_projection,
)

MADRID = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 11, 16, 23, 0, tzinfo=MADRID)
MIDNIGHT = datetime(2026, 11, 17, 0, 0, tzinfo=MADRID)
QUARTER = timedelta(minutes=15)
WINDOW = PriceSlot(MIDNIGHT, MIDNIGHT + timedelta(hours=1), 0.0)
FAR = MIDNIGHT + timedelta(days=2)


def _plan_intervals(after_window: list[float]) -> list[EnergyInterval]:
    """23:00 to 00:00 draining 0.1 a quarter, the window idle, then
    ``after_window`` (positive drains, negative is sun) by quarter."""
    before = [
        EnergyInterval(NOW + QUARTER * i, NOW + QUARTER * (i + 1), 0.1, 0.0)
        for i in range(4)
    ]
    inside = [
        EnergyInterval(
            MIDNIGHT + QUARTER * i, MIDNIGHT + QUARTER * (i + 1), 0.0, 0.0
        )
        for i in range(4)
    ]
    start = WINDOW.end
    after = [
        EnergyInterval(
            start + QUARTER * i,
            start + QUARTER * (i + 1),
            max(0.0, value),
            max(0.0, -value),
        )
        for i, value in enumerate(after_window)
    ]
    return before + inside + after


def _keeps(after_window, *, allocations=(), usable=5.0, now=NOW, horizon=None,
           active=None):
    if horizon is None:  # the plan reaches exactly the control's horizon
        horizon = WINDOW.end + QUARTER * len(after_window)
    return time_slot_window_keeps(
        _plan_intervals(after_window),
        list(allocations),
        [WINDOW],
        usable_kwh=usable,
        capacity_kwh=10.0,
        now=now,
        horizon_of=lambda _start: horizon,
        active_keep_kwh=active,
    )


def test_a_window_keeps_what_the_plan_drains_after_it():
    # 4.6 kWh at the window, 1.5 kWh drained before the sun returns.
    keeps = _keeps([0.5, 0.5, 0.5, -1.0, -1.0])

    assert keeps == ((WINDOW.start, WINDOW.end, pytest.approx(1.5)),)


def test_a_window_keeps_everything_it_has_when_the_night_needs_more():
    # 4.6 kWh at the window and 6 kWh of drain after it.
    keeps = _keeps([1.5, 1.5, 1.5, 1.5])

    assert keeps[0][2] == pytest.approx(4.6)


def test_a_window_that_charges_holds_whole():
    charge = SlotAllocation(WINDOW, 2.0, None, "deadline")

    assert _keeps([0.5, 0.5], allocations=[charge])[0][2] is None


def test_the_drain_past_the_control_horizon_does_not_count():
    """The control plans a window up to its own horizon: only that drain."""
    keeps = _keeps([0.5, 0.5, 0.5, 0.5], horizon=WINDOW.end + QUARTER * 2)

    assert keeps[0][2] == pytest.approx(1.0)


def test_a_plan_that_stops_before_the_control_horizon_holds_whole():
    """Its lowest point is not in the plan: a shorter look would keep 1 kWh
    and draw a discharge the control, which sees the whole day, will not allow."""
    assert _keeps([0.5, 0.5], horizon=FAR)[0][2] is None


@pytest.mark.parametrize("active", [None, 1.2])
def test_the_open_window_keeps_the_control_s_live_floor(active):
    inside = MIDNIGHT + timedelta(minutes=20)

    assert _keeps([0.5], now=inside, active=active)[0][2] == active


def test_no_windows_keeps_nothing():
    assert time_slot_window_keeps(
        _plan_intervals([0.5]), [], [],
        usable_kwh=5.0, capacity_kwh=10.0, now=NOW,
        horizon_of=lambda _start: FAR, active_keep_kwh=None,
    ) == ()


# ----------------------------------------------------------------------
# The drawn projection
# ----------------------------------------------------------------------


def _projection(keep, *, stored=3.0, house=0.25):
    """A window of 1 h with ``house`` kWh a quarter and ``keep`` reserved."""
    intervals = [
        ProjectionIntervalInput(
            MIDNIGHT + QUARTER * i, MIDNIGHT + QUARTER * (i + 1), consumption_kwh=house
        )
        for i in range(6)
    ]
    plan = SimpleNamespace(window_keeps=((WINDOW.start, WINDOW.end, keep),))
    result = build_daily_operation_projection(
        DailyOperationProjectionRequest(
            now=MIDNIGHT,
            plan_intervals=tuple(intervals),
            allocations=(),
            battery_inputs=(
                BatteryProjectionInput("a", stored, 10.0, 0.0, 100.0, 4000.0, 4000.0),
            ),
            mode="time_slot",
            decision_data={},
            reserve_floor=ChargeDischargeController._daily_operation_reserve_floor(
                SimpleNamespace(), plan
            ),
        )
    )
    return result["intervals"]


def test_spare_above_the_window_load_is_spent_inside_the_window():
    """3 kWh stored, 1 kWh kept: the battery covers the whole window."""
    cells = _projection(1.0)

    assert [c["battery_to_home_kwh"] for c in cells] == pytest.approx([0.25] * 6)
    assert cells[3]["stored_energy_end_kwh"] == pytest.approx(2.0)


def test_below_the_floor_the_grid_carries_the_window():
    """3 kWh stored, 2.6 kWh kept: 0.4 kWh goes, then the grid until 01:00."""
    cells = _projection(2.6)

    assert [c["battery_to_home_kwh"] for c in cells] == pytest.approx(
        [0.25, 0.15, 0.0, 0.0, 0.25, 0.25]
    )
    assert [c["grid_to_home_kwh"] for c in cells[:4]] == pytest.approx(
        [0.0, 0.1, 0.25, 0.25]
    )
    assert cells[3]["stored_energy_end_kwh"] == pytest.approx(2.6)


def test_a_window_that_holds_whole_stays_idle():
    cells = _projection(None)

    assert [c["battery_to_home_kwh"] for c in cells[:4]] == pytest.approx([0.0] * 4)
    assert cells[4]["battery_to_home_kwh"] == pytest.approx(0.25)


def _held(cells) -> list[bool]:
    return [bool(c["context_mask"] & CONTEXT_BATTERY_HOLD) for c in cells]


def test_only_the_quarters_the_reserve_leaves_to_the_grid_are_marked_held():
    """The panel draws them as "battery held in reserve": the quarter that
    reaches the floor and the ones below it, not the free discharge before
    or after the window."""
    assert _held(_projection(2.6)) == [False, True, True, True, False, False]
    assert _held(_projection(None)) == [True, True, True, True, False, False]
    assert _held(_projection(1.0)) == [False] * 6


def test_an_empty_battery_is_not_a_battery_held():
    assert _held(_projection(0.0, stored=0.5))[2:] == [False] * 4
