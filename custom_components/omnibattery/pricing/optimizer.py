"""2.0.0 optimizer, step 3: grid charging only (docs design §2, §6, §7).

Dynamic programming over the stored energy, one decision per price slot: how
much the slot charges from the grid, executed as the controller does it
(``battery_model``): the slot's SOC target is set once from the whole-percent
reading, the grid carries the house while it charges, the slot is complete the
moment the target is reached, and the battery is back to normal after that.
Outside a charge the battery covers the house and stores the solar surplus.

A slot can also charge "to its end": its target is one SOC step above what it
can store, so a run of cheap quarters is not cut short a percent each time the
whole-percent target is reached early (the cause of the plan costing more than
it promised, measured 2026-11-13).

Objective: import x price - export x export price, plus the arbitrage margin
per kWh bought to charge (decides, never reported), minus the energy left at
the end valued at the cheapest positive import price x sqrt(rte) (D1). With
negative-price charging off, a negative price counts as 0 for the energy the
battery buys (D3).

Pure function of the request: it runs in an executor, off the event loop.
"""
from __future__ import annotations

from bisect import bisect_right
import dataclasses
from datetime import timedelta
import math

import numpy as np

from ..const import DEFAULT_ROUND_TRIP_EFFICIENCY
from .battery_model import BIG, BatteryModel, Quarter
from .chronological import (
    ChronologicalDiagnostics,
    ChronologicalEvaluationRequest,
    ChronologicalEvaluationResult,
    ChronologicalPlan,
    SlotAllocation,
    build_energy_deadlines,
)

# Energy grid of the value function: 0.01 kWh (§6 said 0.05; 0.01 closes a
# 0.32 c miss for +18 % time), at most MAX_LEVELS levels on large fleets.
LATTICE_KWH = 0.01
MAX_LEVELS = 1000
# Quota menu per slot: 0.05 kWh steps, at most MAX_ASKS of them (an hourly
# slot at 10 kW would offer 184). ponytail: coarser on large fleets (40 kWh /
# 10 kW / hourly / 36 h: 5.5 s -> 0.6 s on a PC); finer if a fleet needs it.
QUOTA_STEP_KWH = 0.05
MAX_ASKS = 24
TIE_EUR = 1e-6  # an equal plan that does not charge now wins (charge late)
HOLD = "hold"  # menu option: the battery covers nothing for the whole slot
EXPORT = "export"  # menu option: the slot's solar surplus is exported, not stored
SELL = "sell"  # menu option ("sell", kW): the grid is held at -kW for the slot
# Smart Pre-Discharge ("room", kW): the grid at -kW until the battery reaches
# the slot's room level, then normal. Its value is the model's own: the risk
# sun that does not fit is exported at the risk's price (<= the threshold).
ROOM = "room"
# Solar Charge Delay (D9): at most this much over the whole horizon, on the
# solar surplus stored before each deadline, decreasing to 0 there: a
# tie-break, never reported (per day it bought delay with real money once the
# battery refilled several times over the horizon).
DELAY_TIE_EUR = 0.001
# The later sun must refill the battery this many times over before the delay
# exports (the module's DELAY_SAFETY_FACTOR): a short forecast left it unfilled.
DELAY_REFILL_FACTOR = 1.3
# Per kWh under the guaranteed floor, per quarter before the sun: above any
# price, so the floor wins whenever it can be met; never reported.
FLOOR_PENALTY_EUR = 100.0
_TOL = 1e-9


