"""Slots are sized with the power the fleet averages over the charge.

A battery that is full stops charging. Two batteries of 2.4 kW with 4.25 and
7.18 kWh of room (11 Nov 2026: a 5.12 kWh and an 8.16 kWh battery, both to
95 %) charge at 4.8 kW only until the smaller one is full. The planner sized
every slot at 4.8 kW: 12 slots for 11.43 kWh, of which the larger battery,
needing 14, can store all but about 1 kWh.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import CHARGE_EFFICIENCY
from custom_components.omnibattery.pricing import PriceSlot, calculations
from custom_components.omnibattery.pricing.chronological import (
    ChronologicalEvaluationRequest,
    EnergyInterval,
    build_energy_deadlines,
    evaluate_chronological_request,
)
from custom_components.omnibattery.pricing.engine import PricingManager

SMALL = (4.25, 2.4)
LARGE = (7.18, 2.4)
BASE = datetime(2026, 11, 11)
QUARTER = timedelta(minutes=15)


def test_filling_unequal_batteries_takes_as_long_as_the_slower_one():
    power = calculations.fleet_charge_power_kw([SMALL, LARGE], 11.43)
    assert power == pytest.approx(11.43 / (7.18 / 2.4))
    assert power == pytest.approx(3.82, abs=0.01)


def test_a_charge_that_ends_before_a_battery_is_full_keeps_the_summed_power():
    # 4.25 kWh of room at 2.4 kW is full after 1.77 h; 4.8 kW x 1.77 h = 8.5 kWh.
    assert calculations.fleet_charge_power_kw([SMALL, LARGE], 5.0) == pytest.approx(4.8)
    assert calculations.fleet_charge_power_kw([SMALL, LARGE], 8.5) == pytest.approx(4.8)


def test_a_charge_past_the_first_full_battery_averages_both_rates():
    # 8.5 kWh at 4.8 kW, then 1.5 kWh at 2.4 kW.
    hours = 8.5 / 4.8 + 1.5 / 2.4
    assert calculations.fleet_charge_power_kw([SMALL, LARGE], 10.0) == pytest.approx(
        10.0 / hours
    )


def test_equal_batteries_keep_the_summed_power():
    assert calculations.fleet_charge_power_kw([(5.0, 2.4), (5.0, 2.4)], 10.0) == pytest.approx(4.8)


def test_a_full_battery_adds_no_power():
    assert calculations.fleet_charge_power_kw([(0.0, 2.4), LARGE], 5.0) == pytest.approx(2.4)


def test_more_than_the_room_is_sized_as_the_room():
    assert calculations.fleet_charge_power_kw([SMALL, LARGE], 50.0) == pytest.approx(
        calculations.fleet_charge_power_kw([SMALL, LARGE], 11.43)
    )


def test_no_energy_or_no_room_has_no_average():
    assert calculations.fleet_charge_power_kw([SMALL, LARGE], 0.0) == pytest.approx(4.8)
    assert calculations.fleet_charge_power_kw([(0.0, 2.4)], 5.0) is None
    assert calculations.fleet_charge_power_kw([(5.0, 0.0)], 5.0) is None
    assert calculations.fleet_charge_power_kw([], 5.0) is None


# ----------------------------------------------------------------------
# The plan
# ----------------------------------------------------------------------


def _night_plan(power_kw: float):
    """A night of 0.44 slots before a day at 0.68; the day needs the whole room."""
    intervals = [
        EnergyInterval(BASE + i * QUARTER, BASE + (i + 1) * QUARTER, 0.25, 0.0)
        for i in range(96)
    ]
    slots = tuple(
        PriceSlot(BASE + i * QUARTER, BASE + (i + 1) * QUARTER, 0.44 if i < 24 else 0.68)
        for i in range(96)
    )
    room = SMALL[0] + LARGE[0]
    return evaluate_chronological_request(
        ChronologicalEvaluationRequest(
            now=BASE,
            horizon_end=BASE + timedelta(days=1),
            intervals=tuple(intervals),
            price_slots=slots,
            total_required_kwh=room,
            effective_power_kw=power_kw,
            headroom_kwh=room,
            usable_initial_kwh=0.0,
            deadlines=tuple(build_energy_deadlines(intervals, 0.0)),
            import_value_efficiency=0.8146,
            import_value_margin=0.05,
        )
    ).plan


def _stored(plan) -> float:
    """What the two batteries store in the plan's slots, each at its own power.

    As the runtime does: a slot's energy goes to the batteries with room, each
    up to what it charges in the slot, and what neither can take is lost.
    """
    room = [SMALL[0], LARGE[0]]
    per_slot = [power * 0.25 * CHARGE_EFFICIENCY for _room, power in (SMALL, LARGE)]
    stored = 0.0
    for allocation in plan.allocations:
        left = allocation.planned_battery_kwh
        for index in (1, 0):
            take = min(left, room[index], per_slot[index])
            room[index] -= take
            left -= take
            stored += take
    return stored


def test_slots_sized_at_the_summed_power_leave_the_larger_battery_short():
    plan = _night_plan(4.8)
    assert len(plan.allocations) == 12
    assert plan.allocated_kwh == pytest.approx(11.43)
    assert _stored(plan) < 11.43 - 1.0


def test_slots_sized_at_the_fleet_average_fill_both_batteries():
    power = calculations.fleet_charge_power_kw([SMALL, LARGE], 11.43)
    plan = _night_plan(power)
    assert len(plan.allocations) == 15
    assert plan.allocated_kwh == pytest.approx(11.43)
    assert _stored(plan) == pytest.approx(11.43, abs=0.01)


# ----------------------------------------------------------------------
# The engine
# ----------------------------------------------------------------------


def _manager(limits: dict | None):
    controller = SimpleNamespace()
    if limits is not None:
        controller._battery_power_limit = lambda coordinator, charging: limits[coordinator]
    return PricingManager(SimpleNamespace(), controller)


def test_the_engine_reads_each_battery_s_charge_limit():
    """The limit carries the temperature derate and the taper of that moment."""
    manager = _manager({"small": 2400, "large": 1800})
    power = manager._fleet_charge_power_kw(["small", "large"], [4.25, 7.18], 11.43)
    assert power == pytest.approx(11.43 / (7.18 / 1.8))


def test_without_a_per_battery_limit_the_summed_power_stays():
    assert _manager(None)._fleet_charge_power_kw(["small"], [4.25], 4.0) is None
