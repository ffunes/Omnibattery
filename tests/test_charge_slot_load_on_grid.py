"""The grid feeds the house while a predictive slot charges.

The scalar balance and the depletion deadlines both count the house load in
every interval as battery drain, including the intervals in which a slot is
charging the battery from the grid. During those intervals the grid carries
the house, so the battery should only store what is needed after the charge.
"""
from datetime import datetime, timedelta

import pytest

from custom_components.omnibattery.const import CHARGE_EFFICIENCY
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.chronological import (
    ChronologicalEvaluationRequest,
    EnergyInterval,
    _net_of_load_carried_by_grid,
    evaluate_chronological_request,
)

BASE = datetime(2026, 11, 2)  # midnight
QUARTER = timedelta(minutes=15)


def _night(load_kw: float, hours: int = 7) -> list[EnergyInterval]:
    """Flat house load from midnight, no solar."""
    per_quarter = load_kw / 4.0
    return [
        EnergyInterval(BASE + i * QUARTER, BASE + (i + 1) * QUARTER, per_quarter, 0.0)
        for i in range(hours * 4)
    ]


def _request(intervals, slot, *, required, carried, power_kw=2.0, usable=0.0):
    return ChronologicalEvaluationRequest(
        now=BASE,
        horizon_end=BASE + timedelta(hours=7),
        intervals=tuple(intervals),
        price_slots=(slot,),
        total_required_kwh=required,
        effective_power_kw=power_kw,
        headroom_kwh=10.0,
        usable_initial_kwh=usable,
        grid_carries_load_while_charging=carried,
    )


def test_load_during_the_charge_is_not_charged_into_the_battery():
    """0.4 kW house, 02:00-05:00 slot, battery empty at 02:00, sunrise 07:00.

    From 02:00 to 07:00 the house uses 2.0 kWh. The battery charges from
    02:00, and the grid carries the house until the charge stops, so the
    battery only stores the load that remains after that point.
    """
    intervals = _night(0.4)
    slot = PriceSlot(BASE + timedelta(hours=2), BASE + timedelta(hours=5), 0.1)
    # 0.8 kWh covers the house until the slot opens.
    old = evaluate_chronological_request(
        _request(intervals, slot, required=2.0, carried=False, usable=0.8)
    ).plan
    new = evaluate_chronological_request(
        _request(intervals, slot, required=2.0, carried=True, usable=0.8)
    ).plan

    rate = 2.0 * CHARGE_EFFICIENCY
    # stored + 0.4 * t = 2.0 and stored = rate * t
    t = 2.0 / (rate + 0.4)
    expected = rate * t

    assert old.allocated_kwh == pytest.approx(2.0)
    assert new.allocated_kwh == pytest.approx(expected, abs=1e-6)
    assert new.allocated_kwh < old.allocated_kwh
    # The reduced need is not reported as a shortfall.
    assert new.total_shortfall_kwh == pytest.approx(0.0, abs=1e-6)
    assert new.total_required_kwh == pytest.approx(expected, abs=1e-6)
    assert new.reason == "ok"


def test_no_house_load_keeps_the_allocation():
    intervals = _night(0.0)
    slot = PriceSlot(BASE + timedelta(hours=2), BASE + timedelta(hours=5), 0.1)
    new = evaluate_chronological_request(
        _request(intervals, slot, required=1.5, carried=True, usable=0.0)
    ).plan
    assert new.allocated_kwh == pytest.approx(1.5)


def test_solar_covering_the_house_carries_no_load():
    """Only the load solar does not cover would have drained the battery."""
    intervals = [
        EnergyInterval(BASE + i * QUARTER, BASE + (i + 1) * QUARTER, 0.1, 0.2)
        for i in range(8)
    ]
    assert _net_of_load_carried_by_grid(
        intervals, 1.0, BASE, BASE + timedelta(hours=2), 1.8
    ) == pytest.approx(1.0)


