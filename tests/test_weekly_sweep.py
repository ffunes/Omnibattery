"""Weekly Full Charge sweep (docs/plans/carga-semanal-en-el-optimizador.md).

A grid of weekly days of its own, not a dimension of the main sweep (its keys
in ``sweep_known.json`` and its time do not move). Each case runs through the
scenario harness and a quarter-by-quarter arbiter that executes what the
engine publishes (slot quotas, slots charged to their end or to the top, the
plan's holds, the old weekly hold by threshold) in a world fixed once per case
(forecast sun, or 30 % of it; in the ``today`` cases tomorrow's prices appear
at 13:30). The arbiter re-plans at the production triggers' own times: the
price publication (13:30), the evening re-evaluation (16:00: the stub has no
solar start), 00:05, and the weekly completion (D-9). The pre-slot gate and
the SOC-drop trigger are not replayed.

Two arms: the engine as it is ("new") and the same engine with the previous
allocator forced for the weekly day ("old": ``optimizer_fallback_reason``
patched), both priced by the same arbiter, the energy left at the window's end
valued the same.

* W1 the fleet reaches 100 % before the window ends whenever it fits (charging
  at full power in every quarter from now), in both worlds.
* W2 with the forecast sun alone filling the gap, the weekly day buys no more
  from the grid than the same day without the weekly charge.
* W3 the new arm costs no more than the old one + COST_EPS, in both worlds,
  each charge and hold start the arbiter executes once the weekly charge is
  over counted at the optimizer's start costs.
* W4 the optimizer plans the weekly day and its holds are published.
* W5 once complete, no weekly hold: in the arbiter, and in the published plan
  after its own trajectory touches the top.
* I4, I5, I6 of the main sweep, on the first evaluation.

Marker ``weekly_sweep`` inside ``sweep``: ``pytest -m weekly_sweep -n auto``.
Findings go through the main sweep's known list, keys ``"<rule> weekly <case>"``.
"""
from __future__ import annotations

import dataclasses
import itertools
import math
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing import engine as pricing_engine
from custom_components.omnibattery.pricing.battery_model import BatteryModel, Quarter
from custom_components.omnibattery.pricing.optimizer import HOLD_START_EUR, START_COST_EUR
from custom_components.omnibattery.pricing.solar_timeline import build_boundaries
from custom_components.omnibattery.solar_forecast import (
    SolarForecastPeriod,
    solar_forecast_period_energy_between,
)
from tests import test_predictive_scenarios as h
from tests import test_predictive_sweep as sw

pytestmark = [pytest.mark.sweep, pytest.mark.weekly_sweep]

DAY = sw.DAY  # 2026-11-10, a Tuesday: the target day
TARGET = "tue"
WINDOW_END = DAY + timedelta(days=1, hours=sw.SUNRISE)  # the sunrise after it
# The arbiter runs on to the end of the next day, both arms planned alike
# after the window, so the energy left at the window's end is not valued by
# a rule of thumb (a cut there priced a refill bought for the next morning
# at the cheapest price, below what it cost).
SIM_END = DAY + timedelta(days=2)
# OMNI_WEEKLY_BASE=1 replays the code before the weekly charge moved into
# the optimizer: no re-plan on completion (D-9) or when behind the plan, and a
# slot charged to its end keeps its quota. OMNI_WEEKLY_COSTS=<dir> writes each
# arm's cost per case and world there (old arm on the base = forced old arm).
BASE = os.environ.get("OMNI_WEEKLY_BASE") == "1"
RTE = 0.85
MAX_SOC = 90
MIN_SOC = 10
POWER_W = 2500
CONTRACTED_W = 5750
COST_EPS = 0.003
EPS = 0.05
FULL_TOL = 1e-6
PUBLISH_AT = (13, 30)

