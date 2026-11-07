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
