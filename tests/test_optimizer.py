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


def _replay(request, plan, held=(), exported=(), starts=None):
    """Cost of executing the published plan, as the controller does it. In a
    ``held`` slot the battery covers nothing (solar surplus still stores).
    ``starts`` (a list) gets the charger's starts appended: a charge that
    reaches its target inside a quarter has stopped there."""
    m = BatteryModel(capacity_kwh=request.fleet_capacity_kwh, round_trip_efficiency=0.85,
                     floor_kwh=request.fleet_capacity_kwh * request.fleet_soc_pct / 100
                     - request.usable_initial_kwh,
                     usable_kwh=request.usable_initial_kwh + request.headroom_kwh,
                     soc_resolution_pct=request.soc_resolution_pct)
    step = request.fleet_capacity_kwh * request.soc_resolution_pct / 100
    quota = {a.slot: (a.planned_battery_kwh, a.to_slot_end) for a in plan.allocations}
    e, total, target, done, prices = request.usable_initial_kwh, 0.0, {}, set(), []
    n, running = 0, False
    for i in request.intervals:
        s = next(s for s in request.price_slots if s.start <= i.start < s.end)
        prices.append(s.price)
        h = (i.end - i.start).total_seconds() / 3600
        q = Quarter(i.grid_load_kwh, i.solar_kwh, s.price,
                    (request.export_prices or {}).get(s.start, 0.0), True,
                    0.0 if s in exported else request.effective_power_kw * h,
                    0.0 if s in held else request.discharge_kw * h)
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
        add = float(m.charge_phase(q, e, t)[0]) if t is not None else 0.0
        n += add > 1e-9 and not running
        running = add > 1e-9 and e + add < t - 1e-6
        e1, c = m.normal_step(q, e, t)
        total += float(c)
        e = float(e1)
    if starts is not None:
        starts.append(n)
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


@pytest.mark.parametrize("prices", [[0.10] * 4, [0.12, 0.10, 0.11, 0.13], [0.13, 0.12, 0.11, 0.10]])
@pytest.mark.parametrize(("soc", "usable"), [(10.0, 0.3), (40.0, 2.7)])
def test_cheap_hours_at_one_price_stop_less(monkeypatch, prices, soc, usable):
    # #899, #571: 2400 W on 8.16 kWh, four cheap hours before a dear evening.
    # At one price the plan split the charge into quotas stopping mid-hour:
    # 4 starts. Two are the fewest at that price (one block costs ~4.7 c more:
    # it ends the cheap hours less full). Different prices keep their plan,
    # up to the start cost of the starts it saves.
    from custom_components.omnibattery.pricing import optimizer

    request = _request(prices + [0.40] * 4, house=[0.05] * 16 + [0.5] * 16, slot_quarters=4,
                       cap=8.16, soc=soc, usable=usable, room=7.3, power=2.4, discharge=2.4)
    plan = optimize_charge(request).plan
    c = optimizer.START_COST_EUR
    monkeypatch.setattr(optimizer, "START_COST_EUR", 0.0)
    off = optimize_charge(request).plan
    starts, starts_off = [], []
    assert _replay(request, plan, starts=starts) == pytest.approx(plan.optimizer_cost, abs=1e-4)
    _replay(request, off, starts=starts_off)
    assert (plan.optimizer_cost + c * starts[0]
            <= off.optimizer_cost + c * starts_off[0] + 1e-4)
    assert starts[0] <= starts_off[0]
    if len(set(prices)) == 1:
        assert starts_off[0] == 4 and starts[0] == 2


def test_the_last_charge_of_a_run_keeps_its_quota(monkeypatch):
    # Nothing charges after it: run to its end, it would save no start and
    # only cost more.
    from custom_components.omnibattery.pricing import optimizer

    request = _request([0.30, 0.11, 0.20, 0.20], house=0.2, usable=0.0, room=8.0, soc=10.0,
                       power=2.4, discharge=2.4)
    plan = optimize_charge(request).plan
    monkeypatch.setattr(optimizer, "START_COST_EUR", 0.0)
    assert [(a.planned_battery_kwh, a.to_slot_end) for a in plan.allocations] == [
        (a.planned_battery_kwh, a.to_slot_end) for a in optimize_charge(request).plan.allocations]
    assert not plan.allocations[0].to_slot_end


# --- Charge start cost ------------------------------------------------------

def _evening(prices, evening=0.265):
    """Cheap quarters, then four dear ones the battery covers: at 0.265 kWh a
    quarter, two quarters of charge at 2.5 kW pay."""
    n = len(prices)
    return _request(list(prices) + [0.40] * 4, house=[0.0] * n + [evening] * 4,
                    usable=0.0, room=3.0, soc=10.0, discharge=2.5)


def _starts(request, plan):
    starts = []
    cost = _replay(request, plan, starts=starts)
    assert cost == pytest.approx(plan.optimizer_cost, abs=1e-4)  # starts are never published
    return starts[0]


def test_a_charge_joins_quarters_half_a_cent_apart():
    # Joined: 0.3 c dearer, under a start's 1 c (without the cost: 3 starts).
    request = _evening([0.10, 0.105, 0.105, 0.10])
    plan = optimize_charge(request).plan
    assert _starts(request, plan) == plan.charge_starts == 1


def test_a_charge_does_not_join_quarters_far_apart():
    request = _evening([0.10, 0.13, 0.13, 0.10])
    plan = optimize_charge(request).plan
    assert _starts(request, plan) == plan.charge_starts == 2


def test_no_top_up_that_saves_less_than_a_start():
    # Full after the first two quarters; the house drains it a hair before
    # another cheap quarter. Refilling that hair saves well under 1 c.
    request = _request([0.10, 0.10, 0.30, 0.10] + [0.40] * 4,
                       house=[0.01] * 4 + [0.45] * 4, usable=0.0, room=1.1, soc=10.0,
                       discharge=2.5)
    plan = optimize_charge(request).plan
    assert [a.slot.start for a in plan.allocations] == [T0, T0 + Q]
    assert _starts(request, plan) == plan.charge_starts == 1


def test_a_charge_running_at_now_goes_on():
    # Needs three quarters. From T0 one block (0, 1, 2) beats two starts for
    # 0.1 c. Re-planned at quarter 1 while charging, it goes on; a plan that
    # counted a start there would move to (2, 3), a hair cheaper.
    import dataclasses

    prices = [0.10, 0.104, 0.10, 0.103]
    request = _evening(prices, evening=0.39)
    plan = optimize_charge(request).plan
    assert [a.slot.start for a in plan.allocations] == [T0, T0 + Q, T0 + 2 * Q]
    e1 = dict(plan.trajectory)[T0 + Q]
    rest = [(a.slot.start, round(a.planned_battery_kwh, 3), a.to_slot_end)
            for a in plan.allocations if a.slot.start >= T0 + Q]

    def replan(charging):
        later = dataclasses.replace(
            request, now=T0 + Q, usable_initial_kwh=e1, headroom_kwh=3.0 - e1,
            fleet_soc_pct=10.0 + e1 / 10.0 * 100.0, charging_now=charging)
        return [(a.slot.start, round(a.planned_battery_kwh, 3), a.to_slot_end)
                for a in optimize_charge(later).plan.allocations]

    assert replan(True) == rest
    assert replan(False) != rest


