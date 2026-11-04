"""Estimated cost / average price follow planned slot energy (#715)."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import CHARGE_EFFICIENCY
from custom_components.omnibattery.pricing import (
    SLOT_PURPOSE_DEFICIT,
    SLOT_PURPOSE_NEGATIVE_PRICE,
    DynamicPricingSchedule,
    PriceSlot,
)
from custom_components.omnibattery.pricing.engine import PricingManager

_START = datetime(2026, 11, 3, 13, 0)


def _slot(index: int, price: float) -> PriceSlot:
    start = _START + timedelta(minutes=15 * index)
    return PriceSlot(start, start + timedelta(minutes=15), price)


def _issue_plan():
    """Seven 1.59 kWh slots, nine unallocated slots and one 0.60 kWh slot."""
    flexible = [_slot(i, p) for i, p in enumerate(
        [0.2255, 0.2118, 0.2258, 0.2218, 0.2194, 0.2205, 0.2262]
    )]
    unallocated = [_slot(30 + i, 0.25) for i in range(9)]
    tail = _slot(43, 0.2264)
    targets = {slot: 1.59 for slot in flexible}
    targets[tail] = 0.60
    return flexible + unallocated + [tail], targets


def _expected(targets):
    battery_cost = sum(slot.price * kwh for slot, kwh in targets.items())
    return battery_cost / sum(targets.values()), battery_cost / CHARGE_EFFICIENCY


def test_prune_uses_planned_slot_energy_for_cost_and_average():
    slots, targets = _issue_plan()
    opportunity = _slot(60, -0.05)
    purposes = {slot: SLOT_PURPOSE_DEFICIT for slot in slots}
    purposes[opportunity] = SLOT_PURPOSE_NEGATIVE_PRICE
    schedule = DynamicPricingSchedule(
        hours_needed=4.5,
        selected_slots=slots + [opportunity],
        average_price=0.0,
        estimated_cost=0.0,
        total_available_slots=96,
        evaluation_time=_START,
        energy_deficit_kwh=11.73,
        slot_purposes=purposes,
        deficit_charging_needed=True,
        slot_energy_targets_kwh=dict(targets),
        slot_cost_energy_kwh=dict(targets),
        chronological_planning_active=True,
    )
    ctrl = SimpleNamespace(
        _dynamic_pricing_schedule=schedule,
        max_contracted_power=7500,
        max_charge_capacity=7500,
    )

    PricingManager(SimpleNamespace(), ctrl)._prune_completed_opportunities()

    avg, cost = _expected(targets)
    assert schedule.selected_slots == slots
    assert schedule.estimated_cost == pytest.approx(cost)
    assert schedule.estimated_cost == pytest.approx(3.07, abs=0.01)
    assert schedule.average_price == pytest.approx(avg)


def test_without_plan_falls_back_to_full_power_estimate():
    from custom_components.omnibattery.pricing import calculations

    slots = [_slot(0, 0.20), _slot(1, 0.30)]
    avg, cost = calculations.schedule_price_summary(slots, 4.0)
    assert avg == pytest.approx(0.25)
    assert cost == pytest.approx((0.20 + 0.30) * 4.0 * 0.25)
