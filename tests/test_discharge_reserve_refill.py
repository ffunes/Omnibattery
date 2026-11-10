"""The discharge reserve counts a cheaper grid charge before a dear hour.

Energy held for tomorrow evening's peak can instead be used now and bought back
tonight. When that costs less than the minimum saving over the price now, the
reserve releases it, and the charge planner's next evaluation from the live SOC
books the night charge. The prices below are from 9 and 10 Nov 2026 (Tibber,
gross), with the measured round-trip efficiency of 0.8157, the arbitrage margin
of 0.05 and a minimum saving of 0.02.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    CHARGE_EFFICIENCY,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
)
from custom_components.omnibattery.control.discharge_reserve import (
    DischargeReserveManager,
)
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.chronological import (
    ChronologicalEvaluationRequest,
    EnergyInterval,
    build_energy_deadlines,
    evaluate_chronological_request,
)
from custom_components.omnibattery.pricing.discharge_reserve import (
    REASON_GRID_REFILLS_CHEAPER,
    REASON_RESERVED,
    RefillTerms,
    plan_discharge_reserve,
)

DAY = datetime(2026, 11, 10)
EFFICIENCY = 0.8157
MARGIN = 0.05
MIN_SAVING = 0.02


def _slot(day: int, hour: float, price: float) -> PriceSlot:
    start = DAY + timedelta(days=day, hours=hour)
    return PriceSlot(start, start + timedelta(hours=1), price)


def _terms(start: datetime, **overrides) -> RefillTerms:
    kwargs = dict(
        start=start,
        efficiency=EFFICIENCY,
        margin=MARGIN,
        charge_kwh_per_h=4.0,
        price_ceiling=None,
    )
    kwargs.update(overrides)
    return RefillTerms(**kwargs)


def _plan(slots, demand, now, current_price, refill, **overrides):
    kwargs = dict(
        usable_energy_kwh=2.0,
        total_capacity_kwh=13.28,
        free_space_kwh=10.0,
        current_price=current_price,
        min_saving=MIN_SAVING,
        now=now,
        refill=refill,
    )
    kwargs.update(overrides)
    return plan_discharge_reserve(slots, demand, **kwargs)


def _evening_to_evening(night_price: float = 0.4363):
    """10 Nov 18:00 to 11 Nov evening: a cheap night between two peaks."""
    night = _slot(1, 2, night_price)
    peak = _slot(1, 17, 0.7427)
    return night, peak


# ----------------------------------------------------------------------
# The decision
# ----------------------------------------------------------------------


def test_a_cheaper_night_releases_the_energy_held_for_tomorrow_evening():
    """10 Nov 18:15 at 0.676: the night refills at 0.4363 / 0.8157 + 0.05 = 0.585."""
    night, peak = _evening_to_evening()
    now = DAY + timedelta(hours=18, minutes=15)
    plan = _plan(
        [night, peak], {night: 0.4, peak: 1.5}, now, 0.676,
        _terms(DAY + timedelta(days=1, minutes=5)),
    )
    assert plan.reserve_kwh == 0.0
    assert plan.reason == REASON_GRID_REFILLS_CHEAPER
    assert plan.refill_credit_kwh == pytest.approx(1.5)
    assert [(slot.start, kwh) for slot, kwh in plan.refill_breakdown] == [
        (peak.start, pytest.approx(1.5))
    ]


def test_a_night_dearer_than_the_price_now_keeps_the_hold():
    """9 Nov 21:00 at 0.58: the night refills at 0.504 / 0.8157 + 0.05 = 0.668."""
    night = _slot(0, 3, 0.504)
    morning = _slot(0, 7, 0.713)
    now = DAY - timedelta(hours=3)
    plan = _plan(
        [night, morning], {night: 0.3, morning: 1.0}, now, 0.58,
        _terms(DAY + timedelta(minutes=5)),
    )
    assert plan.reserve_kwh == pytest.approx(1.0)
    assert plan.reason == REASON_RESERVED
    assert plan.refill_credit_kwh == 0.0


def test_holding_for_less_than_the_minimum_saving_is_not_worth_it():
    """10 Nov 20:30 at 0.5675: holding saves 0.585 - 0.5675 = 1.75 ct, under 2 ct."""
    night, peak = _evening_to_evening()
    now = DAY + timedelta(hours=20, minutes=30)
    plan = _plan(
        [night, peak], {night: 0.4, peak: 1.5}, now, 0.5675,
        _terms(DAY + timedelta(days=1, minutes=5)),
    )
    assert plan.reserve_kwh == 0.0
    assert plan.reason == REASON_GRID_REFILLS_CHEAPER


def test_a_cheap_slot_before_the_next_evaluation_does_not_count():
    """23:00 tonight comes before the 00:05 re-plan, so nothing books it for sure."""
    late = _slot(0, 23, 0.4476)
    peak = _slot(1, 17, 0.7427)
    now = DAY + timedelta(hours=18, minutes=15)
    plan = _plan(
        [late, peak], {late: 0.3, peak: 1.5}, now, 0.676,
        _terms(DAY + timedelta(days=1, minutes=5)),
    )
    assert plan.reserve_kwh == pytest.approx(1.5)
    assert plan.reason == REASON_RESERVED


def test_without_refill_terms_only_the_sun_refills():
    night, peak = _evening_to_evening()
    now = DAY + timedelta(hours=18, minutes=15)
    plan = _plan([night, peak], {night: 0.4, peak: 1.5}, now, 0.676, None)
    assert plan.reserve_kwh == pytest.approx(1.5)
    assert plan.reason == REASON_RESERVED


def test_a_static_price_ceiling_below_the_night_blocks_the_refill():
    """The planner does not charge above the ceiling, so neither does the reserve count it."""
    night, peak = _evening_to_evening()
    now = DAY + timedelta(hours=18, minutes=15)
    plan = _plan(
        [night, peak], {night: 0.4, peak: 1.5}, now, 0.676,
        _terms(DAY + timedelta(days=1, minutes=5), price_ceiling=0.40),
    )
    assert plan.reserve_kwh == pytest.approx(1.5)


def test_with_the_margin_off_the_refill_costs_the_price_over_the_efficiency():
    """0.55 / 0.8157 = 0.674: refills under 0.696 only once the margin is off."""
    night, peak = _evening_to_evening(night_price=0.55)
    now = DAY + timedelta(hours=18, minutes=15)
    held = _plan(
        [night, peak], {night: 0.4, peak: 1.5}, now, 0.676,
        _terms(DAY + timedelta(days=1, minutes=5)),
    )
    released = _plan(
        [night, peak], {night: 0.4, peak: 1.5}, now, 0.676,
        _terms(DAY + timedelta(days=1, minutes=5), margin=0.0),
    )
    assert held.reserve_kwh == pytest.approx(1.5)
    assert released.reserve_kwh == 0.0


# ----------------------------------------------------------------------
# What the grid can physically put back
# ----------------------------------------------------------------------


def test_a_night_too_short_for_all_of_it_keeps_the_rest():
    """One hour at 1 kWh/h refills 1 of the 1.5 kWh the peak needs."""
    night, peak = _evening_to_evening()
    now = DAY + timedelta(hours=18, minutes=15)
    plan = _plan(
        [night, peak], {night: 0.4, peak: 1.5}, now, 0.676,
        _terms(DAY + timedelta(days=1, minutes=5), charge_kwh_per_h=1.0),
    )
    assert plan.refill_credit_kwh == pytest.approx(1.0)
    assert plan.reserve_kwh == pytest.approx(0.5)
    assert plan.reason == REASON_RESERVED


def test_a_full_battery_has_no_room_to_refill():
    """No free space and no demand served before the peak: nothing fits."""
    night, peak = _evening_to_evening()
    now = DAY + timedelta(hours=18, minutes=15)
    plan = _plan(
        [night, peak], {peak: 1.5}, now, 0.676,
        _terms(DAY + timedelta(days=1, minutes=5)),
        free_space_kwh=0.0,
    )
    assert plan.refill_credit_kwh == 0.0
    assert plan.reserve_kwh == pytest.approx(1.5)


def test_the_grid_leaves_the_room_the_sun_still_needs():
    """2 kWh free, 1.5 kWh of sun ahead after the night: the grid gets 0.5 kWh."""
    night = _slot(1, 2, 0.4363)
    sun = _slot(1, 12, 0.60)
    peak = _slot(1, 17, 0.7427)
    now = DAY + timedelta(hours=18, minutes=15)
    plan = _plan(
        [night, sun, peak], {peak: 1.0}, now, 0.676,
        _terms(DAY + timedelta(days=1, minutes=5)),
        free_space_kwh=2.0,
        surplus_by_slot={sun: 1.5},
        usable_energy_kwh=3.0,
    )
    assert plan.refill_credit_kwh == pytest.approx(0.5)


def test_the_budget_goes_to_the_dear_hour_nothing_refills():
    """Tonight's 19:00 comes before the refill, tomorrow's peak after it.

    With 1 kWh stored, the dearer peak used to take it all, and tonight's
    hour went to the grid. The night refills the peak, so the stored kWh is
    held for tonight instead.
    """
    tonight = _slot(0, 19, 0.70)
    night, peak = _evening_to_evening()
    peak = PriceSlot(peak.start, peak.end, 0.80)
    now = DAY + timedelta(hours=18, minutes=15)
    plan = _plan(
        [tonight, night, peak], {tonight: 1.0, night: 0.4, peak: 1.5}, now, 0.676,
        _terms(DAY + timedelta(days=1, minutes=5)),
        usable_energy_kwh=1.0,
    )
    assert [slot.start for slot in plan.selected_slots] == [tonight.start]
    assert plan.reserve_kwh == pytest.approx(1.0)


# ----------------------------------------------------------------------
# The manager
# ----------------------------------------------------------------------


def _controller(**overrides):
    base = SimpleNamespace(
        discharge_reserve_enabled=True,
        discharge_reserve_min_saving=MIN_SAVING,
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        round_trip_efficiency=EFFICIENCY,
        min_arbitrage_margin=MARGIN,
        max_price_threshold=None,
        max_charge_capacity=4_900.0,
        coordinators=[],
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _terms_at(now: datetime, **overrides) -> RefillTerms | None:
    manager = DischargeReserveManager(SimpleNamespace(), _controller(**overrides))
    return manager._refill_terms(now)


def test_the_refill_counts_from_tonight_s_daily_evaluation():
    terms = _terms_at(DAY + timedelta(hours=18, minutes=15))
    assert terms.start == DAY + timedelta(days=1, minutes=5)
    assert terms.efficiency == pytest.approx(EFFICIENCY)
    assert terms.margin == pytest.approx(MARGIN)
    assert terms.charge_kwh_per_h == pytest.approx(4.9 * CHARGE_EFFICIENCY)
    assert terms.price_ceiling is None


def test_just_after_midnight_the_refill_counts_from_today_s_evaluation():
    terms = _terms_at(DAY + timedelta(minutes=2))
    assert terms.start == DAY + timedelta(minutes=5)


def test_the_refill_follows_the_ceiling_and_a_margin_that_is_off():
    terms = _terms_at(
        DAY + timedelta(hours=18), max_price_threshold=0.40, min_arbitrage_margin=None
    )
    assert terms.price_ceiling == pytest.approx(0.40)
    assert terms.margin == 0.0


def test_no_charge_power_means_no_refill():
    assert _terms_at(DAY + timedelta(hours=18), max_charge_capacity=0.0) is None


def test_the_status_shows_what_the_night_refills():
    night, peak = _evening_to_evening()
    now = DAY + timedelta(hours=18, minutes=15)
    manager = DischargeReserveManager(SimpleNamespace(), _controller())
    plan = _plan([night, peak], {night: 0.4, peak: 1.5}, now, 0.676, None)
    plan.refill = manager._refill_terms(now)
    plan.reserve_kwh_at(now, 0.676)
    manager._set_status(0.0, "released", plan.reason, plan)
    status = manager.get_status()
    assert status["refill_credit_kwh"] == pytest.approx(1.5)
    assert status["refill_from"] == (DAY + timedelta(days=1, minutes=5)).isoformat()
    assert status["refills"] == [
        {"start": peak.start.isoformat(), "price": 0.7427, "refilled_kwh": 1.5}
    ]


# ----------------------------------------------------------------------
# The planner books what the reserve released
# ----------------------------------------------------------------------


def _night_plan(stored_kwh: float):
    """11 Nov as the 00:05 evaluation sees it, with ``stored_kwh`` in the battery."""
    now = DAY + timedelta(days=1, minutes=5)
    hours = range(24)
    prices = {hour: 0.4363 if hour < 6 else 0.63 for hour in hours}
    prices.update({17: 0.7427, 18: 0.7248})
    load = {17: 1.0, 18: 0.8}
    slots = [_slot(1, hour, prices[hour]) for hour in hours]
    intervals = [
        EnergyInterval(slot.start, slot.end, load.get(hour, 0.0), 0.0)
        for hour, slot in zip(hours, slots)
    ]
    deadlines = build_energy_deadlines(intervals, usable_initial_kwh=stored_kwh)
    request = ChronologicalEvaluationRequest(
        now=now,
        horizon_end=DAY + timedelta(days=2),
        intervals=intervals,
        price_slots=slots,
        total_required_kwh=max(0.0, sum(load.values()) - stored_kwh),
        effective_power_kw=4.9,
        usable_initial_kwh=stored_kwh,
        deadlines=tuple(deadlines),
        import_value_efficiency=EFFICIENCY,
        import_value_margin=MARGIN,
        reserve_holds=True,
    )
    return evaluate_chronological_request(request).plan


def test_the_night_charge_takes_over_what_the_reserve_released():
    """Held, the peak is already stored; released, the 00:05 plan buys it at night."""
    held = _night_plan(stored_kwh=1.8)
    released = _night_plan(stored_kwh=0.0)
    assert held.allocated_kwh == pytest.approx(0.0, abs=1e-6)
    assert released.allocated_kwh == pytest.approx(1.8, abs=0.05)
    assert all(item.slot.start.hour < 6 for item in released.allocations)
    assert released.deadline_shortfall_kwh == pytest.approx(0.0, abs=1e-6)