def test_the_guaranteed_floor_holds_even_when_it_costs_a_start():
    prices = [0.40] * 8 + [0.05] * 4 + [0.50] * 4
    request = _with_floor(_request(prices, house=0.3, usable=1.0), 2.0, 8)
    plan = optimize_charge(request).plan
    energy = dict(plan.trajectory)
    assert min(energy[T0 + k * Q] for k in range(2, 9)) >= 2.0 - 0.01
    assert _starts(request, plan) == plan.charge_starts >= 2


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
    the slot end, per slot. The optimizer may not lose more than its lattice,
    each charge start (counted by the replay) at its start cost."""
    from custom_components.omnibattery.pricing.optimizer import START_COST_EUR

    rng = random.Random(7)
    worst = 0.0
    for _ in range(25):
        prices = [rng.choice((-0.05, 0.05, 0.10, 0.20, 0.30, 0.40)) for _ in range(4)]
        request = _request(prices, house=[rng.choice((0.0, 0.05, 0.1, 0.2)) for _ in range(4)],
                           solar=[rng.choice((0.0, 0.0, 0.15)) for _ in range(4)],
                           cap=3.0, soc=20.0, usable=rng.choice((0.0, 0.1, 0.23)), room=0.6,
                           power=0.8, discharge=0.8)
        plan = optimize_charge(request).plan
        starts = []
        got = _replay(request, plan, starts=starts)
        assert got == pytest.approx(plan.optimizer_cost, abs=1e-4)
        assert starts[0] == plan.charge_starts
        got += START_COST_EUR * starts[0]
        options = [None] + [(k * 0.05, False) for k in range(1, 5)] + [(0.2 * math.sqrt(0.85), True)]
        best = math.inf
        for combo in itertools.product(options, repeat=len(prices)):
            fake = type("P", (), {})()
            fake.allocations = [
                type("A", (), dict(slot=s, planned_battery_kwh=o[0], to_slot_end=o[1]))()
                for s, o in zip(request.price_slots, combo) if o is not None
            ]
            starts = []
            best = min(best, _replay(request, fake, starts=starts) + START_COST_EUR * starts[0])
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


def _replay_hold(request, plan):
    """``_replay`` with the plan's held slots: the battery covers nothing then."""
    return _replay(request, plan, held=set(plan.holds))


def _with_hold(request):
    import dataclasses
    return dataclasses.replace(request, hold_allowed=True)


def test_holding_keeps_the_battery_for_the_dearer_hour():
    # 1 kWh stored, no cheap slot: the 0.40 evening is worth it, the 0.20 now is not.
    prices = [0.20] * 4 + [0.40] * 4
    request = _with_hold(_request(prices, house=0.25, usable=0.95, threshold=0.0))
    plan = optimize_charge(request).plan
    assert {slot.price for slot in plan.holds} == {0.20}
    assert _replay_hold(request, plan) == pytest.approx(plan.optimizer_cost, abs=1e-4)
    off = optimize_charge(_request(prices, house=0.25, usable=0.95, threshold=0.0)).plan
    assert plan.optimizer_cost < off.optimizer_cost - 0.03


def test_a_small_price_step_is_held_without_a_minimum_saving():
    # #888: no minimum saving; the plan holds whenever it costs less.
    prices = [0.20] * 4 + [0.25] * 4
    keep = optimize_charge(_with_hold(_request(prices, house=0.25, usable=0.95, threshold=0.0))).plan
    assert keep.holds


def test_a_hold_pays_its_start(monkeypatch):
    """#888 D1: 15-minute prices zigzag; without a hold start cost the plan
    holds every cheaper quarter on its own, with it one run."""
    from custom_components.omnibattery.pricing import optimizer

    prices = [0.20, 0.23] * 6 + [0.40] * 4
    request = _with_hold(_request(prices, house=0.25, usable=1.5, threshold=0.0))

    def runs(plan):
        held = sorted(s.start for s in plan.holds)
        return sum(1 for i, t in enumerate(held) if i == 0 or t - held[i - 1] != Q)

    plan = optimize_charge(request).plan
    assert runs(plan) == 1 and plan.hold_starts == 1
    monkeypatch.setattr(optimizer, "HOLD_START_EUR", 0.0)
    assert runs(optimize_charge(request).plan) > 1


def test_a_hold_going_on_at_now_pays_no_start():
    import dataclasses
    prices = [0.20, 0.23] * 6 + [0.40] * 4
    request = _with_hold(_request(prices, house=0.25, usable=1.5, threshold=0.0))
    assert optimize_charge(request).plan.hold_starts == 1
    assert optimize_charge(dataclasses.replace(request, holding_now=True)).plan.hold_starts == 0


def test_no_hold_without_the_permission():
    prices = [0.20] * 4 + [0.40] * 4
    plan = optimize_charge(_request(prices, house=0.25, usable=0.95, threshold=0.0)).plan
    assert not plan.holds


def test_the_night_is_held_for_the_dearer_day():
    """#888, anorakophobia's night (shape, not their 7.60 EUR: their load is
    unknown): quarter-hour prices, 13.3 kWh with 4.1 kWh usable at 20:30,
    margin 0.05, round trip 0.82. No charge pays; the night (0.40-0.44) is
    held and the stored energy is spent in the dearer day (0.49-0.53)."""
    import dataclasses
    night = [0.40 + 0.04 * ((i * 7) % 10) / 10 for i in range(40)]  # 20:30-06:30
    morning = [0.49 + 0.04 * ((i * 3) % 10) / 10 for i in range(14)]  # -10:00
    midday = [0.45 + 0.02 * ((i * 3) % 10) / 10 for i in range(24)]  # -16:00
    evening = [0.51 + 0.01 * ((i * 7) % 10) / 10 for i in range(18)]  # -20:30
    house = [0.35 * 0.25] * 40 + [0.9 * 0.25] * 14 + [0.4 * 0.25] * 24 + [1.0 * 0.25] * 18
    request = dataclasses.replace(
        _request(night + morning + midday + evening, house=house, cap=13.3, soc=40.0, usable=4.1,
                 room=13.3 * 0.9, margin=0.05, discharge=2.5),
        import_value_efficiency=0.82, hold_allowed=True)
    plan = optimize_charge(request).plan
    assert not plan.allocations
    held = {s.start for s in plan.holds}
    assert all(s.start in held for s in request.price_slots[:40])
    at_dawn = dict(plan.trajectory)[T0 + 40 * Q]
    assert at_dawn == pytest.approx(4.1, abs=1e-6)
    assert plan.trajectory[-1][1] < 0.1
    spent = [s.price for s in request.price_slots if s.start not in held]
    assert min(spent) >= 0.49
    off = optimize_charge(dataclasses.replace(request, hold_allowed=False)).plan
    assert plan.optimizer_cost < off.optimizer_cost - 0.2


def test_a_2_0td_valley_lets_the_grid_carry_the_house_with_one_charge():
    """#888's announcement: 10.4 kWh at 70 %, a cloudy day on 2.0TD prices.
    In the valley the grid carries the house (the battery is held) and the
    plan charges once."""
    td = [0.08 if hr < 8 else (0.25 if 10 <= hr < 14 or 18 <= hr < 22 else 0.15)
          for hr in range(24) for _ in range(4)]
    house = ([0.3 * 0.25] * 28 + [0.8 * 0.25] * 8 + [0.5 * 0.25] * 36 + [1.4 * 0.25] * 16
             + [0.4 * 0.25] * 8)
    solar = [0.0] * 40 + [0.15] * 24 + [0.0] * 32
    request = _with_hold(_request(td, house=house, solar=solar, cap=10.4, soc=70.0,
                                  usable=10.4 * 0.6, room=10.4 * 0.9, discharge=2.5))
    plan = optimize_charge(request).plan
    valley = [s for s in request.price_slots if s.price == 0.08]
    charged = {a.slot.start for a in plan.allocations}
    held = {s.start for s in plan.holds}
    assert plan.charge_starts == 1 and charged
    assert all(s.start in held or s.start in charged for s in valley)
    assert not (held - {s.start for s in valley})


