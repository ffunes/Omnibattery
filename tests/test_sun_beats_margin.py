"""#958: the plan books its grid charges on the sun after the safety margin; a
day whose real sun beats that figure must not buy what the sun has already put
in the battery.

The reporter's day (2026-11-27, 2.0TD, 10.24 kWh, hourly prices; house and sun
read off his chart): planned at 11:15 on the margined sun, the optimizer books
the llano charge at 14:00-15:00 and the slot buys its quota from the SOC it
starts at. The world here delivers the sun times ``world``; the plan believes
``BELIEF``. The engine's above-the-line trigger, which now counts the margin's
sun within the hour before a grid charge, is simulated: at each hour from
noon, if the SOC is 3 points over the plan's line and a charge starts within
the hour, the plan is rebuilt from the real SOC. The trigger's own code is
covered in ``test_soc_deviation_trigger``; this checks what the re-plan buys.
"""
from __future__ import annotations

import dataclasses
import math
from datetime import datetime, timedelta

import pytest

from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing import optimizer as opt
from custom_components.omnibattery.pricing.battery_model import BatteryModel
from custom_components.omnibattery.pricing.engine import (
    SOC_DROP_BELOW_PROJECTION,
    SOC_RISE_PRE_SLOT,
)
from custom_components.omnibattery.pricing.chronological import (
    ChronologicalEvaluationRequest,
    EnergyInterval,
)

Q = timedelta(minutes=15)
NOW = datetime(2026, 11, 27, 11, 15)
END = datetime(2026, 11, 29)
CAP, FLOOR, RTE = 10.24, 2.048, 0.7644
USABLE = CAP - FLOOR
BELIEF = 0.64  # his 36 % measured margin
MODEL = BatteryModel(capacity_kwh=CAP, round_trip_efficiency=RTE, floor_kwh=FLOOR,
                     usable_kwh=USABLE, soc_resolution_pct=1.0)
# kWh per quarter on the 27th from 11:15, read off his chart; 0.3 after.
HOUSE = [.38] * 6 + [.3, .42, .6, .45, .3, .27, .37, .3, .32, .32, .24, .22, .25, .28, .3, .2,
                     .18, .18, .18, .18, .18, .18, .2, .25, .3, .3, .3, .31, .29, .27, .3, .28,
                     .27, .35, .33, .2, .2, .25, .23, .22, .22, .22, .23, .24, .25]
SUN = [.62, .62, .67, .68, .68, .69, .69, .9, .9, .88, .88, .75, .75, .65, .65, .6, .6, .47, .47,
       .3, .3, .15, .15, .03, .03]  # raw forecast, 11:15-17:30


def _price(t):
    if t.day == 28 or t.hour < 8:
        return 0.079
    return 0.195 if 10 <= t.hour < 14 or 18 <= t.hour < 22 else 0.116


def _request(sun):
    qs = [NOW + i * Q for i in range(int((END - NOW) / Q))]
    today = [q for q in qs if q.day == 27]
    # Tomorrow: the rest of the 36.86 kWh he expected, flat; 12.6 kWh of sun.
    house = HOUSE + [0.3] * (len(today) - len(HOUSE))
    rest = (36.86 - sum(house)) / (len(qs) - len(today))
    tomorrow = [math.sin(math.pi * (q.hour + q.minute / 60 + .125 - 8.25) / 9.15) ** 2
                if 8.25 < q.hour + q.minute / 60 + .125 < 17.4 else 0.0 for q in qs[len(today):]]
    intervals = tuple(
        EnergyInterval(q, q + Q, house[i] if i < len(today) else rest,
                       sun * (SUN[i] if i < len(SUN) else 0.0) if i < len(today)
                       else 12.6 * tomorrow[i - len(today)] / sum(tomorrow))
        for i, q in enumerate(qs))
    slots = tuple(PriceSlot(t, t + timedelta(hours=1), _price(t))
                  for t in (NOW.replace(minute=0) + timedelta(hours=h)
                            for h in range(int((END - NOW.replace(minute=0)) / timedelta(hours=1)))))
    e = 4.1984 - FLOOR
    return ChronologicalEvaluationRequest(
        now=NOW, horizon_end=END, intervals=intervals, price_slots=slots, total_required_kwh=0.0,
        effective_power_kw=2.5, headroom_kwh=USABLE - e, usable_initial_kwh=e,
        import_value_efficiency=RTE, grid_ceiling_kw=2.5, fleet_capacity_kwh=CAP,
        fleet_soc_pct=4.1984 / CAP * 100, soc_resolution_pct=1.0, discharge_kw=5.0,
        hold_allowed=True, export_prices={s.start: 0.06 for s in slots})


