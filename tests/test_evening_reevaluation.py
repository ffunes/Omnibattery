"""The evening re-evaluation is the single remaining-horizon evaluator.

It used to run a second evaluator of its own (no arbitrage ceiling, no
extended horizon, merging into the old schedule). These tests drive the
evaluator the evening trigger now calls, ``_evaluate_dynamic_pricing`` with
``REMAINING``, and check the outcomes the old path guaranteed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing import engine as pricing_engine
from custom_components.omnibattery.pricing.chronological import (
    ChronologicalPlan,
    SlotAllocation,
)
from custom_components.omnibattery.pricing.engine import (
    DynamicPricingEvaluationHorizon,
    PricingManager,
)
from tests.test_weekly_predictive_charge import _Coord, _ctrl


async def _no_op(*_args, **_kwargs):
    return None


def _evening_evaluation(monkeypatch, now, slots, build_plan, *, claim=None, **ctrl_attrs):
    """Run the remaining-horizon evaluation at ``now`` with a 3 kWh deficit."""

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(pricing_engine, "datetime", FixedDateTime)
    monkeypatch.setattr(pricing_engine.calculations, "datetime", FixedDateTime)
    ctrl = _ctrl([_Coord(20.0, 10.0)], weekly=False, solar="0.0")
    ctrl.__dict__.update(
        _dynamic_pricing_evaluated_date=None,
        _dp_eval_retry_count=0,
        _dp_price_publication_reeval_date=None,
        _dp_arbitrage_ceiling=None,
        _dp_daily_avg_price=None,
        _dp_last_eval_excluded_claim_kwh=None,
        _last_chronological_diagnostics=None,
        _dynamic_pricing_schedule=None,
        _last_decision_data={},
        _external_loads=SimpleNamespace(claimable_solar_demand_kwh=lambda: claim),
        max_contracted_power=1500,
        max_charge_capacity=1500,
        max_price_threshold=None,
        min_arbitrage_margin=0.0,
        round_trip_efficiency=1.0,
        discharge_price_threshold=1.0,
        _predictive_min_soc_floor_enabled=False,
    )
    ctrl.__dict__.update(ctrl_attrs)

    async def remaining(*, now=None, horizon_end=None):
        return {
            "should_charge": True,
            "energy_deficit_kwh": 3.0,
            "avg_soc": 20.0,
            "remaining_consumption_kwh": 4.0,
            "solar_forecast_kwh": 0.0,
        }

    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._mark_surplus_hold_stale = lambda _reason: None
    manager._mark_discharge_reserve_stale = lambda _reason: None
    manager._prices_reach_beyond_today = lambda _now: False
    manager._smart_predischarge_enabled = lambda: False
    manager._maybe_refresh_service_prices = _no_op
    manager._refresh_solar_forecast_reference = lambda _now: None
    manager._evaluate_remaining_grid_charging = remaining
    manager.energy_horizon_end = lambda _now: now.replace(hour=0, minute=0) + timedelta(
        days=1, hours=7
    )
    manager._parse_price_data = lambda **_kwargs: list(slots)
    manager._negative_price_feature_enabled = lambda: False
    manager._build_curtailment_plan = lambda *_a, **_k: SimpleNamespace(risk_slots=[])
    manager._send_dynamic_pricing_notification = _no_op
    manager._get_price_unit = lambda: "EUR/kWh"
    manager._build_chronological_plan = build_plan

    asyncio.run(
        manager._evaluate_dynamic_pricing(horizon=DynamicPricingEvaluationHorizon.REMAINING)
    )
    return ctrl


def test_evening_overnight_slots_are_booked_when_the_solar_fell_short(monkeypatch):
    now = datetime(2026, 9, 19, 18, 0)
    evening = PriceSlot(now + timedelta(hours=2), now + timedelta(hours=3), 0.20)
    one_am = PriceSlot(now + timedelta(hours=7), now + timedelta(hours=8), 0.10)
    four_am = PriceSlot(now + timedelta(hours=10), now + timedelta(hours=11), 0.01)

    def plan(**_kwargs):
        return ChronologicalPlan(
            allocations=[SlotAllocation(one_am, 3.0, one_am.end, "deadline")]
        )

    ctrl = _evening_evaluation(monkeypatch, now, [evening, one_am, four_am], plan)

    schedule = ctrl._dynamic_pricing_schedule
    assert schedule.selected_slots == [one_am]
    assert schedule.slot_energy_targets_kwh == {one_am: 3.0}


def test_evening_evaluation_applies_the_arbitrage_ceiling(monkeypatch):
    # #721: with no static ceiling the peak must not be booked as a charge slot.
    now = datetime(2026, 11, 3, 16, 0)

    def _slot(hour, price):
        start = now.replace(hour=hour, minute=0)
        return PriceSlot(start, start + timedelta(hours=1), price)

    peak = [_slot(17, 0.50), _slot(18, 0.50), _slot(19, 0.50)]
    night = _slot(22, 0.30)
    morning = [
        PriceSlot(s.start + timedelta(days=1), s.end + timedelta(days=1), 0.50)
        for s in (_slot(6, 0), _slot(7, 0))
    ]
    ceilings = []

    def plan(**kwargs):
        if kwargs.get("slot_ceilings") is not None:
            ceilings.append(kwargs["slot_ceilings"])
        return None

    ctrl = _evening_evaluation(
        monkeypatch,
        now,
        [*peak, night, *morning],
        plan,
        min_arbitrage_margin=0.05,
        round_trip_efficiency=0.85,
    )

    # Expected discharge 0.50 x 0.85 - 0.05 margin = 0.375.
    assert ceilings[0][night.start] == pytest.approx(0.375)
    assert ctrl._dynamic_pricing_schedule.selected_slots == [night]


def test_evening_evaluation_rearms_the_claim_reference(monkeypatch):
    now = datetime(2026, 9, 19, 18, 0)
    slot = PriceSlot(now + timedelta(hours=2), now + timedelta(hours=3), 0.10)

    ctrl = _evening_evaluation(monkeypatch, now, [slot], lambda **_kwargs: None, claim=4.0)

    assert ctrl._dp_last_eval_excluded_claim_kwh == pytest.approx(4.0)
