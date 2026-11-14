"""The control plan's trace: what the planner saw and booked, for the decision log."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.engine import PricingManager, _plan_trace
from custom_components.omnibattery.solar_forecast import (
    SolarForecastInput,
    SolarForecastPeriod,
)
from custom_components.omnibattery.tracking.consumption_profile import (
    INTERVAL_COUNT,
    ConsumptionForecast,
)

NOW = datetime(2026, 11, 11, 0, 5, 2)
HORIZON = datetime(2026, 11, 12, 7, 54, 44)
DAY = datetime(2026, 11, 11)


def _load_kw(hour):
    return 1.2 if hour < 7 else 1.1 if hour < 17 else 1.6 if hour < 22 else 1.2


def _scenario():
    """A night plan over a 30-hour horizon: 7 kWh, 800 W battery, heat-pump load."""
    shape = [_load_kw(i // 4) / 4 for i in range(INTERVAL_COUNT)]

    def forecast_between(start, end, **_kw):
        total, t = 0.0, start
        while t < end:
            nxt = min(end, t.replace(minute=t.minute // 15 * 15, second=0, microsecond=0)
                      + timedelta(minutes=15))
            total += _load_kw(t.hour) * (nxt - t).total_seconds() / 3600
            t = nxt
        return ConsumptionForecast(
            total, list(shape), "profile", True,
            intervals_by_date={(DAY + timedelta(days=i)).date(): list(shape) for i in range(3)},
        )

    weights = [1, 2, 3, 3.5, 3, 2, 1]
    periods = tuple(
        SolarForecastPeriod(
            datetime(2026, 11, 11, h, tzinfo=timezone.utc),
            datetime(2026, 11, 11, h + 1, tzinfo=timezone.utc),
            5.17 * w / sum(weights),
        )
        for h, w in zip(range(9, 16), weights)
    )

    def price(q):
        h = q // 4
        return 0.20 if h < 6 else 0.26 if h < 9 else 0.22 if h < 16 else 0.32 if h < 21 else 0.24

    slots = [
        PriceSlot(DAY + timedelta(minutes=15 * q), DAY + timedelta(minutes=15 * (q + 1)), price(q))
        for q in range(96)
    ]
    battery = SimpleNamespace(
        data={"battery_soc": 21.0, "battery_total_energy": 7.0},
        min_soc=0, max_soc=100, effective_max_discharge_power=800.0,
    )
    controller = SimpleNamespace(
        _consumption_tracker=SimpleNamespace(
            consumption_profile=SimpleNamespace(),
            forecast_consumption_between=forecast_between,
        ),
        coordinators=[battery],
        _is_battery_manual_owned=lambda _c: False,
        solar_profile_mode="active",
        _predictive_safety_margin_kwh=0.35,
        max_contracted_power=7000,
        max_charge_capacity=1200,
        max_price_threshold=None,
        predictive_charging_mode="dynamic_pricing",
        dp_price_discharge_control=False,
        price_sensor=None,
        min_arbitrage_margin=None,
        _weekly_full_charge_pending=lambda: False,
        _last_plan_trace=None,
    )
    manager = PricingManager(SimpleNamespace(config=SimpleNamespace(time_zone="UTC")), controller)
    manager._read_remaining_solar_input = lambda **_kw: SolarForecastInput(
        5.17, "remaining_sensor", periods=periods
    )
    manager.energy_horizon_end = lambda _now: HORIZON
    decision = {
        "should_charge": True,
        "planned_grid_charge_kwh": 5.53,
        "energy_deficit_kwh": 33.34,
        "avg_consumption_kwh": 39.63,
        "remaining_consumption_kwh": 39.63,
        "floor_deficit_kwh": 0.0,
        "energy_horizon_end": HORIZON,
        "total_available_kwh": 6.29,
    }
    return manager, controller, slots, decision


def test_the_trace_holds_everything_the_control_plan_used_and_booked():
    manager, controller, slots, decision = _scenario()
    plan = manager._build_chronological_plan(
        now=NOW, slots=slots, decision_data=decision, price_ceiling=None,
    )
    trace = controller._last_plan_trace

    json.dumps(trace)  # JSON-safe as built
    assert trace["now"] == NOW.isoformat() and trace["horizon_end"] == HORIZON.isoformat()
    # Untruncated: the decision log cut these lists at 24 items.
    assert len(trace["intervals"]) == len(plan.intervals) > 24
    assert len(trace["deadlines"]) == len(plan.deadlines) > 24
    assert len(trace["price_slots"]) == len(slots)
    assert [item[0] for item in trace["allocations"]] == [
        a.slot.start.isoformat() for a in plan.allocations
    ]
    assert [item[2] for item in trace["allocations"]] == [
        pytest.approx(a.planned_battery_kwh, abs=1e-4) for a in plan.allocations
    ]
    assert [item[1] for item in trace["deadlines"]] == [
        pytest.approx(d.required_cumulative_kwh, abs=1e-4) for d in plan.deadlines
    ]
    first = plan.intervals[0]
    assert trace["intervals"][0][:3] == [
        first.start.isoformat(),
        pytest.approx(first.consumption_kwh, abs=1e-4),
        pytest.approx(first.solar_kwh, abs=1e-4),
    ]
    assert sum(item[2] for item in trace["intervals"]) == pytest.approx(
        sum(i.solar_kwh for i in plan.intervals), abs=1e-3
    )
    assert sum(item[1] for item in trace["intervals"]) == pytest.approx(
        sum(i.consumption_kwh for i in plan.intervals), abs=1e-2
    )
    inputs = trace["inputs"]
    assert inputs["usable_initial_kwh"] == pytest.approx(0.21 * 7.0, abs=0.01)
    assert inputs["required_from_deadlines"] is False
    assert inputs["discharge_limit_w"] == pytest.approx(800.0)
    assert trace["result"]["allocated_kwh"] == pytest.approx(plan.allocated_kwh, abs=1e-4)
    assert trace["result"]["reason"] == plan.reason


def test_a_diagnostic_plan_does_not_replace_the_control_trace():
    manager, controller, slots, decision = _scenario()
    manager._build_chronological_plan(
        now=NOW, slots=slots, decision_data=dict(decision), price_ceiling=None,
    )
    trace = controller._last_plan_trace
    manager._build_chronological_plan(
        now=NOW + timedelta(minutes=5), slots=slots, decision_data=dict(decision),
        price_ceiling=None, diagnostic_only=True,
    )
    assert controller._last_plan_trace is trace


def test_a_trace_that_cannot_be_built_is_none_and_never_raises():
    assert _plan_trace(
        now=NOW, horizon_end=HORIZON, intervals=[object()], slots=[],
        slot_ceilings=None, plan=SimpleNamespace(), inputs={},
    ) is None


def test_trace_inputs_keep_booleans_and_drop_non_finite_numbers():
    plan = SimpleNamespace(
        reason="ok", total_required_kwh=1.0, deadline_required_kwh=1.0,
        flexible_required_kwh=0.0, allocated_kwh=1.0, deadline_shortfall_kwh=0.0,
        total_shortfall_kwh=0.0, minimum_projected_energy_kwh=-1.0,
        earliest_depletion_at=None, cheaper_to_import_kwh=0.0, awaiting_prices_kwh=0.0,
        deadlines=[], allocations=[],
    )
    trace = _plan_trace(
        now=NOW, horizon_end=HORIZON, intervals=[], slots=[], slot_ceilings=None, plan=plan,
        inputs={"flag": True, "room": float("inf"), "none": None, "power": 2.5},
    )
    assert trace["inputs"] == {"flag": True, "room": None, "none": None, "power": 2.5}