def test_a_day_with_sun_to_spare_holds_nothing():
    """#888: the sun refills the battery anyway; a held night would only
    leave sun unstored. The plan is the one without holds."""
    prices = [0.20] * 36 + [0.25] * 36 + [0.30] * 24
    solar = [0.0] * 36 + [1.2] * 36 + [0.0] * 24
    request = _request(prices, house=0.1, solar=solar, cap=10.0, soc=50.0, usable=4.0,
                       room=9.0, discharge=2.5)
    held = optimize_charge(_with_hold(request)).plan
    plain = optimize_charge(request).plan
    assert not held.holds
    assert held.optimizer_cost == pytest.approx(plain.optimizer_cost, abs=1e-9)
    assert [(a.slot, a.planned_battery_kwh) for a in held.allocations] == [
        (a.slot, a.planned_battery_kwh) for a in plain.allocations]


def test_a_charge_that_ends_early_holds_the_rest_of_its_slot():
    """#878: an empty 7.1 kWh battery (2.5 kW / 0.8 kW), quarter-hour night
    prices. The charge reaches its target inside its quarter; the battery is
    then held (bought energy is not spent at the price it was bought), and it
    pays no hold start: the charge start covered the change."""
    prices = [0.184 + 0.015 * ((i * 7) % 10) / 10 for i in range(8)] + [0.30] * 2
    request = _with_hold(_request(prices, house=0.125, cap=7.1, soc=2.0, usable=0.0,
                                  room=7.1 * 0.9, discharge=0.8, power=2.5))
    plan = optimize_charge(request).plan
    (charge,) = plan.allocations
    assert not charge.to_slot_end
    assert charge.slot in plan.holds
    assert plan.hold_starts == 0
    assert _replay_hold(request, plan) == pytest.approx(plan.optimizer_cost, abs=1e-4)


def test_a_slot_may_charge_and_then_hold():
    """The discharge reserve held the rest of a charging quarter once its quota
    was reached; the optimizer offers the same: charge, then keep it."""
    prices = [0.22] * 4 + [0.32] * 4
    request = _with_hold(_request(prices, house=0.3, usable=0.0, slot_quarters=1, power=5.0))
    plan = optimize_charge(request).plan
    both = set(plan.holds) & {a.slot for a in plan.allocations}
    assert both
    assert _replay_hold(request, plan) == pytest.approx(plan.optimizer_cost, abs=1e-4)


def _with_export(request, export_prices, min_saving=0.0):
    import dataclasses
    prices = {s.start: p for s, p in zip(request.price_slots, export_prices)}
    return dataclasses.replace(request, export_prices=prices, export_choice_allowed=True,
                               export_choice_min_saving=min_saving)


def _replay_export(request, plan):
    """``_replay`` where the plan exports the surplus of some slots (the
    battery stores none of it then)."""
    import dataclasses
    exported = set(plan.exports)
    m = BatteryModel(capacity_kwh=request.fleet_capacity_kwh, round_trip_efficiency=0.85,
                     floor_kwh=request.fleet_capacity_kwh * request.fleet_soc_pct / 100
                     - request.usable_initial_kwh,
                     usable_kwh=request.usable_initial_kwh + request.headroom_kwh)
    e, total, prices = request.usable_initial_kwh, 0.0, []
    for i in request.intervals:
        s = next(s for s in request.price_slots if s.start <= i.start < s.end)
        prices.append(s.price)
        h = (i.end - i.start).total_seconds() / 3600
        exporting = s in exported
        q = Quarter(i.grid_load_kwh, i.solar_kwh, s.price, request.export_prices.get(s.start, 0.0),
                    True, 0.0 if exporting else request.effective_power_kw * h,
                    request.discharge_kw * h)
        e1, c = m.normal_step(q, e, None)
        total += float(c)
        e = float(e1)
    return total - min((p for p in prices if p > 0), default=0.0) * m.eff * e


def test_morning_surplus_exports_while_the_midday_fills_the_battery():
    # Morning surplus sells at 0.18, midday at 0.02: the midday sun alone fills
    # the battery, so the morning surplus is worth exporting.
    prices = [0.20] * 8
    exports = [0.18] * 4 + [0.02] * 4
    solar = [0.3] * 4 + [1.0] * 4
    request = _with_export(_request(prices, house=0.05, solar=solar, usable=0.0, room=2.0,
                                    threshold=0.0), exports)
    plan = optimize_charge(request).plan
    assert {s.start for s in plan.exports} == {s.start for s in request.price_slots[:4]}
    assert _replay_export(request, plan) == pytest.approx(plan.optimizer_cost, abs=1e-4)


def test_on_a_tie_the_surplus_is_stored():
    prices = [0.20] * 8
    request = _with_export(_request(prices, house=0.05, solar=[0.3] * 4 + [1.0] * 4, usable=0.0,
                                    room=2.0, threshold=0.0), [0.02] * 8)
    assert not optimize_charge(request).plan.exports


def test_the_minimum_saving_decides_an_export():
    prices = [0.20] * 8
    base = _request(prices, house=0.05, solar=[0.3] * 4 + [1.0] * 4, usable=0.0, room=2.0, threshold=0.0)
    request = _with_export(base, [0.18] * 4 + [0.02] * 4, min_saving=0.20)
    assert not optimize_charge(request).plan.exports


def _with_sale(request, export_prices, power_kw=2.5, margin=0.0):
    import dataclasses
    prices = {s.start: p for s, p in zip(request.price_slots, export_prices)}
    return dataclasses.replace(request, export_prices=prices, battery_export_prices=prices,
                               sell_allowed=True, sell_power_kw=power_kw, import_value_margin=margin)


def _replay_sale(request, plan):
    """The controller's sale: grid at -P for the slot (the battery covers the
    house and exports the rest; solar beyond P charges it)."""
    sales = dict(plan.sales)
    m = BatteryModel(capacity_kwh=request.fleet_capacity_kwh, round_trip_efficiency=0.85,
                     floor_kwh=request.fleet_capacity_kwh * request.fleet_soc_pct / 100
                     - request.usable_initial_kwh,
                     usable_kwh=request.usable_initial_kwh + request.headroom_kwh)
    e, total, prices = request.usable_initial_kwh, 0.0, []
    for i in request.intervals:
        s = next(s for s in request.price_slots if s.start <= i.start < s.end)
        prices.append(s.price)
        h = (i.end - i.start).total_seconds() / 3600
        net = i.grid_load_kwh - i.solar_kwh
        if s in sales:
            flow = net + sales[s] * h
            if flow >= 0:
                out = min(e * m.eff, flow, request.discharge_kw * h)
                e -= out / m.eff
                grid = net - out
            else:
                st = min(-flow, request.effective_power_kw * h, (m.cap - e) / m.eff)
                e += st * m.eff
                grid = net + st
            total += max(grid, 0) * s.price - max(-grid, 0) * request.battery_export_prices[s.start]
            continue
        q = Quarter(i.grid_load_kwh, i.solar_kwh, s.price, request.export_prices.get(s.start, 0.0),
                    True, request.effective_power_kw * h, request.discharge_kw * h)
        e1, c = m.normal_step(q, e, None)
        total += float(c)
        e = float(e1)
    return total - min((p for p in prices if p > 0), default=0.0) * m.eff * e


def test_a_peak_worth_more_than_refilling_is_sold():
    # 4 kWh stored; the peak sells at 0.50, refilling tomorrow costs 0.10 (end value).
    prices = [0.20] * 4 + [0.10] * 4
    exports = [0.50] * 4 + [0.0] * 4
    request = _with_sale(_request(prices, house=0.05, usable=4.0, threshold=0.0), exports)
    plan = optimize_charge(request).plan
    assert {s.start for s, _p in plan.sales} == {s.start for s in request.price_slots[:4]}
    assert _replay_sale(request, plan) == pytest.approx(plan.optimizer_cost, abs=1e-4)


