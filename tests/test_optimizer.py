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


def _replay(request, plan, held=(), exported=()):
    """Cost of executing the published plan, as the controller does it. In a
    ``held`` slot the battery covers nothing (solar surplus still stores)."""
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


def _replay_hold(request, plan):
    """``_replay`` with the plan's held slots: the battery covers nothing then."""
    return _replay(request, plan, held=set(plan.holds))


def _with_hold(request, min_saving=0.0):
    import dataclasses
    return dataclasses.replace(request, hold_allowed=True, hold_min_saving=min_saving)


def test_holding_keeps_the_battery_for_the_dearer_hour():
    # 1 kWh stored, no cheap slot: the 0.40 evening is worth it, the 0.20 now is not.
    prices = [0.20] * 4 + [0.40] * 4
    request = _with_hold(_request(prices, house=0.25, usable=0.95, threshold=0.0))
    plan = optimize_charge(request).plan
    assert {slot.price for slot in plan.holds} == {0.20}
    assert _replay_hold(request, plan) == pytest.approx(plan.optimizer_cost, abs=1e-4)
    off = optimize_charge(_request(prices, house=0.25, usable=0.95, threshold=0.0)).plan
    assert plan.optimizer_cost < off.optimizer_cost - 0.03


def test_the_minimum_saving_decides_a_hold():
    prices = [0.20] * 4 + [0.25] * 4
    keep = optimize_charge(_with_hold(_request(prices, house=0.25, usable=0.95, threshold=0.0))).plan
    assert keep.holds
    skip = optimize_charge(
        _with_hold(_request(prices, house=0.25, usable=0.95, threshold=0.0), min_saving=0.10)
    ).plan
    assert not skip.holds


def test_no_hold_without_the_permission():
    prices = [0.20] * 4 + [0.40] * 4
    plan = optimize_charge(_request(prices, house=0.25, usable=0.95, threshold=0.0)).plan
    assert not plan.holds


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
