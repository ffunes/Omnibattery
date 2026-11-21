"""Invariant sweep for the Dynamic Pricing charge planner.

A grid of generic nights (fleet, SOC, house load, price shape, tomorrow's
prices or not, sun, evaluation time) runs through the scenario bank's engine
harness, and each run is held to rules that must hold whatever the inputs:

* I1 more load never books less: the same night with 30 % more load. For a
  plan of the 2.0.0 optimizer (D8): with prices >= 0, more load never makes
  the plan cheaper (it may book less: the load above the discharge power
  goes to the grid, the contracted power leaves less for the charge). Its
  cost counts each charge and hold start at its start cost, as the plan decides.
* I4 the published planned charge equals what the schedule books plus what
  the extended horizon defers to tomorrow.
* I5 stable: re-evaluating at the next quarter after one hour, with the SOC
  the plan itself projects, keeps the plan for what is left.
* I6 nothing is booked with no room when its slot charges (the controller
  charges a slot until its amount is stored, the grid carrying the house
  meanwhile, and the battery covers the house again after that).
* I7 holding never makes the plan dearer (#888): the optimizer's objective
  (cost, charge and hold starts) with holds allowed is never above the same
  request's without them, within HOLD_EPS (the lattice's interpolation).

A finding listed in ``tests/sweep_known.json`` (key ``"<rule> <case>"``, value
the reason) is expected; a listed one that no longer shows fails too, so the
list only shrinks by fixing. Not part of the normal run (``pytest.ini``
deselects the ``sweep`` marker); run it with::

    pytest -m sweep -n auto

``OMNI_SWEEP_RECORD=<dir>`` writes every finding to ``<dir>`` instead of
failing, to (re)build the known list. ``OMNI_SWEEP_SAMPLE=<n>`` runs one case
in ``n`` (the pre-merge hook uses 11).
"""
from __future__ import annotations

import dataclasses
import functools
import itertools
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.const import CHARGE_EFFICIENCY
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.optimizer import (
    HOLD_START_EUR,
    START_COST_EUR,
    optimize_charge,
)
from custom_components.omnibattery.solar_forecast import SolarForecastPeriod
from tests import test_predictive_scenarios as h

pytestmark = pytest.mark.sweep

KNOWN = json.loads((Path(__file__).parent / "sweep_known.json").read_text(encoding="utf-8"))
TZ = "Europe/Madrid"
DAY = datetime(2026, 11, 10)
SUNRISE, NOON = 8.0, 13.25
LOAD_SCALE = 1.3
EPS = 0.05
COST_EPS = 0.003  # the optimizer's precision against exhaustive search (<= 0.2 c)
HOLD_EPS = 5e-4  # I7: at most 1.4e-4 (interpolation) on the 1224 NL plans of #888

FLEETS = {
    "1x5k-2500": [dict(capacity_kwh=5.12, max_charge_w=2500, max_discharge_w=2500)],
    "1x7k-800": [dict(capacity_kwh=7.0, max_charge_w=2500, max_discharge_w=800)],
    "1x10k-800": [dict(capacity_kwh=10.0, max_charge_w=2500, max_discharge_w=800)],
    "2x5k-2500": [dict(capacity_kwh=5.12, max_charge_w=2500, max_discharge_w=2500)] * 2,
}
SOCS = (15, 60)
LOADS = {  # (from HH:MM, kW) steps
    "flat0.4": [("00:00", 0.4)],
    "flat1.2": [("00:00", 1.2)],
    "evening": [("00:00", 0.35), ("07:00", 0.8), ("09:00", 0.5), ("18:00", 1.6), ("23:00", 0.4)],
}
PRICES = {  # hour -> price, the same every day
    "night-cheap": lambda hr: 0.10 if hr < 7 else (0.30 if 18 <= hr < 22 else 0.20),
    "falling-night": lambda hr: 0.30 - 0.01 * hr if hr < 8 else (0.32 if 18 <= hr < 22 else 0.22),
    "solar-dip": lambda hr: 0.18 if hr < 7 else (0.08 if 12 <= hr < 16 else (0.28 if 19 <= hr < 22 else 0.20)),
}
SOLAR = {"none": 0.0, "dull": 3.0, "sunny": 15.0}
NOWS = ("00:05", "03:05", "16:00", "22:40")

