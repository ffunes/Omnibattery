"""Time Slot: never charge and then discharge inside the same cheap window.

Field case 2026-11-09 (window 00:00-08:00): the batteries charged to ~80% by
03:17 and then covered the house until 08:00, ~1.1 kWh bought in the window
and spent in the same window, losing the round trip on it.

Inside a configured window the battery now discharges only down to the energy
needed after the window (the floor); the grid carries the rest of the window.
The plan therefore buys only what is needed after the window.
"""
from __future__ import annotations

import math
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


def _forecast(start, end, load_kw=LOAD_KW):
    shape = [load_kw / 4] * INTERVAL_COUNT
    hours = (end - start).total_seconds() / 3600.0
    return ConsumptionForecast(
        load_kw * hours, list(shape), "profile", True,
        intervals_by_date={start.date(): list(shape)},
    )


def _manager(
    soc, *, windows=(WINDOW,), weekly=False, capacity=10.0, min_soc=10,
    load_kw=LOAD_KW, max_charge=3000, sunrise_h=None,
):
    blocks = {}
    coordinator = SimpleNamespace(
        data={"battery_soc": soc, "battery_total_energy": capacity},
        min_soc=min_soc,
        max_soc=100,
    )
    controller = SimpleNamespace(
        _consumption_tracker=SimpleNamespace(
            consumption_profile=SimpleNamespace(),
            forecast_consumption_between=lambda s, e, **_kw: _forecast(s, e, load_kw),
        ),
        coordinators=[coordinator],
        _is_battery_manual_owned=lambda _c: False,
        solar_profile_mode="active",
        _predictive_safety_margin_pct=0.0,
        max_contracted_power=3000,
        max_charge_capacity=max_charge,
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
    if sunrise_h is not None:
        controller._consumption_tracker.calculate_sunrise = lambda _d: sunrise_h
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
    if blocked:
        assert blocks["time_slot_floor_hold"]["release_above_kwh"] == 9.05


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


# ----------------------------------------------------------------------
# #938: the reporter's windows, whole-percent SOC and the slot-entry wait
# ----------------------------------------------------------------------

# 8.16 kWh, min SOC 5 %, 0.8 kW house, no sun; 22:00-07:00 every day and
# 07:00-22:00 at the weekend. 2026-11-22 is a Sunday.
WINDOWS_938 = (
    {"start_time": "22:00:00", "end_time": "07:00:00", "days": ALL_DAYS},
    {"start_time": "07:00:00", "end_time": "22:00:00", "days": ["sat", "sun"]},
)


def _manager_938(soc):
    return _manager(
        soc, windows=WINDOWS_938, capacity=8.16, min_soc=5, load_kw=0.8,
        max_charge=2400, sunrise_h=8.2,
    )


def _no_charge():
    return {"avg_consumption_kwh": 1.0, "planned_grid_charge_kwh": 0.0, "should_charge": False}


def test_contiguous_windows_do_not_hold_before_the_next_window():
    # Sunday 16:57: the plan reaches only 22:00, when the night window
    # opens, so the floor is the minimum SOC and nothing is held.
    manager, _, blocks = _manager_938(15.0)
    manager._apply_time_slot_chronological_plan(_no_charge(), now=datetime(2026, 11, 22, 16, 57))
    manager.apply_time_slot_floor_hold()
    assert manager._controller._time_slot_floor_kwh == pytest.approx(0.408, abs=1e-3)
    assert "time_slot_floor_hold" not in blocks


@pytest.mark.parametrize(("soc", "blocked"), [(17.0, True), (40.0, False)])
def test_the_night_window_keeps_the_morning_need(soc, blocked):
    # 22:30: the plan reaches Monday's sunrise; the morning needs 1.368 kWh.
    manager, _, blocks = _manager_938(soc)
    manager._apply_time_slot_chronological_plan(_no_charge(), now=datetime(2026, 11, 22, 22, 30))
    manager.apply_time_slot_floor_hold()
    assert manager._controller._time_slot_floor_kwh == pytest.approx(1.368, abs=1e-3)
    assert ("time_slot_floor_hold" in blocks) is blocked


@pytest.mark.parametrize("load_kw", [0.300, 0.302, 0.304, 0.306, 0.308, 0.310, 0.312])
def test_whole_percent_soc_holds_within_one_step_of_the_floor(load_kw):
    """The 50 Wh release margin lets out at most one SOC point (100 Wh here)
    after the charge; a re-evaluation buys back only what fell below the floor."""
    day_load = 23.5 * load_kw
    manager, coordinator, blocks = _manager(30.0, load_kw=load_kw)

    def decision(usable):
        return {
            "avg_consumption_kwh": day_load,
            "planned_grid_charge_kwh": max(0.0, day_load - usable),
            "should_charge": day_load > usable,
        }

    quota = manager._apply_time_slot_chronological_plan(decision(2.0), now=NOW)[
        "active_slot_energy_target_kwh"
    ]
    floor = manager._controller._time_slot_floor_kwh
    # The charge stops at the first whole percent at or above the target.
    coordinator.data["battery_soc"] = float(math.ceil(30.0 + quota * 10 - 1e-9))
    steps = 0
    while True:
        manager.apply_time_slot_floor_hold()
        if "time_slot_floor_hold" in blocks:
            break
        coordinator.data["battery_soc"] -= 1
        steps += 1
    assert steps <= 1
    assert coordinator.data["battery_soc"] / 10 >= floor - (0.1 - 0.05) - 1e-9
    usable = coordinator.data["battery_soc"] / 10 - 1.0
    again = manager._apply_time_slot_chronological_plan(decision(usable), now=NOW)
    end = coordinator.data["battery_soc"] / 10
    assert again["active_slot_energy_target_kwh"] == pytest.approx(
        max(0.0, floor - end), abs=1e-3
    )


async def test_the_forecast_grace_holds_instead_of_discharging():
    # Slot entry with the forecast not published yet: no plan, no floor.
    manager, _, blocks = _manager(95.0)
    await manager._retry_time_slot_evaluation(day_rolled=False)
    manager.apply_time_slot_floor_hold()
    assert manager._controller._time_slot_floor_kwh == pytest.approx(9.5)
    assert "time_slot_floor_hold" in blocks
    # The evaluation then finds spare energy and lets it out.
    manager._apply_time_slot_chronological_plan(_decision(8.5), now=NOW)
    manager.apply_time_slot_floor_hold()
    assert "time_slot_floor_hold" not in blocks


# ----------------------------------------------------------------------
# The Daily Operation preview of a window that has not opened yet
# ----------------------------------------------------------------------

EVENING = datetime(2026, 11, 9, 22, 0)
EVENING_LOAD = 2 * LOAD_KW  # 22:00-24:00


def _preview(soc):
    """The panel's plan at 22:00 for the window that opens at midnight."""
    manager, _, _ = _manager(soc)
    opens = EVENING + timedelta(hours=2)
    horizon = manager.time_slot_control_horizon(opens)
    slots = [
        slot
        for slot in manager._time_slot_price_slots_for_horizon(EVENING, horizon)
        if slot.start == opens
    ]
    usable = (soc - 10.0) / 10.0
    plan = manager.build_extended_chronological_projection(
        now=EVENING,
        slots=slots,
        base_decision_data=_decision(usable),
        price_ceiling=None,
        horizon_end=horizon,
    ).plan
    keep = next(k for start, _end, k in plan.window_keeps if start == opens)
    return plan, keep


def test_the_preview_keeps_what_the_control_will_keep_when_the_window_opens():
    """95% at 22:00 is 89% at midnight: the control then holds the 4.8 kWh
    of the day after the window, and so does the preview drawn at 22:00."""
    plan, keep = _preview(95.0)

    at_midnight = 95.0 - EVENING_LOAD * 10.0
    control, _, _ = _manager(at_midnight)
    usable = (at_midnight - 10.0) / 10.0
    control._apply_time_slot_chronological_plan(
        {
            "avg_consumption_kwh": 24 * LOAD_KW,
            "planned_grid_charge_kwh": 0.0,
            "should_charge": False,
        },
        now=EVENING + timedelta(hours=2),
    )
    assert usable > 16 * LOAD_KW  # no charge needed: the spare rule applies
    control_keep = control._controller._time_slot_floor_kwh - 1.0  # over min SOC

    assert not plan.allocations
    assert keep == pytest.approx(16 * LOAD_KW, abs=0.05)
    assert keep == pytest.approx(control_keep, abs=0.05)


def test_the_preview_draws_the_window_load_the_plan_idles():
    """The plan still idles the window to size the charge; the drawn
    intervals carry its load, which the kept energy then limits."""
    plan, _keep = _preview(95.0)
    opens = EVENING + timedelta(hours=2)

    def inside(items):
        return [i for i in items if opens <= i.start < opens + timedelta(hours=8)]

    assert all(i.consumption_kwh == 0.0 for i in inside(plan.intervals))
    assert all(
        i.consumption_kwh == pytest.approx(LOAD_KW / 4)
        for i in inside(plan.shown_intervals)
    )


def test_the_preview_holds_whole_a_window_that_will_charge():
    plan, keep = _preview(30.0)

    assert sum(a.planned_battery_kwh for a in plan.allocations) > 0.5
    assert keep is None


def test_the_preview_reaches_tomorrow_only_with_dated_sun_for_it():
    """Without dated periods for tomorrow the longer plan would see a sunless
    day and book a charge the control, reading tomorrow's forecast when the
    window opens, will not make: the plan stays short and the window held."""
    from custom_components.omnibattery.solar_forecast import (
        SolarForecastInput,
        SolarForecastPeriod,
    )

    opens = EVENING + timedelta(hours=2)
    from datetime import timezone

    noon = (opens + timedelta(hours=12)).replace(tzinfo=timezone.utc)
    dated = SolarForecastInput(
        0.0, "remaining", periods=(SolarForecastPeriod(noon, noon + timedelta(hours=1), 1.0),)
    )
    manager, _, _ = _manager(95.0)

    assert manager.time_slot_preview_horizon(EVENING, opens) is None
    manager._read_remaining_solar_input = lambda **_kw: dated
    assert manager.time_slot_preview_horizon(EVENING, opens) == (
        manager.time_slot_control_horizon(opens)
    )
    manager._controller.solar_profile_mode = "off"
    assert manager.time_slot_preview_horizon(EVENING, opens) is None
    # A window that opens today is planned within today's horizon anyway.
    assert manager.time_slot_preview_horizon(
        EVENING, EVENING + timedelta(hours=1)
    ) is not None


def test_the_longer_plan_draws_the_same_series_and_sees_tomorrow_s_sun():
    """One forecast, the 12 h plan and the one to the control's horizon: what
    they both draw is equal, the longer one carries tomorrow afternoon's sun,
    and only it knows what the window keeps (the short one holds whole)."""
    from datetime import timezone

    from custom_components.omnibattery.solar_forecast import (
        SolarForecastInput,
        SolarForecastPeriod,
    )

    opens = EVENING + timedelta(hours=2)
    hours = [(opens + timedelta(hours=h)).replace(tzinfo=timezone.utc) for h in range(9, 16)]
    dated = SolarForecastInput(
        0.0,
        "remaining",
        periods=tuple(SolarForecastPeriod(h, h + timedelta(hours=1), 0.5) for h in hours),
    )

    def build(reach_h):
        manager, _, _ = _manager(95.0)
        manager._read_remaining_solar_input = lambda **_kw: dated
        slots = [
            slot
            for slot in manager._time_slot_price_slots_for_horizon(
                EVENING, opens + timedelta(hours=12)
            )
            if slot.start == opens
        ]
        return manager.build_extended_chronological_projection(
            now=EVENING,
            slots=slots,
            base_decision_data=_decision(8.5),
            price_ceiling=None,
            horizon_end=opens + timedelta(hours=reach_h),
        ).plan

    short, long = build(12), build(24)

    assert long.shown_intervals[: len(short.shown_intervals)] == short.shown_intervals
    assert sum(
        i.solar_kwh for i in long.shown_intervals if i.start >= opens + timedelta(hours=12)
    ) == pytest.approx(2.0)
    assert short.window_keeps[0][2] is None
    # 4.8 kWh of load after the window, 3.5 kWh of sun on the way.
    assert long.window_keeps[0][2] == pytest.approx(1.3, abs=0.05)


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
