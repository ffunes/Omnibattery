"""Pure planning helpers for high-price discharge (#270).

Trigger 1 sells only surplus that tomorrow's solar can replace at a profitable
export price; trigger 2 sells battery energy reserved for household
consumption, but only when each sold kWh is linked 1:1 to a *later* household
deficit that can be re-bought from the grid, and the export price strictly
beats the linked buy-back cost. It never sells the surplus that trigger 1
handles and it never dips below a battery's floor.

The module has no Home Assistant dependency. ``now`` and ``horizon_end`` are
always injected; the module never calls ``datetime.now()`` and never searches
for the solar-recovery instant itself - the runtime computes that boundary
(``PricingManager.energy_horizon_end``) and passes it in as an opaque limit.

Two invariants hold everywhere in this module:

1. **Every uncertainty resolves to "do not sell".** A disabled function, an
   invalid horizon, a coverage gap or overlap, a missing price, or a missing
   later import price never falls back to zero, the last known value, or a
   fixed duration - it fails safe with zero allocations.
2. **Protected demand is never sold without a later buy-back.** Trigger 2
   links each kWh to a specific later deficit (``DemandLink``); trigger 1
   sells only energy beyond that demand and the safety margin, and requires
   a proven solar refill.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Sequence

from .curtailment import BatterySnapshot


EPSILON = 1e-6

STATUS_DISABLED = "disabled"
STATUS_FAIL_SAFE = "fail_safe"
STATUS_PLANNED = "planned"
STATUS_NO_OPPORTUNITY = "no_opportunity"

REASON_DISABLED = "disabled"
REASON_INVALID_NOW = "invalid_now"
REASON_INVALID_HORIZON_END = "invalid_horizon_end"
REASON_INVALID_CONFIGURATION = "invalid_configuration"
REASON_NO_SLOTS_IN_WINDOW = "no_slots_in_window"
REASON_COVERAGE_GAP = "coverage_gap"
REASON_COVERAGE_OVERLAP = "coverage_overlap"
REASON_NO_PROTECTED_DEMAND = "no_protected_demand"
REASON_NO_USABLE_ENERGY = "no_usable_energy"
REASON_NO_ELIGIBLE_CANDIDATES = "no_eligible_candidates"
REASON_NO_SURPLUS = "no_surplus"
REASON_NO_REFILL_PRICE = "no_refill_price"
REASON_PLANNED = "high_price_discharge_planned"


def _finite(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _aware(moment: object) -> bool:
    """True if ``moment`` is a timezone-aware ``datetime``.

    Comparing an aware and a naive datetime raises ``TypeError`` instead of
    giving a wrong answer. That must become a fail-safe, not an unhandled
    crash, so every boundary is checked before it is ever compared.
    """
    return (
        isinstance(moment, datetime)
        and moment.tzinfo is not None
        and moment.tzinfo.utcoffset(moment) is not None
    )


def _utc(moment: datetime) -> datetime:
    """Normalise an aware datetime to UTC before any arithmetic.

    Python compares and subtracts two aware datetimes that share a ``tzinfo``
    *object* by wall clock, ignoring the offset. Two boundaries an hour apart
    around a DST change therefore read as 2 h (spring forward) or 0 h (fall
    back), which would over-sell or silently drop a slot. Normalising first
    makes every comparison and subtraction elapsed time.
    """
    return moment.astimezone(timezone.utc)


def _duration_hours(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() / 3600.0)


@dataclass(frozen=True)
class HorizonSlot:
    """One atomic planning slot: its prices and its forecast, in AC terms."""

    start: datetime
    end: datetime
    export_price: float | None
    import_price: float | None
    consumption_kwh: float
    solar_kwh: float

    @property
    def duration_hours(self) -> float:
        return _duration_hours(self.start, self.end)


@dataclass(frozen=True)
class DemandLink:
    """The portion of one later deficit slot that a sale is repaying."""

    demand_start: datetime
    demand_end: datetime
    energy_kwh: float


@dataclass(frozen=True)
class TriggerAllocation:
    """A sale in one slot, kWh for kWh linked to later household demand."""

    start: datetime
    end: datetime
    export_price: float
    threshold: float
    energy_kwh: float
    power_w: float
    demand_links: tuple[DemandLink, ...] = ()
    surplus_kwh: float = 0.0
    surplus_threshold: float | None = None


@dataclass(frozen=True)
class HighPriceDischargePlan:
    """A discharge plan and the diagnostics needed to explain it."""

    status: str = STATUS_DISABLED
    reason: str = REASON_DISABLED
    evaluation_time: datetime | None = None
    horizon_end: datetime | None = None
    protected_demand_kwh: float = 0.0
    usable_energy_kwh: float = 0.0
    allocations: tuple[TriggerAllocation, ...] = ()
    total_allocated_kwh: float = 0.0
    trigger_1_budget_kwh: float = 0.0
    refill_price: float | None = None
    trigger_1_reason: str | None = None

    @property
    def is_fail_safe(self) -> bool:
        return self.status == STATUS_FAIL_SAFE

    def allocation_at(self, moment: datetime) -> TriggerAllocation | None:
        for allocation in self.allocations:
            if allocation.start <= moment < allocation.end:
                return allocation
        return None

    def remaining_kwh(self, moment: datetime) -> float:
        """Energy of the still-future allocations, for the live enforcer."""
        return sum(
            allocation.energy_kwh
            for allocation in self.allocations
            if allocation.end > moment
        )


@dataclass
class _LedgerEntry:
    """Internal bookkeeping for one deficit slot's still-unassigned energy."""

    start: datetime
    end: datetime
    remaining_kwh: float
    import_price: float | None