CASES = list(itertools.product(FLEETS, SOCS, LOADS, PRICES, (False, True), SOLAR, NOWS))


def case_id(case) -> str:
    fleet, soc, load, prices, tomorrow, solar, now = case
    return f"{fleet}-soc{soc}-{load}-{prices}-{'tomorrow' if tomorrow else 'today'}-{solar}-{now.replace(':', '')}"


def scenario(case, *, scale=1.0, soc=None, now=None) -> h.Scenario:
    fleet, soc0, load, prices, tomorrow, solar, now0 = case
    hh, mm = map(int, (now or now0).split(":"))
    instant = DAY.replace(hour=hh, minute=mm)
    zone = ZoneInfo(TZ)
    days = [DAY, DAY + timedelta(days=1)] if tomorrow else [DAY]
    slots = tuple(
        PriceSlot(start, start + timedelta(minutes=15), PRICES[prices](hour))
        for day in days
        for hour in range(24)
        for start in (day + timedelta(hours=hour, minutes=15 * q) for q in range(4))
    )
    steps = [(h._hours(at), kw) for at, kw in LOADS[load]]
    bins = tuple(
        [kw for at, kw in steps if at <= i / 4.0][-1] * 0.25 * scale for i in range(96)
    )
    periods = []
    if SOLAR[solar]:
        hours = range(9, 18)
        weights = [min(hr - 8, 18 - hr) for hr in hours]
        for day in days:
            periods += [
                SolarForecastPeriod(
                    (day + timedelta(hours=hr)).replace(tzinfo=zone),
                    (day + timedelta(hours=hr + 1)).replace(tzinfo=zone),
                    SOLAR[solar] * w / sum(weights),
                )
                for hr, w in zip(hours, weights)
            ]
    aware = instant.replace(tzinfo=zone)
    remaining = sum(
        p.energy_kwh * max(0.0, min(1.0, (p.end - aware).total_seconds() / 3600))
        for p in periods
        if p.start.date() == DAY.date()
    )
    batteries = tuple(
        dict(b, soc=soc0 if soc is None else soc, min_soc=10, max_soc=100)
        for b in FLEETS[fleet]
    )
    return h.Scenario(
        name=case_id(case), level="engine", now=instant, time_zone=TZ,
        sunrise_h=SUNRISE, solar_noon_h=NOON, slots=slots, bins_kwh=bins,
        solar_remaining_kwh=remaining, solar_periods=tuple(periods), fleet=batteries,
        settings=dict(
            max_price_threshold=1.0, min_arbitrage_margin=None, round_trip_efficiency=0.85,
            peak_shaving=None, contracted_power_w=5750,
            guaranteed_floor_soc=None, safety_margin_pct=0.0,
        ),
        expected={},
    )


def run(sc: h.Scenario, *, daily=False):
    """One evaluation: the outcome, the decision data and the full plan."""
    box = {}
    controller = h._controller

    def capture(s):
        box["c"] = controller(s)
        if daily:
            # The scheduled 00:05 run reads the configured remaining-today
            # sensor; without it production takes the legacy path.
            box["c"].config_entry.data["solar_forecast_remaining_sensor"] = "sensor.sweep"
        return box["c"]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(h, "_controller", capture)
        if daily:
            mp.setattr(h, "DynamicPricingEvaluationHorizon", SimpleNamespace(
                REMAINING=h.DynamicPricingEvaluationHorizon.DAILY))
        outcome = h._run_engine(sc, mp)
    plan = outcome.plan
    return SimpleNamespace(o=outcome, d=box["c"]._last_decision_data or {}, plan=plan)


def total(r) -> float:
    """Booked now plus what the extended horizon defers to tomorrow's slots."""
    return sum(k for _s, k in r.o.booked) + float(r.d.get("deferred_grid_charge_kwh") or 0.0)


