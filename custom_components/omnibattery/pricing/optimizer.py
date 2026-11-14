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

    def menu(slot, quarters):
        if not quarters[0].grid_charge_allowed:
            return [None]
        most = sum(model.charge_rate(q) for q in quarters)
        if most <= _TOL:
            return [None]
        step = max(QUOTA_STEP_KWH, most / MAX_ASKS)
        asks = list(np.arange(step, most, step)) + [most]
        return [None] + [(a, False) for a in asks] + [(most + max(soc_step, QUOTA_STEP_KWH), True)]

    def run(quarters, e, option, slot_price):
        """Exact execution of one slot from energies ``e`` (array)."""
        e = np.array(e, dtype=float)
        cost = np.zeros_like(e)
        decide = np.zeros_like(e)
        stored = np.zeros_like(e)
        target = None if option is None else model.slot_target(e, option[0])
        done = np.zeros(e.shape, dtype=bool)
        ends = []
        for q in quarters:
            t = None
            if target is not None:
                t = np.where(done, -np.inf, target)
                add = np.minimum(np.maximum(t - e, 0.0), model.charge_rate(q))
                done |= target - e <= model.charge_rate(q) + _TOL
                bought = add / model.eff
                stored += add
                decide += margin * bought
                if not request.negative_price_charging and slot_price < 0:
                    decide -= slot_price * bought  # D3: counts as 0
            e, c = model.normal_step(q, e, t)
            cost += c
            decide += c
            ends.append(e)
        # Published "to the end": a target not reached in the slot, or the
        # battery's top (the controller stops it there; as a plain quota it
        # would round from its own reading and stop short).
        to_end = (~done | (target >= model.cap - 1e-6)) if target is not None else done
        return e, cost, decide, stored, to_end, ends

    # Backward pass: values[k] = cost-to-go before group k, on the lattice.
    values = [-end_value * levels]
    for slot, quarters, _ends in reversed(groups):
        nxt = values[-1]
        best = None
        for option in menu(slot, quarters):
            e1, _c, decide, *_ = run(quarters, levels, option, slot.price)
            v = decide + np.interp(e1, levels, nxt)
            best = v if best is None else np.minimum(best, v)
        values.append(best)
    values.reverse()

    # Forward pass from the exact energy.
    e = e0
    total = 0.0
    allocations: list[SlotAllocation] = []
    trajectory: list = []
    for k, (slot, quarters, ends) in enumerate(groups):
        nxt = values[k + 1]
        chosen = None
        for option in menu(slot, quarters):
            e1, c, decide, stored, open_, path = run(quarters, [e], option, slot.price)
            v = float(decide[0] + np.interp(e1[0], levels, nxt))
            if chosen is None or v < chosen[0] - (TIE_EUR if chosen[1] is None else 0.0):
                chosen = (v, option, float(e1[0]), float(c[0]), float(stored[0]),
                          bool(open_[0]), [float(x[0]) for x in path])
        _v, option, e_end, c, stored, open_, path = chosen
        if option is not None and stored > _TOL:
            allocations.append(SlotAllocation(
                slot, _published_quota(model, soc_step, e, option, stored, open_),
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
    )
    plan.optimizer_cost = total
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


def _published_quota(model, soc_step, e, option, stored, to_end):
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
    if to_end or soc_step <= 0:
        return stored
    reading = float(np.floor((model.floor_kwh + e) / soc_step + 1e-6) * soc_step - model.floor_kwh)
    target = float(model.slot_target(e, option[0]))
    margin = soc_step * 1e-3
    return min(max(stored, target - reading - soc_step + margin), target - reading - margin)


def _slot_groups(request: ChronologicalEvaluationRequest):
    """[(slot, [Quarter], [interval end])] from now to the last published price."""
    slots = sorted(
        (s for s in request.price_slots if math.isfinite(float(s.price))),
        key=lambda s: s.start,
    )
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
            s.end > request.now
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

    Not modelled yet, each moved in by a later step of the design: the
    guaranteed floor and its premium (step 4), a pending weekly full charge
    (step 4), the discharge reserve holding energy (step 5); Time Slot prices
    are synthetic (out of scope).
    """
    if request.import_value_efficiency is None:
        return "time_slot"
    if request.fleet_capacity_kwh is None or request.fleet_soc_pct is None:
        return "no_fleet_data"
    if request.premium_kwh > _TOL or any(
        d.kind == "guaranteed_floor" for d in request.deadlines
    ):
        return "guaranteed_floor"
    if not request.grid_carries_load_while_charging:
        return "weekly_full_charge"
    if request.reserve_holds:
        return "discharge_reserve"
    return None
