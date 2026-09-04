"""Regression tests for Dynamic Pricing negative-price opportunistic charging."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import (
    CONF_NEGATIVE_PRICE_CHARGING_ENABLED,
    CONF_SMART_PREDISCHARGE_ENABLED,
    DEFAULT_NEGATIVE_PRICE_CHARGING_ENABLED,
    DEFAULT_ROUND_TRIP_EFFICIENCY,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
)
from custom_components.omnibattery import diagnostics
from custom_components.omnibattery.pricing import (
    CurtailmentPlan,
    DynamicPricingSchedule,
    PriceSlot,
    SLOT_PURPOSE_COMBINED,
    SLOT_PURPOSE_DEFICIT,
    SLOT_PURPOSE_NEGATIVE_PRICE,
    calculations,
)
from custom_components.omnibattery.pricing.engine import (
    DynamicPricingEvaluationHorizon,
    PricingManager,
)
from custom_components.omnibattery.switch import (
    NegativePriceChargingSwitch,
    SmartPredischargeSwitch,
)


class _Battery:
    """Small battery stand-in with the telemetry used by the pricing engine."""

    def __init__(
        self,
        name: str,
        soc: float,
        capacity_kwh: float,
        *,
        max_soc: float = 100.0,
        available: bool = True,
    ) -> None:
        self.name = name
        self.max_soc = max_soc
        self.is_available = available
        self.data = {
            "battery_soc": soc,
            "battery_total_energy": capacity_kwh,
        }


def _decision(*, should_charge: bool = False, deficit: float = 0.0) -> dict:
    return {
        "should_charge": should_charge,
        "avg_soc": 30.0,
        "avg_consumption_kwh": 4.0,
        "energy_deficit_kwh": deficit,
        "planned_grid_charge_kwh": deficit,
        "solar_forecast_kwh": None,
        "usable_energy_kwh": 2.0,
        "total_available_kwh": 2.0,
        "days_in_history": 0,
    }


def _controller(
    batteries: list[_Battery],
    *,
    enabled: bool = True,
    decision: dict | None = None,
) -> SimpleNamespace:
    async def should_activate():
        return decision or _decision()

    return SimpleNamespace(
        coordinators=batteries,
        predictive_charging_enabled=True,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        predictive_charging_overridden=False,
        negative_price_charging_enabled=enabled,
        smart_predischarge_enabled=False,
        max_contracted_power=4000,
        max_charge_capacity=4000,
        max_price_threshold=None,
        min_arbitrage_margin=None,
        round_trip_efficiency=DEFAULT_ROUND_TRIP_EFFICIENCY,
        price_integration_type="nordpool",
        price_sensor="sensor.price",
        _should_activate_grid_charging=should_activate,
        _last_decision_data=None,
        _dynamic_pricing_schedule=None,
        _dynamic_pricing_evaluated_date=None,
        _dp_eval_retry_count=0,
        _dp_last_eval_soc=None,
        _dp_arbitrage_ceiling=None,
        _dp_daily_avg_price=None,
        _dp_pre_evaluated_slots={},
        _dp_pre_evaluated_purposes={},
        _dp_completed_slots=set(),
        _current_price_slot_active=False,
        _active_dynamic_slot_purpose=None,
        _predictive_charge_target_soc=None,
        grid_charging_active=False,
        _grid_charging_initialized=False,
        previous_power=0,
        previous_error=0,
        first_execution=True,
    )


async def _noop(*_args, **_kwargs):
    return None


def _evaluate(ctrl: SimpleNamespace, slots: list[PriceSlot]) -> PricingManager:
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._maybe_refresh_service_prices = _noop
    manager._parse_price_data = lambda horizon_end=None: slots
    manager._send_dynamic_pricing_notification = _noop
    manager._build_curtailment_plan = lambda *_args, **_kwargs: CurtailmentPlan(
        status="no_risk", reason="none"
    )
    asyncio.run(
        manager._evaluate_dynamic_pricing(
            horizon=DynamicPricingEvaluationHorizon.DAILY,
        )
    )
    return manager


def _future_slots(prices: list[float], minutes: int = 60) -> list[PriceSlot]:
    start = datetime.now() + timedelta(hours=2)
    return [
        PriceSlot(
            start=start + timedelta(minutes=minutes * index),
            end=start + timedelta(minutes=minutes * (index + 1)),
            price=price,
        )
        for index, price in enumerate(prices)
    ]


def _schedule(
    slots: list[PriceSlot],
    purposes: dict[PriceSlot, str],
    *,
    deficit_needed: bool = False,
) -> DynamicPricingSchedule:
    values = set(purposes.values())
    schedule_type = (
        SLOT_PURPOSE_COMBINED
        if SLOT_PURPOSE_COMBINED in values or len(values) > 1
        else next(iter(values))
    )
    return DynamicPricingSchedule(
        hours_needed=sum(
            (slot.end - slot.start).total_seconds() / 3600 for slot in slots
        ),
        selected_slots=slots,
        average_price=sum(slot.price for slot in slots) / len(slots),
        estimated_cost=0.0,
        total_available_slots=len(slots),
        evaluation_time=datetime.now(),
        energy_deficit_kwh=1.0 if deficit_needed else 0.0,
        charging_needed=True,
        slot_purposes=purposes,
        schedule_type=schedule_type,
        deficit_charging_needed=deficit_needed,
        negative_price_charging_needed=any(
            purpose in {SLOT_PURPOSE_NEGATIVE_PRICE, SLOT_PURPOSE_COMBINED}
            for purpose in purposes.values()
        ),
    )


def test_feature_disabled_keeps_informational_deficit_calendar_behavior():
    slots = _future_slots([-0.20, -0.10, 0.10])
    ctrl = _controller([_Battery("b1", 20, 10)], enabled=False)

    _evaluate(ctrl, slots)

    schedule = ctrl._dynamic_pricing_schedule
    assert schedule.charging_needed is False
    assert schedule.negative_price_charging_needed is False
    assert schedule.schedule_type == SLOT_PURPOSE_DEFICIT
    assert set(schedule.slot_purposes.values()) == {SLOT_PURPOSE_DEFICIT}


def test_defaults_are_opt_in_and_dynamic_pricing_scope_is_enforced():
    assert DEFAULT_NEGATIVE_PRICE_CHARGING_ENABLED is False

    ctrl = _controller([_Battery("b1", 20, 10)])
    ctrl.predictive_charging_mode = "realtime_price"

    assert PricingManager(SimpleNamespace(), ctrl)._negative_price_feature_enabled() is False


def test_no_solar_no_deficit_negative_price_creates_real_charge_calendar():
    slots = _future_slots([-0.05, -0.30, -0.10])
    ctrl = _controller([_Battery("b1", 50, 4, max_soc=75)])

    _evaluate(ctrl, slots)

    schedule = ctrl._dynamic_pricing_schedule
    assert schedule.charging_needed is True
    assert schedule.deficit_charging_needed is False
    assert schedule.negative_price_charging_needed is True
    assert schedule.schedule_type == SLOT_PURPOSE_NEGATIVE_PRICE
    assert [slot.price for slot in schedule.selected_slots] == [-0.30]
    assert schedule.negative_price_energy_kwh == pytest.approx(1.0)


def test_zero_or_positive_price_does_not_charge_opportunistically():
    ctrl = _controller([_Battery("b1", 20, 10)])

    _evaluate(ctrl, _future_slots([0.0, 0.05, 0.10]))

    schedule = ctrl._dynamic_pricing_schedule
    assert schedule is not None  # legacy informational cheapest-slot calendar
    assert schedule.charging_needed is False
    assert schedule.negative_price_charging_needed is False


def test_missing_price_data_retries_when_opportunity_target_is_pending():
    ctrl = _controller([_Battery("b1", 20, 10)])

    _evaluate(ctrl, [])

    assert ctrl._dynamic_pricing_schedule is None
    assert ctrl._dynamic_pricing_evaluated_date is None
    assert ctrl._dp_eval_retry_count == 1


def test_threshold_is_inclusive_and_most_negative_slots_are_selected():
    slots = _future_slots([-0.10, -0.40, -0.20, 0.01])

    selected = calculations.select_cheapest_slots_by_duration(
        slots, hours_needed=2.0, max_price_threshold=-0.10
    )

    assert {slot.price for slot in selected} == {-0.40, -0.20}
    inclusive = calculations.select_cheapest_slots_by_duration(
        [slots[0]], hours_needed=1.0, max_price_threshold=-0.10
    )
    assert inclusive == [slots[0]]


def test_current_partial_slot_counts_only_its_remaining_duration():
    now = datetime.now()
    almost_finished = PriceSlot(
        start=now - timedelta(minutes=14),
        end=now + timedelta(minutes=1),
        price=-0.50,
    )
    next_slot = PriceSlot(
        start=now + timedelta(minutes=1),
        end=now + timedelta(minutes=16),
        price=-0.20,
    )

    selected = calculations.select_cheapest_slots_by_duration(
        [almost_finished, next_slot],
        hours_needed=0.20,
        max_price_threshold=0.0,
        now=now,
    )

    assert selected == [almost_finished, next_slot]


@pytest.mark.parametrize(
    ("minutes", "energy_kwh", "expected_count"),
    [(60, 3.0, 1), (15, 1.6, 2)],
)
def test_hourly_and_quarter_hour_opportunities_use_needed_duration(
    minutes: int, energy_kwh: float, expected_count: int
):
    # At 4 kW and 85% efficiency these requests need 0.882 h and 0.471 h.
    battery = _Battery("b1", 50, energy_kwh * 2)
    ctrl = _controller([battery])
    slots = _future_slots([-0.10, -0.40, -0.30, -0.20], minutes)

    _evaluate(ctrl, slots)

    selected = ctrl._dynamic_pricing_schedule.selected_slots
    assert len(selected) == expected_count
    assert [slot.price for slot in selected] == sorted(
        [slot.price for slot in selected]
    )
    assert set(slot.price for slot in selected) == set(
        sorted(slot.price for slot in slots)[:expected_count]
    )


def test_energy_and_targets_cover_multiple_different_batteries():
    first = _Battery("large", 20, 10, max_soc=80)
    second = _Battery("small", 60, 5, max_soc=100)
    ctrl = _controller([first, second])
    manager = PricingManager(SimpleNamespace(), ctrl)

    assert manager._negative_price_energy_needed_kwh() == pytest.approx(8.0)

    ctrl._active_dynamic_slot_purpose = SLOT_PURPOSE_NEGATIVE_PRICE
    targets = ChargeDischargeController._compute_predictive_target_soc(ctrl)
    assert targets[first] == 80
    assert targets[second] == 100


def test_opportunity_target_stays_authoritative_during_weekly_full_charge():
    battery = _Battery("b1", 70, 10, max_soc=90)
    ctrl = SimpleNamespace(
        grid_charging_active=True,
        _active_dynamic_slot_purpose=SLOT_PURPOSE_NEGATIVE_PRICE,
        _predictive_charge_target_soc={battery: 90.0},
    )

    ceiling, source = ChargeDischargeController._effective_charge_max_soc(
        ctrl, battery, weekly_100_unlocked=True
    )

    assert (ceiling, source) == (90.0, "predictive_target")


def test_normal_predictive_target_stays_authoritative_during_weekly_full_charge():
    """Weekly full charge permits solar top-up, never extra grid charging."""
    battery = _Battery("b1", 20, 10, max_soc=95)
    ctrl = SimpleNamespace(
        grid_charging_active=True,
        _active_dynamic_slot_purpose=SLOT_PURPOSE_DEFICIT,
        _predictive_charge_target_soc={battery: 30.0},
    )

    ceiling, source = ChargeDischargeController._effective_charge_max_soc(
        ctrl, battery, weekly_100_unlocked=True
    )

    assert (ceiling, source) == (30.0, "predictive_target")


def test_unavailable_battery_does_not_create_opportunistic_energy():
    battery = _Battery("offline", 10, 10, available=False)
    ctrl = _controller([battery])
    manager = PricingManager(SimpleNamespace(), ctrl)

    assert manager._negative_price_energy_needed_kwh() == 0.0
    assert manager._opportunistic_target_pending() is False


def test_exact_duration_uses_contracted_and_system_charge_bottlenecks():
    assert calculations.calculate_exact_charging_hours_needed(1.7, 1000, 4000) == 2.0
    assert calculations.calculate_exact_charging_hours_needed(1.7, 4000, 1000) == 2.0


def test_combined_calendar_keeps_positive_slot_deficit_only():
    battery = _Battery("b1", 20, 10, max_soc=80)
    ctrl = _controller(
        [battery],
        decision=_decision(should_charge=True, deficit=5.0),
    )
    slots = _future_slots([-0.20, 0.05, 0.40])

    _evaluate(ctrl, slots)

    schedule = ctrl._dynamic_pricing_schedule
    assert schedule.schedule_type == SLOT_PURPOSE_COMBINED
    assert schedule.purpose_for(slots[0]) == SLOT_PURPOSE_COMBINED
    assert schedule.purpose_for(slots[1]) == SLOT_PURPOSE_DEFICIT

    ctrl._last_decision_data = _decision(should_charge=True, deficit=2.0)
    ctrl._predictive_grid_charge_margin_pct = 0.0
    ctrl._active_dynamic_slot_purpose = SLOT_PURPOSE_DEFICIT
    positive_target = ChargeDischargeController._compute_predictive_target_soc(ctrl)
    ctrl._active_dynamic_slot_purpose = SLOT_PURPOSE_COMBINED
    combined_target = ChargeDischargeController._compute_predictive_target_soc(ctrl)
    assert positive_target[battery] == pytest.approx(40.0)
    assert combined_target[battery] == 80.0


def test_pre_slot_reevaluation_preserves_valid_opportunity_without_deficit_call():
    battery = _Battery("b1", 30, 10)
    ctrl = _controller([battery])
    slot = PriceSlot(
        datetime.now() + timedelta(hours=1),
        datetime.now() + timedelta(hours=2),
        -0.10,
    )
    ctrl._dynamic_pricing_schedule = _schedule(
        [slot], {slot: SLOT_PURPOSE_NEGATIVE_PRICE}
    )
    ctrl._current_price_slot_active = False
    calls = []

    async def should_not_run():
        calls.append(True)
        return _decision()

    ctrl._should_activate_grid_charging = should_not_run
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._slot_overlaps_curtailment_risk = lambda _slot: False

    asyncio.run(manager._check_dp_pre_slot_reevaluation())

    assert calls == []
    assert ctrl._dp_pre_evaluated_slots[slot.start] is True
    assert (
        ctrl._dp_pre_evaluated_purposes[slot.start]
        == SLOT_PURPOSE_NEGATIVE_PRICE
    )


@pytest.mark.parametrize("current_price", [None, float("nan"), 0.01])
def test_invalid_or_nonnegative_live_price_revokes_pure_opportunity(
    current_price: float | None,
):
    battery = _Battery("b1", 30, 10)
    ctrl = _controller([battery])
    slot = _future_slots([-0.10])[0]
    ctrl._dynamic_pricing_schedule = _schedule(
        [slot], {slot: SLOT_PURPOSE_NEGATIVE_PRICE}
    )
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._get_current_price = lambda: current_price
    manager._slot_overlaps_curtailment_risk = lambda _slot: False

    assert manager._effective_slot_purpose(slot) is None


def test_live_price_zero_is_not_authorized():
    battery = _Battery("b1", 30, 10)
    ctrl = _controller([battery])
    slot = _future_slots([-0.10])[0]
    ctrl._dynamic_pricing_schedule = _schedule(
        [slot], {slot: SLOT_PURPOSE_NEGATIVE_PRICE}
    )
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._get_current_price = lambda: 0.0
    manager._slot_overlaps_curtailment_risk = lambda _slot: False

    assert manager._effective_slot_purpose(slot) is None


def test_live_negative_price_remains_authorized():
    battery = _Battery("b1", 30, 10)
    ctrl = _controller([battery])
    slot = _future_slots([-0.10])[0]
    ctrl._dynamic_pricing_schedule = _schedule(
        [slot], {slot: SLOT_PURPOSE_NEGATIVE_PRICE}
    )
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._get_current_price = lambda: -0.01
    manager._slot_overlaps_curtailment_risk = lambda _slot: False

    assert manager._effective_slot_purpose(slot) == SLOT_PURPOSE_NEGATIVE_PRICE


def test_smart_predischarge_risk_revokes_opportunity_but_fail_safe_does_not():
    battery = _Battery("b1", 30, 10)
    ctrl = _controller([battery])
    ctrl.smart_predischarge_enabled = True
    slot = _future_slots([-0.10])[0]
    ctrl._dynamic_pricing_schedule = _schedule(
        [slot], {slot: SLOT_PURPOSE_NEGATIVE_PRICE}
    )
    ctrl._curtailment_plan = CurtailmentPlan(
        status="planned", reason="solar_risk", risk_slots=[slot]
    )
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._get_current_price = lambda: -0.10

    assert manager._effective_slot_purpose(slot) is None

    # Missing solar/forecast data makes the solar planner fail safe; it must not
    # veto an otherwise valid import-price opportunity.
    ctrl._curtailment_plan = CurtailmentPlan(
        status="fail_safe", reason="missing_forecast", risk_slots=[slot]
    )
    assert manager._effective_slot_purpose(slot) == SLOT_PURPOSE_NEGATIVE_PRICE


def test_negative_price_inside_risk_uses_only_opportunistic_space():
    battery = _Battery("b1", 60, 10, max_soc=100)
    ctrl = _controller([battery])
    ctrl.smart_predischarge_enabled = True
    slot = _future_slots([-0.10])[0]
    ctrl._dynamic_pricing_schedule = _schedule(
        [slot], {slot: SLOT_PURPOSE_NEGATIVE_PRICE}
    )
    ctrl._curtailment_plan = CurtailmentPlan(
        status="protected",
        reason="headroom_sufficient",
        risk_slots=[slot],
        current_headroom_kwh=4.0,
        required_headroom_kwh=2.0,
        solar_reserve_remaining_kwh=2.0,
        opportunistic_space_kwh=2.0,
    )
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._get_current_price = lambda: -0.10

    assert manager._effective_slot_purpose(slot) == SLOT_PURPOSE_NEGATIVE_PRICE

    # Reaching the reserve does not revoke the physical negative-price slot
    # permanently; it simply makes this cycle ineligible for import charging.
    ctrl._curtailment_plan.opportunistic_space_kwh = 0.0
    ctrl._curtailment_plan.current_headroom_kwh = 2.0
    assert manager._effective_slot_purpose(slot) is None


def test_risk_window_target_is_capped_to_free_space_and_soc_limit():
    battery = _Battery("b1", 60, 10, max_soc=100)
    ctrl = _controller([battery])
    ctrl.smart_predischarge_enabled = True
    slot = _future_slots([-0.10])[0]
    plan = CurtailmentPlan(
        status="protected",
        risk_slots=[slot],
        current_headroom_kwh=4.0,
        required_headroom_kwh=2.0,
        solar_reserve_remaining_kwh=2.0,
        opportunistic_space_kwh=2.0,
    )
    ctrl._curtailment_plan = plan
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._get_current_price = lambda: -0.10

    assert manager._prepare_curtailment_opportunistic_charge(
        plan, slot, SLOT_PURPOSE_NEGATIVE_PRICE
    ) is True
    assert ctrl._predictive_charge_target_soc[battery] == pytest.approx(80.0)

    ctrl._curtailment_opportunistic_space_kwh = 0.0
    plan.opportunistic_space_kwh = 0.0
    assert manager._prepare_curtailment_opportunistic_charge(
        plan, slot, SLOT_PURPOSE_NEGATIVE_PRICE
    ) is False


def test_negative_price_outside_risk_releases_transient_reserve_ceiling():
    battery = _Battery("b1", 60, 10, max_soc=100)
    ctrl = _controller([battery])
    ctrl.smart_predischarge_enabled = True
    slot = _future_slots([-0.10])[0]
    plan = CurtailmentPlan(status="protected", reason="headroom_sufficient")
    ctrl._curtailment_opportunity_limited = True
    ctrl._curtailment_opportunistic_target_soc = {battery: 80.0}
    ctrl._predictive_charge_target_soc = {battery: 80.0}
    ctrl._grid_charging_initialized = True
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._slot_overlaps_curtailment_risk = lambda _slot: False

    assert manager._prepare_curtailment_opportunistic_charge(
        plan, slot, SLOT_PURPOSE_NEGATIVE_PRICE
    ) is False
    assert ctrl._curtailment_opportunistic_target_soc is None
    assert ctrl._predictive_charge_target_soc is None
    assert ctrl._grid_charging_initialized is False


def test_evaluation_moves_opportunity_out_of_solar_risk_window():
    battery = _Battery("b1", 50, 4)
    ctrl = _controller([battery])
    risky, safe = _future_slots([-0.50, -0.20])
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._maybe_refresh_service_prices = _noop
    manager._parse_price_data = lambda horizon_end=None: [risky, safe]
    manager._send_dynamic_pricing_notification = _noop
    manager._build_curtailment_plan = lambda *_args, **_kwargs: CurtailmentPlan(
        status="planned", reason="solar_risk", risk_slots=[risky]
    )

    asyncio.run(
        manager._evaluate_dynamic_pricing(
            horizon=DynamicPricingEvaluationHorizon.DAILY,
        )
    )

    schedule = ctrl._dynamic_pricing_schedule
    assert schedule.selected_slots == [safe]
    assert schedule.purpose_for(safe) == SLOT_PURPOSE_NEGATIVE_PRICE


def test_guaranteed_minimum_floor_keeps_only_deficit_in_solar_risk_window():
    battery = _Battery("b1", 10, 4)
    decision = _decision(should_charge=True, deficit=1.0)
    decision["floor_active"] = True
    ctrl = _controller([battery], decision=decision)
    risky = _future_slots([-0.50])[0]
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._maybe_refresh_service_prices = _noop
    manager._parse_price_data = lambda horizon_end=None: [risky]
    manager._send_dynamic_pricing_notification = _noop
    manager._build_curtailment_plan = lambda *_args, **_kwargs: CurtailmentPlan(
        status="planned", reason="solar_risk", risk_slots=[risky]
    )

    asyncio.run(
        manager._evaluate_dynamic_pricing(
            horizon=DynamicPricingEvaluationHorizon.DAILY,
        )
    )

    schedule = ctrl._dynamic_pricing_schedule
    assert schedule.selected_slots == [risky]
    assert schedule.purpose_for(risky) == SLOT_PURPOSE_DEFICIT
    assert schedule.negative_price_charging_needed is False


def _prepare_runtime_manager(
    ctrl: SimpleNamespace, slot: PriceSlot, commands: list
) -> PricingManager:
    ctrl._dynamic_pricing_schedule = _schedule(
        [slot], {slot: SLOT_PURPOSE_NEGATIVE_PRICE}
    )
    ctrl._dynamic_pricing_evaluated_date = datetime.now().date()
    ctrl._dp_evening_reevaluated_date = datetime.now().date()
    ctrl._current_price_slot_active = True
    ctrl._active_dynamic_slot_purpose = SLOT_PURPOSE_NEGATIVE_PRICE
    ctrl.grid_charging_active = True
    ctrl._predictive_charge_target_soc = {
        battery: battery.max_soc
        for battery in ctrl.coordinators
    }

    async def set_power(battery, charge, discharge):
        commands.append((battery.name, charge, discharge))

    ctrl._set_battery_power = set_power
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._maybe_refresh_service_prices = _noop
    manager._check_dp_pre_slot_reevaluation = _noop
    manager._is_evening_reevaluation_time = lambda: False
    manager._is_dp_soc_drop_reeval = lambda: False
    manager._get_current_price = lambda: slot.price
    return manager


def test_demand_protection_keeps_slot_owned_by_predictive_controller():
    battery = _Battery("b1", 30, 10)
    ctrl = _controller([battery])
    now = datetime.now()
    slot = PriceSlot(now - timedelta(minutes=10), now + timedelta(minutes=50), 0.10)
    state_holder = {"state": SimpleNamespace(state="1900")}
    ctrl._dynamic_pricing_schedule = _schedule(
        [slot], {slot: SLOT_PURPOSE_DEFICIT}, deficit_needed=True
    )
    ctrl._dynamic_pricing_evaluated_date = now.date()
    ctrl._dp_evening_reevaluated_date = now.date()
    ctrl._current_price_slot_active = True
    ctrl._active_dynamic_slot_purpose = SLOT_PURPOSE_DEFICIT
    ctrl._predictive_charge_suspended_for_demand = True
    ctrl.grid_charging_active = True
    ctrl.consumption_sensor = "sensor.grid"
    ctrl._apply_meter_transform = lambda state: float(state.state)
    ctrl.deadband = 40.0
    ctrl.max_contracted_power = 2000.0

    calls = []

    async def handle_predictive():
        calls.append("predictive")

    ctrl._handle_predictive_grid_charging = handle_predictive
    manager = PricingManager(
        SimpleNamespace(
            states=SimpleNamespace(get=lambda _entity_id: state_holder["state"])
        ),
        ctrl,
    )
    manager._maybe_refresh_service_prices = _noop
    manager._check_dp_pre_slot_reevaluation = _noop
    manager._is_evening_reevaluation_time = lambda: False
    manager._is_dp_soc_drop_reeval = lambda: False

    # PricingManager no longer makes a handoff decision from one meter value.
    # It delegates the settling/hysteresis state to the controller and preserves
    # the physical price slot.
    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())
    assert ctrl._predictive_charge_suspended_for_demand is True
    assert ctrl._current_price_slot_active is True
    assert ctrl.grid_charging_active is True
    assert calls == ["predictive"]

    # Further samples stay in the same slot; the actual controller owns the
    # two-sample recovery check, not the pricing schedule.
    state_holder["state"] = SimpleNamespace(state="1700")
    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())
    assert ctrl._predictive_charge_suspended_for_demand is True
    assert ctrl._current_price_slot_active is True
    assert ctrl.grid_charging_active is True
    assert calls == ["predictive", "predictive"]


def test_predictive_shortfall_uses_live_soc_and_preserves_diagnostic():
    battery = _Battery("b1", 40, 10)
    ctrl = _controller([battery])
    ctrl._predictive_charge_target_soc = {battery: 60.0}
    manager = PricingManager(SimpleNamespace(), ctrl)

    missing = manager._record_predictive_shortfall("realtime_price")

    assert missing == pytest.approx(2.0)
    assert ctrl._predictive_charge_target_soc is None
    assert ctrl._last_decision_data["predictive_shortfall_kwh"] == 2.0
    assert ctrl._last_decision_data["deadline_shortfall_kwh"] == 2.0
    assert ctrl._last_decision_data["shortfall_mode"] == "realtime_price"


def test_safety_discharge_bypasses_only_economic_blockers():
    battery = _Battery("b1", 40, 10)
    ctrl = SimpleNamespace(
        _global_discharge_blockers={"price_discharge": {"reason": "cheap"}},
        _battery_discharge_blockers={battery: {}},
        _capacity_protection_overrides_curtailment=lambda: False,
    )
    ctrl.is_discharge_blocked = ChargeDischargeController.is_discharge_blocked.__get__(
        ctrl, ChargeDischargeController
    )

    assert ctrl.is_discharge_blocked(battery) is True
    assert ctrl.is_discharge_blocked(battery, ignore_economic=True) is False

    ctrl._battery_discharge_blockers[battery]["minimum_soc"] = {
        "reason": "reserve"
    }
    assert ctrl.is_discharge_blocked(battery, ignore_economic=True) is True


def test_reaching_target_stops_inside_slot_prunes_future_and_does_not_resume():
    battery = _Battery("b1", 80, 10, max_soc=80)
    ctrl = _controller([battery])
    now = datetime.now()
    slot = PriceSlot(now - timedelta(minutes=10), now + timedelta(minutes=50), -0.20)
    commands: list = []
    manager = _prepare_runtime_manager(ctrl, slot, commands)

    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())

    assert ctrl.grid_charging_active is False
    assert ctrl._current_price_slot_active is False
    assert ctrl._dynamic_pricing_schedule is None
    assert commands == [("b1", 0, 0)]

    # A later SOC dip in the same physical interval cannot resurrect the removed
    # opportunity and charge on toward max_soc.
    battery.data["battery_soc"] = 79
    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())
    assert ctrl.grid_charging_active is False
    assert commands == [("b1", 0, 0)]


def test_live_nonnegative_price_stops_active_pure_opportunity_safely():
    battery = _Battery("b1", 30, 10)
    ctrl = _controller([battery])
    now = datetime.now()
    slot = PriceSlot(now - timedelta(minutes=10), now + timedelta(minutes=50), -0.20)
    commands: list = []
    manager = _prepare_runtime_manager(ctrl, slot, commands)
    manager._get_current_price = lambda: 0.01

    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())

    assert ctrl.grid_charging_active is False
    assert ctrl._current_price_slot_active is False
    assert commands == [("b1", 0, 0)]


def test_charge_blocker_prevents_entering_an_opportunity_slot():
    battery = _Battery("b1", 30, 10)
    ctrl = _controller([battery])
    now = datetime.now()
    slot = PriceSlot(now - timedelta(minutes=10), now + timedelta(minutes=50), -0.20)
    ctrl._dynamic_pricing_schedule = _schedule(
        [slot], {slot: SLOT_PURPOSE_NEGATIVE_PRICE}
    )
    ctrl._dynamic_pricing_evaluated_date = now.date()
    ctrl._dp_evening_reevaluated_date = now.date()
    ctrl.is_charge_blocked = lambda: True
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._maybe_refresh_service_prices = _noop
    manager._check_dp_pre_slot_reevaluation = _noop
    manager._is_evening_reevaluation_time = lambda: False
    manager._is_dp_soc_drop_reeval = lambda: False
    manager._get_current_price = lambda: -0.20

    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())

    assert ctrl.grid_charging_active is False
    assert ctrl._current_price_slot_active is False


def test_cleanup_removes_only_opportunity_purpose_from_combined_calendar():
    battery = _Battery("b1", 30, 10)
    ctrl = _controller([battery])
    slot = _future_slots([-0.20])[0]
    ctrl._dynamic_pricing_schedule = _schedule(
        [slot], {slot: SLOT_PURPOSE_COMBINED}, deficit_needed=True
    )
    ctrl._active_dynamic_slot_purpose = SLOT_PURPOSE_COMBINED
    ctrl._current_price_slot_active = True
    ctrl.grid_charging_active = True
    manager = PricingManager(SimpleNamespace(), ctrl)

    manager.clear_negative_price_runtime("mode_changed")

    schedule = ctrl._dynamic_pricing_schedule
    assert schedule is not None
    assert schedule.schedule_type == SLOT_PURPOSE_DEFICIT
    assert schedule.purpose_for(slot) == SLOT_PURPOSE_DEFICIT
    assert schedule.negative_price_charging_needed is False
    assert ctrl._active_dynamic_slot_purpose == SLOT_PURPOSE_DEFICIT


def test_cleanup_clears_all_pure_opportunity_runtime_state():
    battery = _Battery("b1", 30, 10)
    ctrl = _controller([battery])
    slot = _future_slots([-0.20])[0]
    ctrl._dynamic_pricing_schedule = _schedule(
        [slot], {slot: SLOT_PURPOSE_NEGATIVE_PRICE}
    )
    ctrl._active_dynamic_slot_purpose = SLOT_PURPOSE_NEGATIVE_PRICE
    ctrl._current_price_slot_active = True
    ctrl.grid_charging_active = True

    PricingManager(SimpleNamespace(), ctrl).clear_negative_price_runtime("unload")

    assert ctrl._dynamic_pricing_schedule is None
    assert ctrl._active_dynamic_slot_purpose is None
    assert ctrl._current_price_slot_active is False
    assert ctrl.grid_charging_active is False


def test_cleanup_does_not_disturb_active_deficit_target():
    battery = _Battery("b1", 30, 10)
    ctrl = _controller([battery], enabled=False)
    slot = _future_slots([0.10])[0]
    ctrl._dynamic_pricing_schedule = _schedule(
        [slot], {slot: SLOT_PURPOSE_DEFICIT}, deficit_needed=True
    )
    target = {battery: 55.0}
    ctrl._active_dynamic_slot_purpose = SLOT_PURPOSE_DEFICIT
    ctrl._current_price_slot_active = True
    ctrl.grid_charging_active = True
    ctrl._predictive_charge_target_soc = target

    PricingManager(SimpleNamespace(), ctrl).clear_negative_price_runtime(
        "unrelated_config_update"
    )

    assert ctrl._active_dynamic_slot_purpose == SLOT_PURPOSE_DEFICIT
    assert ctrl._predictive_charge_target_soc is target
    assert ctrl._current_price_slot_active is True
    assert ctrl.grid_charging_active is True


def test_combined_downgrade_preserves_original_deficit_target_snapshot():
    battery = _Battery("b1", 80, 10, max_soc=80)
    ctrl = _controller(
        [battery],
        decision=_decision(should_charge=True, deficit=2.0),
    )
    slot = _future_slots([-0.20])[0]
    ctrl._dynamic_pricing_schedule = _schedule(
        [slot], {slot: SLOT_PURPOSE_COMBINED}, deficit_needed=True
    )
    ctrl._active_dynamic_slot_purpose = SLOT_PURPOSE_COMBINED
    ctrl._current_price_slot_active = True
    ctrl.grid_charging_active = True
    ctrl._grid_charging_initialized = True
    ctrl._predictive_charge_target_soc = {battery: 80.0}
    ctrl._predictive_deficit_target_soc = {battery: 40.0}

    manager = PricingManager(SimpleNamespace(), ctrl)
    manager.clear_negative_price_runtime("opportunity_complete")

    assert ctrl._active_dynamic_slot_purpose == SLOT_PURPOSE_DEFICIT
    assert ctrl._predictive_charge_target_soc == {battery: 40.0}
    assert ctrl._grid_charging_initialized is True


def test_combined_runtime_stops_when_opportunity_already_covers_deficit():
    battery = _Battery("b1", 80, 10, max_soc=80)
    ctrl = _controller(
        [battery],
        decision=_decision(should_charge=True, deficit=2.0),
    )
    now = datetime.now()
    slot = PriceSlot(
        now - timedelta(minutes=10),
        now + timedelta(minutes=50),
        -0.20,
    )
    commands: list = []
    manager = _prepare_runtime_manager(ctrl, slot, commands)
    ctrl._dynamic_pricing_schedule = _schedule(
        [slot], {slot: SLOT_PURPOSE_COMBINED}, deficit_needed=True
    )
    ctrl._active_dynamic_slot_purpose = SLOT_PURPOSE_COMBINED
    ctrl._predictive_charge_target_soc = {battery: 80.0}
    ctrl._predictive_deficit_target_soc = {battery: 40.0}

    async def handle_charge():
        # The already-stored opportunity energy covered the smaller deficit;
        # the stale 2 kWh plan must not be rebased from the new 80% SOC.
        assert ctrl._predictive_charge_target_soc == {battery: 40.0}
        ctrl.grid_charging_active = False

    ctrl._handle_predictive_grid_charging = handle_charge

    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())

    assert ctrl.grid_charging_active is False
    assert ctrl._current_price_slot_active is False
    assert commands == [("b1", 0, 0)]


def test_runtime_switch_persists_enable_and_disable_with_safe_cleanup():
    calls: list = []

    class _Pricing:
        async def _evaluate_dynamic_pricing(self, *, horizon, extended_horizon=False):
            calls.append(("evaluate", horizon, extended_horizon))

        async def _stop_dynamic_price_slot(self, reason):
            calls.append(("stop", reason))

        def clear_negative_price_runtime(self, reason):
            calls.append(("clear", reason))

    controller = SimpleNamespace(
        negative_price_charging_enabled=False,
        _active_dynamic_slot_purpose=None,
        _pricing_mgr=_Pricing(),
    )
    entry = SimpleNamespace(entry_id="entry", data={})

    def update_entry(target, *, data):
        target.data = data

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=update_entry)
    )
    entity = NegativePriceChargingSwitch(hass, entry, controller)
    entity.async_write_ha_state = lambda: None

    asyncio.run(entity.async_turn_on())
    assert entry.data[CONF_NEGATIVE_PRICE_CHARGING_ENABLED] is True
    assert controller.negative_price_charging_enabled is True
    assert (
        "evaluate",
        DynamicPricingEvaluationHorizon.REMAINING,
        True,
    ) in calls

    controller._active_dynamic_slot_purpose = SLOT_PURPOSE_NEGATIVE_PRICE
    asyncio.run(entity.async_turn_off())
    assert entry.data[CONF_NEGATIVE_PRICE_CHARGING_ENABLED] is False
    assert controller.negative_price_charging_enabled is False
    assert ("stop", "negative_price_feature_disabled") in calls
    assert ("clear", "disabled") in calls


def test_smart_predischarge_switch_rebuilds_remaining_horizon():
    calls: list = []

    class PricingStub:
        async def _evaluate_dynamic_pricing(self, *, horizon, extended_horizon=False):
            calls.append(("evaluate", horizon, extended_horizon))

        def clear_curtailment_runtime(self, reason):
            calls.append(("clear", reason))

    controller = SimpleNamespace(
        smart_predischarge_enabled=False,
        _pricing_mgr=PricingStub(),
    )
    entry = SimpleNamespace(entry_id="entry", data={})

    def update_entry(target, *, data):
        target.data = data

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=update_entry)
    )
    entity = SmartPredischargeSwitch(hass, entry, controller)
    entity.async_write_ha_state = lambda: None

    asyncio.run(entity.async_turn_on())

    assert entry.data[CONF_SMART_PREDISCHARGE_ENABLED] is True
    assert controller.smart_predischarge_enabled is True
    assert calls == [
        (
            "evaluate",
            DynamicPricingEvaluationHorizon.REMAINING,
            True,
        )
    ]


def test_download_diagnostics_exposes_typed_calendar():
    slot = _future_slots([-0.20])[0]
    schedule = _schedule([slot], {slot: SLOT_PURPOSE_NEGATIVE_PRICE})
    controller = SimpleNamespace(
        negative_price_charging_enabled=True,
        _active_dynamic_slot_purpose=SLOT_PURPOSE_NEGATIVE_PRICE,
        _dynamic_pricing_schedule=schedule,
    )

    info = diagnostics._dynamic_pricing_info(controller)

    assert info["schedule_type"] == SLOT_PURPOSE_NEGATIVE_PRICE
    assert info["selected_slots"][0]["purpose"] == SLOT_PURPOSE_NEGATIVE_PRICE
    assert info["active_slot_purpose"] == SLOT_PURPOSE_NEGATIVE_PRICE
    assert "negative_price_charging_threshold" not in info
    assert "negative_price_charging_target_soc" not in info
