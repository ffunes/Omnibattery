"""Weekly full charge feeds the predictive energy balance (#404).

The balance answers "will I run out of battery", never "is the battery full",
so a weekly 100% day produced no deficit and nothing charged unless the sun
happened to cover it. The weekly gap now enters the same balance the guaranteed
floor uses, and the charge ceiling rises to 100% for the whole planning chain.

``_should_activate_grid_charging`` touches few attributes, so it is exercised
unbound on a stub controller (no Home Assistant runtime needed).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing import engine as pricing_engine
from custom_components.omnibattery.pricing.engine import (
    DynamicPricingEvaluationHorizon,
    PricingManager,
)


class _Coord:
    def __init__(self, soc, capacity_kwh, min_soc=12, max_soc=90):
        self.name = f"battery_{soc:.0f}"
        self.min_soc = min_soc
        self.max_soc = max_soc
        self.data = {"battery_soc": soc, "battery_total_energy": capacity_kwh}


def _consumption(value):
    async def _f():
        return value
    return _f


def _ctrl(coords, *, weekly, solar="5.0", consumption=2.0):
    return SimpleNamespace(
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        coordinators=list(coords),
        _weekly_charge_mgr=SimpleNamespace(is_active=lambda: weekly),
        _predictive_safety_margin_kwh=0.0,
        _predictive_min_soc_floor=0.0,
        _predictive_min_soc_floor_enabled=False,
        _daily_consumption_history=[],
        solar_forecast_sensor="sensor.solar" if solar is not None else None,
        hass=SimpleNamespace(
            states=SimpleNamespace(
                get=lambda _e: None if solar is None else SimpleNamespace(state=solar)
            )
        ),
        _consumption_tracker=SimpleNamespace(
            get_dynamic_base_consumption=_consumption(consumption)
        ),
    )


def _run(ctrl):
    return asyncio.run(ChargeDischargeController._should_activate_grid_charging(ctrl))


# 10 kWh pack at 50%, min 12%, max 90%: usable 3.8 kWh, gap to 100% is 5.0 kWh.
# Consumption 2.0 kWh, so the day is solar-positive and the plain balance says
# "no charge" in every case below.


def test_weekly_day_charges_the_gap_the_sun_cannot_cover():
    # Solar 5.0 - consumption 2.0 = 3.0 kWh surplus into the pack.
    # Weekly needs 5.0 kWh → grid buys the remaining 2.0 kWh.
    result = _run(_ctrl([_Coord(50.0, 10.0)], weekly=True, solar="5.0"))

    assert result["should_charge"] is True
    assert abs(result["energy_deficit_kwh"] - 2.0) < 0.05
    assert result["weekly_full_charge_active"] is True
    assert "Weekly full charge" in result["reason"]


def test_weekly_day_buys_nothing_when_solar_covers_the_gap():
    # Solar 8.0 - consumption 2.0 = 6.0 kWh surplus ≥ the 5.0 kWh gap.
    result = _run(_ctrl([_Coord(50.0, 10.0)], weekly=True, solar="8.0"))

    assert result["should_charge"] is False


def test_no_weekly_charge_on_an_ordinary_day():
    # Same numbers, weekly inactive → the ceiling stays at max_soc, no deficit.
    result = _run(_ctrl([_Coord(50.0, 10.0)], weekly=False, solar="5.0"))

    assert result["should_charge"] is False
    assert result["weekly_full_charge_active"] is False


def test_weekly_headroom_reaches_100_not_max_soc():
    # planned_grid_charge_kwh is clipped to the headroom. At max_soc=90 the clip
    # would be 4.0 kWh; the weekly ceiling of 100% must not clip the 5.0 kWh gap.
    result = _run(_ctrl([_Coord(50.0, 10.0)], weekly=True, solar=None))

    assert abs(result["energy_deficit_kwh"] - 5.0) < 0.05
    assert abs(result["planned_grid_charge_kwh"] - 5.0) < 0.05


def test_conservative_branch_takes_the_whole_gap():
    # No solar forecast → that branch assumes zero solar, so does the weekly gap.
    result = _run(_ctrl([_Coord(80.0, 10.0)], weekly=True, solar=None))

    assert result["should_charge"] is True
    assert abs(result["energy_deficit_kwh"] - 2.0) < 0.05


def _target_ctrl(coords, *, weekly, deficit_kwh):
    return SimpleNamespace(
        coordinators=list(coords),
        _weekly_charge_mgr=SimpleNamespace(is_active=lambda: weekly),
        _last_decision_data={"energy_deficit_kwh": deficit_kwh},
    )


def test_deficit_targets_may_reach_100_on_the_weekly_day():
    coord = _Coord(50.0, 10.0)
    targets = ChargeDischargeController._compute_deficit_target_soc(
        _target_ctrl([coord], weekly=True, deficit_kwh=5.0)
    )

    assert abs(targets[coord] - 100.0) < 0.5


def test_deficit_targets_still_stop_at_max_soc_otherwise():
    coord = _Coord(50.0, 10.0)
    targets = ChargeDischargeController._compute_deficit_target_soc(
        _target_ctrl([coord], weekly=False, deficit_kwh=5.0)
    )

    assert abs(targets[coord] - 90.0) < 0.5


def test_weekly_reserve_survives_a_fully_covering_solar_forecast():
    """A covering solar forecast must not erase the weekly fallback reserve."""
    now = datetime(2026, 9, 21, 0, 5)
    coord = _Coord(50.0, 10.0)
    ctrl = _ctrl([coord], weekly=True, solar="8.0", consumption=2.0)
    weekly = _run(ctrl)
    forecast = SimpleNamespace(
        energy_kwh=2.0,
        intervals_kwh=[2.0 / 96] * 96,
        intervals_by_date={},
        source="profile",
        coverage_ratio=1.0,
        total_days=7,
    )
    ctrl._consumption_tracker = SimpleNamespace(
        consumption_profile=object(),
        solar_profile=None,
        get_dynamic_base_consumption=_consumption(2.0),
        forecast_consumption_between=lambda *_args, **_kwargs: forecast,
        calculate_sunrise=lambda *_args: 6.0,
        calculate_solar_noon=lambda: 12.0,
    )
    ctrl.solar_profile_mode = "off"
    ctrl._is_battery_manual_owned = lambda _coord: False
    ctrl._charge_ceiling_soc = lambda battery: (
        ChargeDischargeController._charge_ceiling_soc(ctrl, battery)
    )
    ctrl._weekly_full_charge_pending = lambda: (
        ChargeDischargeController._weekly_full_charge_pending(ctrl)
    )
    ctrl._predictive_min_soc_floor_enabled = False
    ctrl.config_entry = SimpleNamespace(data={})
    ctrl.max_contracted_power = 5000
    ctrl.max_charge_capacity = 5000
    ctrl._last_chronological_diagnostics = None
    manager = PricingManager(SimpleNamespace(config=SimpleNamespace(time_zone="UTC")), ctrl)
    manager.energy_horizon_end = lambda _now: now.replace(
        hour=0, minute=0
    ) + timedelta(days=1)

    manager._build_chronological_plan(
        now=now,
        slots=[],
        decision_data=weekly,
        price_ceiling=None,
        diagnostic_only=True,
    )
    ctrl._weekly_charge_mgr = SimpleNamespace(is_active=lambda: False)
    ordinary = _run(_ctrl([coord], weekly=False, solar="8.0", consumption=2.0))
    manager._build_chronological_plan(
        now=now,
        slots=[],
        decision_data=ordinary,
        price_ceiling=None,
        diagnostic_only=True,
    )

    assert "weekly_reserve_kwh" not in ordinary
    assert "weekly_reserve_not_before" not in ordinary
    assert weekly["planned_grid_charge_kwh"] == 0
    assert weekly["weekly_reserve_kwh"] > 0


def test_weekly_schedule_reserves_only_post_solar_slots(monkeypatch):
    """The weekly fallback must not arm cheap pre-dawn informational slots (#489)."""
    now = datetime(2026, 9, 21, 0, 5)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(pricing_engine, "datetime", FixedDateTime)
    monkeypatch.setattr(pricing_engine.calculations, "datetime", FixedDateTime)
    ctrl = _ctrl([_Coord(50.0, 10.0)], weekly=True, solar="8.0")
    decision = _run(ctrl)

    async def should_activate():
        return dict(decision)

    async def no_op(*_args, **_kwargs):
        return None

    ctrl._should_activate_grid_charging = should_activate
    ctrl._dynamic_pricing_evaluated_date = None
    ctrl._dp_eval_retry_count = 0
    ctrl._dp_price_publication_reeval_date = None
    ctrl._dp_arbitrage_ceiling = None
    ctrl._dp_daily_avg_price = None
    ctrl._last_chronological_diagnostics = None
    ctrl._dynamic_pricing_schedule = None
    ctrl.max_contracted_power = 5000
    ctrl.max_charge_capacity = 5000
    ctrl.max_price_threshold = 1.0
    ctrl.min_arbitrage_margin = 0.0
    ctrl.round_trip_efficiency = 1.0
    ctrl.discharge_price_threshold = 1.0
    ctrl._predictive_min_soc_floor_enabled = False
    pre_dawn = PriceSlot(
        now.replace(hour=1), now.replace(hour=2), 0.01
    )
    post_solar = PriceSlot(
        now.replace(hour=19), now.replace(hour=20), 0.02
    )
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._mark_surplus_hold_stale = lambda _reason: None
    manager._mark_discharge_reserve_stale = lambda _reason: None
    manager._prices_reach_beyond_today = lambda _now: False
    manager._smart_predischarge_enabled = lambda: False
    manager._maybe_refresh_service_prices = no_op
    manager._refresh_excluded_demand_reference = lambda: None
    manager._refresh_solar_forecast_reference = lambda _now: None
    manager._parse_price_data = lambda **_kwargs: [pre_dawn, post_solar]
    manager._negative_price_feature_enabled = lambda: False
    manager._build_curtailment_plan = lambda *_args, **_kwargs: SimpleNamespace(
        risk_slots=[]
    )
    manager._send_dynamic_pricing_notification = no_op
    manager._get_price_unit = lambda: "EUR/kWh"

    def publish_weekly_reserve(**kwargs):
        kwargs["decision_data"].update(
            weekly_reserve_kwh=5.0,
            weekly_reserve_not_before=now.replace(hour=18).isoformat(),
        )
        return None

    manager._build_chronological_plan = publish_weekly_reserve

    asyncio.run(
        manager._evaluate_dynamic_pricing(
            horizon=DynamicPricingEvaluationHorizon.DAILY
        )
    )

    schedule = ctrl._dynamic_pricing_schedule
    reserve_start = datetime.fromisoformat(
        ctrl._last_decision_data["weekly_reserve_not_before"]
    )
    assert any(slot.start >= reserve_start for slot in schedule.selected_slots)
    assert all(slot.start >= reserve_start for slot in schedule.selected_slots)


def test_weekly_reserved_slot_is_cancelled_only_after_reaching_full_soc(monkeypatch):
    """Pre-slot evaluation must cancel a solar-filled reserve but keep a 62% gap."""
    now = datetime(2026, 9, 21, 18, 0)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(pricing_engine, "datetime", FixedDateTime)
    slot = PriceSlot(now + timedelta(hours=1), now + timedelta(hours=2), 0.10)

    for soc, expected in ((100.0, False), (62.0, True)):
        ctrl = _ctrl([_Coord(soc, 10.0)], weekly=True, solar=None)
        ctrl._dynamic_pricing_schedule = SimpleNamespace(
            selected_slots=[slot],
            charging_needed=True,
            deficit_charging_needed=True,
            chronological_planning_active=False,
        )
        ctrl._dp_pre_evaluated_slots = {}
        ctrl._dp_pre_evaluated_purposes = {}
        ctrl._current_price_slot_active = False
        ctrl._curtailment_plan = None

        async def remaining_decision(*, now=None, controller=ctrl):
            return await ChargeDischargeController._should_activate_grid_charging(
                controller
            )

        async def no_op(*_args, **_kwargs):
            return None

        manager = PricingManager(SimpleNamespace(), ctrl)
        manager._evaluate_remaining_grid_charging = remaining_decision
        manager._refresh_excluded_demand_reference = lambda: None
        manager._refresh_solar_forecast_reference = lambda _now: None
        manager._send_dp_pre_slot_reevaluation_notification = no_op

        asyncio.run(manager._check_dp_pre_slot_reevaluation())

        assert ctrl._dp_pre_evaluated_slots[slot.start] is expected


def test_evening_weekly_fallback_sizes_the_deficit_to_full_soc(monkeypatch):
    """Evening fallback must target 100%, rather than stopping at max_soc."""
    import datetime as datetime_module

    now = datetime(2026, 9, 21, 18, 0)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(datetime_module, "datetime", FixedDateTime)
    monkeypatch.setattr(pricing_engine.calculations, "datetime", FixedDateTime)
    coord = _Coord(62.0, 10.0, max_soc=90)
    ctrl = _ctrl([coord], weekly=True, solar=None)
    ctrl._charge_ceiling_soc = lambda battery: (
        ChargeDischargeController._charge_ceiling_soc(ctrl, battery)
    )
    ctrl._weekly_full_charge_gap_kwh = lambda batteries: (
        ChargeDischargeController._weekly_full_charge_gap_kwh(ctrl, batteries)
    )
    ctrl._dp_last_eval_soc = None
    ctrl._dynamic_pricing_schedule = None
    ctrl._last_decision_data = {}
    ctrl.max_contracted_power = 3000
    ctrl.max_charge_capacity = 3000
    ctrl.max_price_threshold = 1.0
    ctrl._dynamic_pricing_evaluated_date = None
    slots = [
        PriceSlot(now + timedelta(hours=i), now + timedelta(hours=i + 1), 0.10 + i / 100)
        for i in (1, 2)
    ]

    async def no_op(*_args, **_kwargs):
        return None

    async def remaining_decision(**_kwargs):
        return {
            "remaining_consumption_kwh": 0.0,
            "consumption_rate_kwh_h": 0.0,
            "avg_consumption_kwh": 0.0,
            "solar_forecast_kwh": 0.0,
        }

    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._mark_surplus_hold_stale = lambda _reason: None
    manager._mark_discharge_reserve_stale = lambda _reason: None
    manager._maybe_refresh_service_prices = no_op
    manager._remaining_solar_today_kwh = lambda _now: 0.0
    manager._read_excluded_demand_claim_kwh = lambda: 0.0
    manager._evaluate_remaining_grid_charging = remaining_decision
    manager.energy_horizon_end = lambda _now: now + timedelta(hours=6)
    manager._refresh_excluded_demand_reference = lambda: None
    manager._refresh_solar_forecast_reference = lambda _now: None
    manager._parse_price_data = lambda **_kwargs: slots
    manager._build_chronological_plan = lambda **_kwargs: None
    manager._send_evening_recharge_notification = no_op

    asyncio.run(manager._evaluate_evening_recharge())

    schedule = ctrl._dynamic_pricing_schedule
    assert schedule.energy_deficit_kwh == pytest.approx(3.8)
    assert schedule.energy_deficit_kwh > (coord.max_soc - 62.0) / 100.0 * 10.0