def _at(request, now, e, plan):
    return dataclasses.replace(
        request, now=now, usable_initial_kwh=e, headroom_kwh=USABLE - e,
        fleet_soc_pct=(FLOOR + e) / CAP * 100,
        holding_now=any(s.start <= now < s.end for s in plan.holds),
        hold_start_unused=any(s.start <= now < s.end for s in plan.paid_holds))


def _execute(plan, world, now, e, stop):
    """Run ``plan`` in ``world`` from ``now`` until ``stop``: (energy, cost,
    grid kWh bought to charge), as the controller does: a slot's target is set
    once from the SOC it starts at; a held slot covers nothing."""
    quota = {a.slot.start: a for a in plan.allocations}
    holds = {s.start for s in plan.holds}
    cost = bought = 0.0
    for slot, quarters, ends in opt._slot_groups(dataclasses.replace(world, now=now)):
        a = quota.get(slot.start)
        target = None if a is None else float(MODEL.slot_target(
            e, a.planned_battery_kwh + (CAP / 100 if a.to_slot_end else 0.0)))
        for q, end in zip(quarters, ends):
            if end > stop:
                return e, cost, bought
            if target is not None:
                add, busy = MODEL.charge_phase(q, e, target)
                bought += max(float(add) / MODEL.eff
                              - max(0.0, q.solar_kwh - q.house_kwh) * float(busy), 0.0)
            step = dataclasses.replace(q, discharge_kwh=0.0) if slot.start in holds else q
            e1, c = MODEL.normal_step(step, e, target)
            if target is not None and target - e <= MODEL.charge_rate(q) + 1e-9:
                target = None  # reached: the controller completes the slot
            e, cost = float(e1), cost + float(c)
    return e, cost, bought


def _day(world, *, replan):
    belief, real = _request(BELIEF), _request(BELIEF * world)
    plan = opt.optimize_charge(belief).plan
    now, e, bought = NOW, belief.usable_initial_kwh, 0.0
    cost = 0.0
    for hour in range(12, 18):  # ponytail: hourly checks hold while the window is an hour
        stop = NOW.replace(hour=hour, minute=0)
        e, c, b = _execute(plan, real, now, e, stop)
        cost, bought, now = cost + c, bought + b, stop
        line = dict(plan.trajectory).get(now)
        soon = any(now < a.slot.start <= now + SOC_RISE_PRE_SLOT for a in plan.allocations)
        if (replan and soon and line is not None
                and (e - line) / CAP * 100 >= SOC_DROP_BELOW_PROJECTION):
            plan = opt.optimize_charge(_at(belief, now, e, plan)).plan
    e, c, b = _execute(plan, real, now, e, END)
    # What is left at the end is worth the night's valley price.
    return bought + b, cost + c - 0.079 * MODEL.eff * e


@pytest.fixture(scope="module")
def plan_at_11():
    return opt.optimize_charge(_request(BELIEF)).plan


def test_the_reporter_s_day_books_an_early_llano_charge(plan_at_11):
    hours = [a.slot.start.hour for a in plan_at_11.allocations if a.slot.start.day == 27]
    assert hours and all(h in (14, 15) for h in hours)


@pytest.mark.parametrize("world", [1.0 / BELIEF, 0.9 / BELIEF, 0.8 / BELIEF, 0.7 / BELIEF])
def test_sun_above_the_margin_buys_less_and_pays_less_after_the_pre_slot_replan(world):
    (bought, cost), (bought_a, cost_a) = _day(world, replan=False), _day(world, replan=True)
    assert bought_a < bought and cost_a < cost
    if world >= 0.8 / BELIEF:  # measured: 0.77-1.20 kWh -> 0-0.06; at 0.7 x raw 1.30 -> 0.84
        assert bought_a < 0.1 * bought


@pytest.mark.parametrize("world", [1.0, 0.3 / BELIEF])
def test_sun_on_or_below_the_margin_is_unchanged(world):
    assert _day(world, replan=True) == pytest.approx(_day(world, replan=False))