def _valid_battery(snapshot: BatterySnapshot) -> bool:
    """A battery that cannot discharge or reports bad data sells nothing.

    Never averaged into the aggregate: an ineligible battery's floor and
    energy simply drop out, they are never smoothed over by a healthy one.
    """
    return (
        getattr(snapshot, "eligible", False)
        and getattr(snapshot, "can_discharge", False)
        and _finite(snapshot.soc_pct)
        and _finite(snapshot.capacity_kwh)
        and _finite(snapshot.floor_soc_pct)
        and _finite(snapshot.max_discharge_power_w)
        and snapshot.capacity_kwh > 0
        and snapshot.max_discharge_power_w >= 0
    )


def _trim_slots(
    slots: Sequence[HorizonSlot], now: datetime, horizon_end: datetime
) -> list[HorizonSlot]:
    """Clip slots to ``[now, horizon_end)`` without inventing coverage.

    A slot with bad boundaries, a non-finite forecast, or a tzinfo that
    cannot be compared to ``now``/``horizon_end`` is dropped rather than
    guessed at; the coverage check downstream turns that hole into a
    fail-safe instead of silently shrinking the protected demand.
    """
    trimmed: list[HorizonSlot] = []
    for slot in slots:
        try:
            if not (
                _aware(slot.start)
                and _aware(slot.end)
                and _finite(slot.consumption_kwh)
                and _finite(slot.solar_kwh)
            ):
                continue
            start, end = _utc(slot.start), _utc(slot.end)
            if end <= start or end <= now or start >= horizon_end:
                continue
            new_start = max(start, now)
            new_end = min(end, horizon_end)
        except (TypeError, ValueError, OverflowError):
            continue
        if new_end <= new_start:
            continue

        # The current slot (and, symmetrically, one that straddles
        # horizon_end) only contributes the fraction of its forecast that
        # falls inside the protected window (RF-028).
        original_hours = _duration_hours(start, end)
        new_hours = _duration_hours(new_start, new_end)
        fraction = new_hours / original_hours if original_hours > EPSILON else 0.0

        trimmed.append(
            HorizonSlot(
                start=new_start,
                end=new_end,
                export_price=slot.export_price,
                import_price=slot.import_price,
                consumption_kwh=float(slot.consumption_kwh) * fraction,
                solar_kwh=float(slot.solar_kwh) * fraction,
            )
        )
    trimmed.sort(key=lambda item: item.start)
    return trimmed


