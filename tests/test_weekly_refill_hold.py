"""The weekly full charge holds the batteries between its charge blocks.

Energy discharged between two weekly blocks has to be charged again in the
later block. On 7 Nov the cheapest slots split into 16:30 to 17:15 and 19:15
to 24:00, and the batteries covered the house at about 0.587 in between with
energy bought at 0.584, then bought it back at about 0.53: a loss after the
round trip. Discharging in the gap pays only when the price then beats the
refill price after the round trip plus the arbitrage margin.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import PREDICTIVE_MODE_DYNAMIC_PRICING
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing import engine as pricing_engine
from custom_components.omnibattery.pricing.engine import (
    SLOT_PURPOSE_DEFICIT,
    PricingManager,
)

NOW = datetime(2026, 11, 7, 17, 40)


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(pricing_engine, "datetime", FixedDateTime)


def _slot(hour, minute, price):
    start = NOW.replace(hour=hour, minute=minute)
    return PriceSlot(start, start + timedelta(minutes=15), price)


REFILL = [_slot(20, 0, 0.53), _slot(20, 15, 0.52), _slot(20, 30, 0.53)]


def _manager(*, pending=True, slots=REFILL, current=0.587, in_slot=False, decided=None):
    blocks = {}
    ctrl = SimpleNamespace(
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        _weekly_full_charge_pending=lambda: pending,
        _dynamic_pricing_schedule=SimpleNamespace(
            selected_slots=list(slots),
            charging_needed=True,
            purpose_for=lambda _slot: SLOT_PURPOSE_DEFICIT,
        ),
        _dp_pre_evaluated_slots=dict(decided or {}),
        _current_price_slot_active=in_slot,
        min_arbitrage_margin=0.05,
        round_trip_efficiency=0.8156,
        coordinators=[],
        set_discharge_block=lambda source, reason, details=None: blocks.__setitem__(
            source, details
        ),
        remove_discharge_block=lambda source: blocks.pop(source, None),
    )
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._get_current_price = lambda: current
    manager.price_slots_between = lambda _start, _end: [
        _slot(17, 30, 0.588),
        _slot(17, 45, 0.587),
        _slot(18, 0, 0.75),
        *slots,
    ]
    return manager, blocks


def test_the_gap_runs_on_the_grid_when_discharging_would_not_pay():
    # Refill at up to 0.53: 0.53 / 0.8156 + 0.05 = 0.70 > 0.587.
    manager, blocks = _manager()

    manager.apply_weekly_refill_hold()

    assert blocks["weekly_refill_hold"]["threshold"] == pytest.approx(0.7, abs=0.002)


def test_a_dear_gap_may_discharge():
    manager, blocks = _manager(current=0.75)

    manager.apply_weekly_refill_hold()

    assert "weekly_refill_hold" not in blocks


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pending": False},  # run complete, or not the weekly day
        {"slots": []},  # no weekly block ahead
        {"in_slot": True},  # a charge slot owns the batteries
        {"decided": {slot.start: False for slot in REFILL}},  # all dropped
    ],
)
def test_no_hold_without_a_refill_ahead(kwargs):
    manager, blocks = _manager(**kwargs)
    blocks["weekly_refill_hold"] = {}

    manager.apply_weekly_refill_hold()

    assert "weekly_refill_hold" not in blocks


def test_the_projection_holds_the_same_gap():
    manager, _blocks = _manager()
    hold = manager.weekly_refill_hold_fn()

    assert hold(NOW.replace(minute=45), 3.0, 5.0) == 3.0  # 0.587: held
    assert hold(NOW.replace(hour=18, minute=0), 3.0, 5.0) is None  # 0.75: pays
    assert hold(NOW.replace(hour=20, minute=0), 3.0, 5.0) is None  # charging
    assert hold(NOW.replace(hour=21, minute=0), 3.0, 5.0) is None  # after the run


def test_the_timeline_floor_prefers_the_weekly_hold_over_the_reserve():
    manager, _blocks = _manager()
    controller = SimpleNamespace(
        _pricing_mgr=manager,
        _discharge_reserve_mgr=SimpleNamespace(
            projected_reserve_fn=lambda: (lambda _start, _usable, _free: 0.5)
        ),
    )
    floor = ChargeDischargeController._daily_operation_reserve_floor(controller)

    assert floor(NOW.replace(minute=45), 3.0, 5.0) == 3.0
    assert floor(NOW.replace(hour=18, minute=0), 3.0, 5.0) == 0.5


def test_safety_paths_may_still_discharge_through_the_hold():
    controller = SimpleNamespace(
        _global_discharge_blockers={"weekly_refill_hold": object()},
        _battery_discharge_blockers={},
    )

    assert ChargeDischargeController.is_discharge_blocked(controller) is True
    assert (
        ChargeDischargeController.is_discharge_blocked(controller, ignore_economic=True)
        is False
    )
