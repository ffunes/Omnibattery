"""2.0.0 optimizer, step 3 (grid charge only): pricing/optimizer.py.

The plan must cost what it promises when the controller executes it: a slot's
target is set once from the whole-percent reading, the slot completes the
moment it is reached, and a slot published "to the end" charges until it ends.
``_replay`` below is that execution, written apart from the optimizer.
"""
from __future__ import annotations

import itertools
import math
import random
from datetime import datetime, timedelta

import pytest

from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.battery_model import BatteryModel, Quarter
from custom_components.omnibattery.pricing.chronological import (
    ChronologicalEvaluationRequest,
    EnergyInterval,
)
from custom_components.omnibattery.pricing.optimizer import optimize_charge

T0 = datetime(2026, 11, 10, 0, 0)
Q = timedelta(minutes=15)


def _request(prices, *, house=0.1, solar=0.0, cap=10.0, soc=15.0, usable=0.5, room=9.0,
             power=2.5, slot_quarters=1, threshold=None, margin=0.0, negative=True,
             resolution=1.0, discharge=0.8):
    n = len(prices) * slot_quarters
    houses = house if isinstance(house, (list, tuple)) else [house] * n
    solars = solar if isinstance(solar, (list, tuple)) else [solar] * n
    intervals = [EnergyInterval(T0 + i * Q, T0 + (i + 1) * Q, houses[i], solars[i]) for i in range(n)]
    slots = [PriceSlot(T0 + k * slot_quarters * Q, T0 + (k + 1) * slot_quarters * Q, p)
             for k, p in enumerate(prices)]
    return ChronologicalEvaluationRequest(
        now=T0, horizon_end=T0 + n * Q, intervals=tuple(intervals), price_slots=tuple(slots),
        total_required_kwh=0.0, effective_power_kw=power, headroom_kwh=room - usable,
        usable_initial_kwh=usable, max_price_threshold=threshold, import_value_efficiency=0.85,
        import_value_margin=margin, grid_ceiling_kw=None, fleet_capacity_kwh=cap,
        fleet_soc_pct=soc, soc_resolution_pct=resolution, discharge_kw=discharge,
        negative_price_charging=negative,
    )


def _replay(request, plan):
    """Cost of executing the published plan, as the controller does it."""
    m = BatteryModel(capacity_kwh=request.fleet_capacity_kwh, round_trip_efficiency=0.85,
                     floor_kwh=request.fleet_capacity_kwh * request.fleet_soc_pct / 100
                     - request.usable_initial_kwh,
                     usable_kwh=request.usable_initial_kwh + request.headroom_kwh,
                     soc_resolution_pct=request.soc_resolution_pct)
    step = request.fleet_capacity_kwh * request.soc_resolution_pct / 100
    quota = {a.slot: (a.planned_battery_kwh, a.to_slot_end) for a in plan.allocations}
    e, total, target, done, prices = request.usable_initial_kwh, 0.0, {}, set(), []
    for i in request.intervals:
        s = next(s for s in request.price_slots if s.start <= i.start < s.end)
        prices.append(s.price)
        h = (i.end - i.start).total_seconds() / 3600
        q = Quarter(i.grid_load_kwh, i.solar_kwh, s.price, 0.0, True,
                    request.effective_power_kw * h, request.discharge_kw * h)
        t = None
        if s in quota and s not in done:
            if s not in target:
                kwh, to_end = quota[s]
                target[s] = float(m.slot_target(e, kwh + (step if to_end else 0.0)))
                if request.floor_kwh > 0:  # the controller raises it to the floor
                    target[s] = max(target[s], min(request.floor_kwh, m.cap))
            t = target[s]
            if t - e <= m.charge_rate(q) + 1e-9:
                done.add(s)
        e1, c = m.normal_step(q, e, t)
        total += float(c)
        e = float(e1)
    end = min((p for p in prices if p > 0), default=0.0) * m.eff
    return total - end * e