def _coverage_gap_reason(
    working: Sequence[HorizonSlot], now: datetime, horizon_end: datetime
) -> str | None:
    """Return why coverage falls short of ``horizon_end``, or ``None``.

    A gap or an overlap can never be bridged: bridging a gap would understate
    the protected demand and sell energy the house still needs, and an
    overlap would double count a slot's forecast. Either truncates the usable
    horizon right there, which - since it must reach ``horizon_end`` - is
    always a fail-safe.
    """
    if working[0].start > now:
        return REASON_COVERAGE_GAP
    previous_end = working[0].end
    for slot in working[1:]:
        if slot.start > previous_end:
            return REASON_COVERAGE_GAP
        if slot.start < previous_end:
            return REASON_COVERAGE_OVERLAP
        previous_end = slot.end
    if previous_end < horizon_end:
        return REASON_COVERAGE_GAP
    return None


def _slot_capacity_kwh(
    slot: HorizonSlot,
    ceiling_w: float,
    total_discharge_power_w: float,
) -> float:
    """Return how much this slot may export, net at the meter.

    Two independent ceilings apply: the net export allowed at the connection
    point (after whatever solar/other sources already export), and the
    battery power left over once the household's own deficit is served. The
    smaller one wins; the household's consumption never expands the export
    ceiling, only the battery-side discharge budget.
    """
    duration = slot.duration_hours
    if duration <= EPSILON:
        return 0.0

    non_battery_net_export_w = (
        max(0.0, slot.solar_kwh - slot.consumption_kwh) / duration * 1000.0
    )
    net_export_headroom_w = max(0.0, ceiling_w - non_battery_net_export_w)

    household_deficit_w = max(0.0, slot.consumption_kwh - slot.solar_kwh) / duration * 1000.0
    battery_export_headroom_w = max(0.0, total_discharge_power_w - household_deficit_w)

    slot_export_power_w = min(net_export_headroom_w, battery_export_headroom_w)
    return slot_export_power_w * duration / 1000.0


