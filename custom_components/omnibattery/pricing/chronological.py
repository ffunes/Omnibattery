"""Pure chronological planner for predictive grid charging.

The module deliberately has no Home Assistant dependencies.  Energies are
expressed as kWh stored in the batteries; slot input power is derated by the
charge efficiency when its usable capacity is calculated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
from typing import Iterable, Sequence

from ..const import CHARGE_EFFICIENCY
from . import PriceSlot

ENERGY_TOLERANCE_KWH = 0.05
_EPSILON = 1e-9


@dataclass(frozen=True)
class EnergyInterval:
    start: datetime
    end: datetime
    consumption_kwh: float
    solar_kwh: float

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("energy interval end must be after start")
        for name in ("consumption_kwh", "solar_kwh"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True)
class EnergyDeadline:
    deadline: datetime
    required_cumulative_kwh: float
    kind: str = "depletion"
    projected_soc_pct: float | None = None


@dataclass(frozen=True)
class SlotAllocation:
    slot: PriceSlot
    planned_battery_kwh: float
    deadline: datetime | None
    kind: str


@dataclass(frozen=True)
class EnergySimulationResult:
    minimum_projected_energy_kwh: float
    earliest_depletion_at: datetime | None
    final_projected_energy_kwh: float
    trajectory: tuple[tuple[datetime, float], ...] = ()


@dataclass
class ChronologicalPlan:
    intervals: list[EnergyInterval] = field(default_factory=list)
    deadlines: list[EnergyDeadline] = field(default_factory=list)
    allocations: list[SlotAllocation] = field(default_factory=list)
    total_required_kwh: float = 0.0
    deadline_required_kwh: float = 0.0
    flexible_required_kwh: float = 0.0
    allocated_kwh: float = 0.0
    deadline_shortfall_kwh: float = 0.0
    total_shortfall_kwh: float = 0.0
    earliest_depletion_at: datetime | None = None
    minimum_projected_energy_kwh: float = 0.0
    reason: str = "ok"


@dataclass(frozen=True)
class ChronologicalEvaluationRequest:
    """Immutable input for a chronological allocation evaluation.

    This is deliberately independent from the Home Assistant controller and
    from its mutable decision dictionary.  Callers that need live forecasts
    adapt them before constructing this value; visual projections can use the
    same evaluator without gaining any path to runtime control state.
    """

    now: datetime
    horizon_end: datetime
    intervals: tuple[EnergyInterval, ...]
    price_slots: tuple[PriceSlot, ...]
    total_required_kwh: float
    effective_power_kw: float
    headroom_kwh: float = math.inf
    usable_initial_kwh: float = 0.0
    max_price_threshold: float | None = None
    deadlines: tuple[EnergyDeadline, ...] = ()

    def __post_init__(self) -> None:
        if self.horizon_end <= self.now:
            raise ValueError("chronological horizon must end after now")
        # Accept ordinary sequences at the public boundary but retain only
        # immutable containers while the evaluator is running.
        object.__setattr__(self, "intervals", tuple(self.intervals))
        object.__setattr__(self, "price_slots", tuple(self.price_slots))
        object.__setattr__(self, "deadlines", tuple(self.deadlines))


@dataclass(frozen=True)
class ChronologicalDiagnostics:
    """Read-only diagnostics produced by a chronological evaluation."""

    earliest_projected_depletion: datetime | None
    minimum_projected_energy_kwh: float
    deadline_required_kwh: float
    flexible_required_kwh: float
    deadline_shortfall_kwh: float
    total_shortfall_kwh: float
    energy_deadlines: tuple[EnergyDeadline, ...]
    reason: str


@dataclass(frozen=True)
class ChronologicalEvaluationResult:
    """Pure evaluation output: allocation plan plus its diagnostics."""

    plan: ChronologicalPlan
    diagnostics: ChronologicalDiagnostics


def _finite_non_negative(value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) and parsed > 0 else 0.0


def normalize_energy_shape(values: Sequence[float], total_kwh: float) -> list[float]:
    """Return a safe shape whose sum equals ``total_kwh`` (within float error)."""
    total_kwh = _finite_non_negative(total_kwh)
    safe = [_finite_non_negative(value) for value in values]
    shape_total = sum(safe)
    if not safe:
        return []
    if shape_total <= _EPSILON:
        return [0.0] * len(safe)
    result = [value * total_kwh / shape_total for value in safe]
    result[-1] += total_kwh - sum(result)
    return result


def build_energy_intervals(
    boundaries: Sequence[tuple[datetime, datetime]],
    consumption_shape: Sequence[float],
    solar_shape: Sequence[float],
    *,
    consumption_total_kwh: float | None = None,
    solar_total_kwh: float | None = None,
) -> list[EnergyInterval]:
    """Combine matching temporal shapes into validated energy intervals."""
    if not (len(boundaries) == len(consumption_shape) == len(solar_shape)):
        raise ValueError("boundaries and energy shapes must have equal lengths")
    consumption = (
        normalize_energy_shape(consumption_shape, consumption_total_kwh)
        if consumption_total_kwh is not None
        else [_finite_non_negative(v) for v in consumption_shape]
    )
    solar = (
        normalize_energy_shape(solar_shape, solar_total_kwh)
        if solar_total_kwh is not None
        else [_finite_non_negative(v) for v in solar_shape]
    )
    return [
        EnergyInterval(start, end, consumption[index], solar[index])
        for index, (start, end) in enumerate(boundaries)
    ]


def simulate_allocations(
    intervals: Iterable[EnergyInterval],
    usable_initial_kwh: float,
    allocations: Iterable[SlotAllocation] = (),
) -> EnergySimulationResult:
    """Simulate the stored usable energy at each interval boundary."""
    ordered = sorted(intervals, key=lambda item: item.start)
    energy = _finite_non_negative(usable_initial_kwh)
    minimum = energy
    earliest: datetime | None = None
    trajectory: list[tuple[datetime, float]] = []
    allocated = list(allocations)
    applied_allocations: set[int] = set()
    for interval in ordered:
        # Treat an allocation as available once the usable part of its price
        # slot has ended.  Deadline allocation already caps crossing slots to
        # the capacity deliverable before the deadline.
        for index, allocation in enumerate(allocated):
            if index not in applied_allocations and allocation.slot.end <= interval.end:
                energy += allocation.planned_battery_kwh
                applied_allocations.add(index)
        energy += interval.solar_kwh - interval.consumption_kwh
        trajectory.append((interval.end, energy))
        if energy < minimum:
            minimum = energy
        if earliest is None and energy < -_EPSILON:
            earliest = interval.end
    return EnergySimulationResult(minimum, earliest, energy, tuple(trajectory))


def build_energy_deadlines(
    intervals: Iterable[EnergyInterval],
    usable_initial_kwh: float,
    *,
    tolerance_kwh: float = ENERGY_TOLERANCE_KWH,
    kind: str = "depletion",
    total_capacity_kwh: float | None = None,
    minimum_soc_pct: float | None = None,
) -> list[EnergyDeadline]:
    """Derive monotonic cumulative requirements from the no-grid trajectory."""
    intervals = sorted(intervals, key=lambda item: item.start)
    usable = _finite_non_negative(usable_initial_kwh)
    tolerance = max(0.0, float(tolerance_kwh))
    prefix_net = 0.0
    maximum_required = 0.0
    emitted_required = 0.0
    deadlines: list[EnergyDeadline] = []
    for interval in intervals:
        prefix_net += interval.consumption_kwh - interval.solar_kwh
        required = max(0.0, prefix_net - usable)
        maximum_required = max(maximum_required, required)
        if maximum_required <= emitted_required + tolerance:
            continue
        projected_soc = None
        if total_capacity_kwh and total_capacity_kwh > 0 and minimum_soc_pct is not None:
            projected_soc = minimum_soc_pct - maximum_required / total_capacity_kwh * 100.0
        deadlines.append(
            EnergyDeadline(interval.end, maximum_required, kind, projected_soc)
        )
        emitted_required = maximum_required

    # Never hide a final sub-tolerance increase: update the last cumulative
    # requirement while retaining its grouped deadline.
    if deadlines and maximum_required > deadlines[-1].required_cumulative_kwh + _EPSILON:
        last = deadlines[-1]
        deadlines[-1] = EnergyDeadline(
            last.deadline, maximum_required, last.kind, last.projected_soc_pct
        )
    return deadlines


def _duration_hours(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() / 3600.0)


def allocate_price_slots(
    intervals: Iterable[EnergyInterval],
    deadlines: Iterable[EnergyDeadline],
    price_slots: Iterable[PriceSlot],
    *,
    total_required_kwh: float,
    effective_power_kw: float,
    now: datetime,
    horizon_end: datetime,
    headroom_kwh: float = math.inf,
    usable_initial_kwh: float = 0.0,
    max_price_threshold: float | None = None,
    charge_efficiency: float = CHARGE_EFFICIENCY,
) -> ChronologicalPlan:
    """Allocate the cheapest feasible slot capacity to nested deadlines."""
    intervals = sorted(intervals, key=lambda item: item.start)
    deadlines = sorted(deadlines, key=lambda item: item.deadline)
    required = min(
        _finite_non_negative(total_required_kwh),
        max(0.0, float(headroom_kwh)),
    )
    power = _finite_non_negative(effective_power_kw)
    efficiency = max(0.0, min(1.0, float(charge_efficiency)))
    candidates = [
        slot for slot in price_slots
        if slot.end > now
        and slot.start < horizon_end
        and math.isfinite(float(slot.price))
        and (max_price_threshold is None or slot.price <= max_price_threshold)
    ]

    allocations: dict[PriceSlot, float] = {}
    allocation_deadlines: dict[PriceSlot, datetime | None] = {}
    deadline_required = min(
        required,
        max((d.required_cumulative_kwh for d in deadlines), default=0.0),
    )
    deadline_shortfall = 0.0

    def capacity(slot: PriceSlot, deadline: datetime | None = None) -> float:
        start = max(now, slot.start)
        end = min(slot.end, horizon_end, deadline or horizon_end)
        return power * _duration_hours(start, end) * efficiency

    for deadline in deadlines:
        cumulative_target = min(required, deadline.required_cumulative_kwh)
        already = sum(
            amount
            for slot, amount in allocations.items()
            if slot.start < deadline.deadline
        )
        missing = max(0.0, cumulative_target - already)
        eligible = sorted(
            (slot for slot in candidates if slot.start < deadline.deadline),
            key=lambda slot: (
                slot.price,
                0 if slot in allocations else 1,
                -capacity(slot, deadline.deadline),
                slot.start,
            ),
        )
        for slot in eligible:
            free = max(0.0, capacity(slot, deadline.deadline) - allocations.get(slot, 0.0))
            take = min(missing, free)
            if take <= _EPSILON:
                continue
            allocations[slot] = allocations.get(slot, 0.0) + take
            previous = allocation_deadlines.get(slot)
            allocation_deadlines[slot] = (
                deadline.deadline if previous is None else min(previous, deadline.deadline)
            )
            missing -= take
            if missing <= _EPSILON:
                break
        deadline_shortfall = max(deadline_shortfall, missing)

    flexible_required = max(0.0, required - deadline_required)
    # A missed deadline is intentionally not relabelled as flexible work.  It
    # remains a chronological shortfall instead of being made to look covered
    # by energy delivered after the need occurred.
    missing_total = flexible_required
    for slot in sorted(candidates, key=lambda item: (item.price, item.start)):
        free = max(0.0, capacity(slot) - allocations.get(slot, 0.0))
        take = min(missing_total, free)
        if take <= _EPSILON:
            continue
        allocations[slot] = allocations.get(slot, 0.0) + take
        allocation_deadlines.setdefault(slot, None)
        missing_total -= take
        if missing_total <= _EPSILON:
            break

    result_allocations = [
        SlotAllocation(
            slot,
            amount,
            allocation_deadlines.get(slot),
            "deadline" if allocation_deadlines.get(slot) is not None else "flexible",
        )
        for slot, amount in sorted(allocations.items(), key=lambda item: item[0].start)
    ]
    simulation = simulate_allocations(intervals, usable_initial_kwh)
    allocated = sum(allocations.values())
    total_shortfall = max(0.0, required - allocated)
    if power <= 0:
        reason = "insufficient_power"
    elif not candidates and required > 0:
        reason = "price_threshold" if max_price_threshold is not None else "no_eligible_slots"
    elif deadline_shortfall > _EPSILON:
        reason = "deadline_capacity_shortfall"
    elif total_shortfall > _EPSILON:
        reason = "insufficient_slot_capacity"
    else:
        reason = "ok"
    earliest = simulate_allocations(intervals, usable_initial_kwh).earliest_depletion_at
    return ChronologicalPlan(
        intervals=intervals,
        deadlines=deadlines,
        allocations=result_allocations,
        total_required_kwh=required,
        deadline_required_kwh=deadline_required,
        flexible_required_kwh=flexible_required,
        allocated_kwh=allocated,
        deadline_shortfall_kwh=deadline_shortfall,
        total_shortfall_kwh=total_shortfall,
        earliest_depletion_at=earliest,
        minimum_projected_energy_kwh=simulation.minimum_projected_energy_kwh,
        reason=reason,
    )


def evaluate_chronological_request(
    request: ChronologicalEvaluationRequest,
) -> ChronologicalEvaluationResult:
    """Evaluate an immutable chronological request without runtime side effects.

    The caller may provide already-computed deadlines (for example a guaranteed
    minimum-SOC floor).  If it does not, ordinary depletion deadlines are
    derived from the supplied intervals.  No input collection, controller
    access or diagnostics persistence happens in this function.
    """
    intervals = tuple(request.intervals)
    deadlines = (
        tuple(request.deadlines)
        if request.deadlines
        else tuple(build_energy_deadlines(intervals, request.usable_initial_kwh))
    )
    plan = allocate_price_slots(
        intervals,
        deadlines,
        request.price_slots,
        total_required_kwh=request.total_required_kwh,
        effective_power_kw=request.effective_power_kw,
        now=request.now,
        horizon_end=request.horizon_end,
        headroom_kwh=request.headroom_kwh,
        usable_initial_kwh=request.usable_initial_kwh,
        max_price_threshold=request.max_price_threshold,
    )
    return ChronologicalEvaluationResult(
        plan=plan,
        diagnostics=ChronologicalDiagnostics(
            earliest_projected_depletion=plan.earliest_depletion_at,
            minimum_projected_energy_kwh=plan.minimum_projected_energy_kwh,
            deadline_required_kwh=plan.deadline_required_kwh,
            flexible_required_kwh=plan.flexible_required_kwh,
            deadline_shortfall_kwh=plan.deadline_shortfall_kwh,
            total_shortfall_kwh=plan.total_shortfall_kwh,
            energy_deadlines=tuple(plan.deadlines),
            reason=plan.reason,
        ),
    )