def _energy_at(p, at, start_kwh) -> float:
    """Usable energy the 2.0.0 optimizer projects at ``at`` (its trajectory)."""
    energy = start_kwh
    for end, value in p.trajectory:
        if end > at:
            break
        energy = value
    return energy


def spill(r) -> float:
    q, p = r.o.request, r.plan
    if getattr(p, "trajectory", None):
        # The optimizer's own model: a slot books at most the room it has then.
        cap = q.usable_initial_kwh + q.headroom_kwh
        return sum(
            max(0.0, a.planned_battery_kwh - (cap - _energy_at(p, a.slot.start, q.usable_initial_kwh)))
            for a in p.allocations
        )
    rate = q.effective_power_kw * CHARGE_EFFICIENCY
    cap = q.usable_initial_kwh + q.headroom_kwh
    energy, lost = q.usable_initial_kwh, 0.0
    for i in sorted(q.intervals, key=lambda x: x.start):
        hours = (i.end - i.start).total_seconds() / 3600.0
        add = sum(a.planned_battery_kwh for a in p.allocations if a.slot.start <= i.start < a.slot.end)
        busy = min(1.0, add / (rate * hours)) if add and rate > 0 else 0.0
        if add:
            lost += max(0.0, min(add, energy + add - cap))
            energy = min(cap, energy + add)
        flow = i.solar_kwh - i.consumption_kwh
        if flow < 0:
            flow *= 1.0 - busy
        energy = max(0.0, min(cap, energy + flow))
    return lost


def projected_soc(sc, r, at) -> float:
    """SOC at ``at`` from the plan: no drain while a slot charges."""
    q, p = r.o.request, r.plan
    cap = q.usable_initial_kwh + q.headroom_kwh
    energy = q.usable_initial_kwh
    if getattr(p, "trajectory", None):
        energy = _energy_at(p, at, energy)
        return min(100.0, sc.fleet[0]["min_soc"] + max(0.0, energy) / sum(b["capacity_kwh"] for b in sc.fleet) * 100.0)
    for i in sorted(q.intervals, key=lambda x: x.start):
        if i.start >= at:
            break
        for a in p.allocations:
            if a.slot.start <= i.start < a.slot.end and a.slot.end <= i.end:
                energy += a.planned_battery_kwh
        if not any(a.slot.start <= i.start < a.slot.end for a in p.allocations):
            energy = max(0.0, energy - i.consumption_kwh) + i.solar_kwh
        energy = min(cap, energy)
    return min(100.0, sc.fleet[0]["min_soc"] + max(0.0, energy) / sum(b["capacity_kwh"] for b in sc.fleet) * 100.0)


def check(case_key: str, findings: list[tuple[str, str]]) -> None:
    record = os.environ.get("OMNI_SWEEP_RECORD")
    if record:
        out = Path(record) / f"{os.environ.get('PYTEST_XDIST_WORKER', 'main')}.jsonl"
        with out.open("a", encoding="utf-8") as fh:
            for rule, message in findings:
                fh.write(json.dumps({"key": f"{rule} {case_key}", "message": message}) + "\n")
        return
    found = {f"{rule} {case_key}": message for rule, message in findings}
    expected = {key for key in KNOWN if key.split(" ", 1)[1] == case_key}
    new = {key: msg for key, msg in found.items() if key not in KNOWN}
    gone = sorted(expected - set(found))
    assert not new and not gone, (
        "".join(f"\nNEW {key}: {msg}" for key, msg in new.items())
        + "".join(f"\nFIXED, remove from sweep_known.json: {key}" for key in gone)
    )


@functools.lru_cache(maxsize=1)
def base_run(case, daily):
    """The case's own evaluation, shared by evaluate_pair and evaluate_stability
    (each test ran it twice: a quarter of the sweep's plans)."""
    return run(scenario(case), daily=daily)


def objective(plan) -> float:
    """What the optimizer minimises, without the margin and floor terms the
    sweep leaves at 0."""
    return (plan.optimizer_cost + START_COST_EUR * plan.charge_starts
            + HOLD_START_EUR * plan.hold_starts)


