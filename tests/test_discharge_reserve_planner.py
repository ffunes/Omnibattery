"""Unit tests for the pure price-aware discharge-reserve planner.

No Home Assistant, no controller: these exercise the arithmetic that decides how
much stored energy belongs to a later, dearer hour.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.curtailment import BatterySnapshot
from custom_components.omnibattery.pricing.discharge_reserve import (
    REASON_NOTHING_DEARER,
    REASON_NO_CAPACITY,
    REASON_NO_DEMAND,
    REASON_NO_PRICE,
    REASON_NO_SLOTS,
    REASON_NO_USABLE_ENERGY,
    REASON_PV_COVERS_IT,
    STATUS_NO_RESERVE,
    STATUS_RESERVING,
    ReserveSlot,
    battery_reserve_soc_pct,
    consumption_by_slot,
    eligible_capacity_kwh,
    net_demand_by_slot,
    plan_discharge_reserve,
    reserve_soc_pct,
    usable_energy_kwh,
)

DAY = datetime(2026, 9, 4)
NOW = DAY + timedelta(hours=13)


def _slot(hour: float, price: float, minutes: int = 60) -> PriceSlot:
    start = DAY + timedelta(hours=hour)
    return PriceSlot(start, start + timedelta(minutes=minutes), price)


def _snapshot(
    soc: float = 80.0,
    floor: float = 10.0,
    capacity: float = 10.0,
    eligible: bool = True,
    can_discharge: bool = True,
) -> BatterySnapshot:
    return BatterySnapshot(
        "battery-1", soc, capacity, 100.0, floor, 2500.0, eligible, can_discharge
    )


def _plan(slots, demand, **overrides):
    kwargs = dict(
        usable_energy_kwh=7.0,
        total_capacity_kwh=10.0,
        current_price=0.20,
        min_saving=0.05,
        now=NOW,
    )
    kwargs.update(overrides)
    return plan_discharge_reserve(slots, demand, **kwargs)


# ----------------------------------------------------------------------
# The reserve itself
# ----------------------------------------------------------------------


def test_reserves_the_energy_the_dearer_hours_need():
    """Two evening hours above the margin claim exactly their own demand."""
    evening_a = _slot(18, 0.40)
    evening_b = _slot(19, 0.35)
    plan = _plan(
        [evening_a, evening_b],
        {evening_a: 1.5, evening_b: 1.2},
    )
    assert plan.status == STATUS_RESERVING
    assert plan.reserve_kwh == 1.5 + 1.2
    assert [slot.start for slot in plan.selected_slots] == [
        evening_a.start,
        evening_b.start,
    ]


def test_reserve_never_exceeds_the_energy_in_the_battery():
    """A peak that wants more than the battery holds claims only what is there."""
    peak = _slot(18, 0.60)
    plan = _plan([peak], {peak: 12.0}, usable_energy_kwh=4.0)
    assert plan.reserve_kwh == 4.0


def test_dearest_hour_has_first_claim():
    """With too little energy for both, the dearest hour is served first."""
    cheap_peak = _slot(17, 0.30)
    dear_peak = _slot(20, 0.50)
    plan = _plan(
        [cheap_peak, dear_peak],
        {cheap_peak: 2.0, dear_peak: 2.0},
        usable_energy_kwh=2.0,
    )
    assert plan.reserve_kwh == 2.0
    assert [slot.price for slot in plan.selected_slots] == [0.50]


def test_no_reserve_when_nothing_ahead_beats_the_margin():
    """A later hour only 0.03 dearer does not clear a 0.05 margin."""
    later = _slot(18, 0.23)
    plan = _plan([later], {later: 3.0})
    assert plan.status == STATUS_NO_RESERVE
    assert plan.reason == REASON_NOTHING_DEARER
    assert plan.reserve_kwh == 0.0


def test_past_slots_never_claim_energy():
    """A slot that already started is now, not later."""
    past = _slot(11, 0.90)
    plan = _plan([past], {past: 3.0})
    assert plan.reserve_kwh == 0.0
    assert plan.reason == REASON_NOTHING_DEARER


def test_slots_without_demand_claim_nothing():
    """An expensive hour the household does not use needs no energy."""
    dear_but_idle = _slot(18, 0.80)
    plan = _plan([dear_but_idle], {dear_but_idle: 0.0})
    assert plan.reserve_kwh == 0.0
    assert plan.reason == REASON_NO_DEMAND


def test_missing_current_price_yields_no_reserve():
    """Without a reference price there is nothing to compare against."""
    peak = _slot(18, 0.80)
    plan = _plan([peak], {peak: 2.0}, current_price=None)
    assert plan.reserve_kwh == 0.0
    assert plan.reason == REASON_NO_PRICE


def test_empty_battery_yields_no_reserve():
    peak = _slot(18, 0.80)
    plan = _plan([peak], {peak: 2.0}, usable_energy_kwh=0.0)
    assert plan.reason == REASON_NO_USABLE_ENERGY


def test_no_capacity_is_reported_before_anything_else():
    peak = _slot(18, 0.80)
    plan = _plan([peak], {peak: 2.0}, total_capacity_kwh=0.0)
    assert plan.reason == REASON_NO_CAPACITY


def test_no_slots_is_reported():
    plan = _plan([], {})
    assert plan.reason == REASON_NO_SLOTS


def test_live_recompute_releases_when_the_current_hour_becomes_the_dear_one():
    """The peak arriving is what releases the floor, not the next rebuild."""
    peak = _slot(18, 0.40)
    plan = _plan([peak], {peak: 2.0})
    assert plan.reserve_kwh == 2.0

    # 17:00, price now 0.38: the 18:00 hour no longer clears the margin.
    reserve, claiming, reason = plan.reserve_kwh_at(
        DAY + timedelta(hours=17), 0.38, 7.0
    )
    assert reserve == 0.0
    assert claiming == []
    assert reason == REASON_NOTHING_DEARER


def test_live_recompute_shrinks_with_the_battery():
    """As the battery empties, the reserve can no longer exceed what is left."""
    peak = _slot(18, 0.40)
    plan = _plan([peak], {peak: 5.0})
    assert plan.reserve_kwh == 5.0
    reserve, _claiming, _reason = plan.reserve_kwh_at(NOW, 0.20, 1.5)
    assert reserve == 1.5


# ----------------------------------------------------------------------
# Fleet arithmetic
# ----------------------------------------------------------------------


def test_reserve_soc_pct_is_the_fleet_share_of_capacity():
    assert reserve_soc_pct(2.5, 10.0) == 25.0
    assert reserve_soc_pct(20.0, 10.0) == 100.0
    assert reserve_soc_pct(1.0, 0.0) == 0.0


def test_a_reserve_of_everything_stored_keeps_every_battery_where_it_is():
    """#700, two Venus E at 68 % and 65 % above a 12 % floor, all claimed.

    The fleet figure puts both floors at 65.73 %, so only the emptier one held.
    """
    fleet = [
        _snapshot(soc=68.0, floor=12.0, capacity=5.12),
        _snapshot(soc=65.0, floor=12.0, capacity=5.12),
    ]
    shares = battery_reserve_soc_pct(usable_energy_kwh(fleet), fleet)
    assert shares == pytest.approx([56.0, 53.0])


def test_every_battery_keeps_the_same_share_of_its_own_charge():
    fleet = [
        _snapshot(soc=80.0, floor=10.0, capacity=10.0),  # 7 kWh above its floor
        _snapshot(soc=40.0, floor=5.0, capacity=4.0),  # 1.4 kWh above its floor
    ]
    shares = battery_reserve_soc_pct(4.2, fleet)  # half of the 8.4 kWh stored
    assert shares == pytest.approx([35.0, 17.5])
    held = sum(s.capacity_kwh * share / 100.0 for s, share in zip(fleet, shares))
    assert held == pytest.approx(4.2)


def test_a_reserve_above_what_is_stored_holds_everything_and_no_more():
    fleet = [_snapshot(soc=30.0, floor=12.0, capacity=5.12)]
    assert battery_reserve_soc_pct(50.0, fleet) == pytest.approx([18.0])


def test_a_battery_that_does_not_back_the_reserve_keeps_none_of_it():
    fleet = [
        _snapshot(soc=80.0, floor=10.0),
        _snapshot(soc=80.0, floor=10.0, eligible=False),
        _snapshot(soc=80.0, floor=10.0, can_discharge=False),
        _snapshot(soc=8.0, floor=10.0),
    ]
    assert battery_reserve_soc_pct(3.5, fleet) == pytest.approx([35.0, 0.0, 0.0, 0.0])


def test_no_reserve_or_nothing_stored_keeps_nothing():
    assert battery_reserve_soc_pct(0.0, [_snapshot()]) == [0.0]
    assert battery_reserve_soc_pct(2.0, [_snapshot(soc=10.0, floor=10.0)]) == [0.0]
    assert battery_reserve_soc_pct(2.0, []) == []


def test_usable_energy_counts_only_the_band_above_the_floor():
    assert usable_energy_kwh([_snapshot(soc=80.0, floor=10.0, capacity=10.0)]) == 7.0
    assert usable_energy_kwh([_snapshot(soc=5.0, floor=10.0)]) == 0.0


def test_ineligible_batteries_contribute_nothing():
    snapshots = [
        _snapshot(eligible=False),
        _snapshot(can_discharge=False),
        _snapshot(soc=60.0, floor=10.0, capacity=5.0),
    ]
    assert eligible_capacity_kwh(snapshots) == 5.0
    assert usable_energy_kwh(snapshots) == 2.5


# ----------------------------------------------------------------------
# Demand projection
# ----------------------------------------------------------------------


def test_consumption_by_slot_sums_the_quarter_hour_profile():
    """An hourly price slot collects its four quarter-hour bins."""
    intervals = [0.0] * 96
    for index in range(72, 76):  # 18:00 - 19:00
        intervals[index] = 0.4
    slot = _slot(18, 0.40)
    demand = consumption_by_slot([slot], {}, intervals)
    assert round(demand[slot], 6) == round(1.6, 6)


def test_consumption_by_slot_prorates_a_quarter_hour_slot():
    intervals = [0.0] * 96
    intervals[72] = 0.4
    slot = _slot(18, 0.40, minutes=15)
    demand = consumption_by_slot([slot], {}, intervals)
    assert round(demand[slot], 6) == 0.4


def test_consumption_by_slot_prefers_the_dated_shape():
    """A horizon crossing midnight must not reuse today's shape for tomorrow."""
    today = [0.0] * 96
    today[72] = 1.0
    tomorrow = [0.0] * 96
    tomorrow[72] = 5.0
    slot_today = _slot(18, 0.40, minutes=15)
    slot_tomorrow = PriceSlot(
        DAY + timedelta(days=1, hours=18),
        DAY + timedelta(days=1, hours=18, minutes=15),
        0.40,
    )
    demand = consumption_by_slot(
        [slot_today, slot_tomorrow],
        {DAY.date(): today, (DAY + timedelta(days=1)).date(): tomorrow},
    )
    assert demand[slot_today] == 1.0
    assert demand[slot_tomorrow] == 5.0