FLEETS = {"1x5k": (0,), "2x5k-uneven": (-20, 20)}  # SOC offsets per battery
SOCS = (30, 70)
PRICES = {
    # 2.0TD: flat valley 00-08, peak 10-14 and 18-22, flat rest.
    "2.0TD": lambda hr: 0.08 if hr < 8 else (0.25 if 10 <= hr < 14 or 18 <= hr < 22 else 0.15),
    "night-cheap": sw.PRICES["night-cheap"],
    "solar-dip": sw.PRICES["solar-dip"],
}
SOLAR = {"none": 0.0, "dull": 3.0, "sunny": 15.0}
# Target day: midnight run, morning, after the sun; the night after: 02:00, and
# 07:00 (often too late for 100 %).
NOWS = ("00:05", "10:00", "19:00", "+02:00", "+07:00")
WORLDS = {"forecast": 1.0, "sun30": 0.3}

CASES = [
    c for c in itertools.product(FLEETS, SOCS, PRICES, (False, True), SOLAR, NOWS)
    if not (c[5].startswith("+") and c[3])  # the night after: tomorrow = that day
]


def case_id(case) -> str:
    fleet, soc, prices, tomorrow, solar, now = case
    return (f"{fleet}-soc{soc}-{prices}-{'tomorrow' if tomorrow else 'today'}-{solar}"
            f"-{now.replace(':', '').replace('+', 'n')}")


def start_of(case) -> datetime:
    now = case[5]
    day = DAY + timedelta(days=1) if now.startswith("+") else DAY
    hh, mm = map(int, now.lstrip("+").split(":"))
    return day.replace(hour=hh, minute=mm)


def socs0(case) -> list[float]:
    return [min(100.0, max(MIN_SOC, case[1] + off)) for off in FLEETS[case[0]]]


def known_days(case, at: datetime) -> list[datetime]:
    """Price days the feed holds at ``at``: today, and tomorrow from the start
    (``tomorrow`` cases) or from the publication."""
    today = at.replace(hour=0, minute=0, second=0, microsecond=0)
    published = case[3] or (at.hour, at.minute) >= PUBLISH_AT
    return [today, today + timedelta(days=1)] if published else [today]


def _slots(case, days) -> tuple[PriceSlot, ...]:
    return tuple(
        PriceSlot(start, start + timedelta(minutes=15), PRICES[case[2]](hour))
        for day in days
        for hour in range(24)
        for start in (day + timedelta(hours=hour, minutes=15 * q) for q in range(4))
    )


def _periods(case, factor=1.0) -> tuple[SolarForecastPeriod, ...]:
    zone = ZoneInfo(sw.TZ)
    hours = range(9, 18)
    weights = [min(hr - 8, 18 - hr) for hr in hours]
    total = SOLAR[case[4]] * factor
    if not total:
        return ()
    return tuple(
        SolarForecastPeriod(
            (day + timedelta(hours=hr)).replace(tzinfo=zone),
            (day + timedelta(hours=hr + 1)).replace(tzinfo=zone),
            total * w / sum(weights),
        )
        for day in (DAY, DAY + timedelta(days=1), DAY + timedelta(days=2))
        for hr, w in zip(hours, weights)
    )


BINS = tuple(
    [kw for at, kw in [(h._hours(a), k) for a, k in sw.LOADS["evening"]] if at <= i / 4.0][-1] * 0.25
    for i in range(96)
)


def scenario(case, *, at: datetime, socs, pending: bool) -> h.Scenario:
    zone = ZoneInfo(sw.TZ)
    periods = _periods(case)
    aware = at.replace(tzinfo=zone)
    remaining = sum(
        p.energy_kwh * max(0.0, min(1.0, (p.end - aware).total_seconds() / 3600))
        for p in periods
        if p.start.date() == at.date()
    )
    fleet = tuple(
        dict(capacity_kwh=5.12, max_charge_w=POWER_W, max_discharge_w=POWER_W,
             soc=soc, min_soc=MIN_SOC, max_soc=MAX_SOC)
        for soc in socs
    )
    return h.Scenario(
        name=case_id(case), level="engine", now=at, time_zone=sw.TZ,
        sunrise_h=sw.SUNRISE, solar_noon_h=sw.NOON, slots=_slots(case, known_days(case, at)),
        bins_kwh=BINS, solar_remaining_kwh=remaining, solar_periods=periods, fleet=fleet,
        settings=dict(
            max_price_threshold=1.0, min_arbitrage_margin=None, round_trip_efficiency=RTE,
            peak_shaving=None, contracted_power_w=CONTRACTED_W,
            guaranteed_floor_soc=None, safety_margin_pct=0.0,
        ),
        expected={},
        weekly={"target_day": TARGET, "pending": pending},
    )