def test_no_sale_when_it_does_not_pay_after_the_margin():
    prices = [0.20] * 4 + [0.10] * 4
    exports = [0.13] * 4 + [0.0] * 4  # worth 0.13 now, refill 0.10 / 0.92 = 0.108
    assert optimize_charge(_with_sale(_request(prices, house=0.05, usable=4.0, threshold=0.0), exports)).plan.sales
    plan = optimize_charge(_with_sale(_request(prices, house=0.05, usable=4.0, threshold=0.0), exports,
                                      margin=0.05)).plan
    assert not plan.sales


# --- Step 8: Solar Charge Delay (D9) -------------------------------------

def _with_delay(request, margin_h=0.0, setpoint_pct=None):
    import dataclasses
    return dataclasses.replace(request, solar_delay_margin_h=margin_h, solar_delay_setpoint_pct=setpoint_pct)


def _full_at(plan, cap):
    return next(t for t, e in plan.trajectory if e >= cap - 1e-6)


def test_solar_delay_stores_the_sun_as_late_as_it_still_fills():
    # No export price: storing early or late costs the same. With the delay
    # the morning surplus is exported and the battery fills at the end of the
    # sun; without it, as soon as the sun comes.
    base = _request([0.20] * 12, house=0.05, solar=0.5, usable=0.0, room=2.0, threshold=0.0)
    off = optimize_charge(base).plan
    on = optimize_charge(_with_delay(base)).plan
    assert not off.exports and _full_at(off, 2.0) <= T0 + 6 * Q
    assert on.exports and _full_at(on, 2.0) >= T0 + 11 * Q
    assert on.optimizer_cost == pytest.approx(off.optimizer_cost, abs=1e-6)


def test_solar_delay_never_exports_what_the_later_sun_cannot_refill():
    # Strong sun first, weak after: the late surplus alone cannot fill the
    # battery, so only what it can refill is exported and the battery ends
    # the sun as full as without the delay.
    base = _request([0.20] * 8, house=0.05, solar=[0.7] * 4 + [0.2] * 4, usable=0.0, room=2.0,
                    threshold=0.0)
    off = optimize_charge(base).plan
    on = optimize_charge(_with_delay(base)).plan
    assert on.trajectory[-1][1] >= off.trajectory[-1][1] - 0.1  # one 1 % step of 10 kWh


def test_solar_delay_fills_to_the_setpoint_first():
    base = _request([0.20] * 12, house=0.05, solar=0.5, usable=0.0, room=2.0, threshold=0.0)
    plan = optimize_charge(_with_delay(base, setpoint_pct=25.0)).plan  # 15 % + 1 kWh of 10
    first_export = min(s.start for s in plan.exports)
    reached = next(t for t, e in plan.trajectory if e >= 1.0 - 1e-6)
    assert first_export >= reached


def test_solar_delay_fills_before_the_time_margin():
    base = _request([0.20] * 12, house=0.05, solar=0.5, usable=0.0, room=2.0, threshold=0.0)
    plan = optimize_charge(_with_delay(base, margin_h=1.0)).plan  # sun ends at 03:00
    assert T0 + 7 * Q <= _full_at(plan, 2.0) <= T0 + 8 * Q


def test_solar_delay_keeps_a_30_percent_cushion_on_the_later_sun():
    # The surplus left after the first slot refills the battery 1.15 times
    # over: enough with an exact forecast, not with the delay's 30 % cushion
    # (a forecast that comes in short left the battery unfilled).
    base = _request([0.20] * 6, house=0.05, solar=[0.5] + [0.549] * 5, usable=0.0, room=2.0,
                    threshold=0.0)
    plan = optimize_charge(_with_delay(base)).plan
    assert base.price_slots[0] not in set(plan.exports)


def test_solar_delay_does_not_delay_on_a_day_that_needs_the_grid():
    # The module's daily balance: stored energy + the sun left today short of
    # the house load left today (minus its 0.5 kWh deadband) means the day
    # needs the grid anyway; delaying the sun then gains nothing and risks it.
    sunny = _request([0.20] * 20, house=[0.05] * 12 + [1.0] * 8, solar=[0.5] * 12 + [0.0] * 8,
                     usable=0.0, room=2.0, threshold=0.0)
    assert not optimize_charge(_with_delay(sunny)).plan.exports
    light = _request([0.20] * 20, house=[0.05] * 12 + [0.2] * 8, solar=[0.5] * 12 + [0.0] * 8,
                     usable=0.0, room=2.0, threshold=0.0)
    assert optimize_charge(_with_delay(light)).plan.exports


def test_a_day_that_needs_the_grid_at_planning_time_stays_closed():
    # The balance is short now (a night load the grid carries with the battery
    # empty) and sufficient from the first sunny slot: like the module, which
    # decides once and keeps the day unlocked, today exports nothing.
    req = _request([0.20] * 20, house=[2.0] * 4 + [0.05] * 12 + [0.2] * 4,
                   solar=[0.0] * 4 + [0.5] * 12 + [0.0] * 4, usable=0.0, room=2.0, threshold=0.0)
    plan = optimize_charge(_with_delay(req)).plan
    assert not plan.exports and plan.solar_delay_day_closed


def test_a_closed_day_reaches_the_optimizer():
    import dataclasses
    base = _request([0.20] * 12, house=0.05, solar=0.5, usable=0.0, room=2.0, threshold=0.0)
    closed = dataclasses.replace(_with_delay(base), solar_delay_closed_until=T0 + timedelta(days=1))
    assert optimize_charge(_with_delay(base)).plan.exports
    assert not optimize_charge(closed).plan.exports


def test_the_delay_tie_break_never_adds_up_to_more_than_a_tenth_of_a_cent():
    # Two sunny days and a small battery (it refills many times over): the
    # tie-break may decide, never cost real money, so its total over the whole
    # horizon stays within 0.1 c.
    from custom_components.omnibattery.pricing.battery_model import BatteryModel
    from custom_components.omnibattery.pricing.optimizer import DELAY_TIE_EUR, _slot_groups, _solar_delay

    n = 192  # two days of quarters
    solar = [0.5 if 32 <= k % 96 < 72 else 0.0 for k in range(n)]
    req = _with_delay(_request([0.20] * n, house=0.05, solar=solar, usable=0.0, room=2.0, threshold=0.0))
    m = BatteryModel(capacity_kwh=10.0, round_trip_efficiency=0.85, floor_kwh=1.5, usable_kwh=2.0)
    groups = _slot_groups(req)
    delay = _solar_delay(req, groups, m, 10.0)
    ends = [end for _s, _qs, es in groups for end in es]
    storable = [min(max(0.0, sun - 0.05), 2.5 * 0.25) * m.eff for sun in solar]
    assert sum(delay.weight(end) * v for end, v in zip(ends, storable)) <= DELAY_TIE_EUR + 1e-12


def test_a_closed_day_carries_no_delay_tie_break():
    # No delay on a closed day, the tie-break included: with stress it moved
    # sales and fills on a day the module would not have delayed.
    import dataclasses
    from custom_components.omnibattery.pricing.battery_model import BatteryModel
    from custom_components.omnibattery.pricing.optimizer import _slot_groups, _solar_delay

    req = _with_delay(_request([0.20] * 12, house=0.05, solar=0.5, usable=0.0, room=2.0, threshold=0.0))
    m = BatteryModel(capacity_kwh=10.0, round_trip_efficiency=0.85, floor_kwh=1.5, usable_kwh=2.0)
    open_ = _solar_delay(req, _slot_groups(req), m, 10.0)
    closed_req = dataclasses.replace(req, solar_delay_closed_until=T0 + timedelta(days=1))
    closed = _solar_delay(closed_req, _slot_groups(closed_req), m, 10.0)
    assert open_.weight(T0 + Q) > 0
    assert closed.weight(T0 + Q) == 0


