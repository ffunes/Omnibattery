"""Pure chronological planner for predictive grid charging.

The module deliberately has no Home Assistant dependencies.  Energies are
expressed as kWh stored in the batteries; slot input power is derated by the
charge efficiency when its usable capacity is calculated.
"""
from __future__ import annotations

from bisect import bisect_right, insort
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import math
from typing import Iterable, Mapping, Sequence

from ..const import CHARGE_EFFICIENCY
from . import PriceSlot

ENERGY_TOLERANCE_KWH = 0.05
_EPSILON = 1e-9
# value_cap of a deadline past the last published price.
_UNPRICED = -math.inf


@dataclass(frozen=True)
class EnergyInterval:
    start: datetime
    end: datetime
    consumption_kwh: float
    solar_kwh: float
    # The real house load when ``consumption_kwh`` was capped to what the
    # battery can deliver (``cap_battery_drain``). ``consumption_kwh`` is the
    # battery drain; the grid import ceiling must still see the whole load.
    house_load_kwh: float | None = None

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("energy interval end must be after start")
        for name in ("consumption_kwh", "solar_kwh"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.house_load_kwh is not None:
            value = float(self.house_load_kwh)
            if not math.isfinite(value) or value < 0:
                raise ValueError("house_load_kwh must be finite and non-negative")

    @property
    def grid_load_kwh(self) -> float:
        """House load the grid import ceiling sees (uncapped)."""
        if self.house_load_kwh is None:
            return self.consumption_kwh
        return max(self.consumption_kwh, self.house_load_kwh)

    def with_battery_idle(self) -> "EnergyInterval":
        """Interval in which the battery may not discharge.

        The grid carries the load the battery would have covered, but the
        household still consumes solar first: only the surplus charges (#616).
        """
        return EnergyInterval(
            self.start,
            self.end,
            0.0,
            max(0.0, self.solar_kwh - self.consumption_kwh),
        )


def cap_battery_drain(
    intervals: Iterable[EnergyInterval], discharge_kw: float | None
) -> list[EnergyInterval]:
    """Limit each interval's battery drain to what the batteries can deliver.

    The house load above the fleet's discharge power comes from the grid,
    not from the battery. Projecting the whole load as drain sized the night
    charge for energy the battery could never hand out, so it reached sunrise
    half full. Solar still feeds the house first. The uncapped load is kept
    in ``house_load_kwh`` for the grid import ceiling.
    """
    items = list(intervals)
    if discharge_kw is None or not math.isfinite(float(discharge_kw)):
        return items
    rate = max(0.0, float(discharge_kw))
    capped: list[EnergyInterval] = []
    for item in items:
        limit = item.solar_kwh + rate * _duration_hours(item.start, item.end)
        if item.consumption_kwh > limit:
            item = EnergyInterval(
                item.start, item.end, limit, item.solar_kwh, item.grid_load_kwh
            )
        capped.append(item)
    return capped


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
    # Deadline energy left to the grid: importing it when it is used costs
    # less than storing it earlier with the round-trip losses (#662).
    cheaper_to_import_kwh: float = 0.0
    # The part of ``cheaper_to_import_kwh`` left to the grid only because its
    # import price is not published yet (not because importing is cheaper).
    awaiting_prices_kwh: float = 0.0
    # Cheapest slot the price limits rejected, as (start, price, ceiling), so a
    # ``price_threshold`` plan shows by how much it missed.
    cheapest_rejected: tuple[datetime, float, float | None] | None = None


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
    # Guaranteed-floor energy that may be bought above a slot's ceiling
    # (``slot_ceilings``, else ``max_price_threshold``), capped only by
    # ``premium_price_threshold``.
    premium_kwh: float = 0.0
    premium_price_threshold: float | None = None
    # Per-slot (arbitrage) ceilings keyed by ``slot.start``; a slot missing from
    # the mapping falls back to ``max_price_threshold``.
    slot_ceilings: Mapping[datetime, float] | None = None
    # Size the plan from the deadlines when they ask for more than
    # ``total_required_kwh``. A scalar balance over a multi-day horizon nets
    # tomorrow's solar against tonight's gap, which the deadlines do not (#710).
    required_from_deadlines: bool = False
    # Value each ordinary deadline against the import price of the interval it
    # serves (#662): a slot charges for it only when
    #   slot_price <= import_price * import_value_efficiency - import_value_margin.
    # None leaves the deadlines priced by the ceilings alone (Time Slot mode,
    # whose slot prices are synthetic).
    import_value_efficiency: float | None = None
    import_value_margin: float = 0.0
    # The discharge reserve keeps stored energy for dearer hours until the sun
    # covers the house, so a valued deadline may be served across an hour left
    # to the grid up to then.
    reserve_holds: bool = False
    # While a slot charges, the grid carries the house load, so the battery
    # does not drain then and needs that much less charge. Off for a weekly
    # full charge, whose target is the gap to 100%, not a drain. The runtime
    # planner turns this on; the pure evaluator keeps the plain model.
    grid_carries_load_while_charging: bool = False
    # The energy the battery would still need without the room limit (the
    # deficit after the load above the discharge power). When it exceeds
    # ``total_required_kwh`` the plan is bound by the room, and the load the
    # grid carries while a slot charges only cancels the need beyond it.
    # None keeps the plain trim (the need is the total).
    uncapped_required_kwh: float | None = None
    # Grid import ceiling (kW) the controller regulates to while a slot
    # charges. The house load comes out of it, so a slot stores at most
    # ``min(power, ceiling - net load)``. None keeps the flat ``power``.
    grid_ceiling_kw: float | None = None

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
    cheaper_to_import_kwh: float = 0.0
    cheapest_rejected: tuple[datetime, float, float | None] | None = None
    awaiting_prices_kwh: float = 0.0


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
    *,
    usable_capacity_kwh: float = math.inf,
) -> EnergySimulationResult:
    """Simulate the stored usable energy at each interval boundary."""
    ordered = sorted(intervals, key=lambda item: item.start)
    capacity = max(0.0, float(usable_capacity_kwh))
    energy = min(_finite_non_negative(usable_initial_kwh), capacity)
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
        energy = min(
            capacity,
            energy + interval.solar_kwh - interval.consumption_kwh,
        )
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
    usable_capacity_kwh: float = math.inf,
) -> list[EnergyDeadline]:
    """Derive cumulative requirements from the capacity-bounded trajectory."""
    intervals = sorted(intervals, key=lambda item: item.start)
    capacity = max(0.0, float(usable_capacity_kwh))
    segment_initial = min(_finite_non_negative(usable_initial_kwh), capacity)
    tolerance = max(0.0, float(tolerance_kwh))
    prefix_net = 0.0
    maximum_required = 0.0
    emitted_required = 0.0
    segment_deadline_index: int | None = None
    deadlines: list[EnergyDeadline] = []
    for interval in intervals:
        prefix_net += interval.consumption_kwh - interval.solar_kwh
        required = max(0.0, prefix_net - segment_initial)
        maximum_required = max(maximum_required, required)
        projected = segment_initial - prefix_net + maximum_required
        if capacity > _EPSILON and projected >= capacity:
            if segment_deadline_index is not None:
                last = deadlines[segment_deadline_index]
                if maximum_required > last.required_cumulative_kwh + _EPSILON:
                    deadlines[segment_deadline_index] = EnergyDeadline(
                        last.deadline,
                        maximum_required,
                        last.kind,
                        last.projected_soc_pct,
                    )
            segment_initial = capacity
            prefix_net = 0.0
            maximum_required = 0.0
            emitted_required = 0.0
            segment_deadline_index = None
            continue
        if maximum_required <= emitted_required + tolerance:
            continue
        projected_soc = None
        if total_capacity_kwh and total_capacity_kwh > 0 and minimum_soc_pct is not None:
            projected_soc = minimum_soc_pct - maximum_required / total_capacity_kwh * 100.0
        deadlines.append(
            EnergyDeadline(interval.end, maximum_required, kind, projected_soc)
        )
        emitted_required = maximum_required
        segment_deadline_index = len(deadlines) - 1

    # Never hide a final sub-tolerance increase: update the last cumulative
    # requirement while retaining its grouped deadline.
    if segment_deadline_index is not None:
        last = deadlines[segment_deadline_index]
        if maximum_required > last.required_cumulative_kwh + _EPSILON:
            deadlines[segment_deadline_index] = EnergyDeadline(
                last.deadline, maximum_required, last.kind, last.projected_soc_pct
            )
    return deadlines