def test_a_run_of_cheap_quarters_charges_each_to_its_end():
    # 10 kWh at 15 %: 1 % is 0.1 kWh. Quotas that fit a quarter reach their
    # whole-percent target early, the house takes the battery just under it,
    # and the next slot starts a percent lower: ~0.07 kWh lost per quarter.
    request = _request([-0.05] * 8 + [0.30] * 8, house=0.035)
    plan = optimize_charge(request).plan
    run = [a for a in plan.allocations if a.slot.price < 0]
    assert len(run) == 8
    rate = 2.5 * 0.25 * math.sqrt(0.85)
    assert sum(a.planned_battery_kwh for a in run) == pytest.approx(8 * rate, abs=0.01)
    assert all(a.to_slot_end for a in run[:-1])
    assert _replay(request, plan) == pytest.approx(plan.optimizer_cost, abs=1e-4)


def test_an_hourly_slot_sets_its_target_once_and_completes():
    # One target for the hour; once reached the slot is over (no top-up after
    # the house drains it), as the controller completes it.
    request = _request([0.10, 0.40], house=0.2, slot_quarters=4, usable=1.0)
    plan = optimize_charge(request).plan
    assert [a.slot.price for a in plan.allocations] == [0.10]
    assert _replay(request, plan) == pytest.approx(plan.optimizer_cost, abs=1e-4)


def test_a_threshold_forbids_the_slot():
    request = _request([0.10, 0.40], house=0.3, threshold=0.05)
    assert optimize_charge(request).plan.allocations == []


def test_margin_decides_but_is_not_in_the_cost():
    # 0.20 now vs 0.30 later: worth it at round trip 0.85 only without a margin.
    cheap = _request([0.20, 0.30, 0.30, 0.30], house=0.4, usable=0.0)
    assert optimize_charge(cheap).plan.allocations
    dear = _request([0.20, 0.30, 0.30, 0.30], house=0.4, usable=0.0, margin=0.10)
    plan = optimize_charge(dear).plan
    assert plan.allocations == []
    assert _replay(dear, plan) == pytest.approx(plan.optimizer_cost, abs=1e-4)


def test_negative_price_charging_off_counts_the_price_as_zero():
    # No positive price left, so stored energy is worth nothing at the end:
    # on, being paid to charge is worth it; off (D3), -0.05 counts as 0.
    prices = [-0.05] * 4 + [0.0] * 4
    on = optimize_charge(_request(prices, house=0.1, usable=0.0)).plan
    off_request = _request(prices, house=0.1, usable=0.0, negative=False)
    off = optimize_charge(off_request).plan
    assert on.allocated_kwh > 1.0
    assert off.allocations == []
    assert _replay(off_request, off) == pytest.approx(off.optimizer_cost, abs=1e-4)


def test_the_trajectory_follows_the_plan():
    request = _request([0.10, 0.10, 0.40, 0.40], house=0.3, usable=0.2)
    plan = optimize_charge(request).plan
    assert len(plan.trajectory) == 4
    assert plan.trajectory[-1][0] == request.intervals[-1].end
    assert plan.charge_efficiency == pytest.approx(math.sqrt(0.85))


def test_the_plan_is_as_cheap_as_any_plan_the_controller_can_run():
    """Exhaustive search over small nights: every quota on a 0.05 grid, or to
    the slot end, per slot. The optimizer may not lose more than its lattice."""
    rng = random.Random(7)
    worst = 0.0
    for _ in range(25):
        prices = [rng.choice((-0.05, 0.05, 0.10, 0.20, 0.30, 0.40)) for _ in range(4)]
        request = _request(prices, house=[rng.choice((0.0, 0.05, 0.1, 0.2)) for _ in range(4)],
                           solar=[rng.choice((0.0, 0.0, 0.15)) for _ in range(4)],
                           cap=3.0, soc=20.0, usable=rng.choice((0.0, 0.1, 0.23)), room=0.6,
                           power=0.8, discharge=0.8)
        plan = optimize_charge(request).plan
        got = _replay(request, plan)
        assert got == pytest.approx(plan.optimizer_cost, abs=1e-4)
        options = [None] + [(k * 0.05, False) for k in range(1, 5)] + [(0.2 * math.sqrt(0.85), True)]
        best = math.inf
        for combo in itertools.product(options, repeat=len(prices)):
            fake = type("P", (), {})()
            fake.allocations = [
                type("A", (), dict(slot=s, planned_battery_kwh=o[0], to_slot_end=o[1]))()
                for s, o in zip(request.price_slots, combo) if o is not None
            ]
            best = min(best, _replay(request, fake))
        worst = max(worst, got - best)
    assert worst <= 0.003