def test_the_delay_tie_break_does_not_push_sales_where_it_may_not_export():
    # Flat export prices: selling the morning sun or the afternoon's costs the
    # same. Under the setpoint the delay may not export, so its tie-break must
    # not steer the sales either (it sold the morning sun from an empty battery
    # and missed the re-plans a short sun would have triggered).
    prices = [0.22] * 16 + [0.32] * 8
    base = _request(prices, house=[0.05] * 16 + [0.4] * 8, solar=[0.0] * 2 + [1.0] * 12 + [0.0] * 10,
                    usable=0.0, room=2.0, threshold=0.0)
    sale = _with_sale(base, [p - 0.02 for p in prices])
    off = optimize_charge(sale).plan
    on = optimize_charge(_with_delay(sale, setpoint_pct=90.0)).plan
    assert sorted(s.start for s, _kw in on.sales) == sorted(s.start for s, _kw in off.sales)


def test_the_plan_never_sells_at_a_battery_price_at_or_below_zero():
    # A long negative-price window with sun and an evening peak: selling there
    # paid to export (it made room for the paid-to-charge hours). Negative
    # export prices belong to Smart Pre-Discharge (its own switch and reserve
    # SOC, design §3), not to High-Price Sale's permission.
    prices = [0.20] * 4 + [-0.05] * 12 + [0.30] * 8
    req = _with_sale(_request(prices, house=0.1, solar=[0.2] * 4 + [0.4] * 12 + [0.0] * 8, usable=0.3,
                              room=2.0), [p - 0.02 for p in prices])
    plan = optimize_charge(req).plan
    assert not [s for s, _kw in plan.sales if req.battery_export_prices[s.start] <= 0]


def test_the_arbitrage_margin_counts_only_the_grid_energy_of_a_charge():
    # Sun in the charge quarter: part of what the slot stores comes from the
    # sun. The margin is per kWh BOUGHT; counted on the sun too it made a
    # profitable grid top-up (0.10 + 0.10 margin, worth 0.30 x losses later)
    # look unprofitable, and the plan charged nothing.
    prices = [0.10] * 2 + [0.30] * 6
    req = _request(prices, house=[0.05] * 2 + [0.4] * 6, solar=[0.5] * 2 + [0.0] * 6, usable=0.0,
                   room=2.0, margin=0.10, power=5.0)
    assert optimize_charge(req).plan.allocations


def test_a_negative_price_charge_counts_only_its_grid_energy_as_zero():
    # Negative-price charging off (D3): what the battery buys at -0.05 counts
    # as 0. Counting the sun stored in the charge quarter as bought made a
    # free top-up for the evening look dear, and the plan charged nothing.
    prices = [-0.05] * 2 + [0.06] * 6
    req = _request(prices, house=[0.05] * 2 + [0.4] * 6, solar=[0.8] * 2 + [0.0] * 6, usable=0.0,
                   room=2.0, negative=False, power=5.0)
    assert optimize_charge(req).plan.allocations


# --- Smart Pre-Discharge in the optimizer: "make room" ----------------------
# (docs/plans/predescarga-en-el-optimizador.md). A risk quarter: sun above the
# house and an export price at or below the threshold (read from the export
# map; a slot missing from it is no risk). Before a risk the plan may export
# battery energy ("make room") down to L = full - the risk sun the battery can
# store + what the house drains before the risk, never below the reserve.

def _room_day(*, usable=9.0, morning=(0.30, 0.20), house_morning=0.1, solar_morning=0.0, risk_sun=1.0,
              risk=(-0.02, -0.05), evening=(0.30, 0.05), threshold=0.0, power_kw=2.5, floor_kwh=0.0,
              room_prices=None, n=8):
    """n morning quarters, n risk quarters, n evening quarters on a 10 kWh
    battery with 9 kWh usable (10 % floor), 2.5 kW both ways."""
    import dataclasses
    prices = [morning[0]] * n + [risk[0]] * n + [evening[0]] * n
    exports = [morning[1]] * n + [risk[1]] * n + [evening[1]] * n
    req = _request(prices, house=[house_morning] * n + [0.1] * n + [0.4] * n,
                   solar=[solar_morning] * n + [risk_sun] * n + [0.0] * n,
                   cap=10.0, soc=10.0 + usable * 10.0, usable=usable, room=9.0, power=2.5, discharge=2.5)
    starts = [s.start for s in req.price_slots]
    return dataclasses.replace(
        req, export_prices=dict(zip(starts, exports)), room_threshold=threshold, room_power_kw=power_kw,
        room_floor_kwh=floor_kwh,
        room_export_prices=dict(zip(starts, exports)) if room_prices is None else room_prices,
    )


def _storable_risk_ac(request):
    """The risk sun the battery could take, AC side, written apart."""
    exports = request.export_prices
    total = 0.0
    for i in request.intervals:
        s = next(s for s in request.price_slots if s.start <= i.start < s.end)
        if s.start in exports and exports[s.start] <= request.room_threshold and i.solar_kwh > i.grid_load_kwh:
            total += min(i.solar_kwh - i.grid_load_kwh, request.effective_power_kw * 0.25)
    return total


def _replay_room(request, plan):
    """The controller: slot quotas as in ``_replay``; a room order holds the
    grid at -kW until the battery reaches the order's level, then normal (the
    battery covers the house); sun beyond kW charges it. Returns (cost, AC
    exported in room slots, energy after each interval)."""
    m = BatteryModel(capacity_kwh=request.fleet_capacity_kwh, round_trip_efficiency=0.85,
                     floor_kwh=request.fleet_capacity_kwh * request.fleet_soc_pct / 100
                     - request.usable_initial_kwh,
                     usable_kwh=request.usable_initial_kwh + request.headroom_kwh,
                     soc_resolution_pct=request.soc_resolution_pct)
    step = request.fleet_capacity_kwh * request.soc_resolution_pct / 100
    quota = {a.slot: (a.planned_battery_kwh, a.to_slot_end) for a in plan.allocations}
    rooms = {s: (kw, level) for s, kw, level in plan.room_sales}
    e, total, target, done, prices, sold, path = request.usable_initial_kwh, 0.0, {}, set(), [], 0.0, []
    for i in request.intervals:
        s = next(s for s in request.price_slots if s.start <= i.start < s.end)
        prices.append(s.price)
        h = (i.end - i.start).total_seconds() / 3600
        net = i.grid_load_kwh - i.solar_kwh
        if s in rooms:
            kw, level = rooms[s]
            # Under the level the order is off: the sun surplus is stored up to it.
            fill = min(max(0.0, -net), request.effective_power_kw * h, max(0.0, level - e) / m.eff)
            e += fill * m.eff
            flow = net + fill + kw * h
            if flow >= 0:
                out = min(max(0.0, e - level) * m.eff, flow, request.discharge_kw * h)
                e -= out / m.eff
                extra = min(max(0.0, net - out), e * m.eff, request.discharge_kw * h - out)
                e -= extra / m.eff
                grid = net + fill - out - extra
            else:
                st = min(-flow, request.effective_power_kw * h - fill, (m.cap - e) / m.eff)
                e += st * m.eff
                grid = net + fill + st
            total += max(grid, 0) * s.price - max(-grid, 0) * request.room_export_prices[s.start]
            sold += max(-grid, 0)
            path.append(e)
            continue
        q = Quarter(i.grid_load_kwh, i.solar_kwh, s.price, (request.export_prices or {}).get(s.start, 0.0),
                    True, request.effective_power_kw * h, request.discharge_kw * h)
        t = None
        if s in quota and s not in done:
            if s not in target:
                kwh, to_end = quota[s]
                target[s] = float(m.slot_target(e, kwh + (step if to_end else 0.0)))
            t = target[s]
            if t - e <= m.charge_rate(q) + 1e-9:
                done.add(s)
        e1, c = m.normal_step(q, e, t)
        total += float(c)
        e = float(e1)
        path.append(e)
    end = min((p for p in prices if p > 0), default=0.0) * m.eff
    return total - end * e, sold, path