def test_consumption_by_slot_without_a_profile_returns_nothing():
    assert consumption_by_slot([_slot(18, 0.4)], {}, []) == {}


def test_net_demand_subtracts_expected_pv_and_floors_at_zero():
    sunny = _slot(13, 0.10)
    evening = _slot(19, 0.40)
    net = net_demand_by_slot(
        {sunny: 1.0, evening: 2.0},
        {sunny: 3.0, evening: 0.0},
    )
    assert net[sunny] == 0.0
    assert net[evening] == 2.0


def test_a_slot_the_sun_covers_claims_no_reserve():
    sunny = _slot(15, 0.50)
    plan = _plan([sunny], net_demand_by_slot({sunny: 2.0}, {sunny: 2.5}))
    assert plan.reserve_kwh == 0.0


# ----------------------------------------------------------------------
# PV credit against demand the battery cannot cover alone (#700)
# ----------------------------------------------------------------------


def _sun_then_evening(evening_kwh: float, sun_kwh: float, **overrides):
    """Sun in the afternoon, then dearer evening hours; the battery has the room."""
    sun = ReserveSlot(DAY + timedelta(hours=14), DAY + timedelta(hours=15), 0.15, 0.0, sun_kwh)
    evening = _slot(18, 0.45)
    price_slot = _slot(14, 0.15)
    kwargs = dict(free_space_kwh=5.0)
    kwargs.update(overrides)
    plan = _plan(
        [price_slot, evening],
        {price_slot: 0.0, evening: evening_kwh},
        surplus_by_slot={price_slot: sun_kwh},
        **kwargs,
    )
    assert plan.slots[0].start == sun.start
    return plan


