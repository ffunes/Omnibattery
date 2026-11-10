"""Time Slot: never charge and then discharge inside the same cheap window.

Field case 2026-11-09 (window 00:00-08:00): the batteries charged to ~80% by
03:17 and then covered the house until 08:00, ~1.1 kWh bought in the window
and spent in the same window, losing the round trip on it.

Inside a configured window the battery now discharges only down to the energy
needed after the window (the floor); the grid carries the rest of the window.
The plan therefore buys only what is needed after the window.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import PREDICTIVE_MODE_TIME_SLOT
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.tracking.consumption_profile import (
    INTERVAL_COUNT,
    ConsumptionForecast,
)

NOW = datetime(2026, 11, 10, 0, 30)
LOAD_KW = 0.3  # flat, no solar
ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WINDOW = {"start_time": "00:00:00", "end_time": "08:00:00", "days": ALL_DAYS}
# 00:30-24:00 at 0.3 kW; 2.25 kWh of it falls inside the window.
DAY_LOAD = 23.5 * LOAD_KW
WINDOW_LOAD = 7.5 * LOAD_KW
AFTER_WINDOW_LOAD = DAY_LOAD - WINDOW_LOAD  # 4.8


def _forecast(start, end):
    shape = [LOAD_KW / 4] * INTERVAL_COUNT
    hours = (end - start).total_seconds() / 3600.0
    return ConsumptionForecast(
        LOAD_KW * hours, list(shape), "profile", True,
        intervals_by_date={start.date(): list(shape)},
    )


def _manager(soc, *, windows=(WINDOW,), weekly=False):
    blocks = {}
    coordinator = SimpleNamespace(
        data={"battery_soc": soc, "battery_total_energy": 10.0},
        min_soc=10,
        max_soc=100,
    )
    controller = SimpleNamespace(
        _consumption_tracker=SimpleNamespace(
            consumption_profile=SimpleNamespace(),
            forecast_consumption_between=lambda s, e, **_kw: _forecast(s, e),
        ),
        coordinators=[coordinator],
        _is_battery_manual_owned=lambda _c: False,
        solar_profile_mode="active",
        _predictive_safety_margin_kwh=0.0,
        max_contracted_power=3000,
        max_charge_capacity=3000,
        max_price_threshold=None,
        predictive_charging_mode=PREDICTIVE_MODE_TIME_SLOT,
        dp_price_discharge_control=False,
        price_sensor=None,
        charging_time_slots=list(windows),
        _weekly_full_charge_pending=lambda: weekly,
        _is_in_predictive_charging_slot=lambda: True,
        set_discharge_block=lambda source, reason, details=None: blocks.__setitem__(
            source, details
        ),
        remove_discharge_block=lambda source: blocks.pop(source, None),
    )
    manager = PricingManager(
        SimpleNamespace(config=SimpleNamespace(time_zone="UTC")), controller
    )
    manager._read_remaining_solar_input = lambda **_kw: None
    return manager, coordinator, blocks


def _decision(usable):
    return {
        "avg_consumption_kwh": DAY_LOAD,
        "planned_grid_charge_kwh": max(0.0, DAY_LOAD - usable),
        "should_charge": DAY_LOAD > usable,
    }


def test_the_window_charges_only_what_is_needed_after_it():
    # 30% of 10 kWh over a 10% floor: 2 kWh usable, 4.8 kWh needed after 08:00.
    manager, _, _ = _manager(30.0)
    decision = manager._apply_time_slot_chronological_plan(_decision(2.0), now=NOW)

    assert decision["active_slot_energy_target_kwh"] == pytest.approx(
        AFTER_WINDOW_LOAD - 2.0, abs=0.05
    )
    # Charge to the target, then hold there until the window ends.
    assert manager._controller._time_slot_floor_kwh == pytest.approx(
        3.0 + decision["active_slot_energy_target_kwh"]
    )


def test_without_the_hold_the_plan_also_bought_the_window_load():
    held, _, _ = _manager(30.0)
    plain, _, _ = _manager(30.0, windows=())
    plain._time_slot_price_slots = held._time_slot_price_slots  # same candidates
    quota_held = held._apply_time_slot_chronological_plan(
        _decision(2.0), now=NOW
    )["active_slot_energy_target_kwh"]
    quota_plain = plain._apply_time_slot_chronological_plan(
        _decision(2.0), now=NOW
    )["active_slot_energy_target_kwh"]
    assert quota_plain > quota_held + 0.5


def test_a_window_with_spare_energy_discharges_only_the_spare():
    # 95%: 8.5 kWh usable, 4.8 kWh needed after the window -> 3.7 kWh spare.
    manager, _, _ = _manager(95.0)
    decision = manager._apply_time_slot_chronological_plan(_decision(8.5), now=NOW)

    assert decision["should_charge"] is False
    spare = 8.5 - AFTER_WINDOW_LOAD
    assert manager._controller._time_slot_floor_kwh == pytest.approx(9.5 - spare, abs=0.05)


def test_a_weekly_full_charge_keeps_its_own_behaviour():
    manager, _, _ = _manager(30.0, weekly=True)
    manager._apply_time_slot_chronological_plan(_decision(2.0), now=NOW)
    assert manager._controller._time_slot_floor_kwh is None


@pytest.mark.parametrize(
    ("soc", "blocked"),
    [(95.0, False), (90.0, True), (80.0, True)],
)
def test_the_hold_blocks_discharge_at_the_floor(soc, blocked):
    manager, coordinator, blocks = _manager(soc)
    manager._controller._time_slot_floor_kwh = 9.0
    manager.apply_time_slot_floor_hold()
    assert ("time_slot_floor_hold" in blocks) is blocked


@pytest.mark.parametrize(
    "change",
    [
        {"_is_in_predictive_charging_slot": lambda: False},
        {"_weekly_full_charge_pending": lambda: True},
        {"predictive_charging_mode": "dynamic_pricing"},
        {"_time_slot_floor_kwh": None},
    ],
)
def test_the_hold_releases_outside_its_case(change):
    manager, _, blocks = _manager(50.0)
    manager._controller._time_slot_floor_kwh = 9.0
    blocks["time_slot_floor_hold"] = {}
    for key, value in change.items():
        setattr(manager._controller, key, value)
    manager.apply_time_slot_floor_hold()
    assert "time_slot_floor_hold" not in blocks


def test_a_headroom_capped_balance_still_buys_the_after_window_need():
    # Field case 2026-11-11: the scalar balance, capped at the room to max_soc,
    # minus the window load left 0.06 kWh while the plan needed 3.2 kWh after
    # the window; the battery then ran out at 18:00.
    # 30%, max_soc 55%: 2.5 kWh room, 2.8 kWh needed after the window.
    manager, coordinator, _ = _manager(30.0)
    coordinator.max_soc = 55
    decision = _decision(2.0)
    decision["planned_grid_charge_kwh"] = 2.5  # min(deficit, room)
    decision = manager._apply_time_slot_chronological_plan(decision, now=NOW)

    assert decision["active_slot_energy_target_kwh"] == pytest.approx(2.5, abs=0.05)