def test_a_full_battery_makes_room_before_a_negative_export_risk():
    import dataclasses
    req = _room_day()
    plan = optimize_charge(req).plan
    starts = {s.start for s, _kw, _l in plan.room_sales}
    assert starts and all(t < req.price_slots[8].start for t in starts)
    cost, _sold, _path = _replay_room(req, plan)
    assert cost == pytest.approx(plan.optimizer_cost, abs=1e-4)
    off = optimize_charge(dataclasses.replace(req, room_threshold=None)).plan
    assert plan.optimizer_cost < off.optimizer_cost - 0.01
    assert {s.start for s in plan.risk_slots} == {s.start for s in req.price_slots[8:16]}


def test_no_room_when_the_risk_sun_fits():
    req = _room_day(usable=4.0, risk_sun=0.4)
    assert not optimize_charge(req).plan.room_sales


def test_room_never_takes_the_battery_under_the_reserve():
    # The morning house is covered by its sun: only the room order moves the
    # battery. Reserve 7 kWh: it stops at 7.0 (80 %, on the whole percent).
    req = _room_day(house_morning=0.1, solar_morning=0.1, floor_kwh=7.0, risk_sun=2.0)
    plan = optimize_charge(req).plan
    assert plan.room_sales
    assert all(level == pytest.approx(7.0) for _s, _kw, level in plan.room_sales)
    _c, sold, path = _replay_room(req, plan)
    assert min(path[:8]) == pytest.approx(7.0, abs=1e-6)
    assert sold == pytest.approx(2.0 * math.sqrt(0.85), abs=1e-3)


def test_room_never_exports_more_than_the_risk_sun_that_would_not_fit():
    # Morning export 0.25 against a cheap end value: without the cap the
    # option would sell the whole battery (arbitrage with the sale off).
    req = _room_day(morning=(0.30, 0.25), evening=(0.22, 0.05), risk_sun=0.4)
    plan = optimize_charge(req).plan
    assert plan.room_sales
    _c, sold, _path = _replay_room(req, plan)
    assert sold <= _storable_risk_ac(req) + 1e-6


def test_the_cap_counts_what_the_house_drains_before_the_risk():
    # Full the evening before, dear export that evening, the risk tomorrow
    # morning: the night drains the battery by itself, so the room order
    # leaves that energy for the night (a cap without it sells it).
    import dataclasses
    n = 8
    prices = [0.30] * n + [0.20] * (2 * n) + [-0.02] * (2 * n)
    exports = [0.25] * n + [0.05] * (2 * n) + [-0.05] * (2 * n)
    house = [0.1] * n + [0.25] * (2 * n) + [0.1] * (2 * n)
    solar = [0.0] * (3 * n) + [1.0] * (2 * n)
    req = _request(prices, house=house, solar=solar, cap=10.0, soc=100.0, usable=9.0, room=9.0,
                   power=2.5, discharge=2.5)
    starts = [s.start for s in req.price_slots]
    req = dataclasses.replace(req, export_prices=dict(zip(starts, exports)), room_threshold=0.0,
                              room_power_kw=2.5, room_export_prices=dict(zip(starts, exports)))
    plan = optimize_charge(req).plan
    eff = math.sqrt(0.85)
    storable = _storable_risk_ac(req) * eff
    drain = sum(min(h - s, 2.5 * 0.25) / eff for h, s in zip(house[n:3 * n], solar[n:3 * n]))
    assert drain > 1.0
    assert plan.room_sales
    for _s, _kw, level in plan.room_sales:
        assert level >= 9.0 - storable + drain - 0.1 - 1e-6  # within one whole-percent step


def test_no_room_at_a_battery_price_at_or_below_the_threshold():
    req = _room_day(threshold=0.10, morning=(0.30, 0.08))
    assert not optimize_charge(req).plan.room_sales  # room there frees sun worth no more


def test_inside_the_risk_the_plan_neither_holds_nor_exports_nor_sells():
    import dataclasses
    req = _room_day(risk=(0.40, 0.30), threshold=0.35, room_prices={})
    req = dataclasses.replace(req, hold_allowed=True, export_choice_allowed=True, sell_allowed=True,
                              sell_power_kw=2.5, battery_export_prices=dict(req.export_prices))
    plan = optimize_charge(req).plan
    risk = {s.start for s in plan.risk_slots}
    assert risk
    assert not risk & {s.start for s in plan.holds}
    assert not risk & {s.start for s in plan.exports}
    assert not risk & {s.start for s, _kw in plan.sales}


def test_a_risk_quarter_needs_its_export_price():
    import dataclasses
    req = _room_day()
    missing = {t: p for t, p in req.export_prices.items() if t != req.price_slots[10].start}
    plan = optimize_charge(dataclasses.replace(req, export_prices=missing)).plan
    assert req.price_slots[10] not in plan.risk_slots
    assert req.price_slots[9] in plan.risk_slots


def test_risk_slots_do_not_charge_from_the_grid_except_for_the_floor():
    # Paid to charge inside the risk: the plan leaves the risk to the sun.
    req = _room_day(usable=1.0, risk=(-0.10, -0.12), risk_sun=0.3)
    plan = optimize_charge(req).plan
    risk = set(plan.risk_slots)
    assert risk and not risk & {a.slot for a in plan.allocations}


def _plan_key(plan):
    return (
        [(a.slot.start, round(a.planned_battery_kwh, 6), a.to_slot_end) for a in plan.allocations],
        [round(x, 6) for _t, x in plan.trajectory], round(plan.optimizer_cost, 9),
    )


def test_pre_discharge_off_leaves_the_plan_as_today():
    import dataclasses
    req = _room_day()
    today = dataclasses.replace(req, room_threshold=None, room_power_kw=0.0, room_floor_kwh=0.0,
                                room_export_prices=None)
    off = optimize_charge(dataclasses.replace(req, room_threshold=None)).plan
    assert _plan_key(off) == _plan_key(optimize_charge(today).plan)
    assert not off.room_sales and not off.risk_slots


def test_self_consumption_power_offers_no_room():
    import dataclasses
    req = _room_day(power_kw=0.0)
    plan = optimize_charge(req).plan
    assert not plan.room_sales
    assert _plan_key(plan) == _plan_key(optimize_charge(dataclasses.replace(req, room_export_prices=None)).plan)


def test_making_room_buys_no_more_grid_before_the_risk_than_without_it():
    # A cheap hour before a dear morning, battery half full: with the option
    # the plan must not fill at 0.05 to "make room" at 0.25.
    import dataclasses
    n = 8
    prices = [0.05] * 4 + [0.30] * n + [-0.02] * n + [0.22] * n
    exports = [0.02] * 4 + [0.25] * n + [-0.05] * n + [0.10] * n
    req = _request(prices, house=[0.075] * 4 + [0.075] * n + [0.1] * n + [0.15] * n,
                   solar=[0.0] * 4 + [0.075] * n + [0.35] * n + [0.05] * n,
                   cap=10.0, soc=80.0, usable=7.0, room=9.0, power=2.5, discharge=2.5)
    starts = [s.start for s in req.price_slots]
    req = dataclasses.replace(req, export_prices=dict(zip(starts, exports)), room_threshold=0.0,
                              room_power_kw=2.5, room_export_prices=dict(zip(starts, exports)))
    on = optimize_charge(req).plan
    off = optimize_charge(dataclasses.replace(req, room_threshold=None)).plan
    bought = lambda p: sum(a.planned_battery_kwh for a in p.allocations if a.slot.price > 0)
    assert bought(on) <= bought(off) + 1e-6


