"""Price-aware discharge reserve.

The existing ``price_discharge`` blocker answers one question: *is the current
price below my threshold?* It never asks the second one: *do I have more energy
than the expensive hours still ahead can absorb?* So a battery that is full at
noon is held back from a cheap-but-not-free hour it could have covered for free,
and a battery that is nearly empty at 16:00 is drained into a mild hour before
the evening peak it was saved for.

This module answers the second question. It projects the household's net demand
(learned consumption minus expected PV) onto the remaining price slots, assigns
the energy currently in the battery to the most expensive of those slots first,
and reports how much of that energy must therefore survive the current hour.

The result is a level, not a verdict: everything above the reserve stays
available for self-consumption right now. That is what makes it composable with
the threshold blocker instead of a second, competing opinion about discharging.

Pure functions only — no Home Assistant, no controller. The runtime manager in
``control/discharge_reserve.py`` owns the guards and the blocker registry.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Sequence

EPSILON = 1e-9
_QUARTER = timedelta(minutes=15)

STATUS_DISABLED = "disabled"
STATUS_RESERVING = "reserving"
STATUS_NO_RESERVE = "no_reserve"

REASON_NO_SLOTS = "no_price_slots"
REASON_NO_PRICE = "no_current_price"
REASON_NO_DEMAND = "no_future_demand"
REASON_NO_USABLE_ENERGY = "no_usable_energy"
REASON_NO_CAPACITY = "no_capacity"
REASON_NOTHING_DEARER = "nothing_dearer_ahead"
REASON_PV_COVERS_IT = "pv_refills_in_time"
REASON_RESERVED = "reserved_for_dearer_hours"


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _positive(value, default: float = 0.0) -> float:
    if not _finite(value):
        return default
    return max(0.0, float(value))


@dataclass(frozen=True)
class ReserveSlot:
    """One future price slot, its net grid demand and its expected PV surplus."""

    start: datetime
    end: datetime
    price: float
    net_demand_kwh: float = 0.0
    expected_surplus_kwh: float = 0.0

    @property
    def duration_hours(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds() / 3600.0)


@dataclass
class ReservePlan:
    """What the battery must keep, and the slots that claim it."""

    slots: list[ReserveSlot] = field(default_factory=list)
    status: str = STATUS_NO_RESERVE
    reason: str = REASON_NO_SLOTS
    min_saving: float = 0.0
    usable_energy_kwh: float = 0.0
    total_capacity_kwh: float = 0.0
    free_space_kwh: float = 0.0
    horizon_end: datetime | None = None
    reference_price: float | None = None
    reserve_kwh: float = 0.0
    selected_slots: list[ReserveSlot] = field(default_factory=list)

    def reserve_kwh_at(
        self,
        now: datetime,
        current_price: float | None,
        usable_energy_kwh: float | None = None,
        free_space_kwh: float | None = None,
    ) -> tuple[float, list[ReserveSlot], str]:
        """Recompute the reserve against the live price and live SOC.

        Cheap enough for the control cycle: at most 96 slots, one sort. Doing it
        here rather than at plan time is what lets the floor fall away the moment
        the current hour becomes the expensive one, without waiting for the next
        rebuild.

        Returns ``(reserve_kwh, claiming_slots, reason)``.
        """
        available = (
            self.usable_energy_kwh
            if usable_energy_kwh is None
            else _positive(usable_energy_kwh)
        )
        if available <= EPSILON:
            return 0.0, [], REASON_NO_USABLE_ENERGY
        if not _finite(current_price):
            return 0.0, [], REASON_NO_PRICE

        threshold = float(current_price) + max(0.0, self.min_saving)
        candidates = [
            slot
            for slot in self.slots
            if slot.start >= now
            and slot.net_demand_kwh > EPSILON
            and slot.price > threshold
        ]
        if not candidates:
            return 0.0, [], REASON_NOTHING_DEARER

        # Most expensive first: stored energy is worth what the hour it replaces
        # costs, so the dearest hour has first claim on it.
        candidates.sort(key=lambda slot: (-slot.price, slot.start))
        reserve = 0.0
        claiming: list[ReserveSlot] = []
        for slot in candidates:
            if reserve >= available - EPSILON:
                break
            take = min(slot.net_demand_kwh, available - reserve)
            if take <= EPSILON:
                continue
            reserve += take
            claiming.append(slot)
        claiming.sort(key=lambda slot: slot.start)

        # Sun that lands in the battery before the first claiming slot pays for
        # part of that claim, so holding the same energy back now would import
        # at today's price and export the PV that was going to replace it.
        recharge = self._expected_recharge_kwh(now, claiming[0].start, free_space_kwh)
        reserve = max(0.0, reserve - recharge)
        if reserve <= EPSILON:
            return 0.0, [], REASON_PV_COVERS_IT
        return reserve, claiming, REASON_RESERVED

    def _expected_recharge_kwh(
        self,
        now: datetime,
        deadline: datetime,
        free_space_kwh: float | None,
    ) -> float:
        """PV surplus expected to reach the battery before ``deadline``."""
        space = self.free_space_kwh if free_space_kwh is None else _positive(free_space_kwh)
        if space <= EPSILON:
            return 0.0
        surplus = 0.0
        for slot in self.slots:
            if slot.start < now or slot.start >= deadline:
                continue
            surplus += _positive(slot.expected_surplus_kwh)
        return min(space, surplus)


def build_reserve_slots(
    price_slots: Sequence,
    demand_by_slot: dict,
    surplus_by_slot: dict | None = None,
) -> list[ReserveSlot]:
    """Pair each price slot with its expected net grid demand and PV surplus."""
    surplus_by_slot = surplus_by_slot or {}
    slots: list[ReserveSlot] = []
    for slot in price_slots:
        if not _finite(getattr(slot, "price", None)):
            continue
        slots.append(
            ReserveSlot(
                start=slot.start,
                end=slot.end,
                price=float(slot.price),
                net_demand_kwh=_positive(demand_by_slot.get(slot, 0.0)),
                expected_surplus_kwh=_positive(surplus_by_slot.get(slot, 0.0)),
            )
        )
    slots.sort(key=lambda entry: entry.start)
    return slots


def consumption_by_slot(
    price_slots: Sequence,
    intervals_by_date: dict,
    intervals_kwh: Sequence[float] | None = None,
) -> dict:
    """Map the learned 15-minute consumption profile onto price slots.

    The profile is 96 quarter-hour bins of local wall-clock time. A price slot
    that covers part of a bin is credited that part of its energy, so hourly and
    quarter-hourly tariffs both land on the same shape.

    ``intervals_by_date`` is preferred over the flat ``intervals_kwh``: a horizon
    that crosses midnight has one shape per date, and applying today's evening to
    tomorrow's evening is exactly the error the dated form exists to prevent.
    Returns an empty mapping when no profile is available, so the caller keeps
    its no-reserve behaviour instead of reserving against an invented shape.
    """
    if not price_slots:
        return {}
    flat = list(intervals_kwh or [])
    dated = {
        local_date: list(values)
        for local_date, values in (intervals_by_date or {}).items()
        if values
    }
    if not dated and not flat:
        return {}

    demand: dict = {}
    for slot in price_slots:
        start = slot.start
        end = slot.end
        if end <= start:
            demand[slot] = 0.0
            continue
        total = 0.0
        cursor = start
        while cursor < end:
            index = (cursor.hour * 60 + cursor.minute) // 15
            bin_start = cursor.replace(
                minute=(index * 15) % 60,
                hour=(index * 15) // 60,
                second=0,
                microsecond=0,
            )
            bin_end = bin_start + _QUARTER
            step_end = min(end, bin_end)
            values = dated.get(cursor.date()) or flat
            if values and 0 <= index < len(values):
                share = (step_end - cursor).total_seconds() / _QUARTER.total_seconds()
                total += _positive(values[index]) * share
            cursor = step_end
        demand[slot] = total
    return demand


def net_demand_by_slot(consumption: dict, solar: dict | None = None) -> dict:
    """Subtract expected PV from expected consumption, floored at zero.

    A slot the sun already covers needs nothing from the battery, so it must not
    claim any reserve.
    """
    solar = solar or {}
    return {
        slot: max(0.0, _positive(value) - _positive(solar.get(slot, 0.0)))
        for slot, value in consumption.items()
    }


def plan_discharge_reserve(
    price_slots: Sequence,
    demand_by_slot: dict,
    *,
    usable_energy_kwh: float,
    total_capacity_kwh: float,
    current_price: float | None,
    min_saving: float,
    now: datetime,
    horizon_end: datetime | None = None,
    surplus_by_slot: dict | None = None,
    free_space_kwh: float = 0.0,
) -> ReservePlan:
    """Build the reserve plan for the remaining horizon."""
    plan = ReservePlan(
        min_saving=max(0.0, _positive(min_saving)),
        usable_energy_kwh=_positive(usable_energy_kwh),
        total_capacity_kwh=_positive(total_capacity_kwh),
        free_space_kwh=_positive(free_space_kwh),
        horizon_end=horizon_end,
        reference_price=float(current_price) if _finite(current_price) else None,
    )
    if plan.total_capacity_kwh <= EPSILON:
        plan.reason = REASON_NO_CAPACITY
        return plan

    plan.slots = build_reserve_slots(price_slots, demand_by_slot, surplus_by_slot)
    if not plan.slots:
        plan.reason = REASON_NO_SLOTS
        return plan
    if all(slot.net_demand_kwh <= EPSILON for slot in plan.slots):
        plan.reason = REASON_NO_DEMAND
        return plan

    reserve, claiming, reason = plan.reserve_kwh_at(now, current_price)
    plan.reserve_kwh = reserve
    plan.selected_slots = claiming
    plan.reason = reason
    plan.status = STATUS_RESERVING if reserve > EPSILON else STATUS_NO_RESERVE
    return plan


def reserve_soc_pct(reserve_kwh: float, total_capacity_kwh: float) -> float:
    """Convert a system-wide reserve into the SOC each battery must keep.

    One percentage for every battery, deliberately: it is independent of the mix
    of capacities and does not move when one battery discharges faster than
    another. It reserves exactly ``reserve_kwh`` only when every battery has at
    least that share of its own capacity left; a battery already below it simply
    stops discharging, and the fleet then holds back less than planned. Under-
    reserving is the safe direction — the alternative would block a battery for
    energy its neighbour is holding.
    """
    capacity = _positive(total_capacity_kwh)
    if capacity <= EPSILON:
        return 0.0
    return max(0.0, min(100.0, 100.0 * _positive(reserve_kwh) / capacity))


def free_space_kwh(snapshots: Sequence) -> float:
    """Room left in the fleet for PV that has not been produced yet."""
    total = 0.0
    for snapshot in snapshots:
        if not getattr(snapshot, "eligible", True):
            continue
        capacity = _positive(getattr(snapshot, "capacity_kwh", 0.0))
        soc = _positive(getattr(snapshot, "soc_pct", 0.0))
        ceiling = _positive(getattr(snapshot, "max_soc_pct", 100.0))
        total += max(0.0, capacity * (ceiling - soc) / 100.0)
    return total


def eligible_capacity_kwh(snapshots: Sequence) -> float:
    """Total capacity of the batteries that may serve the reserve."""
    total = 0.0
    for snapshot in snapshots:
        if not getattr(snapshot, "eligible", True):
            continue
        if not getattr(snapshot, "can_discharge", True):
            continue
        total += _positive(getattr(snapshot, "capacity_kwh", 0.0))
    return total


def usable_energy_kwh(snapshots: Sequence) -> float:
    """Energy the fleet can still deliver above its configured floors."""
    total = 0.0
    for snapshot in snapshots:
        if not getattr(snapshot, "eligible", True):
            continue
        if not getattr(snapshot, "can_discharge", True):
            continue
        capacity = _positive(getattr(snapshot, "capacity_kwh", 0.0))
        soc = _positive(getattr(snapshot, "soc_pct", 0.0))
        floor = _positive(getattr(snapshot, "floor_soc_pct", 0.0))
        total += max(0.0, capacity * (soc - floor) / 100.0)
    return total
