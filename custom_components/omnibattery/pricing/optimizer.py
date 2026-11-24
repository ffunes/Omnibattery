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
per kWh bought to charge, START_COST_EUR per grid charge start and
HOLD_START_EUR per hold start (all decide, never reported), minus the energy left at the end valued at the
cheapest positive import price x sqrt(rte) (D1). A charge starts when the
slot before it was not still charging at its end (open, under the top); the
state carries that bit (and a third value: the slot before held at its end),
and a slot charging or holding at ``now`` goes on without one.
With negative-price charging off, a negative price counts as 0 for the
energy the battery buys (D3).

Pure function of the request: it runs in an executor, off the event loop.
"""
from __future__ import annotations

from bisect import bisect_right
import dataclasses
from datetime import timedelta
import math

import numpy as np

from ..const import (
    DEFAULT_ROUND_TRIP_EFFICIENCY,
    WEEKLY_CHARGE_MARGIN_MIN_H,
    WEEKLY_CHARGE_MARGIN_RATIO,
)
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
# A pending weekly charge runs a second menu (D-1): half the quotas there keep
# every plan of the weekly sweep's requests identical at 2/3 of the time (8
# lost up to 0.23 c).
PENDING_MAX_ASKS = 12
TIE_EUR = 1e-6  # an equal plan that does not charge now wins (charge late)
# Each grid charge start pays this (decides, never reported): with 15-minute
# prices a plan split its charge over price differences of tenths of a cent.
# ponytail: per plan, not per fleet (at 5 kW it joins half the price gap per
# kWh); per battery if a measure by fleet asks for it.
START_COST_EUR = 0.01
# Each hold start pays this (decides, never reported): without it, 15-minute
# prices split the holds (#888, NL history: 5.7 discharge/hold flips per plan,
# 2.4 with it, for 91 % of the saving). A hold right after a charge, or in a
# slot that charges, pays none: the charge start paid for that change (#878).
# ponytail: per plan, like START_COST_EUR.
HOLD_START_EUR = 0.01
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
# Weekly Full Charge (step 4, D-8): per kWh short of the top at the deadline,
# above any price; decides, never reported.
FULL_PENALTY_EUR = FLOOR_PENALTY_EUR
_FULL_TOL = 1e-6  # "full": within this of the top
_TOL = 1e-9


def _holds(option) -> bool:
    """A hold, or a charge that holds once its target is reached."""
    return option == HOLD or (isinstance(option, tuple) and len(option) > 2)


def _bands(request, cap) -> tuple:
    """The weekly charge's curve (D-6, D-7) as the model's bands: ((stored
    kWh, AC kW), ...) ascending; none without a weekly charge."""
    if request.full_by is None:
        return ()
    return tuple(sorted(
        (cap - float(below), max(0.0, float(kw)))
        for below, kw in request.charge_curve or ()
        if 0.0 < float(below) <= cap
    ))


def _run_margin(request, model, e0, cap) -> timedelta:
    """The weekly run's margin before the window's end (D-7): a share of the
    time it takes from ``e0`` to the top at its curve's power, half an hour at
    least. A run slower than its curve (a battery warming up into the
    temperature limit, the house under the grid ceiling) has that to finish."""
    power = request.full_by_power_kw
    if power is None:
        power = request.effective_power_kw
    hours = model.charge_hours(e0, cap, max(0.0, float(power or 0.0)))
    if not math.isfinite(hours):
        hours = 0.0
    return timedelta(hours=max(WEEKLY_CHARGE_MARGIN_MIN_H, hours * WEEKLY_CHARGE_MARGIN_RATIO))


def _reached(peaks, ends, deadline, cap):
    """Whether the run touched the top in a quarter ending by the deadline:
    a weekly charge is done the moment the fleet is full (D-1)."""
    hit = np.zeros(np.shape(peaks[0]), dtype=bool)
    for peak, end in zip(peaks, ends):
        if end <= deadline:
            hit |= peak >= cap - _FULL_TOL
    return hit


def optimize_charge(request: ChronologicalEvaluationRequest) -> ChronologicalEvaluationResult:
    usable = max(0.0, float(request.usable_initial_kwh))
    cap = usable + max(0.0, float(request.headroom_kwh))
    rte = request.import_value_efficiency or DEFAULT_ROUND_TRIP_EFFICIENCY
    fleet = request.fleet_capacity_kwh
    bands = _bands(request, cap)
    if fleet and request.fleet_soc_pct is not None:
        model = BatteryModel(
            capacity_kwh=fleet,
            round_trip_efficiency=rte,
            floor_kwh=fleet * request.fleet_soc_pct / 100.0 - usable,
            usable_kwh=cap,
            soc_resolution_pct=request.soc_resolution_pct,
            bands=bands,
        )
        soc_step = fleet * request.soc_resolution_pct / 100.0
    else:  # no reading to round to
        model = BatteryModel(capacity_kwh=max(cap, _TOL), round_trip_efficiency=rte,
                             usable_kwh=cap, soc_resolution_pct=1e-6, bands=bands)
        soc_step = 0.0
    groups = _slot_groups(request)
    # Once a weekly charge is done the ceiling is max_soc again: the done
    # layer stores up to it (the plan valued room for tomorrow's sun the
    # battery would not take). The pending layer keeps the 100 %.
    # Its power is the day's ordinary one too, not the weekly fleet's (every
    # battery charging): the done layer runs its own quarters.
    done_model = model
    done_quarters = [quarters for _s, quarters, _e in groups]
    if request.full_by is not None and request.done_headroom_kwh is not None:
        done_model = dataclasses.replace(
            model, usable_kwh=min(cap, usable + max(0.0, float(request.done_headroom_kwh))),
            bands=(),
        )
        power = max(0.0, float(request.effective_power_kw))
        done_quarters = [
            [dataclasses.replace(q, charge_kwh=power * q.hours) for q in quarters]
            for _s, quarters, _e in groups
        ]
    layer_model = {False: done_model, True: model}
    # Weekly Full Charge pending (D-1): a second value function for "not yet
    # full", through the group holding the window's end. The shortfall pays
    # at the planned deadline (D-7: the window's end less the run's margin,
    # a quarter of its time and half an hour at least, not before the first
    # quarter) and again at the window's end, so a run that misses the first
    # keeps charging for the second. Penalised only when
    # the window's end is inside the published prices (D-3); before that the
    # day is planned with the top and the hold, freely.
    e0 = min(usable, cap)
    if request.full_by is not None and soc_step > 0 and cap > soc_step:
        # Pending at a reading of 100 %: not complete until the batteries say
        # so (a Venus A/D taper counts its BMS cutoff only while commanded to
        # charge). Planned a reading step short, it books a slot to the top.
        e0 = min(e0, cap - soc_step)
    pending0 = request.full_by is not None and bool(groups) and e0 < cap - _FULL_TOL
    deadline = early = None
    last_k = early_k = -1
    binding = False
    if pending0:
        first_end = groups[0][2][0]
        deadline = max(request.full_by, first_end)
        early = max(request.full_by - _run_margin(request, model, e0, cap), first_end)
        binding = request.full_by <= groups[-1][2][-1]

        def group_of(t):
            return max(k for k, (_s, _q, ends) in enumerate(groups) if ends[0] <= t)

        last_k = group_of(deadline) if binding else len(groups) - 1
        early_k = group_of(early) if binding else -1
    margin = max(0.0, float(request.import_value_margin or 0.0))
    prices = [s.price for s, quarters, _ends in groups for _ in quarters]
    end_value = min((p for p in prices if p > 0), default=0.0) * model.eff

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
    if pending0:
        extra.append(cap - 2 * _FULL_TOL)  # the value jumps at the top: done / not done
    levels = np.unique(np.round(np.concatenate([lattice, extra]), 9))
    levels = levels[(levels >= 0.0) & (levels <= cap + _TOL)]

    delay = _solar_delay(request, groups, model, fleet)
    risk, room_levels, charge_caps, risk_reserve = _room(request, groups, model, soc_step)
    room_prices = request.room_export_prices or {}

    def menu(slot, quarters, slot_end, pending=False):
        # Inside a risk the grid is held at 0 W: only normal behaviour (and a
        # floor charge) executes there.
        in_risk = slot.start in risk
        # A pending weekly charge (§3 of its plan) may always hold (a flat
        # afternoon otherwise discharges and buys back at a loss); it keeps
        # all the sun (only the delay's export), and neither sells nor makes
        # room nor caps its charges (100 % would not fit).
        hold_ok = (request.hold_allowed or pending) and not in_risk
        # Normal first: on a tie neither a charge nor a hold is taken.
        options = [None] + ([HOLD] if hold_ok else [])
        # Store first: on a tie the surplus is stored. Never with a charge: the
        # surplus price hold's blocker stops all charging.
        if not in_risk and (
            (request.export_choice_allowed and not pending)
            or (delay is not None and delay.offers(slot_end))
        ) and any(q.solar_kwh > q.house_kwh for q in quarters):
            options.append(EXPORT)
        # Sell last: on a tie it does not sell. Two powers, exclusive with the rest.
        # Never at a battery price <= 0: paid exports are Smart Pre-Discharge's
        # (its own switch and reserve SOC, design §3), not High-Price Sale's.
        if (not in_risk and not pending and request.sell_allowed and request.sell_power_kw > 0
                and slot.start in sell_prices and sell_prices[slot.start] > 0):
            options += [(SELL, request.sell_power_kw), (SELL, request.sell_power_kw / 2)]
        # Make room: the very last, so on a tie nothing is discharged.
        room = (
            [(ROOM, request.room_power_kw), (ROOM, request.room_power_kw / 2)]
            if slot.start in room_levels and not pending
            else []
        )
        if not quarters[0].grid_charge_allowed:
            return options + room
        most = sum(model.charge_rate(q) for q in quarters)
        if most <= _TOL:
            return options + room
        step = max(QUOTA_STEP_KWH, most / (PENDING_MAX_ASKS if pending else MAX_ASKS))
        asks = [(a, False) for a in list(np.arange(step, most, step)) + [most]]
        asks.append((most + max(soc_step, QUOTA_STEP_KWH), True))
        # With the hold allowed a charge may also hold: once its target is
        # reached the battery keeps it instead of covering the rest of the slot.
        holding = [(a, end, True) for a, end in asks] if hold_ok else []
        return options + asks + holding + room

    floor = min(max(0.0, float(request.floor_kwh or 0.0)), cap)
    export_saving = max(0.0, float(request.export_choice_min_saving or 0.0))
    sell_prices = dict(request.battery_export_prices or request.export_prices or {})

    def target_of(e, ask, slot_start=None, pending=False):
        """The slot's target: capped before a risk where room is offered (a
        charge must not fill what the plan would then export as room; not
        while a weekly charge is pending), then raised to the floor while the
        guaranteed floor is active."""
        model = layer_model[pending]
        target = model.slot_target(e, ask)
        if slot_start in charge_caps and not pending:
            target = np.minimum(target, charge_caps[slot_start])
        return np.maximum(target, floor) if floor > 0 else target

    def run(quarters, e, option, slot_price, quarter_ends, slot_start=None, asks=None,
            pending=False):
        """Exact execution of one slot from energies ``e`` (array).

        ``asks`` (n, 1): n charge quotas at once, row i the run of ``option``
        with ``asks[i]`` as its quota (``option`` says only whether it holds).
        ``pending``: the weekly charge's layer (no charge cap, only the
        delay's export, the 100 % ceiling)."""
        model = layer_model[pending]
        e = np.array(e, dtype=float)
        if asks is not None:
            e = np.broadcast_to(e, (len(asks),) + e.shape).copy()
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
        if export and (pending or not request.export_choice_allowed):
            export = delayed
        target = None if option in (None, HOLD, EXPORT) or sell else target_of(
            e, option[0] if asks is None else asks, slot_start, pending)
        done = np.zeros(e.shape, dtype=bool)
        ends = []
        peaks = []  # the quarter's highest energy: after its charge phase, or at its end
        adds = []  # the grid charge stored in each quarter (output only)
        for q, end in zip(quarters, quarter_ends):
            t = None
            top = e
            if target is not None:
                t = np.where(done, -np.inf, target)
                # Bought = the GRID energy the charge puts in the battery: the
                # sun surplus during the charge phase feeds it too, and is not
                # bought (margin and D3 counted it, making sunny charges dear).
                add, busy = model.charge_phase(q, e, t)
                if model.bands:
                    done |= target - e - add <= _TOL
                else:
                    done |= target - e <= model.charge_rate(q) + _TOL
                top = e + add
                bought = np.maximum(add / model.eff - max(0.0, q.solar_kwh - q.house_kwh) * busy, 0.0)
                stored += add
                decide += margin * bought
                if not request.negative_price_charging and slot_price < 0:
                    decide -= slot_price * bought  # D3: counts as 0
            adds.append(top - e)
            if hold:
                # What the battery would have covered goes to the grid.
                q = dataclasses.replace(q, discharge_kwh=0.0)
            if sell:
                # High-Price Sale's override: the grid at -P; the battery covers
                # the house and exports the rest, solar beyond P charges it.
                # Making room runs only while the battery is at or above the
                # slot's level: below it the battery is normal (it stores the
                # sun surplus up to the level, it covers the house).
                h_q = q.charge_kwh / max(request.effective_power_kw, _TOL)
                net = q.house_kwh - q.solar_kwh
                fill = 0.0
                if room:
                    level = room_levels[slot_start]
                    fill = np.minimum(min(max(-net, 0.0), q.charge_kwh), np.maximum(level - e, 0.0) / model.eff)
                    e = e + fill * model.eff
                flow = net + fill + option[1] * h_q
                above = np.maximum(e - level, 0.0) if room else e
                out = np.minimum(np.minimum(above * model.eff, np.maximum(flow, 0.0)), q.discharge_kwh)
                inn = np.minimum(np.minimum(np.maximum(-flow, 0.0), q.charge_kwh - fill),
                                 np.maximum(model.cap - e, 0.0) / model.eff)
                e = e - out / model.eff + inn * model.eff
                if room:
                    extra = np.minimum(np.minimum(np.maximum(net - out, 0.0), e * model.eff),
                                       q.discharge_kwh - out)
                    e = e - extra / model.eff
                    out = out + extra
                grid = net + fill - out + inn
                c = (np.maximum(grid, 0.0) * q.import_price
                     - np.maximum(-grid, 0.0) * (room_prices if room else sell_prices)[slot_start])
                cost += c
                decide += c if room else c + margin * out
                if floor > 0 and request.floor_until is not None and end <= request.floor_until:
                    decide += FLOOR_PENALTY_EUR * np.maximum(floor - e, 0.0)
                ends.append(e)
                peaks.append(e)
                continue
            kept = np.minimum(
                min(max(0.0, q.solar_kwh - q.house_kwh), q.charge_kwh),
                np.maximum(model.cap - e, 0.0) / model.eff,
            )
            if export is not False and np.any(export):
                # The surplus it would have stored is exported; the minimum
                # saving is charged on it (decides, never reported).
                if request.export_choice_allowed and not pending:
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
            peaks.append(np.maximum(top, e))
        # Published "to the end": a target not reached in the slot, or the
        # battery's top (the controller stops it there; as a plain quota it
        # would round from its own reading and stop short).
        to_end = (~done | (target >= model.cap - 1e-6)) if target is not None else done
        # Still charging at the slot's end (open, under the top): the next
        # slot's charge goes on without a start (#899, #571).
        on = (stored > _TOL) & to_end & (np.max(peaks, axis=0) < model.cap - _FULL_TOL)
        return e, cost, decide, stored, to_end, ends, peaks, on, adds

    def run_menu(slot, quarters, e, slot_ends, pending=False):
        """[(option, run)] for the slot's menu, in its order. The charge quotas
        run in one call per kind (with and without the hold): the same
        numbers, without the per-call overhead (96 % of the runs were quotas)."""
        options = menu(slot, quarters, slot_ends[-1], pending)
        out = [None] * len(options)
        stacks = {}
        for i, option in enumerate(options):
            if isinstance(option, tuple) and option[0] not in (SELL, ROOM):
                stacks.setdefault(len(option), []).append(i)
            else:
                out[i] = (option, run(quarters, e, option, slot.price, slot_ends, slot.start,
                                      pending=pending))
        for rows in stacks.values():
            asks = np.array([[options[i][0]] for i in rows], dtype=float)
            e1, c, decide, stored, to_end, ends, peaks, on, adds = run(
                quarters, e, options[rows[0]], slot.price, slot_ends, slot.start, asks=asks,
                pending=pending)
            for r, i in enumerate(rows):
                out[i] = (options[i], (e1[r], c[r], decide[r], stored[r], to_end[r],
                                       [x[r] for x in ends], [x[r] for x in peaks], on[r],
                                       [x[r] for x in adds]))
        return out

    def shortfall(ends, path, peaks, by):
        """FULL_PENALTY_EUR per kWh short of the top at the slot's last
        quarter ending by ``by``, unless the top was touched by then."""
        j = max(i for i, end in enumerate(ends) if end <= by)
        return np.where(_reached(peaks, ends, by, cap), 0.0,
                        FULL_PENALTY_EUR * np.maximum(cap - path[j], 0.0))

    def onward(k, ends, e1, path, peaks, on, done_on, done_off, pending_next):
        """(cost from a pending run's end on, whether it touched the top): the
        done value once full by the window's end, else the pending one; the
        groups holding a binding deadline pay its shortfall, and the charge
        is over after the window's end. The pending layer pays no start cost;
        the done one takes the run's own charging bit (off once at the top)."""
        hit = _reached(peaks, ends, deadline, cap)
        after = np.where(on, np.interp(e1, levels, done_on), np.interp(e1, levels, done_off))
        penalty = shortfall(ends, path, peaks, early) if k == early_k else 0.0
        if k < last_k or not binding:
            return np.where(hit, after, np.interp(e1, levels, pending_next)) + penalty, hit
        return after + penalty + shortfall(ends, path, peaks, deadline), hit

    # Backward pass: values_on / values_off / values_hold[k] = cost-to-go
    # before group k, on the lattice, with the previous slot still charging at
    # its end, holding at its end, or neither (a charge not "on" pays
    # START_COST_EUR; a hold from neither that stores nothing HOLD_START_EUR:
    # after a charge the charge start already paid for the change);
    # pending_values[k] the same while a weekly charge is still to complete
    # (no start cost there).
    values_on = [-end_value * levels]
    values_off = [values_on[0]]
    values_hold = [values_on[0]]
    pending_values = [values_on[0]]
    for k in reversed(range(len(groups))):
        slot, quarters, slot_ends = groups[k]
        nxt_on, nxt_off, nxt_hold = values_on[-1], values_off[-1], values_hold[-1]
        nxt_pending = pending_values[-1]
        best_on = best_off = best_hold = None
        for option, (e1, _c, decide, stored, _te, _p, _pk, on, _a) in run_menu(
                slot, done_quarters[k], levels, slot_ends):
            after = np.interp(e1, levels, nxt_off)
            hold = 0.0
            if _holds(option):
                held = ~on  # a charge that never reached its target never held
                after = np.where(held, np.interp(e1, levels, nxt_hold), after)
                hold = HOLD_START_EUR * (held & (stored <= _TOL))
            v = decide + np.where(on, np.interp(e1, levels, nxt_on), after)
            charge = START_COST_EUR * (stored > _TOL)
            v_on, v_off, v_hold = v, v + charge + hold, v + charge
            best_on = v_on if best_on is None else np.minimum(best_on, v_on)
            best_off = v_off if best_off is None else np.minimum(best_off, v_off)
            best_hold = v_hold if best_hold is None else np.minimum(best_hold, v_hold)
        values_on.append(best_on)
        values_off.append(best_off)
        values_hold.append(best_hold)
        best = best_off
        if pending0 and k <= last_k:
            best_pending = None
            for _option, (e1, _c, decide, _st, _te, path, peaks, on, _a) in run_menu(
                    slot, quarters, levels, slot_ends, True):
                v = decide + onward(k, slot_ends, e1, path, peaks, on, nxt_on, nxt_off,
                                    nxt_pending)[0]
                best_pending = v if best_pending is None else np.minimum(best_pending, v)
            # Full at the slot's start: already done.
            best = np.where(levels >= cap - _FULL_TOL, best, best_pending)
        pending_values.append(best)
    values_on.reverse()
    values_off.reverse()
    values_hold.reverse()
    pending_values.reverse()

    # Forward pass from the exact energy.
    e = e0
    total = 0.0
    allocations: list[SlotAllocation] = []
    holds: list = []
    weekly_holds: list = []
    exports: list = []
    sales: list = []
    rooms: list = []
    trajectory: list = []
    house_delivery: list = []
    # (start, end, "charge" | "hold" | "empty", price, kWh) per quarter, for the
    # notification (output only): when the charge and the hold really run
    # inside their slot, and what the grid sells with the battery empty.
    quarter_actions: list = []
    pending = pending0
    full_at = None  # when the plan touches the top of a pending weekly charge
    charging = bool(request.charging_now)  # a slot charging at now goes on: no start
    holding = bool(request.holding_now)  # likewise a hold
    starts = hold_starts = 0
    for k, (slot, quarters, ends) in enumerate(groups):
        layer = pending and k <= last_k
        chosen = None
        run_quarters = quarters if layer else done_quarters[k]
        for option, (e1, c, decide, stored, open_, path, peaks, on, adds) in run_menu(
                slot, run_quarters, [e], ends, layer):
            start = held = hold_start = False
            if layer:
                after, hit = onward(k, ends, e1, path, peaks, on, values_on[k + 1],
                                    values_off[k + 1], pending_values[k + 1])
                v, hit = float(decide[0] + after[0]), bool(hit[0])
            else:
                start = bool(stored[0] > _TOL) and not charging
                held = _holds(option) and not on[0]
                hold_start = bool(held and not holding and not charging and stored[0] <= _TOL)
                nxt = values_on[k + 1] if on[0] else (values_hold if held else values_off)[k + 1]
                v, hit = float(decide[0] + START_COST_EUR * start
                               + HOLD_START_EUR * hold_start
                               + np.interp(e1[0], levels, nxt)), False
            row = (v, option, float(e1[0]), float(c[0]), float(stored[0]),
                   bool(open_[0]), [float(x[0]) for x in path], hit,
                   [float(x[0]) for x in peaks], bool(on[0]), start, held, hold_start,
                   [float(x[0]) for x in adds])
            if chosen is None or v < chosen[0] - (TIE_EUR if chosen[1] is None else 0.0):
                chosen = row
        (_v, option, e_end, c, stored, open_, path, hit, top, charging, start, held,
         hold_start, adds) = chosen
        starts += start
        hold_starts += hold_start
        holding = held
        if layer and hit:
            full_at = next(end for end, x in zip(ends, top)
                           if end <= deadline and x >= cap - _FULL_TOL)
        selling = isinstance(option, tuple) and option[0] in (SELL, ROOM)
        if selling and option[0] == ROOM:
            rooms.append((slot, float(option[1]), float(room_levels[slot.start])))
        elif selling:
            sales.append((slot, float(option[1])))
        elif _holds(option):
            # A pending weekly charge's hold is its own (executed without the
            # Discharge Reserve); the reserve's are the done layer's.
            (weekly_holds if layer else holds).append(slot)
        if option == EXPORT and (
            (request.export_choice_allowed and not layer)
            or bool(delay.allows(np.array([e]), slot.start, ends[-1])[0])
        ):
            exports.append(slot)
        if option not in (None, HOLD, EXPORT) and not selling and stored > _TOL:
            allocations.append(SlotAllocation(
                slot,
                _published_quota(model, soc_step, e,
                                 None if open_ else float(target_of(e, option[0], slot.start, layer)),
                                 stored),
                None, "flexible",
                # Charged to the slot's end or to the top: executing
                # ``stored`` as a plain quota would stop it early.
                to_slot_end=open_,
            ))
        total += c
        # What the battery delivered to the house, per quarter (output only).
        eff = layer_model[bool(layer)].eff
        charged = option not in (None, HOLD, EXPORT) and not selling and stored > _TOL
        holds_here = _holds(option) and not selling and not layer
        s0 = e
        for q, end, top_q, x, add in zip(run_quarters, ends, top, path, adds):
            net = max(0.0, q.house_kwh - q.solar_kwh)
            kwh = min(max(0.0, max(s0, top_q) - x) * eff, net)
            if kwh > _TOL:
                house_delivery.append((end, kwh, q.import_price))
            begin = end - timedelta(hours=q.hours)
            if charged and add > _TOL:
                quarter_actions.append((begin, end, "charge", q.import_price, add))
            elif holds_here:
                quarter_actions.append((begin, end, "hold", q.import_price, net))
            elif not selling and x <= _TOL and net - kwh > _TOL:
                quarter_actions.append((begin, end, "empty", q.import_price, net - kwh))
            s0 = x
        e = e_end
        trajectory += list(zip(ends, path))
        if layer and (hit or k >= last_k):
            pending = False
    total -= end_value * e
    # The same start with no plan (the battery covers the house and stores the
    # solar surplus, nothing else), valued the same way: what the plan saves
    # (output only).
    plain_e, plain = e0, 0.0
    for plain_k, (plain_slot, _quarters, plain_ends) in enumerate(groups):
        plain_e1, plain_c, *_rest = run(
            done_quarters[plain_k], [plain_e], None, plain_slot.price, plain_ends,
            plain_slot.start)
        plain_e, plain = float(plain_e1[0]), plain + float(plain_c[0])
    plain -= end_value * plain_e

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
        house_delivery=tuple(house_delivery),
        charge_efficiency=model.eff,
        holds=tuple(holds),
        exports=tuple(exports),
        sales=tuple(sales),
        room_sales=tuple(rooms),
        risk_slots=tuple(s for s, _q, _e in groups if s.start in risk),
    )
    plan.optimizer_cost = total
    plan.plan_saving = plain - total
    plan.quarter_actions = tuple(quarter_actions)
    plan.charge_starts = starts
    plan.hold_starts = hold_starts
    plan.weekly_holds = tuple(weekly_holds)
    plan.weekly_full_at = full_at
    # The plan decided Smart Pre-Discharge (with export prices, design §2.6 A).
    plan.room_allowed = request.room_threshold is not None and bool(request.export_prices)
    # Per risk slot, the stored kWh its sun can add: the solar reserve the
    # negative-price limiter protects (the cap's own measure, stage-0 decision 4).
    plan.risk_reserve = {s: risk_reserve[s.start] for s in plan.risk_slots}
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
    """(risk slot starts, {slot start: room level}, {slot start: charge cap},
    {risk slot start: the stored kWh its sun can add}).

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
        return frozenset(), {}, {}, {}
    at_risk = risk_quarters(request)
    flat = [i for i in sorted(request.intervals, key=lambda x: x.start) if i.start >= request.now]

    def storable(q):
        return min(max(0.0, q.solar_kwh - q.house_kwh), q.charge_kwh) * model.eff

    reserve_by_slot = {}  # risk slot start -> stored kWh its sun can add
    marks = []  # (interval start, Quarter, at risk), in the groups' order
    for slot, quarters, _ends in groups:
        for q in quarters:
            start = flat[len(marks)].start
            marks.append((start, q, start in at_risk))
            if start in at_risk:
                reserve_by_slot[slot.start] = reserve_by_slot.get(slot.start, 0.0) + storable(q)
    risk_slots = set(reserve_by_slot)
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
        sun = sum(storable(q) for start, q, risky in marks[first:] if risky and start.date() == day)
        drain = sum(
            min(max(0.0, q.house_kwh - q.solar_kwh), q.discharge_kwh) / model.eff
            for _start, q, _r in marks[index:first]
        )
        levels[slot.start] = (slot, min(cap, max(reserve, cap - sun + drain)))
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
    return frozenset(risk_slots), room, caps, reserve_by_slot


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
    """[(slot, [Quarter], [interval end])] from now to the last published price.
    A weekly charge (``full_by``) charges at the fleet's power while every
    battery charges; its curve (the model's bands) slows it after that."""
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
    power = max(0.0, float(request.effective_power_kw))
    if request.full_by is not None and request.full_by_power_kw is not None:
        power = max(0.0, float(request.full_by_power_kw))
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
            charge_kwh=power * h,
            discharge_kwh=BIG if discharge is None else max(0.0, float(discharge)) * h,
            ceiling_kwh=BIG if ceiling is None or not math.isfinite(float(ceiling)) else max(0.0, float(ceiling)) * h,
            hours=h,
        )
        if groups and groups[-1][0] is s:
            groups[-1][1].append(q)
            groups[-1][2].append(i.end)
        else:
            groups.append((s, [q], [i.end]))
    return groups


def optimizer_fallback_reason(request: ChronologicalEvaluationRequest) -> str | None:
    """Why step 3 leaves this request to the previous allocator (None = optimizer).

    Not modelled: a floor request without its ``floor_kwh``; Time Slot prices are
    synthetic (out of scope). A pending weekly full charge is the optimizer's
    since step 4 (``full_by``); ``grid_carries_load_while_charging`` is the
    previous allocator's bookkeeping, not read here.
    """
    if request.import_value_efficiency is None:
        return "time_slot"
    if request.fleet_capacity_kwh is None or request.fleet_soc_pct is None:
        return "no_fleet_data"
    if request.floor_kwh <= _TOL and (request.premium_kwh > _TOL or any(
        d.kind == "guaranteed_floor" for d in request.deadlines
    )):
        return "guaranteed_floor"
    return None