def run(sc: h.Scenario, *, old: bool = False, daily: bool = False, charging: bool = False,
        holding: bool = False):
    """One evaluation: outcome, decision data, plan and the controller. ``old``
    forces the previous allocator for a pending weekly day; ``charging`` /
    ``holding``: a price slot is charging / the plan holds at the evaluation
    (the optimizer's start costs)."""
    box = {}
    controller = h._controller
    real = pricing_engine.optimizer_fallback_reason

    def capture(s):
        box["c"] = controller(s)
        box["c"]._current_price_slot_active = box["c"].grid_charging_active = charging
        box["c"]._plan_hold_status = {"held_batteries": ["battery_1"] if holding else []}
        if daily:
            box["c"].config_entry.data["solar_forecast_remaining_sensor"] = "sensor.sweep"
        return box["c"]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(h, "_controller", capture)
        if old and sc.weekly["pending"]:
            mp.setattr(pricing_engine, "optimizer_fallback_reason",
                       lambda r: "weekly_full_charge" if r.import_value_efficiency else real(r))
        if daily:
            mp.setattr(h, "DynamicPricingEvaluationHorizon", SimpleNamespace(
                REMAINING=h.DynamicPricingEvaluationHorizon.DAILY))
        outcome = h._run_engine(sc, mp)
    c = box["c"]
    return SimpleNamespace(o=outcome, d=c._last_decision_data or {}, plan=outcome.plan, c=c)


# ----------------------------------------------------------------------
# The arbiter
# ----------------------------------------------------------------------

@dataclasses.dataclass
class Policy:
    """What the controller executes from one evaluation."""

    quotas: dict  # slot -> quota kWh, None = to the top
    to_end: frozenset
    holds: frozenset  # the plan's holds (and weekly once published)
    weekly_holds: frozenset
    optimizer: bool
    run_slots: tuple  # the old weekly hold's charge slots
    full_at: object = None  # when the plan touches 100 % (the overdue re-plan)
    binding: bool = False  # the window's end inside the published prices (D-3)


def policy_of(r) -> Policy:
    c = r.c
    schedule = c._dynamic_pricing_schedule
    quotas, to_end, run_slots = {}, frozenset(), ()
    if schedule is not None and schedule.charging_needed:
        targets = schedule.slot_energy_targets_kwh or {}
        quotas = {s: targets.get(s) for s in schedule.selected_slots}
        to_end = frozenset(schedule.slot_charge_to_end or ())
        run_slots = tuple(schedule.selected_slots)
    holds = getattr(c, "_dp_plan_holds", None) if r.o.request.hold_allowed else None
    return Policy(
        quotas, to_end, frozenset(holds or ()),
        frozenset(getattr(c, "_dp_plan_weekly_holds", None) or ()),
        r.d.get("planner") == "optimizer", run_slots,
        getattr(c, "_dp_weekly_full_at", None),
        getattr(r.o.request, "full_by", None) is not None  # absent before the change
        and r.o.request.full_by <= max(s.end for s in r.o.request.price_slots),
    )