def test_a_negative_price_charge_before_the_risk_is_not_capped():
    # Paid to charge at night (switch on): the cap would leave that money on
    # the table (stage 0.3d); the night charge is the one the plan makes anyway.
    import dataclasses
    n = 8
    prices = [-0.10] * n + [0.30] * n + [-0.01] * n + [0.22] * n
    exports = [-0.12] * n + [0.25] * n + [-0.02] * n + [0.10] * n
    req = _request(prices, house=[0.075] * n + [0.075] * n + [0.1] * n + [0.15] * n,
                   solar=[0.0] * n + [0.075] * n + [0.35] * n + [0.0] * n,
                   cap=10.0, soc=55.0, usable=4.5, room=9.0, power=2.5, discharge=2.5)
    starts = [s.start for s in req.price_slots]
    req = dataclasses.replace(req, export_prices=dict(zip(starts, exports)), room_threshold=0.0,
                              room_power_kw=2.5, room_export_prices=dict(zip(starts, exports)))
    on = optimize_charge(req).plan
    off = optimize_charge(dataclasses.replace(req, room_threshold=None)).plan
    night = lambda p: sum(a.planned_battery_kwh for a in p.allocations if a.slot.start < starts[n])
    assert night(on) == pytest.approx(night(off), abs=0.05)
    assert on.optimizer_cost <= off.optimizer_cost + 1e-6


def _ledger_request(usable, risk_sun):
    """10 kWh, 10 % floor, 2.5 kW: 2 quarters at 0.30 (export 0.20), 2 risk
    quarters (import -0.02, export -0.05, sun ``risk_sun``), 1 evening quarter
    (house 0.5, import 0.30)."""
    import dataclasses
    prices = [0.30, 0.30, -0.02, -0.02, 0.30]
    exports = [0.20, 0.20, -0.05, -0.05, 0.05]
    req = _request(prices, house=[0.0, 0.0, 0.0, 0.0, 0.5], solar=[0.0, 0.0, risk_sun, risk_sun, 0.0],
                   cap=10.0, soc=10.0 + usable * 10.0, usable=usable, room=9.0, power=2.5, discharge=2.5)
    starts = [s.start for s in req.price_slots]
    return dataclasses.replace(req, export_prices=dict(zip(starts, exports)), room_threshold=0.0,
                               room_power_kw=2.5, room_export_prices=dict(zip(starts, exports)))


EFF_ = math.sqrt(0.85)
END_ = 0.30 * EFF_  # end value per stored kWh: the cheapest positive import x eff


@pytest.mark.parametrize(
    "usable, risk_sun, room, ends, cost",
    [
        # Full: L = 9 - 2 x 0.625 x eff = 7.848 -> 7.9 (whole percent up).
        # Room 1.1 kWh = 1.1 x eff AC sold at 0.20; the risk stores 1.1 back
        # (1.1 / eff AC), the rest of its 2.0 kWh of sun spills at -0.05; the
        # evening takes 0.5 / eff.
        (9.0, 1.0, 1.1 * EFF_, (7.9, 7.9, 7.9 + 0.625 * EFF_, 9.0, 9.0 - 0.5 / EFF_),
         -0.20 * 1.1 * EFF_ + 0.05 * (2.0 - 1.1 / EFF_) - END_ * (9.0 - 0.5 / EFF_)),
        # Half: L = 7.9 is above the battery: no room. The risk stores 0.625 AC
        # a quarter (its charge power) and spills 0.375 of each.
        (5.0, 1.0, 0.0, (5.0, 5.0, 5.0 + 0.625 * EFF_, 5.0 + 1.25 * EFF_, 5.0 + 1.25 * EFF_ - 0.5 / EFF_),
         0.05 * 0.75 - END_ * (5.0 + 1.25 * EFF_ - 0.5 / EFF_)),
        # Too little sun for the room it has: L = 9 - 0.6 x eff = 8.447 -> 8.5,
        # above the 8.0 stored: no room, nothing spills.
        (8.0, 0.3, 0.0, (8.0, 8.0, 8.0 + 0.3 * EFF_, 8.0 + 0.6 * EFF_, 8.0 + 0.6 * EFF_ - 0.5 / EFF_),
         -END_ * (8.0 + 0.6 * EFF_ - 0.5 / EFF_)),
    ],
    ids=["full", "half", "sun-fits"],
)
def test_the_room_ledger_by_hand(usable, risk_sun, room, ends, cost):
    req = _ledger_request(usable, risk_sun)
    plan = optimize_charge(req).plan
    got, sold, path = _replay_room(req, plan)
    assert sold == pytest.approx(room, abs=1e-6)
    assert path[1:] == pytest.approx(list(ends[1:]), abs=1e-6)
    assert plan.optimizer_cost == pytest.approx(cost, abs=1e-6)
    assert got == pytest.approx(cost, abs=1e-6)
    # Every kWh: sun in = stored + spilled; the house is the battery's.
    spilled = 2 * risk_sun - (ends[3] - ends[1]) / EFF_
    assert spilled == pytest.approx(2 * risk_sun - min(2 * risk_sun, 1.25, (9.0 - ends[1]) / EFF_), abs=1e-6)


def test_the_room_is_the_best_a_search_finds():
    """Short version of barrido/opt_precision_room.py: random small nights
    against every room order per slot, the cap computed apart (next risk
    day's storable sun, the house drain up to it, the reserve, whole percent
    up). No grid charge (ceiling -1)."""
    import dataclasses

    rng = random.Random(7)
    eff = math.sqrt(0.85)
    for _night in range(15):
        n_pre, n_risk, n_post = rng.choice((3, 4)), rng.choice((3, 4)), rng.choice((2, 3))
        n = n_pre + n_risk + n_post
        prices = ([rng.choice((0.10, 0.20, 0.30)) for _ in range(n_pre)]
                  + [rng.choice((-0.05, -0.02)) for _ in range(n_risk)]
                  + [rng.choice((0.20, 0.30)) for _ in range(n_post)])
        exports = ([rng.choice((0.0, 0.05, 0.10, 0.25)) for _ in range(n_pre)]
                   + [rng.choice((-0.08, -0.05, -0.01)) for _ in range(n_risk)] + [0.05] * n_post)
        house = [rng.choice((0.0, 0.05, 0.1, 0.2)) for _ in range(n)]
        solar = [0.0] * n_pre + [rng.choice((0.4, 0.8, 1.2)) for _ in range(n_risk)] + [0.0] * n_post
        usable = rng.choice((2.0, 3.5, 4.0, 4.5))
        req = _request(prices, house=house, solar=solar, cap=5.0, soc=(0.5 + usable) * 20.0, usable=usable,
                       room=4.5, power=2.5, discharge=2.5, threshold=-1.0)
        starts = [s.start for s in req.price_slots]
        req = dataclasses.replace(req, export_prices=dict(zip(starts, exports)), room_threshold=0.0,
                                  room_power_kw=2.5, room_floor_kwh=rng.choice((0.0, 1.5)),
                                  room_export_prices=dict(zip(starts, exports)))
        storable = sum(min(s - h, 0.625) * eff for s, h in zip(solar, house) if s > h)
        levels = {}
        for k in range(n_pre):
            drain = sum(min(h - s, 0.625) / eff for s, h in zip(solar[k + 1:n_pre], house[k + 1:n_pre]) if h > s)
            level = min(4.5, max(req.room_floor_kwh, 4.5 - storable + drain))
            levels[req.price_slots[k]] = math.ceil((0.5 + level) / 0.05 - 1e-6) * 0.05 - 0.5
        plan = optimize_charge(req).plan
        assert {s: l for s, _kw, l in plan.room_sales} == pytest.approx(
            {s: levels[s] for s, _kw, _l in plan.room_sales}, abs=1e-9)
        best = math.inf
        offered = [s for s in req.price_slots[:n_pre] if req.room_export_prices[s.start] > 0.0]
        for combo in itertools.product((None, 2.5, 1.25), repeat=len(offered)):
            fake = type("P", (), {})()
            fake.allocations = []
            fake.room_sales = [(s, kw, levels[s]) for s, kw in zip(offered, combo) if kw]
            best = min(best, _replay_room(req, fake)[0])
        got = _replay_room(req, plan)[0]
        assert got == pytest.approx(plan.optimizer_cost, abs=1e-4)
        assert got <= best + 0.0007