def optimize_charge(request: ChronologicalEvaluationRequest) -> ChronologicalEvaluationResult:
    usable = max(0.0, float(request.usable_initial_kwh))
    cap = usable + max(0.0, float(request.headroom_kwh))
    rte = request.import_value_efficiency or DEFAULT_ROUND_TRIP_EFFICIENCY
    fleet = request.fleet_capacity_kwh
    if fleet and request.fleet_soc_pct is not None:
        model = BatteryModel(
            capacity_kwh=fleet,
            round_trip_efficiency=rte,
            floor_kwh=fleet * request.fleet_soc_pct / 100.0 - usable,
            usable_kwh=cap,
            soc_resolution_pct=request.soc_resolution_pct,
        )
        soc_step = fleet * request.soc_resolution_pct / 100.0
    else:  # no reading to round to
        model = BatteryModel(capacity_kwh=max(cap, _TOL), round_trip_efficiency=rte,
                             usable_kwh=cap, soc_resolution_pct=1e-6)
        soc_step = 0.0
    groups = _slot_groups(request)
    margin = max(0.0, float(request.import_value_margin or 0.0))
    prices = [s.price for s, quarters, _ends in groups for _ in quarters]
    end_value = min((p for p in prices if p > 0), default=0.0) * model.eff

    e0 = min(usable, cap)
    grid = max(LATTICE_KWH, cap / MAX_LEVELS)
    lattice = np.arange(e0 % grid, cap + _TOL, grid)
    extra = [0.0, cap, e0]
    if soc_step >= grid / 2:
        # The value jumps where the whole-percent reading changes (a slot's
        # target rounds from it): a point on each side of every step, so the
        # interpolation never crosses a jump. Finer readings jump too little
        # to matter at this lattice.
        base = fleet * request.fleet_soc_pct / 100.0 - usable
        steps = np.arange(math.ceil(base / soc_step), math.floor((base + cap) / soc_step) + 1)
        edges = steps * soc_step - base
        extra += list(edges) + list(edges - soc_step * 1e-4)
    levels = np.unique(np.round(np.concatenate([lattice, extra]), 9))
    levels = levels[(levels >= 0.0) & (levels <= cap + _TOL)]

    delay = _solar_delay(request, groups, model, fleet)
    risk, room_levels, charge_caps = _room(request, groups, model, soc_step)
    room_prices = request.room_export_prices or {}

    def menu(slot, quarters, slot_end):
        # Inside a risk the grid is held at 0 W: only normal behaviour (and a
        # floor charge) executes there.
        in_risk = slot.start in risk
        # Normal first: on a tie neither a charge nor a hold is taken.
        options = [None] + ([HOLD] if request.hold_allowed and not in_risk else [])
        # Store first: on a tie the surplus is stored. Never with a charge: the
        # surplus price hold's blocker stops all charging.
        if not in_risk and (
            request.export_choice_allowed or (delay is not None and delay.offers(slot_end))
        ) and any(q.solar_kwh > q.house_kwh for q in quarters):
            options.append(EXPORT)
        # Sell last: on a tie it does not sell. Two powers, exclusive with the rest.
        # Never at a battery price <= 0: paid exports are Smart Pre-Discharge's
        # (its own switch and reserve SOC, design §3), not High-Price Sale's.
        if (not in_risk and request.sell_allowed and request.sell_power_kw > 0
                and slot.start in sell_prices and sell_prices[slot.start] > 0):
            options += [(SELL, request.sell_power_kw), (SELL, request.sell_power_kw / 2)]
        # Make room: the very last, so on a tie nothing is discharged.
        room = (
            [(ROOM, request.room_power_kw), (ROOM, request.room_power_kw / 2)]
            if slot.start in room_levels
            else []
        )
        if not quarters[0].grid_charge_allowed:
            return options + room
        most = sum(model.charge_rate(q) for q in quarters)
        if most <= _TOL:
            return options + room
        step = max(QUOTA_STEP_KWH, most / MAX_ASKS)
        asks = [(a, False) for a in list(np.arange(step, most, step)) + [most]]
        asks.append((most + max(soc_step, QUOTA_STEP_KWH), True))
        # With the hold allowed a charge may also hold: once its target is
        # reached the battery keeps it instead of covering the rest of the slot.
        holding = [(a, end, True) for a, end in asks] if request.hold_allowed and not in_risk else []
        return options + asks + holding + room

    floor = min(max(0.0, float(request.floor_kwh or 0.0)), cap)
    hold_saving = max(0.0, float(request.hold_min_saving or 0.0))
    export_saving = max(0.0, float(request.export_choice_min_saving or 0.0))
    sell_prices = dict(request.battery_export_prices or request.export_prices or {})

    def target_of(e, ask, slot_start=None):
        """The slot's target: capped before a risk where room is offered (a
        charge must not fill what the plan would then export as room), then
        raised to the floor while the guaranteed floor is active."""
        target = model.slot_target(e, ask)
        if slot_start in charge_caps:
            target = np.minimum(target, charge_caps[slot_start])
        return np.maximum(target, floor) if floor > 0 else target

    def run(quarters, e, option, slot_price, quarter_ends, slot_start=None):
        """Exact execution of one slot from energies ``e`` (array)."""
        e = np.array(e, dtype=float)
        cost = np.zeros_like(e)
        decide = np.zeros_like(e)
        stored = np.zeros_like(e)
        sell = isinstance(option, tuple) and option[0] in (SELL, ROOM)
        room = sell and option[0] == ROOM
        hold = option == HOLD or (not sell and isinstance(option, tuple) and len(option) > 2)
        export = option == EXPORT
        # The delay applies where its mask lets the slot export, judged once at
        # the slot's start for every option: elsewhere neither its export nor its
        # tie-break (which steered sales of the sun from an empty battery).
        delayed = delay.allows(e, slot_start, quarter_ends[-1]) if delay is not None else None
        if export and not request.export_choice_allowed:
            export = delayed
        target = None if option in (None, HOLD, EXPORT) or sell else target_of(e, option[0], slot_start)
        done = np.zeros(e.shape, dtype=bool)
        ends = []
        for q, end in zip(quarters, quarter_ends):
            t = None
            if target is not None:
                t = np.where(done, -np.inf, target)
                add = np.minimum(np.maximum(t - e, 0.0), model.charge_rate(q))
                done |= target - e <= model.charge_rate(q) + _TOL
                # Bought = the GRID energy the charge puts in the battery: the
                # sun surplus during the charge phase feeds it too, and is not
                # bought (margin and D3 counted it, making sunny charges dear).
                rate = model.charge_rate(q)
                busy = np.where(add > 0, add / rate, 0.0) if rate > 0 else np.zeros_like(add)
                bought = np.maximum(add / model.eff - max(0.0, q.solar_kwh - q.house_kwh) * busy, 0.0)
                stored += add
                decide += margin * bought
                if not request.negative_price_charging and slot_price < 0:
                    decide -= slot_price * bought  # D3: counts as 0
            if hold:
                # What the battery would have covered goes to the grid; the
                # minimum saving is charged on it (decides, never reported).
                covered = np.minimum(
                    np.minimum(e * model.eff, max(0.0, q.house_kwh - q.solar_kwh)),
                    q.discharge_kwh,
                )
                decide += hold_saving * covered
                q = dataclasses.replace(q, discharge_kwh=0.0)
            if sell:
                # High-Price Sale's override: the grid at -P; the battery covers
                # the house and exports the rest, solar beyond P charges it.
                # Making room stops at the slot's level, then the battery covers
                # the house as normal.
                h_q = q.charge_kwh / max(request.effective_power_kw, _TOL)
                net = q.house_kwh - q.solar_kwh
                flow = net + option[1] * h_q
                above = np.maximum(e - room_levels[slot_start], 0.0) if room else e
                out = np.minimum(np.minimum(above * model.eff, max(flow, 0.0)), q.discharge_kwh)
                inn = np.minimum(min(max(-flow, 0.0), q.charge_kwh), np.maximum(model.cap - e, 0.0) / model.eff)
                e = e - out / model.eff + inn * model.eff
                if room:
                    extra = np.minimum(np.minimum(np.maximum(net - out, 0.0), e * model.eff),
                                       q.discharge_kwh - out)
                    e = e - extra / model.eff
                    out = out + extra
                grid = net - out + inn
                c = (np.maximum(grid, 0.0) * q.import_price
                     - np.maximum(-grid, 0.0) * (room_prices if room else sell_prices)[slot_start])
                cost += c
                decide += c if room else c + margin * out
                if floor > 0 and request.floor_until is not None and end <= request.floor_until:
                    decide += FLOOR_PENALTY_EUR * np.maximum(floor - e, 0.0)
                ends.append(e)
                continue
            kept = np.minimum(
                min(max(0.0, q.solar_kwh - q.house_kwh), q.charge_kwh),
                np.maximum(model.cap - e, 0.0) / model.eff,
            )
            if export is not False and np.any(export):
                # The surplus it would have stored is exported; the minimum
                # saving is charged on it (decides, never reported).
                if request.export_choice_allowed:
                    decide += export_saving * kept
                e_x, c = model.normal_step(dataclasses.replace(q, charge_kwh=0.0), e, t)
                if export is not True and not np.all(export):
                    e_n, c_n = model.normal_step(q, e, t)
                    e_x, c = np.where(export, e_x, e_n), np.where(export, c, c_n)
                    kept = np.where(export, 0.0, kept)
                else:
                    kept = np.zeros_like(e)
                e = e_x
            else:
                e, c = model.normal_step(q, e, t)
            if delay is not None:
                decide += delay.weight(end) * kept * delayed  # stored early costs a hair more (D9)
            cost += c
            decide += c
            if floor > 0 and request.floor_until is not None and end <= request.floor_until:
                decide += FLOOR_PENALTY_EUR * np.maximum(floor - e, 0.0)
            ends.append(e)
        # Published "to the end": a target not reached in the slot, or the
        # battery's top (the controller stops it there; as a plain quota it
        # would round from its own reading and stop short).
        to_end = (~done | (target >= model.cap - 1e-6)) if target is not None else done
        return e, cost, decide, stored, to_end, ends

    # Backward pass: values[k] = cost-to-go before group k, on the lattice.
    values = [-end_value * levels]
    for slot, quarters, slot_ends in reversed(groups):
        nxt = values[-1]
        best = None
        for option in menu(slot, quarters, slot_ends[-1]):
            e1, _c, decide, *_ = run(quarters, levels, option, slot.price, slot_ends, slot.start)
            v = decide + np.interp(e1, levels, nxt)
            best = v if best is None else np.minimum(best, v)
        values.append(best)
    values.reverse()

    # Forward pass from the exact energy.
    e = e0
    total = 0.0
    allocations: list[SlotAllocation] = []
    holds: list = []
    exports: list = []
    sales: list = []
    rooms: list = []
    trajectory: list = []
    for k, (slot, quarters, ends) in enumerate(groups):
        nxt = values[k + 1]
        chosen = None
        for option in menu(slot, quarters, ends[-1]):
            e1, c, decide, stored, open_, path = run(quarters, [e], option, slot.price, ends, slot.start)
            v = float(decide[0] + np.interp(e1[0], levels, nxt))
            if chosen is None or v < chosen[0] - (TIE_EUR if chosen[1] is None else 0.0):
                chosen = (v, option, float(e1[0]), float(c[0]), float(stored[0]),
                          bool(open_[0]), [float(x[0]) for x in path])
        _v, option, e_end, c, stored, open_, path = chosen
        selling = isinstance(option, tuple) and option[0] in (SELL, ROOM)
        if selling and option[0] == ROOM:
            rooms.append((slot, float(option[1]), float(room_levels[slot.start])))
        elif selling:
            sales.append((slot, float(option[1])))
        elif option == HOLD or (isinstance(option, tuple) and len(option) > 2):
            holds.append(slot)
        if option == EXPORT and (
            request.export_choice_allowed or bool(delay.allows(np.array([e]), slot.start, ends[-1])[0])
        ):
            exports.append(slot)
        if option not in (None, HOLD, EXPORT) and not selling and stored > _TOL:
            allocations.append(SlotAllocation(
                slot,
                _published_quota(model, soc_step, e,
                                 None if open_ else float(target_of(e, option[0], slot.start)), stored),
                None, "flexible",
                # Charged to the slot's end or to the top: executing
                # ``stored`` as a plain quota would stop it early.
                to_slot_end=open_,
            ))
        total += c
        e = e_end
        trajectory += list(zip(ends, path))
    total -= end_value * e

    intervals = sorted(request.intervals, key=lambda item: item.start)
    deadlines = build_energy_deadlines(intervals, usable, usable_capacity_kwh=cap)
    need = max((d.required_cumulative_kwh for d in deadlines), default=0.0)
    allocated = sum(a.planned_battery_kwh for a in allocations)
    cheaper = max(0.0, need - allocated)
    if allocated > _TOL or need <= _TOL:
        reason = "ok"
    elif not any(q.grid_charge_allowed for _s, qs, _e in groups for q in qs):
        reason = "price_threshold" if request.max_price_threshold is not None else "no_eligible_slots"
    else:
        reason = "cheaper_to_import"
    energies = [usable] + [x for _t, x in trajectory]
    depleted = next((t for t, x in trajectory if x <= _TOL), None)
    plan = ChronologicalPlan(
        intervals=intervals,
        deadlines=list(deadlines),
        allocations=allocations,
        total_required_kwh=allocated,
        deadline_required_kwh=need,
        flexible_required_kwh=0.0,
        allocated_kwh=allocated,
        earliest_depletion_at=depleted,
        minimum_projected_energy_kwh=min(energies),
        reason=reason,
        cheaper_to_import_kwh=cheaper if allocated <= _TOL else 0.0,
        trajectory=tuple(trajectory),
        charge_efficiency=model.eff,
        holds=tuple(holds),
        exports=tuple(exports),
        sales=tuple(sales),
        room_sales=tuple(rooms),
        risk_slots=tuple(s for s, _q, _e in groups if s.start in risk),
    )
    plan.optimizer_cost = total
    # The plan decided Smart Pre-Discharge (with export prices, design §2.6 A).
    plan.room_allowed = request.room_threshold is not None and bool(request.export_prices)
    plan.export_choice_allowed = request.export_choice_allowed
    plan.sell_allowed = request.sell_allowed
    plan.solar_delay = delay is not None
    plan.solar_delay_day_closed = delay is not None and delay.day_closed
    return ChronologicalEvaluationResult(
        plan=plan,
        diagnostics=ChronologicalDiagnostics(
            earliest_projected_depletion=depleted,
            minimum_projected_energy_kwh=plan.minimum_projected_energy_kwh,
            deadline_required_kwh=need,
            flexible_required_kwh=0.0,
            deadline_shortfall_kwh=0.0,
            total_shortfall_kwh=0.0,
            energy_deadlines=tuple(deadlines),
            reason=reason,
            cheaper_to_import_kwh=plan.cheaper_to_import_kwh,
        ),
    )