def test_the_real_controller_stores_each_slot_s_planned_energy():
    """Each plain slot of the plan, through the real controller's target and
    stop (tests/test_battery_model.controller_stores), stores what the plan
    expects; a "to the end" slot's target is beyond what its window charges."""
    from tests.test_battery_model import controller_stores

    rng = random.Random(5)
    checked = 0
    for _ in range(20):
        cap, soc0 = rng.choice((5.12, 7.0, 10.0)), rng.choice((15.0, 23.0, 60.0))
        usable = cap * (soc0 - 10.0) / 100.0
        prices = [rng.choice((0.05, 0.10, 0.20, 0.30, 0.40)) for _ in range(12)]
        request = _request(prices, house=rng.choice((0.05, 0.1, 0.3)), cap=cap, soc=soc0,
                           usable=usable, room=cap * 0.9)
        plan = optimize_charge(request).plan
        energy = {request.intervals[0].start: usable}
        energy.update({end: e for end, e in plan.trajectory})
        for a in plan.allocations:
            before = energy[a.slot.start]
            soc = 10.0 + before / cap * 100.0
            if a.to_slot_end:
                continue
            stored = controller_stores([(cap, soc)], a.planned_battery_kwh)
            assert stored == pytest.approx(a.planned_battery_kwh, abs=1e-3), (prices, a)
            checked += 1
    assert checked >= 5


def _with_floor(request, floor_kwh, until_quarter):
    import dataclasses
    return dataclasses.replace(
        request, floor_kwh=floor_kwh,
        floor_until=None if until_quarter is None else T0 + until_quarter * Q,
    )


def test_the_guaranteed_floor_holds_until_the_sun_even_when_it_costs():
    # Dear night, cheap morning after the floor's deadline: without the floor
    # the plan waits; with it the battery stays at 2 kWh above the discharge
    # floor through quarter 8, bought at 0.40.
    prices = [0.40] * 8 + [0.05] * 4 + [0.50] * 4
    plain = optimize_charge(_request(prices, house=0.3, usable=1.0)).plan
    assert not [a for a in plain.allocations if a.slot.price > 0.3]
    request = _with_floor(_request(prices, house=0.3, usable=1.0), 2.0, 8)
    plan = optimize_charge(request).plan
    energy = dict(plan.trajectory)
    # From 1.0 kWh it takes two quarters at 2.5 kW to get there.
    assert min(energy[T0 + k * Q] for k in range(2, 9)) >= 2.0 - 0.01
    assert _replay(request, plan) == pytest.approx(plan.optimizer_cost, abs=1e-4)


def test_an_impossible_floor_gets_as_close_as_it_can():
    request = _with_floor(_request([0.40] * 4, house=0.3, usable=0.0, power=0.8), 5.0, 4)
    plan = optimize_charge(request).plan
    assert all(a.to_slot_end for a in plan.allocations) and len(plan.allocations) == 4


def test_with_the_floor_active_a_slot_charges_at_least_to_the_floor():
    # The controller raises every booked slot's target to the floor SOC while
    # the floor is active; the plan's own energies say so.
    request = _with_floor(_request([0.10, 0.40, 0.40], house=0.4, usable=0.5, power=5.0), 1.5, None)
    plan = optimize_charge(request).plan
    assert plan.allocations and plan.trajectory[0][1] >= 1.5 - 0.11
    assert _replay(request, plan) == pytest.approx(plan.optimizer_cost, abs=1e-4)


def test_slots_left_out_of_the_charge_candidates_still_price_the_plan():
    """Smart Pre-Discharge rebuilds the plan without the slots of its risk
    window: they may not charge, but the night goes on after them."""
    import dataclasses

    full = _request([0.10, 0.10, 0.40, 0.40, 0.40, 0.40], house=0.3, usable=0.0)
    candidates = tuple(s for i, s in enumerate(full.price_slots) if i != 1)
    request = dataclasses.replace(full, price_slots=candidates, price_curve=full.price_slots)
    plan = optimize_charge(request).plan
    assert plan.trajectory[-1][0] == full.intervals[-1].end
    assert full.price_slots[1] not in {a.slot for a in plan.allocations}
    assert plan.allocations  # slot 0 still charges for the dear hours