def test_demand_beyond_stored_plus_sun_holds_everything_stored():
    """#700, 27 Oct 14:07: 3.21 kWh dearer demand, 1.958 stored, 0.347 of sun."""
    plan = _sun_then_evening(3.21, 0.347, usable_energy_kwh=1.958)
    assert plan.reserve_kwh == pytest.approx(1.958)


def test_the_floor_does_not_slide_down_with_the_soc():
    """The same credit used to be granted again after every drop in SOC.

    31 Oct: usable 2.829 to 2.400 kWh, credit 2.436 kWh, 9.73 kWh of dearer
    demand, and the reserve fell from 0.393 kWh to nothing without ever blocking.
    """
    plan = _sun_then_evening(9.73, 2.436, usable_energy_kwh=2.829)
    for usable in (2.829, 2.584, 2.451, 2.400):
        reserve, _claiming, _reason = plan.reserve_kwh_at(NOW, 0.20, usable)
        assert reserve == pytest.approx(usable)


def test_the_sun_still_pays_for_what_stored_energy_need_not_cover():
    """Demand within stored plus sun: the battery only keeps what the sun cannot."""
    plan = _sun_then_evening(3.0, 2.0, usable_energy_kwh=4.0)
    assert plan.reserve_kwh == pytest.approx(1.0)
    # Once the SOC reaches that level the battery holds there; it does not slide.
    reserve, _claiming, _reason = plan.reserve_kwh_at(NOW, 0.20, 1.0)
    assert reserve == pytest.approx(1.0)


def test_sun_that_covers_all_demand_still_releases_the_battery():
    """The credit keeps its job: no hold when the sun refills in time (#446)."""
    plan = _sun_then_evening(2.0, 3.0, usable_energy_kwh=2.714, free_space_kwh=4.0)
    assert plan.reserve_kwh == 0.0
    assert plan.reason == REASON_PV_COVERS_IT


def test_sun_that_does_not_fit_in_the_battery_pays_nothing_extra():
    """With no room, the forecast cannot extend what the claims may count on."""
    plan = _sun_then_evening(3.0, 2.0, usable_energy_kwh=2.0, free_space_kwh=0.0)
    assert plan.pv_credit_kwh == pytest.approx(0.0)
    assert plan.reserve_kwh == pytest.approx(2.0)


def test_claims_show_what_stored_energy_and_sun_can_serve_together():
    """The dearest hours still come first, up to stored plus sun, not stored alone."""
    plan = _sun_then_evening(9.73, 2.436, usable_energy_kwh=2.584)
    assert plan.claimed_kwh == pytest.approx(2.584 + 2.436)
    assert plan.pv_credit_kwh == pytest.approx(2.436)