class _SolarDelay:
    """Solar Charge Delay in the optimizer (step 8, D9).

    Each day's deadline is the forecast sun's end minus the margin. A slot
    ending by it may export its surplus while the battery is at the setpoint
    and the surplus storable after the slot, up to the deadline, refills the
    gap DELAY_REFILL_FACTOR times over, on a day that does not need the grid
    anyway (the module's balance: stored energy + the sun left today >= the
    house left today - its deadband). Storing surplus before the deadline costs a hair, falling
    linearly to 0 there, so that at equal cost the sun is stored late.
    """

    def __init__(self, deadlines, firsts, storable, setpoint_e, cap, quarters, deadband,
                 closed_until):
        self._deadlines, self._firsts, self._cap = deadlines, firsts, cap
        self._quarters, self._deadband = quarters, deadband  # [(start, end, solar, house)]
        self._closed_until = closed_until
        self.day_closed = False
        self._sums = {}  # (slot start, end) -> (later storable sun, sun left, house left)
        self._storable, self._setpoint = storable, setpoint_e
        self._w = DELAY_TIE_EUR / max(cap, sum(v for _e, v in storable), 1e-6)

    def _deadline(self, t):
        return self._deadlines.get(t.date())

    def offers(self, slot_end):
        d = self._deadline(slot_end)
        return d is not None and slot_end <= d

    def allows(self, e, slot_start, slot_end):
        e = np.asarray(e, dtype=float)
        d = self._deadline(slot_end)
        if d is None or slot_end > d or (self._closed_until is not None and slot_start < self._closed_until):
            return np.zeros(e.shape, dtype=bool)
        key = (slot_start, slot_end)
        if key not in self._sums:  # the same slot is asked for every option and state
            day = slot_start.date()
            today = [(sun, house) for start, _end, sun, house in self._quarters
                     if start >= slot_start and start.date() == day]
            self._sums[key] = (
                sum(v for end, v in self._storable if slot_end < end <= d),
                sum(x for x, _h in today),
                sum(h for _x, h in today),
            )
        future, sun, house = self._sums[key]
        return (
            (e >= self._setpoint - _TOL)
            & (future >= DELAY_REFILL_FACTOR * (self._cap - e) - _TOL)
            & (e + sun >= house - self._deadband - _TOL)
        )

    def weight(self, end):
        d = self._deadline(end)
        if d is None or end >= d or (self._closed_until is not None and end <= self._closed_until):
            return 0.0  # past the deadline, or a closed day: no delay, no tie-break
        span = (d - self._firsts[end.date()]).total_seconds()
        return self._w * (min(1.0, (d - end).total_seconds() / span) if span > 0 else 1.0)