def _solar_refill_price(
    fill_slots: Sequence[HorizonSlot],
    horizon_end: datetime,
    start_level: float,
    full_level: float,
    efficiency: float,
) -> float | None:
    """Price of solar displaced while refilling; no proven full refill means no sale."""
    level = start_level
    previous_end = horizon_end
    max_export = 0.0
    if not (_finite(level) and _finite(full_level)):
        return None
    if level >= full_level - EPSILON:
        return 0.0
    for slot in fill_slots:
        try:
            if not (_aware(slot.start) and _aware(slot.end)
                    and _finite(slot.solar_kwh) and _finite(slot.consumption_kwh)):
                return None
            start, end = _utc(slot.start), _utc(slot.end)
            if start != previous_end or end <= start:
                return None
            net = float(slot.solar_kwh) - float(slot.consumption_kwh)
            if not _finite(net):
                return None
            if net > 0:
                if not _finite(slot.export_price):
                    return None
                max_export = max(max_export, float(slot.export_price))
                level += net * efficiency
            else:
                level += net
            if not _finite(level) or level < -EPSILON:
                return None
            if level >= full_level - EPSILON:
                return max_export
            previous_end = end
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def plan_high_price_discharge(
    slots: Sequence[HorizonSlot],
    batteries: Sequence[BatterySnapshot] = (),
    *,
    now: datetime,
    horizon_end: datetime,
    enabled: bool = False,
    trigger_1_enabled: bool = False,
    additional_cost_per_kwh: float = 0.0,
    max_export_power_w: float | None = None,
    discharge_efficiency: float = 1.0,
    safety_margin_kwh: float = 0.0,
    fill_slots: Sequence[HorizonSlot] = (),
    chronological_coverage: bool = True,
) -> HighPriceDischargePlan:
    """Build a discharge plan from a horizon of atomic slots.

    ``horizon_end`` is the end of the protected period (the runtime passes
    ``PricingManager.energy_horizon_end(now)``, next day's sunrise). This
    planner treats it as an opaque boundary: it never searches for the
    solar-recovery crossing itself.
    """
    if not enabled and not trigger_1_enabled:
        return HighPriceDischargePlan(
            status=STATUS_DISABLED, reason=REASON_DISABLED, evaluation_time=now
        )

    if not _aware(now):
        return HighPriceDischargePlan(status=STATUS_FAIL_SAFE, reason=REASON_INVALID_NOW)

    if not _aware(horizon_end):
        return HighPriceDischargePlan(
            status=STATUS_FAIL_SAFE, reason=REASON_INVALID_HORIZON_END, evaluation_time=now
        )

    now, horizon_end = _utc(now), _utc(horizon_end)
    if not horizon_end > now:
        return HighPriceDischargePlan(
            status=STATUS_FAIL_SAFE,
            reason=REASON_INVALID_HORIZON_END,
            evaluation_time=now,
            horizon_end=horizon_end,
        )

    if not _finite(additional_cost_per_kwh) or float(additional_cost_per_kwh) < 0:
        return HighPriceDischargePlan(
            status=STATUS_FAIL_SAFE,
            reason=REASON_INVALID_CONFIGURATION,
            evaluation_time=now,
            horizon_end=horizon_end,
        )
    additional_cost = float(additional_cost_per_kwh)
    if not _finite(safety_margin_kwh) or float(safety_margin_kwh) < 0:
        return HighPriceDischargePlan(
            status=STATUS_FAIL_SAFE,
            reason=REASON_INVALID_CONFIGURATION,
            evaluation_time=now,
            horizon_end=horizon_end,
        )
    margin = float(safety_margin_kwh)

    # ``None`` means "no limit" and is valid; anything else must be a finite,
    # non-negative watt figure (zero legitimately forbids export).
    if max_export_power_w is not None and (
        not _finite(max_export_power_w) or float(max_export_power_w) < 0
    ):
        return HighPriceDischargePlan(
            status=STATUS_FAIL_SAFE,
            reason=REASON_INVALID_CONFIGURATION,
            evaluation_time=now,
            horizon_end=horizon_end,
        )
    ceiling_w = float(max_export_power_w) if max_export_power_w is not None else math.inf

    if not _finite(discharge_efficiency) or float(discharge_efficiency) <= 0:
        return HighPriceDischargePlan(
            status=STATUS_FAIL_SAFE,
            reason=REASON_INVALID_CONFIGURATION,
            evaluation_time=now,
            horizon_end=horizon_end,
        )
    efficiency = float(discharge_efficiency)

    working = _trim_slots(slots, now, horizon_end)
    if not working:
        return HighPriceDischargePlan(
            status=STATUS_FAIL_SAFE,
            reason=REASON_NO_SLOTS_IN_WINDOW,
            evaluation_time=now,
            horizon_end=horizon_end,
        )

    coverage_reason = _coverage_gap_reason(working, now, horizon_end)
    if coverage_reason is not None:
        return HighPriceDischargePlan(
            status=STATUS_FAIL_SAFE,
            reason=coverage_reason,
            evaluation_time=now,
            horizon_end=horizon_end,
        )

    # Intermediate surpluses never repay an earlier or later deficit: the
    # protected demand is the plain sum of every deficit in the window.
    protected_demand_kwh = sum(
        max(0.0, slot.consumption_kwh - slot.solar_kwh) for slot in working
    )

    ledger = [
        _LedgerEntry(slot.start, slot.end, deficit, slot.import_price)
        for slot in working
        if (deficit := max(0.0, slot.consumption_kwh - slot.solar_kwh)) > EPSILON
    ]

    valid_batteries = [battery for battery in batteries if _valid_battery(battery)]
    usable_energy_kwh = sum(
        max(0.0, (battery.soc_pct - battery.floor_soc_pct) / 100.0 * battery.capacity_kwh)
        * efficiency
        for battery in valid_batteries
    )
    total_discharge_power_w = sum(
        battery.max_discharge_power_w for battery in valid_batteries
    )

    allocations: list[TriggerAllocation] = []
    surplus_by_start: dict[datetime, TriggerAllocation] = {}
    budget = 0.0
    refill_price: float | None = None
    trigger_1_reason = REASON_DISABLED
    if trigger_1_enabled:
        budget = max(0.0, usable_energy_kwh - protected_demand_kwh - margin)
        if budget <= EPSILON:
            trigger_1_reason = REASON_NO_SURPLUS
        else:
            # A pack below its floor supplies nothing to the sale, but it still
            # soaks up tomorrow's solar first: its gap to the floor is extra
            # room, which delays the fill and can only raise the refill price.
            full_level = sum(
                max(0.0, (battery.max_soc_pct
                          - min(battery.soc_pct, battery.floor_soc_pct)) / 100.0
                    * battery.capacity_kwh) * efficiency
                for battery in valid_batteries
            )
            refill_price = _solar_refill_price(
                fill_slots, horizon_end,
                max(0.0, usable_energy_kwh - protected_demand_kwh - budget),
                full_level, efficiency,
            )
            if refill_price is None:
                trigger_1_reason = REASON_NO_REFILL_PRICE
            else:
                trigger_1_reason = REASON_NO_ELIGIBLE_CANDIDATES
                threshold = refill_price / efficiency + additional_cost
                budget_left = budget
                ranked = sorted(
                    (slot for slot in working if _finite(slot.export_price)
                     and float(slot.export_price) > threshold),
                    key=lambda slot: (-float(slot.export_price), slot.start),
                )
                for slot in ranked:
                    if budget_left <= EPSILON:
                        break
                    take = min(
                        _slot_capacity_kwh(slot, ceiling_w, total_discharge_power_w),
                        budget_left,
                    )
                    if take <= EPSILON:
                        continue
                    allocation = TriggerAllocation(
                        start=slot.start,
                        end=slot.end,
                        export_price=float(slot.export_price),
                        threshold=threshold,
                        energy_kwh=take,
                        power_w=take / slot.duration_hours * 1000.0,
                        surplus_kwh=take,
                        surplus_threshold=threshold,
                    )
                    allocations.append(allocation)
                    surplus_by_start[slot.start] = allocation
                    budget_left -= take
                    trigger_1_reason = REASON_PLANNED

    remaining_usable_kwh = usable_energy_kwh - sum(
        allocation.surplus_kwh for allocation in allocations
    )
    tail_coverage = (
        chronological_coverage
        and remaining_usable_kwh < protected_demand_kwh - EPSILON
    )
    if tail_coverage:
        # Selling advances run-out: only the latest demand the battery would
        # have covered becomes a grid purchase, never demand after run-out.
        covered_left = remaining_usable_kwh
        for entry in ledger:
            covered = min(entry.remaining_kwh, max(0.0, covered_left))
            entry.remaining_kwh = covered
            covered_left -= covered
        tail = list(reversed(ledger))

    candidates: list[tuple[HorizonSlot, float]] = []
    for index, slot in enumerate(working if enabled else ()):
        if slot.export_price is None or not _finite(slot.export_price):
            continue
        later_slots = working[index + 1 :]
        if not later_slots:
            # Nothing can be linked after the last slot in the window either,
            # so this candidate could never carry a demand link regardless.
            continue
        if any(
            later.import_price is None or not _finite(later.import_price)
            for later in later_slots
        ):
            continue
        threshold = max(float(later.import_price) for later in later_slots) + additional_cost
        if not tail_coverage and not float(slot.export_price) > threshold:
            continue
        candidates.append((slot, threshold))

    # Highest export price first; chronological order breaks ties so a
    # reevaluation with the same data always yields the same plan (RF-045).
    candidates.sort(key=lambda item: (-item[0].export_price, item[0].start))

    for slot, threshold in candidates:
        if remaining_usable_kwh <= EPSILON:
            break
        surplus = surplus_by_start.get(slot.start)
        capacity_kwh = max(
            0.0,
            _slot_capacity_kwh(slot, ceiling_w, total_discharge_power_w)
            - (surplus.surplus_kwh if surplus else 0.0),
        )
        if capacity_kwh <= EPSILON:
            continue

        links: list[DemandLink] = []
        if tail_coverage:
            threshold = -math.inf
            left_to_assign = min(capacity_kwh, remaining_usable_kwh)
            for entry in tail:
                if left_to_assign <= EPSILON:
                    break
                if entry.remaining_kwh <= EPSILON:
                    continue
                if (entry.start < slot.end or not _finite(entry.import_price)
                        or float(slot.export_price) <= float(entry.import_price) + additional_cost):
                    break
                take = min(left_to_assign, entry.remaining_kwh)
                entry.remaining_kwh -= take
                links.append(DemandLink(entry.start, entry.end, take))
                threshold = max(threshold, float(entry.import_price) + additional_cost)
                left_to_assign -= take
            assigned_kwh = sum(link.energy_kwh for link in links)
            if assigned_kwh <= EPSILON:
                continue
        else:
            linkable = sorted(
                (entry for entry in ledger if entry.start >= slot.end and entry.remaining_kwh > EPSILON),
                key=lambda entry: entry.start,
            )
            linkable_demand_kwh = sum(entry.remaining_kwh for entry in linkable)
            allocation_kwh = min(capacity_kwh, remaining_usable_kwh, linkable_demand_kwh)
            if allocation_kwh <= EPSILON:
                continue

            # Consume the earliest unassigned demand first: it preserves later
            # demand for later (lower-priced) candidates, which cannot reach back
            # to demand that ends before their own slot.
            left_to_assign = allocation_kwh
            for entry in linkable:
                if left_to_assign <= EPSILON:
                    break
                take = min(left_to_assign, entry.remaining_kwh)
                entry.remaining_kwh -= take
                links.append(DemandLink(entry.start, entry.end, take))
                left_to_assign -= take
            assigned_kwh = allocation_kwh - left_to_assign

        duration = slot.duration_hours
        power_w = assigned_kwh / duration * 1000.0 if duration > EPSILON else 0.0
        if surplus:
            merged = replace(
                surplus,
                threshold=threshold,
                energy_kwh=surplus.energy_kwh + assigned_kwh,
                power_w=surplus.power_w + power_w,
                demand_links=tuple(links),
            )
            allocations[allocations.index(surplus)] = merged
        else:
            allocations.append(
                TriggerAllocation(
                    start=slot.start,
                    end=slot.end,
                    export_price=float(slot.export_price),
                    threshold=threshold,
                    energy_kwh=assigned_kwh,
                    power_w=power_w,
                    demand_links=tuple(links),
                )
            )
        remaining_usable_kwh -= assigned_kwh

    if allocations:
        status, reason = STATUS_PLANNED, REASON_PLANNED
    elif not enabled:
        status, reason = STATUS_NO_OPPORTUNITY, trigger_1_reason
    elif protected_demand_kwh <= EPSILON:
        status, reason = STATUS_NO_OPPORTUNITY, REASON_NO_PROTECTED_DEMAND
    elif usable_energy_kwh <= EPSILON:
        status, reason = STATUS_NO_OPPORTUNITY, REASON_NO_USABLE_ENERGY
    else:
        status, reason = STATUS_NO_OPPORTUNITY, REASON_NO_ELIGIBLE_CANDIDATES

    return HighPriceDischargePlan(
        status=status,
        reason=reason,
        evaluation_time=now,
        horizon_end=horizon_end,
        protected_demand_kwh=protected_demand_kwh,
        usable_energy_kwh=usable_energy_kwh,
        allocations=tuple(sorted(allocations, key=lambda allocation: allocation.start)),
        total_allocated_kwh=sum(allocation.energy_kwh for allocation in allocations),
        trigger_1_budget_kwh=budget,
        refill_price=refill_price,
        trigger_1_reason=trigger_1_reason,
    )