def _split(stored: float, socs) -> list[float]:
    """Battery SOCs holding ``stored`` kWh, moved by the same SOC points.

    The batteries have one capacity, so the SOCs add up: their clipped sum is
    piecewise linear in the shift, solved between its breakpoints. Each stops
    at MIN_SOC, as the controller stops it (clipped at 0 %, an empty uneven
    fleet left one battery at 0 % and the other above its floor: energy the
    next plan counted and the battery did not have, #888)."""
    def at(delta):
        return [min(100.0, max(MIN_SOC, s + delta)) for s in socs]

    want = stored / 5.12 * 100.0
    points = sorted({MIN_SOC - s for s in socs} | {100.0 - s for s in socs})
    delta = points[-1]
    if want <= sum(at(points[0])):
        delta = points[0]
    for a, b in zip(points, points[1:]):
        low, high = sum(at(a)), sum(at(b))
        if low <= want <= high:
            delta = a + (want - low) / (high - low) * (b - a) if high > low else b
            break
    return at(delta)


def _world_quarters(case, factor, start, end):
    zone = ZoneInfo(sw.TZ)
    periods = _periods(case, factor)
    prices = {s.start: s.price for s in _slots(case, [DAY, DAY + timedelta(days=1), DAY + timedelta(days=2)])}
    n = len(FLEETS[case[0]])
    out = []
    for a, b in build_boundaries(start, end):
        hours = (b - a).total_seconds() / 3600
        slot_start = a.replace(minute=a.minute // 15 * 15, second=0, microsecond=0)
        out.append((a, b, slot_start, Quarter(
            house_kwh=BINS[a.hour * 4 + a.minute // 15] * hours / 0.25,
            solar_kwh=solar_forecast_period_energy_between(periods, a, b, timezone=zone),
            import_price=prices[slot_start],
            export_price=0.0,
            grid_charge_allowed=True,
            charge_kwh=0.0,  # per slice, from the batteries (_step)
            discharge_kwh=POWER_W * n / 1000 * hours,
            ceiling_kwh=CONTRACTED_W / 1000 * hours,
        )))
    return out


SUBSTEPS = 15  # one-minute slices: a battery reaching its taper or 100 % changes the power
TAIL_PCT = 5.0  # WEEKLY_CHARGE_TAIL_PCT, at WEEKLY_CHARGE_TAIL_POWER_RATIO = 0.5


def _power_w(case, e, pending) -> float:
    """The fleet's charge power at stored energy ``e``, from each battery's
    SOC: the controller splits a slot's charge by what each battery charges in
    time (``_shares_within_window``), so each charges at its own power, half
    of it in its last 5 % (the BMS taper) while the weekly charge runs, and
    none once at its ceiling (100 %, max_soc after the run). Equal batteries
    charge the same SOC points (``_split``)."""
    top = 100.0 if pending else MAX_SOC
    total = 0.0
    for soc in _split(_fleet(case)[1] + e, socs0(case)):
        if soc < top - 1e-6:
            total += POWER_W * (0.5 if pending and soc >= 100.0 - TAIL_PCT else 1.0)
    return min(CONTRACTED_W, total)


def _step(case, model, q, hours, e, target, pending):
    """(energy after, cost, kWh stored from the grid, highest energy, still
    charging at its end) of one quarter, in SUBSTEPS slices with the power
    re-read each slice. A slot charge stops once its target is reached (the
    controller completes it)."""
    k = SUBSTEPS
    cost = grid = 0.0
    peak = e
    for _ in range(k):
        sub = dataclasses.replace(
            q, house_kwh=q.house_kwh / k, solar_kwh=q.solar_kwh / k,
            charge_kwh=_power_w(case, e, pending) / 1000 * hours / k,
            discharge_kwh=q.discharge_kwh / k, ceiling_kwh=q.ceiling_kwh / k,
        )
        t = target if target is not None and e < target - FULL_TOL else None
        if t is not None:
            add = float(model.charge_phase(sub, e, t)[0])
            grid += add
            peak = max(peak, e + add)
            if e + add >= target - FULL_TOL:
                target = None  # complete: normal behaviour for the rest
        e1, c = model.normal_step(sub, e, t)
        e, cost = float(e1), cost + float(c)
        peak = max(peak, e)
    return e, cost, grid, peak, target is not None and peak < model.cap - FULL_TOL


def _fleet(case):
    n = len(FLEETS[case[0]])
    capacity = 5.12 * n
    floor = capacity * MIN_SOC / 100
    return capacity, floor, capacity - floor, capacity * MAX_SOC / 100 - floor


def _model(case, pending):
    capacity, floor, cap100, cap_done = _fleet(case)
    # The taper is per battery (_power_w), not the plan's fleet band.
    return BatteryModel(
        capacity_kwh=capacity, round_trip_efficiency=RTE, floor_kwh=floor,
        usable_kwh=cap100 if pending else cap_done,
    )


def fits(case, factor) -> bool:
    """W1's "it fits": charging at full power in every quarter from now."""
    model = _model(case, True)
    e = sum(5.12 * s / 100 for s in socs0(case)) - _fleet(case)[1]
    for a, b, _s, q in _world_quarters(case, factor, start_of(case), WINDOW_END):
        e, _c, _g, peak, _o = _step(case, model, q, (b - a).total_seconds() / 3600, e, model.cap, True)
        if peak >= model.cap - FULL_TOL:
            return True
    return False


def sun_fills(case) -> bool:
    """The forecast sun alone (no grid charge) fills the gap before the window ends."""
    model = _model(case, True)
    e = sum(5.12 * s / 100 for s in socs0(case)) - _fleet(case)[1]
    for a, b, _s, q in _world_quarters(case, 1.0, start_of(case), WINDOW_END):
        e, _c, _g, peak, _o = _step(case, model, q, (b - a).total_seconds() / 3600, e, None, True)
        if peak >= model.cap - FULL_TOL:
            return True
    return False


def simulate(case, factor, *, old=False, weekly=True):
    """Execute the published plans through the window in one world: (cost,
    completion time or None, bought grid kWh, W5 breaches, first run)."""
    t0 = start_of(case)
    capacity, floor, cap100, cap_done = _fleet(case)
    socs = socs0(case)
    e = sum(5.12 * s / 100 for s in socs) - floor
    pending = weekly
    triggers = []
    for day in (DAY, DAY + timedelta(days=1)):
        publish = day.replace(hour=PUBLISH_AT[0], minute=PUBLISH_AT[1])
        if not case[3] and t0 < publish:
            triggers.append(publish)
    triggers += [
        t for t in (DAY.replace(hour=16), DAY + timedelta(days=1, minutes=5),
                    DAY + timedelta(days=1, hours=16))
        if t > t0
    ]
    triggers.sort()

    # Grid charge and hold starts once the weekly charge is over, as the
    # optimizer's start costs count them (the pending layer pays none; a hold
    # right after a charge, or in a slot that charges, pays none); a re-plan
    # while a charge or a hold runs tells the engine.
    starts, charging = 0, False
    hold_starts, holding = 0, False

    def evaluate(at, daily):
        now_socs = _split(floor + e, socs)
        # The 00:05 run with a zero forecast defers to the retry ladder, whose
        # evaluation is a REMAINING one.
        daily = daily and SOLAR[case[4]] > 0
        # The same inputs plan the same: shared between the worlds (identical
        # up to the first re-plan) and, once the weekly charge is over,
        # between the arms.
        key = (old and pending, pending, at, tuple(round(x, 9) for x in now_socs), daily, charging,
               holding)
        if key not in _EVALS:
            sc = scenario(case, at=at, socs=now_socs, pending=pending)
            _EVALS[key] = run(sc, old=old, daily=daily, charging=charging, holding=holding)
        return _EVALS[key]

    first = evaluate(t0, t0.hour == 0 and t0.minute == 5)
    policy = policy_of(first)
    model = _model(case, pending)
    total = bought = 0.0
    complete_at, breaches, replan_now = None, [], False
    targets: dict = {}
    for a, b, slot, q in _world_quarters(case, factor, t0, SIM_END):
        if pending and a >= WINDOW_END:  # the window closed short: the manager resets
            pending = False
            model = _model(case, False)
        if (pending and not old and not BASE and policy.full_at is not None
                and a >= policy.full_at):
            # The engine's overdue trigger (new code only), 2 minutes after the
            # plan's completion: here at the next quarter's start.
            replan_now = True
            policy.full_at = None
        due = [t for t in triggers if t <= a]
        if due or replan_now:
            triggers = [t for t in triggers if t > a]
            policy = policy_of(evaluate(a, any(t.hour == 0 for t in due)))
            targets, replan_now = {}, False
        # The quarter's slot: the price slot it starts in (15 min in this grid).
        ps = next((s for s in policy.quotas if s.start == slot), None)
        target = None
        if ps is not None:
            if ps not in targets:
                quota = policy.quotas[ps]
                if quota is None or (pending and ps in policy.to_end and not BASE):
                    # An old weekly slot, or a slot charged to its end while
                    # the weekly charge is pending: to its end or the top.
                    targets[ps] = model.cap
                else:
                    ask = quota + (capacity / 100 if ps in policy.to_end else 0.0)
                    targets[ps] = float(model.slot_target(e, ask))
            target = targets[ps] if e < targets[ps] - FULL_TOL else None
        held = any(s.start == slot for s in policy.holds)
        weekly_held = False
        if pending and policy.optimizer:
            # The plan's weekly hold; in a charge slot it holds once the
            # target is reached (the slot is then over for the controller).
            weekly_held = any(s.start == slot for s in policy.weekly_holds)
        elif pending and target is None:  # apply_weekly_refill_hold's threshold
            later = [s for s in policy.run_slots if s.start > a]
            if later:
                weekly_held = q.import_price < max(s.price for s in later) / RTE
        if held or weekly_held:
            q = dataclasses.replace(q, discharge_kwh=0.0)
        e, c, grid, peak, open_ = _step(case, model, q, (b - a).total_seconds() / 3600, e, target,
                                        pending)
        starts += grid > 1e-9 and not charging and not pending
        plan_held = held and not pending
        hold_starts += plan_held and not holding and not charging and not grid > 1e-9
        holding = plan_held
        charging = grid > 1e-9 and open_
        if a < WINDOW_END:
            bought += grid / model.eff
        total += c
        if pending and peak >= model.cap - FULL_TOL:
            complete_at, pending = b, False
            # W5: what the plan in force would still hold after its own
            # completion (all of it after now if it never completes: a plan
            # that must be full AT the deadline). Before its own completion a
            # plan behind reality holds legitimately; D-9 replaces it.
            own = b if policy.full_at is None else max(b, policy.full_at)
            # A plan without the obligation yet (D-3) holds freely.
            late = [s for s in policy.weekly_holds if s.start >= own] if policy.binding else []
            if late:
                breaches.append(f"full at {b:%d %H:%M}, the plan holds {len(late)} slots after it")
            model = _model(case, False)
            replan_now = not BASE and not old  # D-9: the new arm re-plans on completion
    prices = [p for p in (PRICES[case[2]](hr) for hr in range(24)) if p > 0]
    total -= min(prices) * math.sqrt(RTE) * e
    return SimpleNamespace(cost=total, complete_at=complete_at, bought=bought,
                           breaches=breaches, first=first, starts=starts,
                           hold_starts=hold_starts)


# ----------------------------------------------------------------------
# Rules
# ----------------------------------------------------------------------

_EVALS: dict = {}  # one case's evaluations, by their inputs


def evaluate_case(case) -> list[tuple[str, str]]:
    _EVALS.clear()
    findings = []
    new = {w: simulate(case, f) for w, f in WORLDS.items()}
    old = {w: simulate(case, f, old=True) for w, f in WORLDS.items()}
    costs = os.environ.get("OMNI_WEEKLY_COSTS")
    if costs:
        with open(os.path.join(costs, f"{os.environ.get('PYTEST_XDIST_WORKER', 'main')}.tsv"),
                  "a", encoding="utf-8") as fh:
            for w in WORLDS:
                fh.write(f"{case_id(case)}\t{w}\t{new[w].cost:.4f}\t{old[w].cost:.4f}\n")
    for w, f in WORLDS.items():
        if fits(case, f) and new[w].complete_at is None:
            findings.append((f"W1{w}", "100 % fits before the window ends and is not reached"))
        n, o = (x[w].cost + START_COST_EUR * x[w].starts + HOLD_START_EUR * x[w].hold_starts
                for x in (new, old))
        if n > o + COST_EPS:
            findings.append((f"W3{w}", f"new {n:.4f} > old {o:.4f} ({new[w].starts} / "
                                       f"{old[w].starts} starts, {new[w].hold_starts} / "
                                       f"{old[w].hold_starts} hold starts)"))
        if new[w].breaches:
            findings.append((f"W5{w}", "; ".join(new[w].breaches[:3])))
    if sun_fills(case):
        plain = simulate(case, 1.0, weekly=False)
        if new["forecast"].bought > plain.bought + EPS:
            findings.append(("W2", f"buys {new['forecast'].bought:.2f} kWh, "
                                   f"{plain.bought:.2f} without the weekly charge"))
    r = new["forecast"].first
    findings += _held_after_full(r)
    if r.d.get("planner") != "optimizer":
        findings.append(("W4", f"planner {r.d.get('planner')}"))
    elif getattr(r.c, "_dp_plan_holds", None) is None:
        findings.append(("W4", "no plan holds published"))
    published = float(r.d.get("planned_grid_charge_kwh") or 0.0)
    if abs(published - sw.total(r)) > EPS:
        findings.append(("I4", f"published {published:.2f} vs booked+deferred {sw.total(r):.2f}"))
    lost = sw.spill(r)
    if lost > EPS:
        findings.append(("I6", f"{lost:.2f} kWh booked with no room"))
    findings += _stability(case, r)
    return findings


def _held_after_full(r) -> list[tuple[str, str]]:
    """W5 on the published plan: once its own trajectory touches the top, no
    weekly hold (a plan that must be full AT the deadline holds and buys back)."""
    plan = r.plan
    reach = getattr(plan, "weekly_full_at", None)
    late = [s for s in getattr(plan, "weekly_holds", ()) if reach is not None and s.start >= reach]
    if late:
        return [("W5plan", f"full at {reach:%d %H:%M}, {len(late)} weekly holds after it")]
    return []


def _stability(case, a) -> list[tuple[str, str]]:
    t0 = start_of(case)
    later = t0 + timedelta(hours=1)
    later += timedelta(minutes=(-later.minute) % 15)
    if any(x.slot.start < later for x in a.plan.allocations) or later >= WINDOW_END:
        return []
    sc = scenario(case, at=t0, socs=socs0(case), pending=True)
    soc = sw.projected_soc(sc, a, later)
    # The fleet SOC spread as the arbiter spreads it: equal SOCs on an uneven
    # fleet change each battery's charge curve, not the plan (#888).
    socs = _split(_fleet(case)[0] * soc / 100, socs0(case))
    b = run(scenario(case, at=later, socs=socs, pending=True))
    rest = sum(x.planned_battery_kwh for x in a.plan.allocations if x.slot.start >= later)
    again = b.plan.allocated_kwh
    if abs(again - rest) > max(0.3, 0.15 * rest):
        return [("I5", f"plan at {t0:%H:%M} keeps {rest:.2f} kWh after {later:%H:%M}; "
                       f"re-evaluated there it books {again:.2f}")]
    return []


SAMPLE = int(os.environ.get("OMNI_SWEEP_SAMPLE") or 1)


@pytest.mark.parametrize("case", CASES[::SAMPLE], ids=case_id)
def test_weekly_sweep(case):
    sw.check("weekly " + case_id(case), evaluate_case(case))