def evaluate_hold(r) -> list[tuple[str, str]]:
    q = r.o.request
    if getattr(r.plan, "optimizer_cost", None) is None or not q.hold_allowed:
        return []
    without = optimize_charge(dataclasses.replace(q, hold_allowed=False)).plan
    held, plain = objective(r.plan), objective(without)
    if held > plain + HOLD_EPS:
        return [("I7", f"holding costs {held:.4f}, without holds {plain:.4f}")]
    return []


def evaluate_pair(case, *, daily=False) -> list[tuple[str, str]]:
    findings = []
    runs = {1.0: base_run(case, daily), LOAD_SCALE: run(scenario(case, scale=LOAD_SCALE), daily=daily)}
    low, high = runs[1.0], runs[LOAD_SCALE]
    costs = [getattr(r.plan, "optimizer_cost", None) for r in (low, high)]
    if None not in costs:
        costs = [c + START_COST_EUR * r.plan.charge_starts + HOLD_START_EUR * r.plan.hold_starts
                 for c, r in zip(costs, (low, high))]
        q = low.o.request
        priced = [s.price for s in q.price_slots if s.end > q.now and s.start < q.horizon_end]
        if min(priced, default=0.0) >= 0 and costs[1] + COST_EPS < costs[0]:
            findings.append(("I1", f"x1.0 costs {costs[0]:.4f}, x{LOAD_SCALE} costs {costs[1]:.4f}"))
    elif total(high) + EPS < total(low):
        findings.append(("I1", f"x1.0 books {total(low):.2f} ({low.o.reason}), x{LOAD_SCALE} books {total(high):.2f} ({high.o.reason})"))
    for scale, r in runs.items():
        published = float(r.d.get("planned_grid_charge_kwh") or 0.0)
        if abs(published - total(r)) > EPS:
            findings.append((f"I4x{scale}", f"published {published:.2f} vs booked+deferred {total(r):.2f}"))
        lost = spill(r)
        if lost > EPS:
            findings.append((f"I6x{scale}", f"{lost:.2f} kWh booked with no room (booked {total(r):.2f})"))
    return findings


def evaluate_stability(case, *, daily=False) -> list[tuple[str, str]]:
    sc = scenario(case)
    a = base_run(case, daily)
    later = sc.now + timedelta(hours=1)
    later += timedelta(minutes=(-later.minute) % 15)
    if any(x.slot.start < later for x in a.plan.allocations):
        return []  # a charge before the re-evaluation: transition not modelled
    b = run(scenario(case, soc=projected_soc(sc, a, later), now=later.strftime("%H:%M")))
    rest = sum(x.planned_battery_kwh for x in a.plan.allocations if x.slot.start >= later)
    again = b.plan.allocated_kwh
    if abs(again - rest) > max(0.3, 0.15 * rest):
        return [("I5", f"plan at {sc.now:%H:%M} keeps {rest:.2f} kWh after {later:%H:%M}; re-evaluated there it books {again:.2f}")]
    return []


# Every n-th case; n coprime with 2, 3 and 4 (the grid's dimension sizes) keeps
# every value of every dimension in the sample.
SAMPLE = int(os.environ.get("OMNI_SWEEP_SAMPLE") or 1)


@pytest.mark.parametrize("case", CASES[::SAMPLE], ids=case_id)
def test_sweep(case):
    check(case_id(case), evaluate_pair(case) + evaluate_stability(case)
          + evaluate_hold(base_run(case, False)))


# The scheduled 00:05 run (DAILY horizon). A zero forecast at 00:05 defers it
# to the retry ladder. With tomorrow's prices it plans on the same extended
# horizon as a later run, so stability holds with them too (2026-11-17).
DAILY_CASES = [c for c in CASES if c[6] == "00:05" and SOLAR[c[5]] > 0]


@pytest.mark.parametrize("case", DAILY_CASES[::SAMPLE], ids=case_id)
def test_sweep_daily(case):
    findings = (evaluate_pair(case, daily=True) + evaluate_stability(case, daily=True)
                + evaluate_hold(base_run(case, True)))
    check("daily " + case_id(case), findings)