def test_net_amount_solves_stored_plus_avoided():
    intervals = _night(1.0)
    rate = 1.0
    net = _net_of_load_carried_by_grid(
        intervals, 2.0, BASE, BASE + timedelta(hours=7), rate
    )
    # stored = t, avoided = t, stored + avoided = 2 -> t = 1 h
    assert net == pytest.approx(1.0)


def test_off_by_default_for_the_pure_evaluator():
    intervals = _night(0.4)
    slot = PriceSlot(BASE + timedelta(hours=2), BASE + timedelta(hours=5), 0.1)
    request = ChronologicalEvaluationRequest(
        now=BASE,
        horizon_end=BASE + timedelta(hours=7),
        intervals=tuple(intervals),
        price_slots=(slot,),
        total_required_kwh=2.0,
        effective_power_kw=2.0,
        headroom_kwh=10.0,
        usable_initial_kwh=0.8,
    )
    assert evaluate_chronological_request(request).plan.allocated_kwh == pytest.approx(2.0)


def test_weekly_full_charge_keeps_the_whole_gap():
    """A weekly charge fills to 100%; the load the grid carries does not shrink it."""
    from tests.test_planning_horizon import (
        CAP,
        _manager,
        _plan,
        _solar,
        _tomorrow,
        _tonight,
        _tonight_kwh,
    )

    slots = _tonight(0.10) + _tomorrow({18: 0.50, 19: 0.50, 20: 0.50})
    normal, _ = _plan(_manager(_solar(20.0)), slots, CAP)

    weekly_manager = _manager(_solar(20.0))
    weekly_manager._controller._weekly_full_charge_pending = lambda: False
    trimmed, _ = _plan(weekly_manager, slots, CAP)
    weekly_manager._controller._weekly_full_charge_pending = lambda: True
    untrimmed, _ = _plan(weekly_manager, slots, CAP)

    assert _tonight_kwh(trimmed) == pytest.approx(_tonight_kwh(normal))
    assert _tonight_kwh(untrimmed) > _tonight_kwh(trimmed) + 0.5


def _simulate(intervals, allocations, start_kwh, rate):
    """Battery energy per interval when each slot charges from its start until
    its planned energy is stored, the grid carrying the house meanwhile.

    Returns (minimum, final) battery energy.
    """
    left = {a.slot: a.planned_battery_kwh for a in allocations}
    energy = start_kwh
    low = energy
    for interval in intervals:
        slot = next(
            (s for s in left if s.start <= interval.start < s.end and left[s] > 1e-9),
            None,
        )
        hours = (interval.end - interval.start).total_seconds() / 3600.0
        drain = max(0.0, interval.consumption_kwh - interval.solar_kwh)
        if slot is None:
            energy -= drain
        else:
            # Charging part of the interval: the grid feeds the house then;
            # the battery drains the house for the rest of it.
            stored = min(left[slot], rate * hours)
            charging_share = stored / (rate * hours)
            left[slot] -= stored
            energy += stored - drain * (1.0 - charging_share)
        low = min(low, energy)
    return low, energy


def test_slots_at_full_capacity_keep_their_charge():
    """#785 follow-up: the slots cannot store the whole need.

    Same night at 1 kW: each slot stores at most 0.85 kWh. Both run the whole
    hour, so the grid carries 0.8 kWh of house load, which covers the 0.7 kWh
    the slots lack. Trimming them as if they stopped early left the battery
    0.7 kWh short before sunrise.
    """
    intervals = _night(0.4)
    slots = (
        PriceSlot(BASE + timedelta(hours=1), BASE + timedelta(hours=2), 0.10),
        PriceSlot(BASE + timedelta(hours=2), BASE + timedelta(hours=3), 0.12),
    )
    plan = evaluate_chronological_request(
        ChronologicalEvaluationRequest(
            now=BASE,
            horizon_end=BASE + timedelta(hours=7),
            intervals=tuple(intervals),
            price_slots=slots,
            total_required_kwh=2.4,
            effective_power_kw=1.0,
            headroom_kwh=10.0,
            usable_initial_kwh=0.4,
            grid_carries_load_while_charging=True,
        )
    ).plan

    assert plan.total_shortfall_kwh == pytest.approx(0.0, abs=1e-6)
    assert plan.reason == "ok"
    # The earlier slot keeps its full hour; the later one stops once the
    # stored energy plus the carried load cover the night.
    first, second = plan.allocations
    assert first.planned_battery_kwh == pytest.approx(CHARGE_EFFICIENCY)
    assert second.planned_battery_kwh < CHARGE_EFFICIENCY
    low, final = _simulate(intervals, plan.allocations, 0.4, CHARGE_EFFICIENCY)
    assert low >= -1e-6
    assert final == pytest.approx(0.0, abs=1e-6)


