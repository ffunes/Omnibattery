"""Tests for deadline-aware predictive charge allocation."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import CHARGE_EFFICIENCY
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.chronological import (
    ChronologicalEvaluationRequest,
    ChronologicalPlan,
    EnergyDeadline,
    EnergyInterval,
    SlotAllocation,
    allocate_price_slots,
    build_energy_deadlines,
    evaluate_chronological_request,
    normalize_energy_shape,
    simulate_allocations,
)
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.pricing.solar_timeline import build_boundaries


BASE = datetime(2026, 8, 18)


def _interval(index, consumption, solar=0.0):
    start = BASE + timedelta(minutes=15 * index)
    return EnergyInterval(start, start + timedelta(minutes=15), consumption, solar)


def _slot(start_minutes, end_minutes, price):
    return PriceSlot(
        BASE + timedelta(minutes=start_minutes),
        BASE + timedelta(minutes=end_minutes),
        price,
    )


def test_normalized_shape_preserves_exact_total():
    result = normalize_energy_shape([1, 2, 3], 2.16)
    assert sum(result) == pytest.approx(2.16)


def test_pure_chronological_evaluation_needs_no_controller_or_mutable_state():
    """The dashboard-safe evaluator must not depend on PricingManager state."""
    intervals = [_interval(0, 0.5)]
    slots = [_slot(0, 15, 0.1)]
    request = ChronologicalEvaluationRequest(
        now=BASE,
        horizon_end=BASE + timedelta(days=1),
        intervals=intervals,
        price_slots=slots,
        total_required_kwh=0.5,
        effective_power_kw=4.0,
        usable_initial_kwh=0.0,
    )
    intervals.clear()
    slots.clear()

    result = evaluate_chronological_request(request)

    assert len(request.intervals) == 1
    assert len(request.price_slots) == 1
    assert result.plan.allocated_kwh == pytest.approx(0.5)
    assert result.diagnostics.deadline_required_kwh == pytest.approx(0.5)
    assert result.diagnostics.energy_deadlines == tuple(result.plan.deadlines)


def test_naive_daily_horizon_allocates_slots_against_energy_deadlines():
    """The 00:05 planner must compare provider slots and derived deadlines."""
    now = BASE + timedelta(minutes=5)
    horizon_end = BASE + timedelta(days=1)
    boundaries = build_boundaries(now, horizon_end)
    intervals = [EnergyInterval(*boundaries[0], 1.0, 0.0)]
    deadlines = build_energy_deadlines(intervals, usable_initial_kwh=0.0)
    slot = _slot(0, 15, 0.1)

    plan = allocate_price_slots(
        intervals,
        deadlines,
        [slot],
        total_required_kwh=1.0,
        effective_power_kw=4.0,
        now=now,
        horizon_end=horizon_end,
    )

    assert deadlines
    assert plan.allocations
    assert plan.deadline_required_kwh == pytest.approx(1.0)


def test_manager_projection_api_matches_pure_chronological_evaluation():
    """The manager forwarding API cannot mutate its controller argument."""
    intervals = (_interval(0, 0.25), _interval(1, 0.25))
    request = ChronologicalEvaluationRequest(
        now=BASE,
        horizon_end=BASE + timedelta(days=1, hours=12),
        intervals=intervals,
        price_slots=(_slot(0, 30, 0.1),),
        total_required_kwh=0.5,
        effective_power_kw=4.0,
    )
    controller = SimpleNamespace(untouched={"decision": "stable"})
    manager = PricingManager(SimpleNamespace(), controller)

    direct = evaluate_chronological_request(request)
    projected = manager.evaluate_chronological_projection(request)

    assert projected == direct
    assert controller.untouched == {"decision": "stable"}


def test_later_solar_does_not_erase_early_deadline():
    intervals = [
        _interval(0, 1.0),
        _interval(1, 1.0),
        _interval(2, 0.0, 5.0),
    ]
    deadlines = build_energy_deadlines(intervals, usable_initial_kwh=1.0)
    assert deadlines
    assert deadlines[-1].required_cumulative_kwh == pytest.approx(1.0)
    assert deadlines[-1].deadline <= intervals[1].end


def test_small_battery_exposes_deficit_after_solar_fills_it():
    intervals = [
        _interval(0, 0.5),
        _interval(1, 0.0, 2.0),
        _interval(2, 1.5),
    ]

    unbounded = build_energy_deadlines(intervals, usable_initial_kwh=0.0)
    bounded = build_energy_deadlines(
        intervals,
        usable_initial_kwh=0.0,
        usable_capacity_kwh=1.0,
    )
    simulation = simulate_allocations(
        intervals,
        usable_initial_kwh=0.0,
        usable_capacity_kwh=1.0,
    )

    assert unbounded[-1].deadline == intervals[0].end
    assert bounded[-1].deadline == intervals[-1].end
    assert bounded[-1].required_cumulative_kwh == pytest.approx(0.5)
    assert simulation.trajectory[1][1] == pytest.approx(1.0)
    assert simulation.final_projected_energy_kwh == pytest.approx(-0.5)


def test_slot_before_projected_full_is_not_allocated_to_later_deadline():
    intervals = [
        _interval(0, 0.0, 2.0),
        _interval(1, 1.5),
    ]
    deadlines = build_energy_deadlines(
        intervals,
        usable_initial_kwh=0.0,
        usable_capacity_kwh=1.0,
    )
    before_full = _slot(0, 15, 0.01)
    after_full = _slot(15, 30, 0.20)

    plan = allocate_price_slots(
        intervals,
        deadlines,
        [before_full, after_full],
        total_required_kwh=0.5,
        effective_power_kw=2.0,
        charge_efficiency=1.0,
        now=BASE,
        horizon_end=BASE + timedelta(hours=1),
        headroom_kwh=1.0,
    )

    assert [allocation.slot for allocation in plan.allocations] == [after_full]
    assert plan.allocated_kwh == pytest.approx(0.5)


def test_capacity_bound_does_not_change_plan_when_battery_never_fills():
    intervals = [
        _interval(0, 0.4, 0.1),
        _interval(1, 1.0),
    ]
    unbounded_deadlines = build_energy_deadlines(intervals, usable_initial_kwh=1.0)
    bounded_deadlines = build_energy_deadlines(
        intervals,
        usable_initial_kwh=1.0,
        usable_capacity_kwh=3.0,
    )
    slots = [_slot(0, 15, 0.1), _slot(15, 30, 0.2)]
    arguments = {
        "total_required_kwh": 0.3,
        "effective_power_kw": 2.0,
        "charge_efficiency": 1.0,
        "usable_initial_kwh": 1.0,
        "now": BASE,
        "horizon_end": BASE + timedelta(hours=1),
    }

    current_plan = allocate_price_slots(
        intervals,
        unbounded_deadlines,
        slots,
        **arguments,
    )
    bounded_plan = allocate_price_slots(
        intervals,
        bounded_deadlines,
        slots,
        headroom_kwh=2.0,
        **arguments,
    )

    assert bounded_deadlines == unbounded_deadlines
    assert bounded_plan == current_plan


def test_reference_pattern_reserves_early_energy_and_keeps_rest_flexible():
    intervals = [_interval(i, 0.1) for i in range(96)]
    # 1.30 kWh has been consumed beyond usable storage by 03:30.
    deadlines = build_energy_deadlines(intervals[:14], usable_initial_kwh=0.1)
    early_a = _slot(30, 45, 0.20)
    early_b = _slot(45, 60, 0.21)
    cheap_late = _slot(13 * 60 + 45, 14 * 60, 0.01)
    plan = allocate_price_slots(
        intervals,
        deadlines,
        [cheap_late, early_b, early_a],
        total_required_kwh=2.16,
        effective_power_kw=6.0,
        now=BASE + timedelta(minutes=5),
        horizon_end=BASE + timedelta(days=1),
        headroom_kwh=5.0,
        usable_initial_kwh=0.1,
    )
    early = sum(a.planned_battery_kwh for a in plan.allocations if a.deadline)
    flexible = sum(a.planned_battery_kwh for a in plan.allocations if not a.deadline)
    assert early == pytest.approx(1.3)
    assert flexible == pytest.approx(0.86)
    assert next(a for a in plan.allocations if a.slot == cheap_late).kind == "flexible"


def test_cheapest_slot_after_deadline_cannot_cover_early_requirement():
    intervals = [_interval(0, 1.0)]
    deadlines = build_energy_deadlines(intervals, usable_initial_kwh=0.0)
    late = _slot(60, 75, -1.0)
    plan = allocate_price_slots(
        intervals,
        deadlines,
        [late],
        total_required_kwh=1.0,
        effective_power_kw=4.0,
        now=BASE,
        horizon_end=BASE + timedelta(days=1),
    )
    assert plan.deadline_shortfall_kwh == pytest.approx(1.0)
    assert plan.allocations == []


def test_crossing_slot_only_contributes_capacity_before_deadline():
    intervals = [_interval(2, 1.0)]  # deadline 00:45
    deadline = build_energy_deadlines(intervals, 0.0)
    crossing = _slot(30, 60, 0.1)
    plan = allocate_price_slots(
        intervals,
        deadline,
        [crossing],
        total_required_kwh=1.0,
        effective_power_kw=2.0,
        charge_efficiency=1.0,
        now=BASE,
        horizon_end=BASE + timedelta(days=1),
    )
    assert plan.allocations[0].planned_battery_kwh == pytest.approx(0.5)
    assert plan.total_shortfall_kwh == pytest.approx(0.5)


def test_current_slot_uses_only_remaining_duration():
    slot = _slot(0, 15, 0.1)
    plan = allocate_price_slots(
        [],
        [],
        [slot],
        total_required_kwh=1.0,
        effective_power_kw=6.0,
        charge_efficiency=1.0,
        now=BASE + timedelta(minutes=5),
        horizon_end=BASE + timedelta(days=1),
    )
    assert plan.allocated_kwh == pytest.approx(1.0)


def test_current_slot_is_eligible_after_its_start_time():
    """A configured 00:00-08:00 window remains usable at 00:04."""
    now = BASE + timedelta(minutes=4)
    slot = _slot(0, 8 * 60, 0.0)

    plan = allocate_price_slots(
        [],
        [],
        [slot],
        total_required_kwh=1.0,
        effective_power_kw=1.0,
        charge_efficiency=1.0,
        now=now,
        horizon_end=BASE + timedelta(days=1),
    )

    assert plan.reason == "ok"
    assert plan.allocated_kwh == pytest.approx(1.0)


def test_price_ceiling_reports_deadline_shortfall():
    interval = _interval(0, 0.5)
    plan = allocate_price_slots(
        [interval],
        build_energy_deadlines([interval], 0.0),
        [_slot(0, 15, 0.3)],
        total_required_kwh=0.5,
        effective_power_kw=4.0,
        now=BASE,
        horizon_end=BASE + timedelta(days=1),
        max_price_threshold=0.2,
    )
    assert plan.deadline_shortfall_kwh == pytest.approx(0.5)
    assert plan.reason == "price_threshold"


def test_simulation_applies_allocation_at_interval_boundary_once():
    intervals = [_interval(0, 1.0), _interval(1, 1.0)]
    slot = _slot(0, 15, 0.1)
    allocation = SlotAllocation(slot, 1.0, intervals[0].end, "deadline")
    result = simulate_allocations(intervals, 0.0, [allocation])
    assert result.final_projected_energy_kwh == pytest.approx(-1.0)


def test_time_slot_windows_are_materialized_and_split_at_midnight():
    now = BASE + timedelta(minutes=30)
    controller = SimpleNamespace(
        charging_time_slots=[
            {
                "start_time": "22:00",
                "end_time": "02:00",
                "days": ["tue"],
            },
            {
                "start_time": "10:00",
                "end_time": "11:00",
                "days": ["tue"],
            },
        ]
    )

    slots = PricingManager(SimpleNamespace(), controller)._time_slot_price_slots(now)

    assert [(slot.start.hour, slot.end.hour) for slot in slots] == [
        (0, 2),
        (10, 11),
        (22, 0),
    ]
    assert slots[-1].end.date() == BASE.date() + timedelta(days=1)


def test_time_slot_windows_parse_the_stored_hh_mm_ss_format():
    """HA's TimeSelector stores "HH:MM:SS" (#447).

    "%H:%M" raised on every configured window, so Time Slot mode produced no
    candidate slots and never got a chronological plan — including the
    guaranteed-minimum-SOC floor deadlines.
    """
    now = BASE + timedelta(minutes=30)
    controller = SimpleNamespace(
        charging_time_slots=[
            {
                "start_time": "01:00:00",
                "end_time": "05:00:00",
                "days": ["tue"],
            }
        ]
    )

    slots = PricingManager(SimpleNamespace(), controller)._time_slot_price_slots(now)

    assert [(slot.start.hour, slot.end.hour) for slot in slots] == [(1, 5)]


def test_no_discharge_window_parses_the_stored_hh_mm_ss_format():
    """Same "%H:%M" parse bug on the no-discharge windows (#447)."""
    manager = PricingManager(SimpleNamespace(), SimpleNamespace())
    blocked = _slot(60, 120, 0.0)
    allowed = _slot(600, 660, 0.0)
    window = {"start_time": "01:00:00", "end_time": "02:00:00", "days": ["tue"]}

    assert manager._future_slot_matches_operation_block(blocked, window) is True
    assert manager._future_slot_matches_operation_block(allowed, window) is False


def test_time_slot_dashboard_preview_extends_known_windows_but_control_does_not():
    now = BASE + timedelta(minutes=30)
    controller = SimpleNamespace(
        charging_time_slots=[
            {
                "start_time": "23:00",
                "end_time": "02:00",
                "days": ["tue", "wed"],
            },
            {
                "start_time": "09:00",
                "end_time": "10:00",
                "days": ["wed"],
            },
        ]
    )
    manager = PricingManager(SimpleNamespace(), controller)
    tomorrow_noon = BASE + timedelta(days=1, hours=12)

    control_slots = manager._time_slot_price_slots(now)
    preview_slots = manager._time_slot_price_slots_for_horizon(
        now, tomorrow_noon
    )

    assert [(slot.start, slot.end) for slot in control_slots] == [
        (BASE, BASE + timedelta(hours=2)),
        (BASE + timedelta(hours=23), BASE + timedelta(days=1)),
    ]
    assert [(slot.start, slot.end) for slot in preview_slots] == [
        (BASE, BASE + timedelta(hours=2)),
        (BASE + timedelta(hours=23), BASE + timedelta(days=1)),
        (BASE + timedelta(days=1), BASE + timedelta(days=1, hours=2)),
        (BASE + timedelta(days=1, hours=9), BASE + timedelta(days=1, hours=10)),
    ]


def test_time_slot_plan_applies_only_the_active_window_quota():
    now = BASE + timedelta(hours=1)
    active = _slot(0, 120, 0.0)
    later = _slot(180, 240, 0.0)
    controller = SimpleNamespace(
        charging_time_slots=[
            {"start_time": "00:00", "end_time": "02:00", "days": ["tue"]},
            {"start_time": "03:00", "end_time": "04:00", "days": ["tue"]},
        ],
        _active_time_slot_quota_kwh=None,
    )
    manager = PricingManager(SimpleNamespace(), controller)
    manager._build_chronological_plan = lambda **_kwargs: ChronologicalPlan(
        allocations=[
            SlotAllocation(active, 0.4, BASE + timedelta(hours=2), "deadline"),
            SlotAllocation(later, 0.8, None, "flexible"),
        ],
        total_required_kwh=1.2,
    )

    decision = manager._apply_time_slot_chronological_plan(
        {"should_charge": True, "planned_grid_charge_kwh": 1.2},
        now=now,
    )

    assert decision["should_charge"] is True
    assert decision["active_slot_energy_target_kwh"] == pytest.approx(0.4)
    assert controller._active_time_slot_quota_kwh == pytest.approx(0.4)


def test_time_slot_preview_is_built_before_the_first_window():
    now = BASE + timedelta(hours=1)
    controller = SimpleNamespace(
        charging_time_slots=[
            {"start_time": "03:00", "end_time": "04:00", "days": ["tue"]},
        ],
        _time_slot_chronological_preview_date=None,
        solar_forecast_remaining_sensor=None,
        solar_forecast_sensor=None,
        _last_decision_data=None,
    )
    manager = PricingManager(SimpleNamespace(), controller)

    async def decision():
        return {"should_charge": True}

    notifications = []

    async def notify(**kwargs):
        notifications.append(kwargs)

    manager._current_horizon_grid_charging_decision = decision
    manager._apply_time_slot_chronological_plan = lambda data, now: {
        **data,
        "should_charge": False,
        "deadline_shortfall_kwh": 0.4,
    }
    manager._send_predictive_charging_notification = notify

    asyncio.run(manager._ensure_time_slot_chronological_preview(now=now))

    assert controller._time_slot_chronological_preview_date == BASE.date()
    assert controller._last_decision_data["deadline_shortfall_kwh"] == 0.4
    assert len(notifications) == 1


def test_time_slot_preview_keeps_balance_diagnostics_after_last_window():
    now = BASE + timedelta(hours=2)
    controller = SimpleNamespace(
        charging_time_slots=[
            {"start_time": "00:30", "end_time": "01:00", "days": ["tue"]},
        ],
        _time_slot_chronological_preview_date=None,
        _active_time_slot_quota_kwh=None,
        solar_forecast_remaining_sensor=None,
        solar_forecast_sensor=None,
        _last_decision_data=None,
    )
    manager = PricingManager(SimpleNamespace(), controller)
    evaluations = []

    async def decision():
        evaluations.append(True)
        return {
            "should_charge": False,
            "stored_energy_kwh": 4.2,
            "avg_consumption_kwh": 7.1,
            "energy_deficit_kwh": 0.0,
        }

    manager._current_horizon_grid_charging_decision = decision

    asyncio.run(manager._ensure_time_slot_chronological_preview(now=now))

    assert evaluations == [True]
    assert controller._time_slot_chronological_preview_date == BASE.date()
    assert controller._last_decision_data["stored_energy_kwh"] == pytest.approx(4.2)
    assert controller._last_decision_data["avg_consumption_kwh"] == pytest.approx(7.1)
    assert controller._last_decision_data["energy_deficit_kwh"] == pytest.approx(0.0)


def _floor_plan(**kwargs):
    # 5 kWh due by 02:00 at 2 kW: one cheap slot (0.30) delivers 0.425 kWh,
    # the expensive ones (0.50) could cover the rest.
    intervals = [_interval(7, 5.0)]
    deadlines = build_energy_deadlines(intervals, usable_initial_kwh=0.0)
    return allocate_price_slots(
        intervals,
        deadlines,
        [_slot(0, 15, 0.30), _slot(15, 60, 0.50), _slot(60, 120, 0.50)],
        total_required_kwh=5.0,
        effective_power_kw=2.0,
        now=BASE,
        horizon_end=BASE + timedelta(hours=12),
        max_price_threshold=0.35,
        **kwargs,
    )


def test_floor_premium_buys_only_the_floor_energy_above_the_ceiling():
    """#724: the floor lifts the margin for its own kWh, not the whole plan."""
    plan = _floor_plan(premium_kwh=1.0)

    premium = sum(a.planned_battery_kwh for a in plan.allocations if a.slot.price > 0.35)
    cheap = sum(a.planned_battery_kwh for a in plan.allocations if a.slot.price <= 0.35)
    assert premium == pytest.approx(1.0)
    assert cheap == pytest.approx(2.0 * 0.25 * CHARGE_EFFICIENCY)


def test_floor_premium_respects_the_user_price_cap():
    plan = _floor_plan(premium_kwh=1.0, premium_price_threshold=0.45)

    assert all(a.slot.price <= 0.35 for a in plan.allocations)


def test_no_floor_premium_keeps_every_slot_under_the_ceiling():
    plan = _floor_plan()

    assert all(a.slot.price <= 0.35 for a in plan.allocations)


def test_slot_ceilings_replace_the_scalar_ceiling_per_slot():
    """#649: each slot is limited by its own arbitrage ceiling (static 0.35)."""
    slots = [_slot(0, 15, 0.30), _slot(15, 60, 0.50), _slot(60, 120, 0.50)]
    # Only the 00:00 slot is arbitrage-justified; the others get -inf.
    ceilings = {slots[0].start: 0.35, slots[1].start: float("-inf"), slots[2].start: float("-inf")}
    intervals = [_interval(7, 5.0)]
    plan = allocate_price_slots(
        intervals,
        build_energy_deadlines(intervals, usable_initial_kwh=0.0),
        slots,
        total_required_kwh=5.0,
        effective_power_kw=2.0,
        now=BASE,
        horizon_end=BASE + timedelta(hours=12),
        max_price_threshold=0.60,
        slot_ceilings=ceilings,
    )
    assert [a.slot for a in plan.allocations] == [slots[0]]


def test_floor_premium_still_buys_above_the_slot_ceiling_up_to_the_static_cap():
    slots = [_slot(0, 15, 0.30), _slot(15, 60, 0.50), _slot(60, 120, 0.70)]
    ceilings = {s.start: 0.35 for s in slots}
    intervals = [_interval(7, 5.0)]
    plan = allocate_price_slots(
        intervals,
        build_energy_deadlines(intervals, usable_initial_kwh=0.0),
        slots,
        total_required_kwh=5.0,
        effective_power_kw=2.0,
        now=BASE,
        horizon_end=BASE + timedelta(hours=12),
        max_price_threshold=0.60,
        slot_ceilings=ceilings,
        premium_kwh=1.0,
        premium_price_threshold=0.60,
    )
    premium = sum(a.planned_battery_kwh for a in plan.allocations if a.slot.price > 0.35)
    assert premium == pytest.approx(1.0)
    assert all(a.slot.price <= 0.60 for a in plan.allocations)


def _night_charge_plan(solar_total_kwh):
    """#704 shape: 01-08 window, morning load, midday solar, evening load."""
    intervals = []
    for index in range(96):
        hour = index / 4
        consumption = 0.0 if hour < 8 else 0.125 if hour < 17 else 0.2
        solar = solar_total_kwh / 20 if 12 <= hour < 17 else 0.0
        intervals.append(_interval(index, consumption, solar))
    deadlines = build_energy_deadlines(
        intervals, usable_initial_kwh=0.61, usable_capacity_kwh=5.12
    )
    return intervals, allocate_price_slots(
        intervals,
        deadlines,
        [_slot(60, 480, 0.1)],
        total_required_kwh=4.51,
        effective_power_kw=2.5,
        charge_efficiency=1.0,
        now=BASE,
        horizon_end=BASE + timedelta(days=1),
        headroom_kwh=4.51,
        usable_initial_kwh=0.61,
    )


def _clipped_kwh(intervals, plan):
    energy = 0.61 + plan.allocated_kwh
    clipped = 0.0
    for interval in intervals[32:]:
        energy += interval.solar_kwh - interval.consumption_kwh
        clipped += max(0.0, energy - 5.12)
        energy = max(0.0, min(5.12, energy))
    return clipped


def test_energy_clipped_under_solar_peak_is_not_charged():
    """#704: night energy the midday solar would export is not bought."""
    intervals, plan = _night_charge_plan(solar_total_kwh=7.0)

    # 1.39 morning deficit + 0.62 the solar leaves free; the rest was exported.
    assert plan.allocated_kwh == pytest.approx(2.01)
    assert plan.total_required_kwh == pytest.approx(plan.allocated_kwh)
    assert _clipped_kwh(intervals, plan) == pytest.approx(0.0, abs=1e-6)
    assert plan.reason == "ok"


def test_charge_unchanged_when_solar_never_fills_the_battery():
    _, plan = _night_charge_plan(solar_total_kwh=4.0)

    assert plan.allocated_kwh == pytest.approx(4.51)


@pytest.mark.parametrize("kind, expected", [("guaranteed_floor", 0.5), ("depletion", 0.0)])
def test_only_floor_energy_is_charged_ahead_of_a_solar_fill(kind, expected):
    intervals = [_interval(0, 0.0), _interval(1, 0.0, 2.0)]
    plan = allocate_price_slots(
        intervals,
        [EnergyDeadline(intervals[0].end, 0.5, kind)],
        [_slot(0, 15, 0.1)],
        total_required_kwh=0.5,
        effective_power_kw=4.0,
        charge_efficiency=1.0,
        now=BASE,
        horizon_end=BASE + timedelta(hours=1),
        headroom_kwh=1.0,
    )

    assert plan.allocated_kwh == pytest.approx(expected)
    assert plan.deadline_shortfall_kwh == pytest.approx(0.0)


def _import_value_plan(**kwargs):
    # 1 kWh used at 10:00 (import 0.30) and 1 kWh at 11:00 (import 0.20);
    # the night slot costs 0.15.
    intervals = [_interval(40, 1.0), _interval(44, 1.0)]
    slots = [_slot(0, 60, 0.15), _slot(600, 615, 0.30), _slot(660, 675, 0.20)]
    return allocate_price_slots(
        intervals,
        kwargs.pop(
            "deadlines", build_energy_deadlines(intervals, usable_initial_kwh=0.0)
        ),
        slots,
        total_required_kwh=2.0,
        effective_power_kw=2.0,
        now=BASE,
        horizon_end=BASE + timedelta(hours=12),
        **kwargs,
    )


def test_deadline_energy_is_bought_only_where_it_beats_the_import_then():
    """#662: 0.30 x 0.82 - 0.06 = 0.186 repays the 0.15 night slot, while
    0.20 x 0.82 - 0.06 = 0.104 does not, so that kWh is imported at 11:00."""
    plan = _import_value_plan(import_value_efficiency=0.82, import_value_margin=0.06)

    assert [a.slot.price for a in plan.allocations] == [0.15]
    assert plan.allocated_kwh == pytest.approx(1.0)
    assert plan.cheaper_to_import_kwh == pytest.approx(1.0)
    assert plan.deadline_shortfall_kwh == pytest.approx(0.0)
    assert plan.reason == "ok"


def test_deadlines_without_import_value_keep_buying_everything():
    plan = _import_value_plan()

    assert plan.allocated_kwh == pytest.approx(2.0)
    assert plan.cheaper_to_import_kwh == 0.0


def test_everything_cheaper_to_import_is_not_a_shortfall():
    plan = _import_value_plan(import_value_efficiency=0.82, import_value_margin=0.20)

    assert plan.allocations == []
    assert plan.deadline_shortfall_kwh == pytest.approx(0.0)
    assert plan.cheaper_to_import_kwh == pytest.approx(2.0)
    assert plan.reason == "cheaper_to_import"


def test_guaranteed_floor_deadline_ignores_the_import_value():
    deadline = EnergyDeadline(BASE + timedelta(hours=11, minutes=15), 2.0, "guaranteed_floor")
    plan = _import_value_plan(
        deadlines=[deadline],
        import_value_efficiency=0.82,
        import_value_margin=0.20,
    )

    assert plan.allocated_kwh == pytest.approx(2.0)
    assert plan.cheaper_to_import_kwh == 0.0


def _unpriced_plan(deadlines=None):
    # 1 kWh used at 10:00 (import 0.30) and 1 kWh at 02:00 the next night,
    # after the last published price (11:15).
    intervals = [_interval(40, 1.0), _interval(104, 1.0)]
    slots = [_slot(0, 60, 0.15), _slot(600, 615, 0.30), _slot(660, 675, 0.20)]
    return allocate_price_slots(
        intervals,
        deadlines or build_energy_deadlines(intervals, usable_initial_kwh=0.0),
        slots,
        total_required_kwh=2.0,
        effective_power_kw=2.0,
        now=BASE,
        horizon_end=BASE + timedelta(hours=27),
        import_value_efficiency=0.82,
        import_value_margin=0.06,
    )


def test_a_deadline_after_the_last_published_price_waits_for_its_price():
    """Its import price is unknown, so no slot is shown to repay it: the
    night slot must not be bought for it at 00:05 (it was, under the slot
    ceiling only, and the energy then went into whatever came first)."""
    plan = _unpriced_plan()

    assert [a.slot.price for a in plan.allocations] == [0.15]
    assert plan.allocated_kwh == pytest.approx(1.0)
    assert plan.cheaper_to_import_kwh == pytest.approx(1.0)
    assert plan.deadline_shortfall_kwh == pytest.approx(0.0)


def test_an_unpriced_guaranteed_floor_deadline_is_still_bought():
    deadline = EnergyDeadline(BASE + timedelta(hours=26, minutes=15), 2.0, "guaranteed_floor")
    plan = _unpriced_plan(deadlines=[deadline])

    assert plan.allocated_kwh == pytest.approx(2.0)
    assert plan.cheaper_to_import_kwh == 0.0


def test_price_threshold_names_the_cheapest_rejected_slot():
    plan = _import_value_plan(max_price_threshold=0.10)

    assert plan.reason == "price_threshold"
    assert plan.cheapest_rejected == (BASE, 0.15, 0.10)


def _left_hour_plan(use_at, *, sun_at=None, dull_at=None, **kwargs):
    # 1 kWh used at 02:00 (import 0.16, left to the grid: 0.16 x 0.82 - 0.06
    # is under every slot) and 1 kWh at ``use_at`` (import 0.40, which repays
    # the 0.15 night slot and the 0.16 slot alike).  ``sun_at`` adds an
    # interval the sun covers, ``dull_at`` one with forecast solar that does
    # not cover the house.
    intervals = [_interval(8, 1.0), _interval(use_at, 1.0)]
    if sun_at is not None:
        intervals.append(_interval(sun_at, 0.1, solar=0.5))
    if dull_at is not None:
        intervals.append(_interval(dull_at, 0.5, solar=0.5 - 1e-3))
    intervals.sort(key=lambda item: item.start)
    slots = [
        _slot(0, 60, 0.15),
        _slot(120, 135, 0.16),
        _slot(15 * use_at, 15 * use_at + 15, 0.40),
    ]
    deadlines = build_energy_deadlines(intervals, usable_initial_kwh=0.0)
    return allocate_price_slots(
        intervals,
        deadlines,
        slots,
        total_required_kwh=deadlines[-1].required_cumulative_kwh,
        effective_power_kw=2.0,
        now=BASE,
        horizon_end=BASE + timedelta(hours=20),
        import_value_efficiency=0.82,
        import_value_margin=0.06,
        **kwargs,
    )


def test_energy_stored_before_an_hour_left_to_the_grid_is_not_booked_after_it():
    """The battery covers 02:00 first, so the 00:00 charge never reaches 18:00.

    Only the 02:00 slot, during which the grid covers the house, still serves
    18:00; the rest of 18:00 is imported then.
    """
    plan = _left_hour_plan(72)

    assert [(a.slot.start.hour, a.slot.price) for a in plan.allocations] == [(2, 0.16)]
    assert plan.allocated_kwh == pytest.approx(0.425)
    assert plan.cheaper_to_import_kwh == pytest.approx(2.0 - 0.425)
    assert plan.deadline_shortfall_kwh == pytest.approx(0.0)


def test_the_discharge_reserve_carries_night_energy_to_the_morning():
    """It keeps the 00:00 charge for 07:00, before the sun covers the house."""
    plan = _left_hour_plan(28, sun_at=36, reserve_holds=True)

    assert [(a.slot.start.hour, a.slot.price) for a in plan.allocations] == [(0, 0.15)]
    assert plan.allocated_kwh == pytest.approx(1.0)
    assert plan.cheaper_to_import_kwh == pytest.approx(1.0)


def test_without_the_reserve_the_morning_gets_only_the_later_slot():
    plan = _left_hour_plan(28, sun_at=36)

    assert [(a.slot.start.hour, a.slot.price) for a in plan.allocations] == [(2, 0.16)]


def test_the_reserve_does_not_carry_night_energy_past_the_sun():
    """After the sun covers the house the reserve holds nothing back for the
    evening, so the 00:00 charge is not booked for it."""
    plan = _left_hour_plan(72, sun_at=36, reserve_holds=True)

    assert all(a.slot.start.hour == 2 for a in plan.allocations)
    assert plan.allocated_kwh <= 0.425 + 1e-9


def test_the_reserve_carries_night_energy_through_a_sunless_day():
    """Forecast solar, but no interval where the sun covers the house: the
    reserve keeps the evening in its plan, so the 00:00 charge is held for
    18:00."""
    plan = _left_hour_plan(72, dull_at=48, reserve_holds=True)

    assert [(a.slot.start.hour, a.slot.price) for a in plan.allocations] == [(0, 0.15)]
    assert plan.allocated_kwh == pytest.approx(1.0 + 1e-3)
    assert plan.cheaper_to_import_kwh == pytest.approx(1.0)


def test_a_day_without_any_forecast_solar_does_not_carry_night_energy():
    """No solar forecast at all (no PV, no forecast, or 0): the reserve plans
    only to sunrise, so the 00:00 charge is not held for 18:00 and the plan
    is the one without the reserve."""
    held = _left_hour_plan(72, reserve_holds=True)
    plain = _left_hour_plan(72)

    assert [(a.slot.start.hour, a.slot.price) for a in held.allocations] == [(2, 0.16)]
    assert held.allocated_kwh == pytest.approx(plain.allocated_kwh)
    assert held.cheaper_to_import_kwh == pytest.approx(plain.cheaper_to_import_kwh)


def test_a_sunless_day_does_not_carry_a_charge_from_before_midnight():
    """Before midnight the reserve plans only to sunrise, so a 22:00 charge is
    not held through the next day to its evening."""
    now = BASE - timedelta(hours=2)
    intervals = [_interval(8, 1.0), _interval(72, 1.0)]
    slots = [_slot(-120, -105, 0.15), _slot(120, 135, 0.16), _slot(1080, 1095, 0.40)]
    deadlines = build_energy_deadlines(intervals, usable_initial_kwh=0.0)
    plan = allocate_price_slots(
        intervals,
        deadlines,
        slots,
        total_required_kwh=deadlines[-1].required_cumulative_kwh,
        effective_power_kw=2.0,
        now=now,
        horizon_end=BASE + timedelta(hours=20),
        import_value_efficiency=0.82,
        import_value_margin=0.06,
        reserve_holds=True,
    )

    assert all(a.slot.start.hour == 2 for a in plan.allocations)


def test_a_brief_dawn_blip_ends_the_reserve_hold():
    """held_until is the first interval whose solar covers the house, even a
    short blip before the morning deadline.  The reserve then no longer carries
    the 00:00 charge past it (conservative: the morning imports instead)."""
    plan = _left_hour_plan(28, sun_at=20, reserve_holds=True)

    assert all(a.slot.start.hour != 0 for a in plan.allocations)
