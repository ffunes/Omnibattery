"""Pure planning helpers for price-aware solar surplus absorption.

Absorbing PV surplus into the battery is not free.  Under a dynamic contract the
energy that is stored instead of exported forfeits that quarter-hour's feed-in
revenue, and feed-in prices swing hard across the day.  This planner decides in
which of the remaining hours the battery should absorb surplus, so the expensive
hours export and the cheap hours charge.

The module has no Home Assistant dependency.  It receives normalized
``PriceSlot`` objects carrying the *export* price curve, a per-slot surplus
model, and snapshots of the batteries, and returns a plan that the runtime
manager applies through the existing charge-blocker registry.

Two invariants hold everywhere in this module:

1. **Release is the safe direction.**  Absorbing surplus too early costs money;
   failing to absorb it costs energy that must be bought back from the grid
   later.  Every uncertainty - missing prices, a missing forecast, non-finite
   values, a deadline in the past, no usable batteries - resolves to a released
   plan with status ``fail_safe``.
2. **A hold must be materially worth it.**  The runtime re-evaluates every
   control cycle, so a hold that flips on a rounding difference would chatter.
   ``min_saving`` is the required advantage of the cheapest hour still ahead
   over the current one, mirroring the role ``min_arbitrage_margin`` plays for
   grid charging.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Sequence

from ..const import CHARGE_EFFICIENCY
from . import PriceSlot
from .curtailment import BatterySnapshot


EPSILON = 1e-6

STATUS_DISABLED = "disabled"
STATUS_FAIL_SAFE = "fail_safe"
STATUS_PLANNED = "planned"
STATUS_INFEASIBLE = "infeasible"
STATUS_NO_TARGET = "no_target"

REASON_ABSORPTION_WINDOW = "absorption_window"
REASON_PAST_DEADLINE = "past_deadline"
REASON_TARGET_MET = "target_met"
REASON_SHORTFALL_RISK = "shortfall_risk"
REASON_NO_MATERIAL_SAVING = "no_material_saving"
REASON_CHEAPER_WINDOW_AHEAD = "cheaper_window_ahead"


def _finite(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _duration_hours(start: datetime, end: datetime) -> float:
    try:
        return max(0.0, (end - start).total_seconds() / 3600.0)
    except (AttributeError, TypeError):
        return 0.0


@dataclass(frozen=True)
class AbsorptionSlot:
    """One future price slot and the surplus it is expected to offer."""

    start: datetime
    end: datetime
    export_price: float
    expected_surplus_kwh: float = 0.0
    selected: bool = False

    @property
    def duration_hours(self) -> float:
        return _duration_hours(self.start, self.end)

    @property
    def absorbable_kwh(self) -> float:
        """Battery-side energy this slot can absorb, after charge losses."""
        return max(0.0, self.expected_surplus_kwh) * CHARGE_EFFICIENCY

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


@dataclass
class AbsorptionPlan:
    """A surplus-absorption plan and its diagnostic accounting."""

    status: str = STATUS_DISABLED
    reason: str = STATUS_DISABLED
    evaluation_time: datetime | None = None
    deadline: datetime | None = None
    target_kwh: float = 0.0
    free_space_kwh: float = 0.0
    slots: list[AbsorptionSlot] = field(default_factory=list)
    selected_slots: list[AbsorptionSlot] = field(default_factory=list)
    absorbable_total_kwh: float = 0.0
    min_saving: float = 0.0

    @property
    def is_fail_safe(self) -> bool:
        return self.status == STATUS_FAIL_SAFE

    @property
    def can_hold(self) -> bool:
        """Only a planned day may ever hold; every other status releases."""
        return self.status == STATUS_PLANNED

    def slot_at(self, moment: datetime) -> AbsorptionSlot | None:
        for slot in self.slots:
            if slot.contains(moment):
                return slot
        return None

    def remaining_absorbable_kwh(self, moment: datetime) -> float:
        """Battery-side energy the still-future selected slots can deliver."""
        return sum(
            slot.absorbable_kwh
            for slot in self.selected_slots
            if slot.end > moment
        )


def calculate_absorption_target_kwh(
    remaining_consumption_kwh: float | None,
    usable_energy_kwh: float | None,
    free_space_kwh: float | None,
    safety_margin_kwh: float = 0.0,
) -> float | None:
    """Return the battery-side energy still worth absorbing today.

    The target is what the household still needs beyond what the battery
    already holds above its floor, capped by the space actually left in the
    battery.  ``None`` signals unusable inputs, which the caller must treat as
    a release.

    Deliberately *not* derived from the pricing engine's ``energy_deficit_kwh``:
    that figure already subtracts the solar forecast, which this planner
    distributes per slot, so reusing it would subtract solar twice.
    """
    if not (
        _finite(remaining_consumption_kwh)
        and _finite(usable_energy_kwh)
        and _finite(free_space_kwh)
    ):
        return None
    margin = float(safety_margin_kwh) if _finite(safety_margin_kwh) else 0.0
    space = max(0.0, float(free_space_kwh))
    needed = float(remaining_consumption_kwh) + max(0.0, margin) - float(usable_energy_kwh)
    return max(0.0, min(needed, space))


def calculate_free_space_kwh(batteries: Sequence[BatterySnapshot]) -> float:
    """Return the charging space left in the eligible batteries."""
    total = 0.0
    for snapshot in batteries:
        if not _usable_battery(snapshot):
            continue
        total += max(
            0.0,
            (snapshot.max_soc_pct - snapshot.soc_pct) / 100.0 * snapshot.capacity_kwh,
        )
    return total


def calculate_usable_energy_kwh(batteries: Sequence[BatterySnapshot]) -> float:
    """Return the energy the eligible batteries hold above their floors.

    The plan target and the live target must measure the same energy, or the
    live target lands above what the plan bought slots for and every cycle
    after a rebuild reports a shortfall.  Both sides call this.
    """
    total = 0.0
    for snapshot in batteries:
        if not _usable_battery(snapshot):
            continue
        total += max(
            0.0,
            (snapshot.soc_pct - snapshot.floor_soc_pct) / 100.0 * snapshot.capacity_kwh,
        )
    return total


def _usable_battery(snapshot: BatterySnapshot) -> bool:
    return (
        getattr(snapshot, "eligible", False)
        and _finite(snapshot.soc_pct)
        and _finite(snapshot.capacity_kwh)
        and _finite(snapshot.max_soc_pct)
        and _finite(snapshot.floor_soc_pct)
        and snapshot.capacity_kwh > 0
        and snapshot.max_soc_pct >= snapshot.floor_soc_pct
    )


def select_absorption_slots(
    slots: Sequence[AbsorptionSlot],
    target_kwh: float,
) -> list[AbsorptionSlot]:
    """Pick the cheapest export hours that together cover ``target_kwh``.

    Ranking by export price ascending is the whole point: the cheapest feed-in
    hour is the one where *not* exporting costs least.  Ties resolve by start
    time so the plan is deterministic and prefers to charge sooner.

    Selection accumulates battery-side energy, not hours.  A cheap slot at
    sunrise offers almost no surplus while an equally cheap midday slot offers
    several kWh, and an hour-counting selector cannot tell them apart.
    """
    if target_kwh <= EPSILON:
        return []
    ranked = sorted(slots, key=lambda slot: (slot.export_price, slot.start))
    selected: list[AbsorptionSlot] = []
    accumulated = 0.0
    for slot in ranked:
        if accumulated >= target_kwh - EPSILON:
            break
        if slot.absorbable_kwh <= EPSILON:
            continue
        selected.append(slot)
        accumulated += slot.absorbable_kwh
    return sorted(selected, key=lambda slot: slot.start)


def plan_surplus_absorption(
    price_slots: Sequence[PriceSlot],
    surplus_by_slot: Mapping[PriceSlot, float] | None = None,
    batteries: Sequence[BatterySnapshot] = (),
    *,
    remaining_consumption_kwh: float | None = None,
    usable_energy_kwh: float | None = None,
    safety_margin_kwh: float = 0.0,
    max_charge_power_w: float | None = None,
    deadline: datetime | None = None,
    min_saving: float = 0.0,
    now: datetime | None = None,
) -> AbsorptionPlan:
    """Build a surplus-absorption plan from an export price curve.

    ``price_slots`` carry the export/feed-in price.  ``surplus_by_slot`` is the
    AC-side PV surplus expected in each slot; the planner clamps it to what the
    batteries can physically take at ``max_charge_power_w`` and converts it to
    battery-side energy with the integration's charge efficiency.
    """
    evaluated_at = now or datetime.now()
    plan = AbsorptionPlan(evaluation_time=evaluated_at, deadline=deadline)
    plan.min_saving = max(0.0, float(min_saving)) if _finite(min_saving) else 0.0

    if not price_slots:
        plan.status, plan.reason = STATUS_FAIL_SAFE, "missing_prices"
        return plan
    if deadline is None or not isinstance(deadline, datetime) or deadline <= evaluated_at:
        plan.status, plan.reason = STATUS_FAIL_SAFE, "no_solar_window_left"
        return plan
    if max_charge_power_w is None or not _finite(max_charge_power_w) or float(max_charge_power_w) <= 0:
        plan.status, plan.reason = STATUS_FAIL_SAFE, "missing_charge_capacity"
        return plan

    usable_batteries = [snapshot for snapshot in batteries if _usable_battery(snapshot)]
    if not usable_batteries:
        plan.status, plan.reason = STATUS_FAIL_SAFE, "missing_battery_capacity_or_soc"
        return plan

    try:
        candidates = sorted(
            (
                slot
                for slot in price_slots
                if slot.end > evaluated_at
                and slot.start < deadline
                and slot.end > slot.start
                and _finite(slot.price)
            ),
            key=lambda slot: slot.start,
        )
    except (AttributeError, TypeError, ValueError):
        candidates = []
    if not candidates:
        plan.status, plan.reason = STATUS_FAIL_SAFE, "no_future_export_slots"
        return plan

    charge_power_w = float(max_charge_power_w)
    surplus_map = surplus_by_slot or {}
    absorption_slots: list[AbsorptionSlot] = []
    for slot in candidates:
        duration = _duration_hours(slot.start, slot.end)
        raw_surplus = surplus_map.get(slot, 0.0)
        surplus = float(raw_surplus) if _finite(raw_surplus) else 0.0
        capped = min(max(0.0, surplus), charge_power_w * duration / 1000.0)
        absorption_slots.append(
            AbsorptionSlot(
                start=slot.start,
                end=slot.end,
                export_price=float(slot.price),
                expected_surplus_kwh=capped,
            )
        )
    plan.slots = absorption_slots
    plan.absorbable_total_kwh = sum(slot.absorbable_kwh for slot in absorption_slots)

    free_space_kwh = calculate_free_space_kwh(usable_batteries)
    plan.free_space_kwh = free_space_kwh
    target_kwh = calculate_absorption_target_kwh(
        remaining_consumption_kwh,
        usable_energy_kwh,
        free_space_kwh,
        safety_margin_kwh,
    )
    if target_kwh is None:
        plan.status, plan.reason = STATUS_FAIL_SAFE, "missing_energy_forecast"
        return plan
    plan.target_kwh = target_kwh

    if target_kwh <= EPSILON:
        # Nothing has to be absorbed today, so nothing has to be held back.
        plan.status, plan.reason = STATUS_NO_TARGET, REASON_TARGET_MET
        return plan

    if plan.absorbable_total_kwh + EPSILON < target_kwh:
        # Even taking every remaining kWh of surplus falls short.  Absorb
        # everything: holding could only make the shortfall worse.
        plan.slots = [
            AbsorptionSlot(
                start=slot.start,
                end=slot.end,
                export_price=slot.export_price,
                expected_surplus_kwh=slot.expected_surplus_kwh,
                selected=slot.absorbable_kwh > EPSILON,
            )
            for slot in absorption_slots
        ]
        plan.selected_slots = [slot for slot in plan.slots if slot.selected]
        plan.status, plan.reason = STATUS_INFEASIBLE, "surplus_below_target"
        return plan

    selected = select_absorption_slots(absorption_slots, target_kwh)
    if not selected:
        plan.status, plan.reason = STATUS_FAIL_SAFE, "no_absorbable_slots"
        return plan

    chosen = {(slot.start, slot.end) for slot in selected}
    plan.slots = [
        AbsorptionSlot(
            start=slot.start,
            end=slot.end,
            export_price=slot.export_price,
            expected_surplus_kwh=slot.expected_surplus_kwh,
            selected=(slot.start, slot.end) in chosen,
        )
        for slot in absorption_slots
    ]
    plan.selected_slots = [slot for slot in plan.slots if slot.selected]
    plan.status, plan.reason = STATUS_PLANNED, "absorption_scheduled"
    return plan


def hold_decision(
    plan: AbsorptionPlan | None,
    now: datetime,
    live_target_kwh: float | None = None,
) -> tuple[bool, str]:
    """Return ``(hold, reason)`` for the current moment.

    ``live_target_kwh`` is the target recomputed from the live SOC.  When a
    cheap window under-delivers it rises while the remaining absorbable energy
    falls; the moment it exceeds what the remaining selected slots can supply,
    the hold drops for the rest of the day.
    """
    if plan is None or not plan.can_hold:
        return False, plan.reason if plan is not None else STATUS_DISABLED

    if plan.deadline is not None and now >= plan.deadline:
        return False, REASON_PAST_DEADLINE

    target = live_target_kwh if _finite(live_target_kwh) else plan.target_kwh
    if float(target) <= EPSILON:
        return False, REASON_TARGET_MET

    current = plan.slot_at(now)
    if current is not None and current.selected:
        return False, REASON_ABSORPTION_WINDOW

    remaining = plan.remaining_absorbable_kwh(now)
    if remaining + EPSILON < float(target):
        return False, REASON_SHORTFALL_RISK

    if current is None:
        # Between slots, or the feed lags: do not block on stale information.
        return False, "no_current_slot"

    future_prices = [
        slot.export_price
        for slot in plan.slots
        if slot.start >= current.end and slot.absorbable_kwh > EPSILON
    ]
    if not future_prices:
        return False, "no_cheaper_slot_ahead"
    if min(future_prices) > current.export_price - plan.min_saving:
        return False, REASON_NO_MATERIAL_SAVING

    return True, REASON_CHEAPER_WINDOW_AHEAD


# Descriptive alias matching ``build_curtailment_plan``.
build_absorption_plan = plan_surplus_absorption
