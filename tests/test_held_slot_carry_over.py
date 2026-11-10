"""A booked slot held by a charge block passes its energy on (P13).

Dynamic Pricing holds a booked slot while a charge blocker (Solar Charge
Delay, a user no-charge window) is active. A slot held for its whole length
was never entered, so the end-of-slot carry-over never ran and its energy was
silently lost, even when a later slot before the deadline could deliver it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import CHARGE_EFFICIENCY
from custom_components.omnibattery.pricing import (
    SLOT_PURPOSE_DEFICIT,
    DynamicPricingSchedule,
    PriceSlot,
)
from custom_components.omnibattery.pricing.engine import PricingManager

DAY = datetime(2026, 11, 10)


def _slot(hour, price=0.2):
    return PriceSlot(DAY + timedelta(hours=hour), DAY + timedelta(hours=hour + 1), price)


def _battery(soc=50.0, capacity_kwh=10.0, max_soc=100.0):
    return SimpleNamespace(
        data={"battery_soc": soc, "battery_total_energy": capacity_kwh},
        max_soc=max_soc,
    )


def _setup(held, later, *, deadline_hours=20, power_w=3000, battery=None):
    schedule = DynamicPricingSchedule(
        hours_needed=2.0,
        selected_slots=[held, later],
        average_price=0.2,
        estimated_cost=0.0,
        total_available_slots=24,
        evaluation_time=DAY,
        energy_deficit_kwh=3.0,
        slot_purposes={held: SLOT_PURPOSE_DEFICIT, later: SLOT_PURPOSE_DEFICIT},
        slot_energy_targets_kwh={held: 2.0, later: 0.5},
        slot_deadlines={
            held: DAY + timedelta(hours=deadline_hours),
            later: DAY + timedelta(hours=deadline_hours),
        },
        chronological_planning_active=True,
    )
    controller = SimpleNamespace(
        _dynamic_pricing_schedule=schedule,
        _dp_held_slots={held.start},
        _dp_completed_slots=set(),
        _predictive_deficit_target_soc=None,
        _last_decision_data={},
        max_contracted_power=power_w,
        max_charge_capacity=power_w,
        capacity_protection_enabled=False,
        coordinators=[battery if battery is not None else _battery()],
    )
    manager = PricingManager(SimpleNamespace(), controller)
    manager._contracted_charge_ceiling = lambda: float(power_w)
    return manager, controller, schedule


def test_a_slot_held_for_its_whole_length_carries_its_quota():
    held, later = _slot(9), _slot(14)
    manager, controller, schedule = _setup(held, later)
    manager._carry_over_held_slots(DAY + timedelta(hours=10, minutes=1))

    assert schedule.slot_energy_targets_kwh[later] == pytest.approx(2.5)
    assert controller._dp_held_slots == set()
    assert held.start in controller._dp_completed_slots
    assert schedule.deadline_shortfall_kwh == 0.0


def test_a_held_slot_still_running_keeps_waiting():
    held, later = _slot(9), _slot(14)
    manager, controller, schedule = _setup(held, later)

    manager._carry_over_held_slots(DAY + timedelta(hours=9, minutes=30))

    assert schedule.slot_energy_targets_kwh[later] == 0.5
    assert controller._dp_held_slots == {held.start}


def test_energy_no_later_slot_can_take_is_reported_as_shortfall():
    held, later = _slot(9), _slot(14)
    # 1 kW: the later slot takes at most 0.85 kWh, 0.5 of it already planned.
    manager, controller, schedule = _setup(held, later, power_w=1000)
    manager._carry_over_held_slots(DAY + timedelta(hours=10, minutes=1))

    room = 1.0 * CHARGE_EFFICIENCY - 0.5
    assert schedule.slot_energy_targets_kwh[later] == pytest.approx(0.5 + room)
    assert schedule.deadline_shortfall_kwh == pytest.approx(2.0 - room)


def test_a_slot_that_was_entered_later_is_left_to_the_normal_path():
    held, later = _slot(9), _slot(14)
    manager, controller, schedule = _setup(held, later)
    controller._dp_completed_slots = {held.start}

    manager._carry_over_held_slots(DAY + timedelta(hours=10, minutes=1))

    assert schedule.slot_energy_targets_kwh[later] == 0.5
    assert controller._dp_held_slots == set()


def test_a_contiguous_next_slot_takes_the_quota_once_it_has_started():
    # The first cycle past 10:00 already falls inside 10-11.
    held, nxt = _slot(9), _slot(10)
    manager, controller, schedule = _setup(held, nxt)

    manager._carry_over_held_slots(DAY + timedelta(hours=10, seconds=3))

    assert schedule.slot_energy_targets_kwh[nxt] == pytest.approx(2.5)
    assert schedule.deadline_shortfall_kwh == 0.0


def test_a_started_slot_only_offers_the_time_it_has_left():
    held, nxt = _slot(9), _slot(10)
    # 3 kW, 15 minutes left in 10-11: room for 0.75 kWh * efficiency.
    manager, controller, schedule = _setup(held, nxt)

    manager._carry_over_held_slots(DAY + timedelta(hours=10, minutes=45))

    room = 3.0 * 0.25 * CHARGE_EFFICIENCY - 0.5
    assert schedule.slot_energy_targets_kwh[nxt] == pytest.approx(0.5 + room)
    assert schedule.deadline_shortfall_kwh == pytest.approx(2.0 - room)


def test_nothing_is_carried_for_a_battery_already_at_its_ceiling():
    held, later = _slot(9), _slot(14)
    manager, controller, schedule = _setup(held, later, battery=_battery(soc=100.0))

    manager._carry_over_held_slots(DAY + timedelta(hours=10, minutes=1))

    assert schedule.slot_energy_targets_kwh[later] == 0.5
    assert schedule.deadline_shortfall_kwh == 0.0
    assert held.start in controller._dp_completed_slots


def test_the_carry_is_capped_at_the_room_left_after_later_plans():
    held, later = _slot(9), _slot(14)
    # 10 kWh at 90%: 1.0 kWh of room, 0.5 of it already planned for 14-15.
    manager, controller, schedule = _setup(held, later, battery=_battery(soc=90.0))

    manager._carry_over_held_slots(DAY + timedelta(hours=10, minutes=1))

    assert schedule.slot_energy_targets_kwh[later] == pytest.approx(1.0)
    assert schedule.deadline_shortfall_kwh == 0.0