def _duration_hours(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() / 3600.0)


def _net_of_load_carried_by_grid(
    intervals: Sequence[EnergyInterval],
    amount_kwh: float,
    start: datetime,
    end: datetime,
    rate_kwh_h: float,
) -> float:
    """Battery energy a slot still has to store when the grid feeds the house.

    The intervals drain the battery by the house load in every interval,
    including those in which a slot is charging. The grid carries that load
    while the slot charges, so the battery needs less. The charge starts at
    ``start`` and stops once stored energy plus avoided drain reaches
    ``amount_kwh``; a shorter charge carries less load, so this solves
    ``stored + avoided = amount`` along the intervals.
    """
    amount = _finite_non_negative(amount_kwh)
    rate = _finite_non_negative(rate_kwh_h)
    if amount <= _EPSILON or rate <= _EPSILON or end <= start:
        return amount
    stored = 0.0
    avoided = 0.0
    for interval in intervals:
        if interval.end <= start:
            continue
        if interval.start >= end:
            break
        seg_start = max(start, interval.start)
        seg_end = min(end, interval.end)
        hours = _duration_hours(seg_start, seg_end)
        interval_hours = _duration_hours(interval.start, interval.end)
        if hours <= 0.0 or interval_hours <= 0.0:
            continue
        # Solar feeds the house first; only the rest would have come out of
        # the battery.
        load_rate = (
            max(0.0, interval.consumption_kwh - interval.solar_kwh) / interval_hours
        )
        left = amount - stored - avoided
        if left <= _EPSILON:
            break
        needed_hours = left / (rate + load_rate)
        if needed_hours <= hours:
            avoided += load_rate * needed_hours
            break
        stored += rate * hours
        avoided += load_rate * hours
    return max(0.0, amount - min(avoided, amount))


def _load_carried_while_storing(
    intervals: Sequence[EnergyInterval],
    stored_kwh: float,
    start: datetime,
    end: datetime,
    rate_kwh_h: float,
) -> float:
    """House load the grid carries while a slot stores ``stored_kwh``.

    The slot charges at ``rate_kwh_h`` from ``start`` until the energy is
    stored (or ``end``); only the load solar does not cover counts.
    """
    rate = _finite_non_negative(rate_kwh_h)
    if rate <= _EPSILON or end <= start:
        return 0.0
    stop = min(end, start + timedelta(hours=_finite_non_negative(stored_kwh) / rate))
    carried = 0.0
    for interval in intervals:
        if interval.end <= start:
            continue
        if interval.start >= stop:
            break
        interval_hours = _duration_hours(interval.start, interval.end)
        if interval_hours <= 0.0:
            continue
        load_rate = (
            max(0.0, interval.consumption_kwh - interval.solar_kwh) / interval_hours
        )
        carried += load_rate * _duration_hours(
            max(start, interval.start), min(stop, interval.end)
        )
    return carried