def test_carried_load_only_shrinks_a_real_shortfall():
    """Slots far too small: the carried load reduces the shortfall, and the
    slots still charge their whole capacity."""
    intervals = _night(0.4)
    slot = PriceSlot(BASE + timedelta(hours=1), BASE + timedelta(hours=2), 0.10)
    plan = evaluate_chronological_request(
        ChronologicalEvaluationRequest(
            now=BASE,
            horizon_end=BASE + timedelta(hours=7),
            intervals=tuple(intervals),
            price_slots=(slot,),
            total_required_kwh=2.4,
            effective_power_kw=1.0,
            headroom_kwh=10.0,
            usable_initial_kwh=0.4,
            grid_carries_load_while_charging=True,
        )
    ).plan

    assert plan.allocated_kwh == pytest.approx(CHARGE_EFFICIENCY)
    # 2.4 needed, 0.85 stored, 0.4 carried during the hour.
    assert plan.total_shortfall_kwh == pytest.approx(2.4 - CHARGE_EFFICIENCY - 0.4)


def test_timeline_charges_the_slot_first_then_covers_the_house():
    """The Daily Operation projection follows the controller: full power from
    the slot start until the quota is stored, then the battery covers the
    house for the rest of the slot. Spreading the quota over the slot kept
    the house on the grid for all three hours and projected the battery
    0.8 kWh above its floor at sunrise."""
    from custom_components.omnibattery.pricing.daily_timeline import (
        BatteryProjectionInput,
        simulate_battery_projection,
    )

    intervals = _night(0.4)
    slot = PriceSlot(BASE + timedelta(hours=2), BASE + timedelta(hours=5), 0.1)
    plan = evaluate_chronological_request(
        _request(intervals, slot, required=2.0, carried=True, usable=0.8)
    ).plan
    floor = 1.0  # 10% of 10 kWh
    battery = BatteryProjectionInput(
        "b", floor + 0.8, 10.0, 10.0, 100.0, 2000.0, 2000.0,
        charge_efficiency=CHARGE_EFFICIENCY, discharge_efficiency=1.0,
    )
    flows = simulate_battery_projection(
        intervals, [battery], allocations=plan.allocations
    ).intervals

    in_slot = [f for f in flows if slot.start <= f.start < slot.end]
    assert in_slot[0].grid_to_battery_kwh == pytest.approx(0.5)  # 2 kW quarter
    assert in_slot[-1].grid_to_battery_kwh == pytest.approx(0.0)
    assert in_slot[-1].battery_to_home_kwh == pytest.approx(0.1)
    # The quarter in which the charge stops does not discharge at all, so
    # the projection ends a few Wh above the floor (it was 0.8 kWh).
    assert flows[-1].stored_energy_end_kwh == pytest.approx(floor, abs=0.03)


def test_scalar_fallback_solves_stored_plus_carried():
    """Without a profile the scalar plan uses the same balance (#785)."""
    from custom_components.omnibattery.pricing.calculations import (
        net_of_load_carried_by_grid,
    )

    rate = 2.0 * CHARGE_EFFICIENCY
    assert net_of_load_carried_by_grid(2.0, 2.0, 0.4) == pytest.approx(
        2.0 * rate / (rate + 0.4)
    )
    assert net_of_load_carried_by_grid(2.0, 2.0, 0.0) == pytest.approx(2.0)
    assert net_of_load_carried_by_grid(2.0, 0.0, 0.4) == pytest.approx(2.0)
