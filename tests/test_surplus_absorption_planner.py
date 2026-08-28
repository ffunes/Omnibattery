"""Pure tests for price-aware solar surplus absorption planning."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from custom_components.omnibattery.const import CHARGE_EFFICIENCY
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.curtailment import BatterySnapshot
from custom_components.omnibattery.pricing.surplus_absorption import (
    REASON_ABSORPTION_WINDOW,
    REASON_CHEAPER_WINDOW_AHEAD,
    REASON_NO_MATERIAL_SAVING,
    REASON_PAST_DEADLINE,
    REASON_SHORTFALL_RISK,
    REASON_TARGET_MET,
    STATUS_FAIL_SAFE,
    STATUS_INFEASIBLE,
    STATUS_NO_TARGET,
    STATUS_PLANNED,
    calculate_absorption_target_kwh,
    calculate_free_space_kwh,
    hold_decision,
    plan_surplus_absorption,
    select_absorption_slots,
)


DAY = datetime(2026, 8, 2)
NOW = DAY + timedelta(hours=9)
DEADLINE = DAY + timedelta(hours=19)


def _slot(hour: float, price: float, *, minutes: int = 60) -> PriceSlot:
    start = DAY + timedelta(hours=hour)
    return PriceSlot(start, start + timedelta(minutes=minutes), price)


def _battery(
    *,
    name: str = "battery-1",
    soc: float = 40.0,
    capacity: float = 10.0,
    max_soc: float = 100.0,
    floor: float = 10.0,
    power: float = 2500.0,
    eligible: bool = True,
) -> BatterySnapshot:
    return BatterySnapshot(name, soc, capacity, max_soc, floor, power, eligible)


def _plan(
    slots,
    surplus,
    *,
    batteries=None,
    remaining_consumption_kwh=6.0,
    usable_energy_kwh=1.0,
    safety_margin_kwh=0.0,
    max_charge_power_w=2500.0,
    deadline=DEADLINE,
    min_saving=0.0,
    now=NOW,
):
    return plan_surplus_absorption(
        slots,
        surplus,
        batteries if batteries is not None else [_battery()],
        remaining_consumption_kwh=remaining_consumption_kwh,
        usable_energy_kwh=usable_energy_kwh,
        safety_margin_kwh=safety_margin_kwh,
        max_charge_power_w=max_charge_power_w,
        deadline=deadline,
        min_saving=min_saving,
        now=now,
    )


# ---------------------------------------------------------------------------
# Target derivation
# ---------------------------------------------------------------------------


def test_target_is_remaining_load_plus_margin_less_stored_energy():
    assert calculate_absorption_target_kwh(6.0, 1.5, 20.0, 0.5) == pytest.approx(5.0)


def test_target_is_capped_by_the_space_left_in_the_battery():
    assert calculate_absorption_target_kwh(20.0, 0.0, 4.0) == pytest.approx(4.0)


def test_target_is_zero_when_the_battery_already_covers_the_day():
    assert calculate_absorption_target_kwh(3.0, 8.0, 20.0) == 0.0


@pytest.mark.parametrize(
    "remaining, usable, space",
    [
        (None, 1.0, 10.0),
        (6.0, None, 10.0),
        (6.0, 1.0, None),
        (float("nan"), 1.0, 10.0),
        (6.0, 1.0, float("inf")),
    ],
)
def test_target_is_unknown_for_unusable_inputs(remaining, usable, space):
    assert calculate_absorption_target_kwh(remaining, usable, space) is None


def test_free_space_ignores_ineligible_and_invalid_batteries():
    batteries = [
        _battery(name="ok", soc=50.0, capacity=10.0, max_soc=100.0),
        _battery(name="skipped", eligible=False),
        _battery(name="broken", capacity=0.0),
    ]
    assert calculate_free_space_kwh(batteries) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Slot ranking and selection
# ---------------------------------------------------------------------------


def test_selection_takes_the_cheapest_export_hours_first():
    slots = [_slot(10, 0.28), _slot(11, 0.16), _slot(12, 0.21)]
    surplus = {slot: 2.0 for slot in slots}
    plan = _plan(slots, surplus, remaining_consumption_kwh=3.0, usable_energy_kwh=1.0)

    assert plan.status == STATUS_PLANNED
    assert [slot.export_price for slot in plan.selected_slots] == [0.16, 0.21]


def test_selection_stops_once_the_target_is_covered_after_charge_losses():
    slots = [_slot(hour, 0.10 + hour / 100.0) for hour in (10, 11, 12, 13)]
    absorbable = 1.0 * CHARGE_EFFICIENCY
    surplus = {slot: 1.0 for slot in slots}
    # Two slots of absorbable energy, minus a sliver, so exactly two are needed.
    target = 2 * absorbable - 0.01
    plan = _plan(
        slots, surplus, remaining_consumption_kwh=target, usable_energy_kwh=0.0
    )

    assert len(plan.selected_slots) == 2


def test_selection_prefers_the_earlier_slot_when_prices_tie():
    slots = [_slot(13, 0.20), _slot(10, 0.20)]
    candidates = _plan(slots, {slot: 5.0 for slot in slots}).slots

    selected = select_absorption_slots(candidates, 1.0)

    assert [slot.start for slot in selected] == [DAY + timedelta(hours=10)]


def test_surplus_is_capped_by_what_the_battery_can_physically_take():
    slot = _slot(12, 0.10)
    plan = _plan([slot], {slot: 9.0}, max_charge_power_w=2000.0)

    # 2 kW for one hour, then charge losses.
    assert plan.slots[0].expected_surplus_kwh == pytest.approx(2.0)
    assert plan.slots[0].absorbable_kwh == pytest.approx(2.0 * CHARGE_EFFICIENCY)


def test_hourly_and_quarter_hourly_feeds_select_the_same_energy():
    hourly = [_slot(10, 0.28), _slot(11, 0.16)]
    quarters = []
    for hour, price in ((10, 0.28), (11, 0.16)):
        for index in range(4):
            start = DAY + timedelta(hours=hour, minutes=15 * index)
            quarters.append(PriceSlot(start, start + timedelta(minutes=15), price))

    hourly_plan = _plan(hourly, {slot: 2.0 for slot in hourly},
                        remaining_consumption_kwh=1.0, usable_energy_kwh=0.0)
    quarter_plan = _plan(quarters, {slot: 0.5 for slot in quarters},
                         remaining_consumption_kwh=1.0, usable_energy_kwh=0.0)

    assert {slot.export_price for slot in hourly_plan.selected_slots} == {0.16}
    assert {slot.export_price for slot in quarter_plan.selected_slots} == {0.16}


# ---------------------------------------------------------------------------
# Plan status
# ---------------------------------------------------------------------------


def test_no_target_day_never_holds():
    slots = [_slot(10, 0.28), _slot(11, 0.16)]
    plan = _plan(slots, {slot: 2.0 for slot in slots},
                 remaining_consumption_kwh=1.0, usable_energy_kwh=5.0)

    assert plan.status == STATUS_NO_TARGET
    assert hold_decision(plan, NOW) == (False, REASON_TARGET_MET)


def test_infeasible_day_selects_every_slot_and_never_holds():
    slots = [_slot(10, 0.28), _slot(11, 0.16)]
    plan = _plan(slots, {slot: 0.5 for slot in slots},
                 remaining_consumption_kwh=20.0, usable_energy_kwh=0.0)

    assert plan.status == STATUS_INFEASIBLE
    assert len(plan.selected_slots) == 2
    assert hold_decision(plan, NOW)[0] is False


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"slots": []}, "missing_prices"),
        ({"deadline": NOW - timedelta(hours=1)}, "no_solar_window_left"),
        ({"deadline": None}, "no_solar_window_left"),
        ({"max_charge_power_w": 0.0}, "missing_charge_capacity"),
        ({"max_charge_power_w": float("nan")}, "missing_charge_capacity"),
        ({"batteries": []}, "missing_battery_capacity_or_soc"),
        ({"batteries": [_battery(capacity=-5.0)]}, "missing_battery_capacity_or_soc"),
        ({"remaining_consumption_kwh": None}, "missing_energy_forecast"),
        ({"usable_energy_kwh": float("inf")}, "missing_energy_forecast"),
    ],
)
def test_degenerate_inputs_fail_safe_to_release(kwargs, reason):
    slots = kwargs.pop("slots", [_slot(10, 0.2), _slot(11, 0.1)])
    plan = _plan(slots, {slot: 2.0 for slot in slots}, **kwargs)

    assert plan.status == STATUS_FAIL_SAFE
    assert plan.reason == reason
    assert hold_decision(plan, NOW)[0] is False


def test_all_slots_in_the_past_fail_safe():
    slots = [_slot(2, 0.2), _slot(3, 0.1)]
    plan = _plan(slots, {slot: 2.0 for slot in slots})

    assert plan.status == STATUS_FAIL_SAFE
    assert plan.reason == "no_future_export_slots"


def test_non_finite_prices_are_dropped():
    good = _slot(11, 0.10)
    bad = PriceSlot(DAY + timedelta(hours=12), DAY + timedelta(hours=13), math.nan)
    plan = _plan([good, bad], {good: 3.0, bad: 3.0},
                 remaining_consumption_kwh=2.0, usable_energy_kwh=0.0)

    assert [slot.export_price for slot in plan.slots] == [0.10]


# ---------------------------------------------------------------------------
# Hold decision
# ---------------------------------------------------------------------------


def test_holds_while_a_cheaper_window_is_still_ahead():
    slots = [_slot(9, 0.28), _slot(12, 0.14)]
    plan = _plan(slots, {slots[0]: 2.0, slots[1]: 2.0},
                 remaining_consumption_kwh=1.0, usable_energy_kwh=0.0)

    assert [slot.export_price for slot in plan.selected_slots] == [0.14]
    assert hold_decision(plan, NOW) == (True, REASON_CHEAPER_WINDOW_AHEAD)


def test_releases_inside_a_selected_absorption_window():
    slots = [_slot(9, 0.14), _slot(12, 0.28)]
    plan = _plan(slots, {slots[0]: 2.0, slots[1]: 2.0},
                 remaining_consumption_kwh=2.0, usable_energy_kwh=0.0)

    assert hold_decision(plan, NOW) == (False, REASON_ABSORPTION_WINDOW)


def test_releases_after_the_solar_deadline():
    slots = [_slot(9, 0.28), _slot(12, 0.14)]
    plan = _plan(slots, {slot: 2.0 for slot in slots},
                 remaining_consumption_kwh=2.0, usable_energy_kwh=0.0)

    assert hold_decision(plan, DEADLINE + timedelta(minutes=1)) == (
        False, REASON_PAST_DEADLINE,
    )


def test_releases_when_the_live_target_is_already_met():
    slots = [_slot(9, 0.28), _slot(12, 0.14)]
    plan = _plan(slots, {slot: 2.0 for slot in slots},
                 remaining_consumption_kwh=2.0, usable_energy_kwh=0.0)

    assert hold_decision(plan, NOW, live_target_kwh=0.0) == (False, REASON_TARGET_MET)


def test_releases_when_the_remaining_cheap_windows_can_no_longer_cover_the_target():
    """A cheap window that under-delivers must not leave the day short."""
    slots = [_slot(9, 0.28), _slot(12, 0.14)]
    plan = _plan(slots, {slot: 2.0 for slot in slots},
                 remaining_consumption_kwh=1.0, usable_energy_kwh=0.0)

    hold, reason = hold_decision(plan, NOW, live_target_kwh=9.0)

    assert (hold, reason) == (False, REASON_SHORTFALL_RISK)


def test_shortfall_release_does_not_return_later_the_same_day():
    slots = [_slot(9, 0.28), _slot(12, 0.14), _slot(13, 0.15)]
    plan = _plan(slots, {slot: 2.0 for slot in slots},
                 remaining_consumption_kwh=1.0, usable_energy_kwh=0.0)

    later = DAY + timedelta(hours=11)
    assert hold_decision(plan, NOW, live_target_kwh=9.0)[0] is False
    assert hold_decision(plan, later, live_target_kwh=9.0)[0] is False


def test_min_saving_suppresses_a_negligible_advantage():
    slots = [_slot(9, 0.2000), _slot(12, 0.1995)]
    surplus = {slot: 2.0 for slot in slots}

    negligible = _plan(slots, surplus, remaining_consumption_kwh=1.0,
                       usable_energy_kwh=0.0, min_saving=0.02)
    assert hold_decision(negligible, NOW) == (False, REASON_NO_MATERIAL_SAVING)

    material_slots = [_slot(9, 0.20), _slot(12, 0.10)]
    material = _plan(material_slots, {slot: 2.0 for slot in material_slots},
                     remaining_consumption_kwh=1.0, usable_energy_kwh=0.0,
                     min_saving=0.02)
    assert hold_decision(material, NOW) == (True, REASON_CHEAPER_WINDOW_AHEAD)


def test_no_plan_never_holds():
    assert hold_decision(None, NOW)[0] is False


def test_release_between_slots_rather_than_acting_on_stale_data():
    slots = [_slot(10, 0.28), _slot(12, 0.14)]
    plan = _plan(slots, {slot: 2.0 for slot in slots},
                 remaining_consumption_kwh=2.0, usable_energy_kwh=0.0)

    # 09:00 falls before the first slot, so no current price is known.
    hold, reason = hold_decision(plan, NOW)

    assert (hold, reason) == (False, "no_current_slot")