def test_under_its_level_a_room_slot_stores_the_sun_as_the_controller_does():
    # Battery under the room level with a morning surplus: the order is off
    # there (the reading is under its stop), so the surplus is stored up to
    # the level; the plan exported it and cost less than what ran.
    req = _room_day(usable=1.0, house_morning=0.1, solar_morning=0.6, morning=(0.30, 0.25), risk_sun=2.0)
    plan = optimize_charge(req).plan
    assert plan.room_sales
    cost, _sold, _path = _replay_room(req, plan)
    assert cost == pytest.approx(plan.optimizer_cost, abs=1e-4)


# --- Weekly Full Charge (step 4) --------------------------------------------

def _weekly(request, full_by_quarter, band_kwh=0.0, band_kw=0.0):
    import dataclasses
    return dataclasses.replace(request, full_by=T0 + full_by_quarter * Q,
                               charge_curve=((band_kwh, band_kw),) if band_kwh else ())


def _reach(plan, cap):
    """When the plan touches the top (it may touch it inside a quarter and
    cover the house after: the quarter's end is below it)."""
    assert plan.weekly_full_at is not None, "never full"
    return plan.weekly_full_at


def test_a_weekly_charge_touches_the_top_by_its_planned_deadline():
    # Flat prices: nothing pays, so without the weekly charge nothing is
    # bought. With it the 2.5 kWh to the top are bought by full_by - 0.5 h.
    request = _request([0.20] * 24, usable=0.5, room=3.0)
    assert not optimize_charge(request).plan.allocations
    plan = optimize_charge(_weekly(request, 24)).plan
    assert plan.allocations
    assert _reach(plan, 3.0) <= T0 + 22 * Q


def test_no_obligation_while_the_deadline_is_past_the_published_prices():
    # D-3: the 00:05 run of a provider that publishes tomorrow at 13:00.
    request = _request([0.20] * 24, usable=0.5, room=3.0)
    assert not optimize_charge(_weekly(request, 30)).plan.allocations


def test_the_sun_fills_the_top_and_nothing_is_bought():
    sun = [0.0] * 4 + [1.0] * 8 + [0.0] * 12
    request = _request([0.20] * 24, usable=0.5, room=3.0, solar=sun)
    plan = optimize_charge(_weekly(request, 24)).plan
    assert not plan.allocations and _reach(plan, 3.0) <= T0 + 12 * Q


def test_once_full_the_battery_is_free_again():
    # D-1: touched by the sun at midday, the battery covers the dear evening
    # and is not held or bought back to be full again at the deadline.
    sun = [0.0] * 4 + [1.0] * 8 + [0.0] * 12
    prices = [0.20] * 12 + [0.30] * 8 + [0.10] * 4
    request = _request(prices, usable=0.5, room=3.0, solar=sun)
    plan = optimize_charge(_weekly(request, 24)).plan
    reach = _reach(plan, 3.0)
    assert reach <= T0 + 12 * Q
    assert not [s for s in plan.weekly_holds if s.start >= reach]
    assert not [a for a in plan.allocations if a.slot.start >= reach]
    assert plan.trajectory[-1][1] < 3.0 - 0.5


def test_a_pending_weekly_charge_holds_without_the_permission():
    # D-5: a flat day, 2 of 3 kWh stored. Covering the house and buying it
    # back loses the round trip: the weekly layer holds, hold_allowed off.
    request = _request([0.20] * 24, usable=2.0, room=3.0)
    assert not request.hold_allowed
    plan = optimize_charge(_weekly(request, 24)).plan
    assert plan.weekly_holds and not plan.holds
    assert _reach(plan, 3.0) <= T0 + 22 * Q


def test_the_top_band_charges_slower():
    # The last 0.5 kWh at 0.5 kW: no quarter adds more than 0.5 x 0.25 x eff there.
    request = _weekly(_request([0.20] * 24, usable=0.5, room=3.0), 24, band_kwh=0.5, band_kw=0.5)
    plan = optimize_charge(request).plan
    band = 0.5 * 0.25 * math.sqrt(0.85)
    previous = 0.5
    for _t, e in plan.trajectory:
        if previous >= 3.0 - 0.5:
            assert e - previous <= band + 1e-6
        previous = e
    assert _reach(plan, 3.0) <= T0 + 22 * Q


def test_a_missed_planned_deadline_keeps_charging_to_the_window_end():
    # 2.5 kWh at 0.576 a quarter need 5 quarters: full_by - 0.5 h (quarter 4)
    # is too early, the window's end (quarter 6) is not.
    request = _request([0.20] * 24, usable=0.5, room=3.0, house=0.0)
    plan = optimize_charge(_weekly(request, 6)).plan
    assert _reach(plan, 3.0) <= T0 + 6 * Q


def test_a_long_weekly_run_plans_a_quarter_of_its_time_as_margin():
    # 8.5 kWh at 0.576 a quarter: 3.7 h, a quarter of it 0.92 h. The cheapest
    # quarters are the window's last, and the run still ends an hour before
    # it: half an hour left a run slowed by a third (a hot battery under the
    # temperature limit) no time to finish.
    prices = [0.40 - 0.005 * k for k in range(32)]
    request = _request(prices, usable=0.5, room=9.0, house=0.0)
    plan = optimize_charge(_weekly(request, 32)).plan
    assert T0 + 27 * Q < _reach(plan, 9.0) <= T0 + 28 * Q


def test_a_short_weekly_run_keeps_half_an_hour_of_margin():
    # 2.5 kWh: 1.1 h, a quarter of it 16 minutes: the half hour stays.
    prices = [0.40 - 0.005 * k for k in range(32)]
    request = _request(prices, usable=0.5, room=3.0, house=0.0)
    plan = optimize_charge(_weekly(request, 32)).plan
    assert T0 + 29 * Q < _reach(plan, 3.0) <= T0 + 30 * Q


def test_a_pending_weekly_charge_keeps_its_quotas(monkeypatch):
    # The start cost is off while a weekly charge is pending: the controller
    # charges such a plan's "to the end" slots to 100 %, so marking one more
    # is not a tie (#899's tie-break there paid 1 EUR more over the weekly
    # sweep's grid).
    from custom_components.omnibattery.pricing import optimizer

    request = _weekly(_request([0.10] * 4 + [0.40] * 4, house=[0.05] * 16 + [0.5] * 16,
                               slot_quarters=4, cap=8.16, soc=60.0, usable=4.3, room=7.3,
                               power=2.4, discharge=2.4), 24)
    plan = optimize_charge(request).plan
    monkeypatch.setattr(optimizer, "START_COST_EUR", 0.0)
    off = optimize_charge(request).plan
    assert [(a.slot, a.planned_battery_kwh, a.to_slot_end) for a in plan.allocations] == [
        (a.slot, a.planned_battery_kwh, a.to_slot_end) for a in off.allocations]


def test_a_pending_weekly_charge_neither_sells_nor_exports():
    prices = [0.20] * 10 + [0.60] * 4 + [0.20] * 10
    request = _with_sale(_request(prices, house=0.05, usable=2.5, room=3.0), [0.5] * 24)
    assert optimize_charge(request).plan.sales
    plan = optimize_charge(_weekly(request, 24)).plan
    reach = _reach(plan, 3.0)
    assert not [s for s, _kw in plan.sales if reach is None or s.start < reach]