def _unclipped_room(
    intervals: Sequence[EnergyInterval],
    usable_initial_kwh: float,
    capacity: float,
    allocations: dict[PriceSlot, float],
    slot: PriceSlot,
    *,
    now: datetime | None = None,
    rate_kwh_h: float = 0.0,
    extra_kwh: float = 0.0,
    pending_base: Sequence[tuple[datetime, float, PriceSlot]] | None = None,
) -> float:
    """Energy ``slot`` can still add without a later full point clipping it.

    Energy charged ahead of a projected solar fill is exported, not stored
    (#704).  A projected deficit after the slot uses up part of the extra
    energy first, which widens the room at every later full point.

    The controller charges from the slot start until the amount is stored and
    the battery then covers the house, so with ``rate_kwh_h`` each slot's
    energy (``slot`` with ``extra_kwh`` added) lands when it is stored, not at
    the slot end. Landing it at the end hid the drain between the end of the
    charge and the end of the window and cut a night charge the sun could
    never top up.
    """
    def landing(item: PriceSlot, amount: float) -> datetime:
        if now is None or rate_kwh_h <= _EPSILON:
            return item.end
        start = max(now, item.start)
        return min(item.end, start + timedelta(hours=amount / rate_kwh_h))

    energy = min(_finite_non_negative(usable_initial_kwh), capacity)
    slot_lands = landing(slot, allocations.get(slot, 0.0) + extra_kwh)
    if pending_base is not None:
        # ``pending_base`` holds every allocation's landing at its current
        # amount, sorted; only ``slot``'s own entry moves with ``extra_kwh``.
        # Same result as rebuilding it, without re-sorting every allocation
        # on each of the allocator's many room queries.
        pending = [
            (lands, amount) for lands, amount, item in pending_base if item != slot
        ]
        if slot in allocations:
            insort(pending, (slot_lands, allocations[slot]))
    else:
        pending = sorted(
            (
                (
                    landing(item, amount + (extra_kwh if item == slot else 0.0)),
                    amount,
                )
                for item, amount in allocations.items()
            ),
            key=lambda pair: pair[0],
        )
    index = 0
    room: float | None = None
    absorbed = 0.0
    for interval in intervals:
        # Energy lands in the interval it is stored in, and is clipped after
        # that interval's flow.
        while index < len(pending) and pending[index][0] <= interval.end:
            energy += pending[index][1]
            index += 1
        if room is None and slot_lands <= interval.end:
            room = math.inf
        energy = min(capacity, energy + interval.solar_kwh - interval.consumption_kwh)
        if energy < 0.0:
            if room is not None:
                absorbed -= energy
            energy = 0.0
        if room is not None:
            room = min(room, absorbed + capacity - energy)
    return max(0.0, capacity - energy if room is None else room)


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
    premium_kwh: float = 0.0,
    premium_price_threshold: float | None = None,
    slot_ceilings: Mapping[datetime, float] | None = None,
    required_from_deadlines: bool = False,
    import_value_efficiency: float | None = None,
    import_value_margin: float = 0.0,
    reserve_holds: bool = False,
    grid_carries_load_while_charging: bool = False,
    grid_ceiling_kw: float | None = None,
    uncapped_required_kwh: float | None = None,
) -> ChronologicalPlan:
    """Allocate the cheapest feasible slot capacity to energy deadlines.

    A slot's ceiling is ``slot_ceilings[slot.start]`` when given, else
    ``max_price_threshold``.  Up to ``premium_kwh`` (guaranteed-floor energy) may
    use slots above their ceiling, up to ``premium_price_threshold`` (None = no
    cap), once the slots under the ceiling are exhausted.  Everything else stays
    under the ceiling (#724).

    With ``import_value_efficiency`` set, an ordinary deadline is valued
    against the import price of the interval it serves instead of the slot
    ceilings: a slot charges for it only up to
    ``import_price * import_value_efficiency - import_value_margin``, and the
    energy no such slot can cover is left to the grid (``cheaper_to_import``),
    not counted as a shortfall (#662). A deadline after the last published
    price is left to the grid the same way until its price is known.

    The battery covers the house first, so energy stored before an hour left
    to the grid is used in that hour, not in a later one. A slot that ends
    before the last hour left to the grid ahead of a valued deadline also
    stores what those hours drain, and serves the deadline only when the
    deadlines it then reaches repay that drain (#834). No drain is counted
    with ``reserve_holds`` (the discharge reserve keeps the energy for dearer
    hours until the sun covers the house) when the deadline comes before that.
    On a day with forecast solar that never covers the house the reserve holds
    a charge made after midnight to that day's evening.
    """
    def ceiling_of(slot: PriceSlot) -> float | None:
        if slot_ceilings is not None:
            return slot_ceilings.get(slot.start, max_price_threshold)
        return max_price_threshold

    has_ceiling = max_price_threshold is not None or slot_ceilings is not None

    intervals = sorted(intervals, key=lambda item: item.start)
    deadlines = sorted(deadlines, key=lambda item: item.deadline)
    headroom = max(0.0, float(headroom_kwh))
    usable_capacity = _finite_non_negative(usable_initial_kwh) + headroom
    required = min(
        _finite_non_negative(total_required_kwh),
        headroom,
    )
    power = _finite_non_negative(effective_power_kw)
    ceiling_kw = (
        None
        if grid_ceiling_kw is None or not math.isfinite(float(grid_ceiling_kw))
        else max(0.0, float(grid_ceiling_kw))
    )
    efficiency = max(0.0, min(1.0, float(charge_efficiency)))
    price_slots = [
        slot for slot in price_slots if math.isfinite(float(slot.price))
    ]
    under_static = [
        slot for slot in price_slots
        if slot.end > now
        and slot.start < horizon_end
        and (max_price_threshold is None or slot.price <= max_price_threshold)
    ]

    priced_until = max((slot.end for slot in price_slots), default=None)

    def value_cap(deadline: EnergyDeadline) -> float | None:
        # Guaranteed-floor energy is needed whatever the import costs then.
        if import_value_efficiency is None or deadline.kind == "guaranteed_floor":
            return None
        served = next(
            (
                slot for slot in price_slots
                if slot.start < deadline.deadline <= slot.end
            ),
            None,
        )
        if served is None:
            if priced_until is not None and deadline.deadline > priced_until:
                return _UNPRICED
            return None
        return served.price * import_value_efficiency - import_value_margin

    candidates = [
        slot for slot in price_slots
        if slot.end > now
        and slot.start < horizon_end
        and math.isfinite(float(slot.price))
        and (ceiling_of(slot) is None or slot.price <= ceiling_of(slot))
    ]
    premium_budget = _finite_non_negative(premium_kwh) if has_ceiling else 0.0
    premium_candidates = [
        slot for slot in price_slots
        if premium_budget > _EPSILON
        and slot.end > now
        and slot.start < horizon_end
        and math.isfinite(float(slot.price))
        and ceiling_of(slot) is not None
        and slot.price > ceiling_of(slot)
        and (premium_price_threshold is None or slot.price <= premium_price_threshold)
    ]

    allocations: dict[PriceSlot, float] = {}
    # The earliest deadline that booked each slot, the fallback label for a
    # slot the relabel below leaves without one.
    booked: dict[PriceSlot, datetime] = {}
    deadline_shortfall = 0.0
    cheaper_to_import = 0.0
    awaiting_prices = 0.0
    any_valued_pool = False

    capacity_cache: dict[tuple[datetime, datetime], float] = {}

    def capacity(
        slot: PriceSlot,
        deadline: datetime | None = None,
        not_before: datetime | None = None,
    ) -> float:
        start = max(now, slot.start, not_before or now)
        end = min(slot.end, horizon_end, deadline or horizon_end)
        if ceiling_kw is None or end <= start:
            return power * _duration_hours(start, end) * efficiency
        cached = capacity_cache.get((start, end))
        if cached is not None:
            return cached
        # The controller holds grid import (house load + charge) at the
        # ceiling, so the battery only gets what the house leaves of it. The
        # whole house load counts, also the part above the discharge power
        # that the battery drain projection leaves out.
        stored = 0.0
        covered = 0.0
        for interval in intervals:
            if interval.end <= start:
                continue
            if interval.start >= end:
                break
            hours = _duration_hours(max(start, interval.start), min(end, interval.end))
            interval_hours = _duration_hours(interval.start, interval.end)
            if hours <= 0.0 or interval_hours <= 0.0:
                continue
            net_load_kw = max(
                0.0, interval.grid_load_kwh - interval.solar_kwh
            ) / interval_hours
            stored += min(power, max(0.0, ceiling_kw - net_load_kw)) * hours
            covered += hours
        # Any part of the slot without a forecast interval keeps full power.
        stored += power * max(0.0, _duration_hours(start, end) - covered)
        capacity_cache[(start, end)] = stored * efficiency
        return stored * efficiency

    no_grid = simulate_allocations(
        intervals,
        usable_initial_kwh,
        usable_capacity_kwh=usable_capacity,
    )
    full_at: list[datetime] = []
    projected_energy = min(_finite_non_negative(usable_initial_kwh), usable_capacity)
    for interval in intervals:
        projected_energy += interval.solar_kwh - interval.consumption_kwh
        if projected_energy >= usable_capacity:
            projected_energy = usable_capacity
            full_at.append(interval.end)
        elif projected_energy < 0.0:
            projected_energy = 0.0

    def allocation_start(deadline: datetime) -> datetime:
        return max(
            (instant for instant in full_at if instant < deadline),
            default=now,
        )

    def in_segment(slot: PriceSlot, start: datetime, deadline: datetime) -> bool:
        # Solar only displaces the part of a slot that lands before the
        # projected fill, so a straddling slot still serves the later deadline.
        return slot.end > start and slot.start < deadline

    segment_required: dict[datetime, float] = {}
    for deadline in deadlines:
        start = allocation_start(deadline.deadline)
        segment_required[start] = max(
            segment_required.get(start, 0.0),
            deadline.required_cumulative_kwh,
        )
    if required_from_deadlines:
        # The extended horizon spans a night: the battery drains before the
        # later slots, so the room *now* does not bound what they may store.
        # A full battery would otherwise plan nothing for tomorrow's deficit.
        # ``room_at_start()`` bounds each slot by what fits when it opens.
        required = max(
            _finite_non_negative(total_required_kwh),
            sum(segment_required.values()),
        )
    deadline_required = min(required, sum(segment_required.values()))

    # Energy charged ahead of a projected solar fill is clipped, not stored, so
    # it saves no import (#704).  Only guaranteed-floor energy, needed at its
    # deadline whatever happens after, is exempt.
    pending_key: tuple[int, float] | None = None
    pending_base: list[tuple[datetime, float, PriceSlot]] = []

    def current_pending() -> list[tuple[datetime, float, PriceSlot]]:
        nonlocal pending_key, pending_base
        key = (len(allocations), sum(allocations.values()))
        if key != pending_key:
            rate = power * efficiency
            pending_base = []
            for item, amount in allocations.items():
                if rate > _EPSILON:
                    start = max(now, item.start)
                    lands = min(item.end, start + timedelta(hours=amount / rate))
                else:
                    lands = item.end
                pending_base.append((lands, amount, item))
            pending_base.sort(key=lambda entry: entry[0])
            pending_key = key
        return pending_base

    def room(slot: PriceSlot, free: float) -> float:
        # The room depends on when the added energy lands, which depends on
        # how much is added. The room shrinks as the amount grows, so the odd
        # steps of this iteration are amounts that fit. ponytail: three steps
        # from the upper bound; bisect if a case needs it exact.
        amount = min(free, usable_capacity)
        for _ in range(3):
            amount = min(
                free,
                _unclipped_room(
                    intervals,
                    usable_initial_kwh,
                    usable_capacity,
                    allocations,
                    slot,
                    now=now,
                    rate_kwh_h=power * efficiency,
                    extra_kwh=amount,
                    pending_base=current_pending(),
                ),
            )
        return amount

    # Energy stored when each interval starts, given the current allocations.
    # Rebuilt only when the allocations change: they only ever grow while
    # slots are being assigned, so their count and total identify a version.
    trajectory_key: tuple[int, float] | None = None
    trajectory_starts: list[datetime] = []
    trajectory_energy: list[float] = []
    trajectory_final = 0.0

    def build_trajectory() -> None:
        nonlocal trajectory_key, trajectory_starts, trajectory_energy, trajectory_final
        rate = power * efficiency
        charges = sorted(
            (
                max(now, item.start),
                (
                    min(item.end, max(now, item.start) + timedelta(hours=amount / rate))
                    if rate > _EPSILON
                    else item.end
                ),
                amount,
            )
            for item, amount in allocations.items()
            if amount > _EPSILON
        )
        energy = min(_finite_non_negative(usable_initial_kwh), usable_capacity)
        starts: list[datetime] = []
        values: list[float] = []
        added = 0
        window = 0
        for interval in intervals:
            # Energy charged before this interval is stored; the battery then
            # covers the house again, so it drains like any other energy.
            while added < len(charges) and charges[added][0] < interval.start:
                energy = min(usable_capacity, energy + charges[added][2])
                added += 1
            starts.append(interval.start)
            values.append(energy)
            while added < len(charges) and charges[added][0] <= interval.start:
                energy = min(usable_capacity, energy + charges[added][2])
                added += 1
            # The grid carries the house only while a slot is still charging
            # (from its start until its amount is stored), not for the rest of
            # the slot.
            while window < len(charges) and charges[window][1] <= interval.start:
                window += 1
            charging = window < len(charges) and charges[window][0] < interval.end
            flow = interval.solar_kwh - interval.consumption_kwh
            if flow < 0.0 and charging:
                flow = 0.0
            energy = max(0.0, min(usable_capacity, energy + flow))
        while added < len(charges):
            energy = min(usable_capacity, energy + charges[added][2])
            added += 1
        trajectory_starts, trajectory_energy = starts, values
        trajectory_final = energy
        trajectory_key = (len(allocations), sum(allocations.values()))

    def room_at_start(slot: PriceSlot) -> float:
        # Room the battery has when ``slot`` opens. Only the extended horizon
        # needs it: there the total is no longer capped at the room *now*.
        # The battery is simulated in time order: energy booked in an earlier
        # slot is stored when it is charged and drained by the house after
        # that, and the house only drains it outside the time a slot is
        # charging (the grid carries it meanwhile). ``room()`` alone credits
        # the drain inside back-to-back slots and overbooks a continuous
        # charge; counting earlier energy as still stored after it drained
        # under-booked the later slots.
        if not required_from_deadlines:
            return math.inf
        if trajectory_key != (len(allocations), sum(allocations.values())):
            build_trajectory()
        opens = max(now, slot.start)
        index = bisect_right(trajectory_starts, opens) - 1
        if index < 0:
            energy = min(_finite_non_negative(usable_initial_kwh), usable_capacity)
        elif index == len(trajectory_starts) - 1 and opens >= intervals[-1].end:
            energy = trajectory_final
        else:
            energy = trajectory_energy[index]
        return max(
            0.0, usable_capacity - energy - allocations.get(slot, 0.0)
        )

    # The last deadline in each segment that was left to the grid: the battery
    # is empty there, so energy stored before it is gone by then.
    left_until: dict[datetime, datetime] = {}
    sun_covers = (
        [
            interval.start
            for interval in intervals
            if interval.end > now
            and interval.solar_kwh > _EPSILON
            and interval.solar_kwh >= interval.consumption_kwh
        ]
        if reserve_holds
        else []
    )
    held_until = sun_covers[0] if sun_covers else None
    forecast_days = {
        interval.start.date()
        for interval in intervals
        if interval.solar_kwh > _EPSILON
    }

    def held_through_the_day(slot: PriceSlot, deadline: datetime) -> bool:
        # A day the sun never covers the house has no surplus to refill the
        # battery, and the reserve keeps that day's evening in its plan. So a
        # charge after midnight is held to a deadline later the same day, as
        # long as the sun does not take over in between. A charge before
        # midnight is not: until then the reserve plans only to sunrise.
        # Without any forecast solar that day the reserve has nothing to go on
        # and also plans only to sunrise, so nothing is carried either.
        if (
            not reserve_holds
            or slot.start.date() != deadline.date()
            or deadline.date() not in forecast_days
        ):
            return False
        return not any(slot.start <= cover < deadline for cover in sun_covers)

    def lasts(
        slot: PriceSlot, deadline: datetime, until: datetime | None
    ) -> bool:
        # The grid covers the house during a charge slot, so a slot ending at
        # or after the hour left to the grid (``until``) still serves later
        # deadlines.
        return (
            until is None
            or slot.end >= until
            or (held_until is not None and deadline <= held_until)
            or held_through_the_day(slot, deadline)
        )

    # Hours left to the grid in each segment: [deadline, kWh, import value,
    # served-deadline record].
    # A slot charged before them crosses them: the house drains that much of
    # its energy first, so serving a later deadline costs that drain on top
    # (#834). The amount drops to 0 once a booked slot covers it.
    left_hours: dict[datetime, list[list]] = {}

    def crossing_entries(slot: PriceSlot, start: datetime, deadline: datetime) -> list[list]:
        """The hours left to the grid that drain ``slot`` before ``deadline``."""
        if lasts(slot, deadline, left_until.get(start)):
            return []
        # The grid covers the house while another booked slot charges, from
        # its start until its amount is stored.
        rate = power * efficiency
        charging = [
            (
                max(now, other.start),
                min(other.end, max(now, other.start) + timedelta(hours=amount / rate))
                if rate > _EPSILON
                else other.end,
            )
            for other, amount in allocations.items()
            if amount > _EPSILON and other is not slot
        ]
        return [
            e for e in left_hours.get(start, ())
            if e[0] > slot.end
            and not any(begin < e[0] <= end for begin, end in charging)
        ]

    def crossing(slot: PriceSlot, start: datetime, deadline: datetime) -> tuple[float, float]:
        """kWh the house drains from ``slot`` before ``deadline``, and what
        storing it loses against importing it then."""
        drained = crossing_entries(slot, start, deadline)
        return (
            sum(e[1] for e in drained),
            sum(e[1] * (slot.price - e[2]) for e in drained),
        )

    deadline_caps = [value_cap(item) for item in deadlines]
    deadline_starts = [allocation_start(item.deadline) for item in deadlines]

    def crossing_gain(price: float, index: int, missing: float, budget: float) -> float:
        # The drain is paid once, then the slot serves this and the later
        # deadlines of the segment that repay it. ponytail: assumes no
        # cheaper slot takes those later deadlines first; optimistic there.
        gain = 0.0
        for j in range(index, len(deadlines)):
            if deadline_starts[j] != deadline_starts[index] or budget <= _EPSILON:
                break
            cap_j = deadline_caps[j]
            if cap_j is None or cap_j == _UNPRICED or cap_j <= price:
                continue
            need = missing if j == index else max(
                0.0,
                deadlines[j].required_cumulative_kwh
                - deadlines[j - 1].required_cumulative_kwh,
            )
            take = min(need, budget)
            gain += take * (cap_j - price)
            budget -= take
        return gain

    segment_unusable: dict[datetime, float] = {}
    # Each deadline the loop served: [deadline, the segment it ran in, the
    # cumulative energy the battery covers up to it, whether it was valued
    # against the import price, the hour left to the grid it saw, the slots
    # its segment booked across hours left to the grid (#834)]. Replayed below to
    # label the slots.
    served_deadlines: list[list] = []
    crossed_in_segment: dict[datetime, set[PriceSlot]] = {}
    for index, deadline in enumerate(deadlines):
        clip = deadline.kind != "guaranteed_floor"
        blocked = 0.0
        start = allocation_start(deadline.deadline)
        until = left_until.get(start)
        allocated_before_segment = sum(
            amount
            for slot, amount in allocations.items()
            if slot.end <= start
        )
        cumulative_target = min(
            deadline.required_cumulative_kwh - segment_unusable.get(start, 0.0),
            max(0.0, required - allocated_before_segment),
        )
        already = sum(
            amount
            for slot, amount in allocations.items()
            if in_segment(slot, start, deadline.deadline)
        )
        missing = max(0.0, cumulative_target - already)
        cap = value_cap(deadline)
        if cap == _UNPRICED:
            # Its import price is not published yet, so nothing shows that a
            # slot now repays it. Leave it to the grid for now; the
            # re-evaluation on publication values it against the real price.
            cheaper_to_import += missing
            awaiting_prices += missing
            segment_unusable[start] = segment_unusable.get(start, 0.0) + missing
            continue
        served_target = cumulative_target
        # Slots booked across this segment's hours left to the grid: those
        # hours are then covered by the battery, so such a slot serves every
        # later deadline of the segment.
        crossed = crossed_in_segment.setdefault(start, set())
        pool = (
            candidates if cap is None
            else [slot for slot in under_static if slot.price <= cap]
        )
        any_valued_pool = any_valued_pool or (cap is not None and bool(pool))
        # Capacity the import price ruled out, or whose energy is used before
        # this deadline: it is cheaper to import when used, not a shortfall.
        import_free = 0.0 if cap is None else sum(
            max(
                0.0,
                capacity(slot, deadline.deadline, start) - allocations.get(slot, 0.0),
            )
            for slot in under_static
            if (slot.price > cap or not lasts(slot, deadline.deadline, until))
            and in_segment(slot, start, deadline.deadline)
        )
        def free_of(slot: PriceSlot) -> float:
            return max(
                0.0,
                capacity(slot, deadline.deadline, start) - allocations.get(slot, 0.0),
            )

        def effective_price(slot: PriceSlot) -> float:
            # A crossing slot's drain loss, spread over what it can deliver.
            if cap is None:
                return slot.price
            drain, loss = crossing(slot, start, deadline.deadline)
            if drain <= _EPSILON:
                return slot.price
            return slot.price + max(0.0, loss) / max(free_of(slot) - drain, _EPSILON)

        eligible = sorted(
            (
                slot
                for slot in pool
                if in_segment(slot, start, deadline.deadline)
            ),
            key=lambda slot: (
                effective_price(slot),
                0 if slot in allocations else 1,
                -capacity(slot, deadline.deadline, start),
                slot.start,
            ),
        )
        for slot in eligible:
            free = free_of(slot)
            usable = min(room(slot, free) if clip else free, room_at_start(slot))
            blocked += free - usable
            drain, loss = (
                (0.0, 0.0) if cap is None
                else crossing(slot, start, deadline.deadline)
            )
            if drain > _EPSILON and (
                usable <= drain
                or crossing_gain(slot.price, index, missing, usable - drain) <= loss
            ):
                continue
            take = min(missing, usable - drain)
            if take <= _EPSILON:
                continue
            if drain > _EPSILON:
                # The battery now covers those hours, so they are no longer
                # left to the grid; the slot stores their drain as well.
                for entry in crossing_entries(slot, start, deadline.deadline):
                    entry[3][2] += entry[1]
                    entry[1] = 0.0
                cheaper_to_import -= drain
                segment_unusable[start] -= drain
                # The drained hours count toward this segment's target again.
                served_target += drain
            if cap is not None and not lasts(slot, deadline.deadline, until):
                # Booked across hours left to the grid (their drain counted
                # here or by an earlier crossing slot).
                crossed.add(slot)
            allocations[slot] = allocations.get(slot, 0.0) + take + drain
            booked[slot] = min(booked.get(slot, deadline.deadline), deadline.deadline)
            missing -= take
            if missing <= _EPSILON:
                break
        for slot in sorted(
            (
                slot
                for slot in premium_candidates
                if in_segment(slot, start, deadline.deadline)
            ),
            key=lambda slot: (slot.price, slot.start),
        ):
            free = max(
                0.0,
                capacity(slot, deadline.deadline, start) - allocations.get(slot, 0.0),
            )
            usable = min(room(slot, free) if clip else free, room_at_start(slot))
            blocked += free - usable
            take = min(missing, usable, premium_budget)
            if take <= _EPSILON:
                continue
            allocations[slot] = allocations.get(slot, 0.0) + take
            booked[slot] = min(booked.get(slot, deadline.deadline), deadline.deadline)
            missing -= take
            premium_budget -= take
            if missing <= _EPSILON or premium_budget <= _EPSILON:
                break
        # More slot capacity would not help clipped energy: it is unusable,
        # not a shortfall.
        cut = min(missing, blocked)
        import_cut = min(missing - cut, import_free)
        cheaper_to_import += import_cut
        # The grid covers what was cut or left to it, not the battery.
        record = [
            deadline, start, served_target - cut - import_cut,
            cap is not None, until, crossed,
        ]
        served_deadlines.append(record)
        if import_cut > _EPSILON:
            left_until[start] = deadline.deadline
            left_hours.setdefault(start, []).append(
                [deadline.deadline, import_cut, cap, record]
            )
        segment_unusable[start] = segment_unusable.get(start, 0.0) + cut + import_cut
        deadline_shortfall = max(deadline_shortfall, missing - cut - import_cut)
    unusable = sum(segment_unusable.values())
    required = max(0.0, required - unusable)
    deadline_required = max(0.0, deadline_required - unusable)

    flexible_required = max(0.0, required - deadline_required)
    # A missed deadline is intentionally not relabelled as flexible work.  It
    # remains a chronological shortfall instead of being made to look covered
    # by energy delivered after the need occurred.
    missing_total = flexible_required
    blocked = 0.0
    for slot in sorted(candidates, key=lambda item: (item.price, item.start)):
        free = max(0.0, capacity(slot) - allocations.get(slot, 0.0))
        usable = min(room(slot, free), room_at_start(slot))
        blocked += free - usable
        take = min(missing_total, usable)
        if take <= _EPSILON:
            continue
        allocations[slot] = allocations.get(slot, 0.0) + take
        missing_total -= take
        if missing_total <= _EPSILON:
            break
    for slot in sorted(premium_candidates, key=lambda item: (item.price, item.start)):
        free = max(0.0, capacity(slot) - allocations.get(slot, 0.0))
        usable = min(room(slot, free), room_at_start(slot))
        blocked += free - usable
        take = min(missing_total, usable, premium_budget)
        if take <= _EPSILON:
            continue
        allocations[slot] = allocations.get(slot, 0.0) + take
        missing_total -= take
        premium_budget -= take
        if missing_total <= _EPSILON or premium_budget <= _EPSILON:
            break
    cut = min(missing_total, blocked)
    required -= cut
    flexible_required -= cut

    # Label each slot with the earliest deadline its energy serves. The loop
    # above books the cheapest capacity for each deadline in turn, so the
    # deadline a slot was booked under only says which requirement first ran
    # out of cheaper slots: a dear slot before midnight that only tomorrow
    # evening's requirement had to fall back on read "before 19:15" while
    # the battery spent its energy at midnight. The battery covers the house
    # with the energy charged first, so walk the deadlines in order and let
    # each take the earliest energy still unassigned in its segment, under
    # the same segment and hour-left-to-the-grid rules the loop applied.
    allocation_deadlines = {slot: None for slot in allocations}
    unassigned = dict(allocations)
    served_in_segment: dict[datetime, float] = {}
    by_start = sorted(allocations, key=lambda item: item.start)
    for deadline, start, target, valued, until, crossed in served_deadlines:
        need = max(0.0, target - served_in_segment.get(start, 0.0))
        for slot in by_start:
            if need <= _EPSILON:
                break
            left = unassigned[slot]
            if (
                left <= _EPSILON
                or not in_segment(slot, start, deadline.deadline)
                or (
                    valued
                    and slot not in crossed
                    and not lasts(slot, deadline.deadline, until)
                )
            ):
                continue
            take = min(need, left)
            unassigned[slot] = left - take
            need -= take
            served_in_segment[start] = served_in_segment.get(start, 0.0) + take
            if allocation_deadlines[slot] is None:
                allocation_deadlines[slot] = deadline.deadline
    # In valued mode an early slot booked by the flexible pass can take a
    # deadline's share in the walk above and push a deadline-booked slot onto
    # later deadlines it cannot serve. Such a slot keeps the deadline that
    # booked it, so it stays forced and bounded, not flexible.
    for slot, label in allocation_deadlines.items():
        if label is None and slot in booked:
            allocation_deadlines[slot] = booked[slot]
    # Keep the labels in time order: energy charged earlier is spent first,
    # so a slot is needed no later than any later slot. Pulling a label
    # earlier only tightens its bound.
    earliest_later: datetime | None = None
    for slot in reversed(by_start):
        label = allocation_deadlines[slot]
        if label is None:
            continue
        if earliest_later is not None and earliest_later < label:
            allocation_deadlines[slot] = earliest_later
        else:
            earliest_later = label

    if grid_carries_load_while_charging:
        # The deadlines and the scalar balance both count the house load
        # inside a charging slot as battery drain, but the grid feeds the
        # house while the slot charges. Charging that load into the battery
        # as well left it well above its floor at sunrise.
        shortfall = max(0.0, required - sum(allocations.values()))
        covered = 0.0
        carried_deadline = 0.0
        carried_flexible = 0.0
        # A plan bound by the room: the battery fills its room whatever the
        # grid carries meanwhile, because the need goes on after it. The
        # carried load first cancels that need beyond the room and only the
        # rest trims the slots, so ``stored = min(room, need - carried)``.
        # Trimming the room itself bought 3.1 of 5.5 kWh for a 21.6 kWh
        # night deficit (11 Nov 2026).
        beyond_room = (
            max(0.0, _finite_non_negative(uncapped_required_kwh) - required)
            if uncapped_required_kwh is not None
            else 0.0
        )
        for slot in sorted(allocations, key=lambda item: item.start):
            amount = allocations[slot]
            start, end = max(now, slot.start), min(slot.end, horizon_end)
            # A slot at full capacity cannot stop sooner: the load carried
            # while it runs first covers the shortfall (#785: trimming such
            # slots emptied the battery before sunrise). A slot below capacity
            # was held there on purpose (price, premium budget, clipping), so
            # its carried load trims it.
            cover = 0.0
            if amount >= capacity(slot) - _EPSILON:
                cover = min(
                    shortfall - covered,
                    _load_carried_while_storing(
                        intervals, amount, start, end, power * efficiency
                    ),
                )
                covered += cover
            # The controller charges from the slot start until the amount is
            # stored, whichever deadline the energy was allocated to.
            net = min(
                amount,
                _net_of_load_carried_by_grid(
                    intervals, amount + cover, start, end, power * efficiency
                ),
            )
            if beyond_room > _EPSILON and amount - net > _EPSILON:
                kept = min(amount - net, beyond_room)
                beyond_room -= kept
                net += kept
            if allocation_deadlines.get(slot) is None:
                carried_flexible += amount - net
            else:
                carried_deadline += amount - net
            if net <= _EPSILON:
                del allocations[slot]
                allocation_deadlines.pop(slot, None)
            else:
                allocations[slot] = net
        covered_deadline = min(covered, deadline_shortfall)
        deadline_shortfall -= covered_deadline
        deadline_required = max(
            0.0, deadline_required - carried_deadline - covered_deadline
        )
        flexible_required = max(
            0.0, flexible_required - carried_flexible - (covered - covered_deadline)
        )
        required = max(0.0, required - carried_deadline - carried_flexible - covered)

    result_allocations = [
        SlotAllocation(
            slot,
            amount,
            allocation_deadlines.get(slot),
            "deadline" if allocation_deadlines.get(slot) is not None else "flexible",
        )
        for slot, amount in sorted(allocations.items(), key=lambda item: item[0].start)
    ]
    simulation = no_grid
    allocated = sum(allocations.values())
    total_shortfall = max(0.0, required - allocated)
    if power <= 0:
        reason = "insufficient_power"
    elif (
        not candidates and not premium_candidates and not any_valued_pool
        and required > 0
    ):
        reason = "price_threshold" if has_ceiling else "no_eligible_slots"
    elif deadline_shortfall > _EPSILON:
        reason = "deadline_capacity_shortfall"
    elif total_shortfall > _EPSILON:
        reason = "insufficient_slot_capacity"
    elif cheaper_to_import > _EPSILON and allocated <= _EPSILON:
        # Energy left to the grid only because tomorrow's prices are not out
        # yet is not "cheaper to import": no margin setting changes it.
        reason = (
            "awaiting_prices"
            if awaiting_prices >= cheaper_to_import - _EPSILON
            else "cheaper_to_import"
        )
    else:
        reason = "ok"
    cheapest_rejected = None
    if reason == "price_threshold":
        rejected = min(
            (
                slot for slot in price_slots
                if slot.end > now and slot.start < horizon_end
            ),
            key=lambda slot: (slot.price, slot.start),
            default=None,
        )
        if rejected is not None:
            cheapest_rejected = (rejected.start, rejected.price, ceiling_of(rejected))
    earliest = simulation.earliest_depletion_at
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
        cheaper_to_import_kwh=cheaper_to_import,
        awaiting_prices_kwh=awaiting_prices,
        cheapest_rejected=cheapest_rejected,
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
    usable_capacity = request.usable_initial_kwh + request.headroom_kwh
    depletion_deadlines = tuple(
        build_energy_deadlines(
            intervals,
            request.usable_initial_kwh,
            usable_capacity_kwh=usable_capacity,
        )
    )
    deadlines = tuple(request.deadlines) or depletion_deadlines
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
        premium_kwh=request.premium_kwh,
        premium_price_threshold=request.premium_price_threshold,
        slot_ceilings=request.slot_ceilings,
        required_from_deadlines=request.required_from_deadlines,
        import_value_efficiency=request.import_value_efficiency,
        import_value_margin=request.import_value_margin,
        reserve_holds=request.reserve_holds,
        grid_carries_load_while_charging=request.grid_carries_load_while_charging,
        grid_ceiling_kw=request.grid_ceiling_kw,
        uncapped_required_kwh=request.uncapped_required_kwh,
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
            cheaper_to_import_kwh=plan.cheaper_to_import_kwh,
            cheapest_rejected=plan.cheapest_rejected,
            awaiting_prices_kwh=plan.awaiting_prices_kwh,
        ),
    )