def _solar_delay(request, groups, model, fleet):
    """The delay's deadlines and refill test, or None when it is off."""
    if request.solar_delay_margin_h is None:
        return None
    margin = timedelta(hours=max(0.0, float(request.solar_delay_margin_h)))
    sunny = [(end, q) for _s, qs, ends in groups for q, end in zip(qs, ends) if q.solar_kwh > _TOL]
    last, firsts = {}, {}
    for end, _q in sunny:
        last[end.date()] = max(last.get(end.date(), end), end)
        firsts[end.date()] = min(firsts.get(end.date(), end), end)
    deadlines = {d: t - margin for d, t in last.items()}
    storable = [
        (end, min(max(0.0, q.solar_kwh - q.house_kwh), q.charge_kwh) * model.eff)
        for end, q in sunny
    ]
    setpoint = request.solar_delay_setpoint_pct
    setpoint_e = (
        fleet * float(setpoint) / 100.0 - model.floor_kwh
        if setpoint is not None and fleet
        else -math.inf
    )
    quarters = []
    for _s, qs, ends in groups:
        for q, end in zip(qs, ends):
            start = quarters[-1][1] if quarters and quarters[-1][1] < end else request.now
            quarters.append((start, end, q.solar_kwh, q.house_kwh))
    deadband = max(0.0, float(request.solar_delay_deadband_kwh or 0.0))
    closed_until = request.solar_delay_closed_until
    # The module's daily latch: today's balance, once, at planning time. Short
    # (and with sun to wait for: a zero forecast is transient, #457) = no
    # delay export today.
    today = [(sun, house) for start, _end, sun, house in quarters if start.date() == request.now.date()]
    sun_today = sum(x for x, _h in today)
    closed = sun_today > _TOL and (
        request.usable_initial_kwh + sun_today < sum(h for _x, h in today) - deadband
    )
    if closed:
        midnight = (request.now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        closed_until = max(closed_until or midnight, midnight)
    delay = _SolarDelay(deadlines, firsts, storable, setpoint_e, model.cap, quarters, deadband,
                        closed_until)
    delay.day_closed = closed
    return delay


def risk_quarters(request: ChronologicalEvaluationRequest) -> frozenset:
    """Starts of the intervals Smart Pre-Discharge protects: sun above the
    house and an export price at or below the threshold. The price is read
    from the export map: a slot missing from it has no risk (no sensor, or no
    price published yet; ``Quarter.export_price`` would read it as 0)."""
    threshold = request.room_threshold
    exports = request.export_prices or {}
    if threshold is None or not exports:
        return frozenset()
    slots = sorted(
        (s for s in (request.price_curve or request.price_slots) if math.isfinite(float(s.price))),
        key=lambda s: s.start,
    )
    starts = [s.start for s in slots]
    risk = set()
    for i in sorted(request.intervals, key=lambda x: x.start):
        if i.start < request.now:
            continue
        k = bisect_right(starts, i.start) - 1
        if k < 0 or not slots[k].start <= i.start < slots[k].end:
            break
        price = exports.get(slots[k].start)
        if price is not None and i.solar_kwh > i.grid_load_kwh and price <= threshold:
            risk.add(i.start)
    return frozenset(risk)


def _room(request, groups, model, soc_step):
    """(risk slot starts, {slot start: room level}, {slot start: charge cap}).

    Risk slots (any quarter at risk) do not charge from the grid, except for
    the guaranteed floor. Before a risk, the room option stops at
    L = full - the sun the next risk day's quarters can store + what the house
    drains up to them, never under the reserve (``room_floor_kwh``); rounded up
    to the whole-percent reading the controller stops at. Where room is
    offered anywhere, charges before a risk are capped at L too (rounded down),
    except a negative-price charge with negative-price charging on: filling a
    cheap slot to export it as "room" is arbitrage with the sale off (stage 0.3).
    """
    threshold = request.room_threshold
    if threshold is None:
        return frozenset(), {}, {}
    at_risk = risk_quarters(request)
    flat = [i for i in sorted(request.intervals, key=lambda x: x.start) if i.start >= request.now]
    risk_slots = set()
    marks = []  # (interval start, Quarter, at risk), in the groups' order
    for slot, quarters, _ends in groups:
        for q in quarters:
            start = flat[len(marks)].start
            marks.append((start, q, start in at_risk))
            if start in at_risk:
                risk_slots.add(slot.start)
    if request.floor_kwh <= _TOL:
        for slot, quarters, _ends in groups:
            if slot.start in risk_slots:
                quarters[:] = [dataclasses.replace(q, grid_charge_allowed=False) for q in quarters]
    cap = model.cap
    reserve = min(max(0.0, float(request.room_floor_kwh or 0.0)), cap)

    def on_grid(x, up):
        if soc_step <= 0:
            return x
        steps = (model.floor_kwh + x) / soc_step
        steps = math.ceil(steps - 1e-6) if up else math.floor(steps + 1e-6)
        return min(cap, max(0.0, steps * soc_step - model.floor_kwh))

    levels = {}
    index = 0
    for slot, quarters, _ends in groups:
        index += len(quarters)
        if slot.start in risk_slots:
            continue
        first = next((j for j in range(index, len(marks)) if marks[j][2]), None)
        if first is None:
            continue
        day = marks[first][0].date()
        storable = sum(
            min(max(0.0, q.solar_kwh - q.house_kwh), q.charge_kwh) * model.eff
            for start, q, risky in marks[first:] if risky and start.date() == day
        )
        drain = sum(
            min(max(0.0, q.house_kwh - q.solar_kwh), q.discharge_kwh) / model.eff
            for _start, q, _r in marks[index:first]
        )
        levels[slot.start] = (slot, min(cap, max(reserve, cap - storable + drain)))
    room_prices = request.room_export_prices or {}
    room = {
        start: on_grid(level, True)
        for start, (slot, level) in levels.items()
        if request.room_power_kw > _TOL and start in room_prices and room_prices[start] > threshold
    }
    caps = {
        start: on_grid(level, False)
        for start, (slot, level) in levels.items()
        if room and not (request.negative_price_charging and slot.price < 0)
    }
    return frozenset(risk_slots), room, caps


def _published_quota(model, soc_step, e, target, stored):
    """The slot energy to publish: what the plan stores, kept inside the
    quotas that give the controller the same target from the same reading.

    The controller adds the quota to the whole-percent reading and stops at the
    first reading at or above it. Any quota in (T - R - step, T - R] reaches
    the plan's target T from reading R; the stored energy T - e lies in it,
    but at its edges a float hair decides (6 % of 5.12 kWh is
    0.30720000000000014: one percent more; a battery the house left 0.5 mWh
    under a whole percent: one percent less). So it keeps a thousandth of a
    step from both.
    """
    if target is None or soc_step <= 0:  # to the end: the step comes on top
        return stored
    reading = float(np.floor((model.floor_kwh + e) / soc_step + 1e-6) * soc_step - model.floor_kwh)
    margin = soc_step * 1e-3
    return min(max(stored, target - reading - soc_step + margin), target - reading - margin)


def _slot_groups(request: ChronologicalEvaluationRequest):
    """[(slot, [Quarter], [interval end])] from now to the last published price."""
    slots = sorted(
        (
            s for s in (request.price_curve or request.price_slots)
            if math.isfinite(float(s.price))
        ),
        key=lambda s: s.start,
    )
    candidates = set(request.price_slots)
    starts = [s.start for s in slots]
    exports = request.export_prices or {}
    discharge = request.discharge_kw
    ceiling = request.grid_ceiling_kw
    groups: list = []
    for i in sorted(request.intervals, key=lambda x: x.start):
        if i.start < request.now:
            continue
        k = bisect_right(starts, i.start) - 1
        s = slots[k] if k >= 0 and slots[k].start <= i.start < slots[k].end else None
        if s is None:
            break  # end of the published prices
        h = (i.end - i.start).total_seconds() / 3600.0
        allowed = (
            s in candidates
            and s.end > request.now
            and s.start < request.horizon_end
            and (request.max_price_threshold is None or s.price <= request.max_price_threshold)
        )
        q = Quarter(
            house_kwh=i.grid_load_kwh,
            solar_kwh=i.solar_kwh,
            import_price=s.price,
            export_price=float(exports.get(s.start, 0.0) or 0.0),
            grid_charge_allowed=allowed,
            charge_kwh=max(0.0, float(request.effective_power_kw)) * h,
            discharge_kwh=BIG if discharge is None else max(0.0, float(discharge)) * h,
            ceiling_kwh=BIG if ceiling is None or not math.isfinite(float(ceiling)) else max(0.0, float(ceiling)) * h,
        )
        if groups and groups[-1][0] is s:
            groups[-1][1].append(q)
            groups[-1][2].append(i.end)
        else:
            groups.append((s, [q], [i.end]))
    return groups


def optimizer_fallback_reason(request: ChronologicalEvaluationRequest) -> str | None:
    """Why step 3 leaves this request to the previous allocator (None = optimizer).

    Not modelled yet, each moved in by a later step of the design: a pending
    weekly full charge (step 4), the discharge reserve holding energy (step 5);
    a floor request without its ``floor_kwh``; Time Slot prices are synthetic
    (out of scope).
    """
    if request.import_value_efficiency is None:
        return "time_slot"
    if request.fleet_capacity_kwh is None or request.fleet_soc_pct is None:
        return "no_fleet_data"
    if request.floor_kwh <= _TOL and (request.premium_kwh > _TOL or any(
        d.kind == "guaranteed_floor" for d in request.deadlines
    )):
        return "guaranteed_floor"
    if not request.grid_carries_load_while_charging:
        return "weekly_full_charge"
    if request.reserve_holds and not request.hold_allowed:
        return "discharge_reserve"
    return None
