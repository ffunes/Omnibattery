"""Pure planning helpers for smart pre-discharge.

The planner has no Home Assistant dependency.  It receives normalized
``PriceSlot`` objects and snapshots of the batteries, then returns a plan that
the pricing engine can safely apply through the normal PD path.  In particular,
it never controls a PV inverter and it treats all battery floors as hard
constraints.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Mapping, Sequence

from ..const import CHARGE_EFFICIENCY
from . import PriceSlot


EPSILON = 1e-6

# These values intentionally live in this pure module as well as in the
# configuration layer.  The planner must remain usable by older callers that
# only provide ``max_export_power_w``.
EXPORT_MODE_SELF_CONSUMPTION = "self_consumption"
EXPORT_MODE_AUTOMATIC = "automatic"
EXPORT_MODE_CUSTOM = "custom"


def normalize_export_mode(
    mode: object | None,
    max_export_power_w: float | None = 0.0,
) -> str:
    """Normalize the selector value while preserving the legacy W setting.

    Older entries have no selector: zero was the self-consumption behaviour and
    a positive value was an intentional-export cap.  Unknown values are treated
    the same way as the legacy field rather than accidentally enabling export.
    """
    if mode is not None:
        value = str(mode).strip().lower().replace("-", "_").replace(" ", "_")
        if value in {
            EXPORT_MODE_SELF_CONSUMPTION,
            "selfconsume",
            "self_consumption_only",
            "solo_autoconsumo",
            "autoconsumo",
            "zero",
        }:
            return EXPORT_MODE_SELF_CONSUMPTION
        if value in {EXPORT_MODE_AUTOMATIC, "auto", "automatico", "automático"}:
            return EXPORT_MODE_AUTOMATIC
        if value in {
            EXPORT_MODE_CUSTOM,
            "custom_limit",
            "custom_limit_w",
            "limite_personalizado",
            "límite_personalizado",
        }:
            return EXPORT_MODE_CUSTOM

    try:
        legacy_limit = float(max_export_power_w or 0.0)
    except (TypeError, ValueError):
        legacy_limit = 0.0
    return EXPORT_MODE_CUSTOM if math.isfinite(legacy_limit) and legacy_limit > EPSILON else EXPORT_MODE_SELF_CONSUMPTION


def calculate_opportunistic_space_kwh(
    free_space_kwh: float,
    solar_reserve_remaining_kwh: float,
) -> float:
    """Return battery space that may be used by opportunistic grid charging."""
    try:
        free = max(0.0, float(free_space_kwh))
        reserve = max(0.0, float(solar_reserve_remaining_kwh))
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(free) or not math.isfinite(reserve):
        return 0.0
    return max(0.0, free - reserve)


@dataclass(frozen=True)
class BatterySnapshot:
    """The battery state needed by the pure planner."""

    name: str
    soc_pct: float
    capacity_kwh: float
    max_soc_pct: float
    floor_soc_pct: float
    max_discharge_power_w: float
    eligible: bool = True
    can_discharge: bool = True


@dataclass(frozen=True)
class PreDischargeSlot:
    """One grouped, future slot in which PD may pre-discharge."""

    start: datetime
    end: datetime
    price: float
    planned_energy_kwh: float = 0.0
    power_w: float = 0.0
    export_target_w: float = 0.0

    @property
    def duration_hours(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds() / 3600.0)


@dataclass
class CurtailmentPlan:
    """A recalculated anti-curtailment plan and its diagnostic accounting."""

    status: str = "disabled"
    reason: str = "disabled"
    evaluation_time: datetime | None = None
    risk_slots: list[PriceSlot] = field(default_factory=list)
    selected_discharge_slots: list[PreDischargeSlot] = field(default_factory=list)
    required_headroom_kwh: float = 0.0
    current_headroom_kwh: float = 0.0
    planned_discharge_kwh: float = 0.0
    shortfall_kwh: float = 0.0
    target_soc_by_battery: dict[str, float] = field(default_factory=dict)
    active_export_target_w: float = 0.0
    solar_surplus_kwh: float = 0.0
    # Anti-curtailment accounting used by the negative-price runtime.  The
    # reserve is battery-side energy still needed for the future solar risk;
    # opportunistic charging may consume only the difference from free space.
    solar_reserve_remaining_kwh: float = 0.0
    opportunistic_space_kwh: float = 0.0
    opportunistic_charge_limit_w: float = 0.0
    opportunistic_charge_reason: str = "not_calculated"
    export_mode: str = EXPORT_MODE_SELF_CONSUMPTION
    export_limit_w: float = 0.0
    solar_reserve_by_slot: dict[PriceSlot, float] = field(default_factory=dict)
    solar_forecast_by_slot: dict[PriceSlot, float] = field(default_factory=dict)
    consumption_forecast_by_slot: dict[PriceSlot, float] = field(default_factory=dict)
    solar_forecast_kwh: float = 0.0
    solar_forecast_is_remaining: bool = False
    headroom_margin_kwh: float = 0.0

    # Short aliases make the object convenient for integrations and tests while
    # keeping the explicit diagnostic name used by the entity attributes.
    @property
    def discharge_slots(self) -> list[PreDischargeSlot]:
        return self.selected_discharge_slots

    @property
    def required_headroom(self) -> float:
        return self.required_headroom_kwh

    @property
    def current_headroom(self) -> float:
        return self.current_headroom_kwh

    @property
    def solar_reserve(self) -> float:
        """Compatibility alias for diagnostics consumers."""
        return self.solar_reserve_remaining_kwh

    @property
    def opportunistic_space(self) -> float:
        """Compatibility alias for diagnostics consumers."""
        return self.opportunistic_space_kwh

    @property
    def is_fail_safe(self) -> bool:
        return self.status == "fail_safe"


def _duration_hours(slot: PriceSlot) -> float:
    return max(0.0, (slot.end - slot.start).total_seconds() / 3600.0)


def _overlaps(left: PriceSlot | PreDischargeSlot, right: PriceSlot | PreDischargeSlot) -> bool:
    return left.start < right.end and right.start < left.end


def _finite(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _cumulative_share(
    fraction_fn: Callable[[float], float] | None,
    start: datetime,
    end: datetime,
) -> float:
    """Return a bounded generation share for one slot.

    ``fraction_fn`` is the existing solar model's cumulative fraction.  The
    fallback is intentionally uniform; it is used only when a caller has no
    solar model and keeps the pure helper useful in fail-safe tests.
    """
    if fraction_fn is None:
        return 0.0
    try:
        start_hour = start.hour + start.minute / 60.0 + start.second / 3600.0
        end_hour = end.hour + end.minute / 60.0 + end.second / 3600.0
        # A final slot may end exactly at local midnight.  Preserve that as
        # hour 24 for the same daily model instead of wrapping it to 00:00
        # and falsely reporting zero production in the last slot.
        day_delta = (end.date() - start.date()).days
        if day_delta > 0:
            end_hour += 24.0 * day_delta
        start_fraction = float(fraction_fn(start_hour))
        end_fraction = float(fraction_fn(end_hour))
    except (TypeError, ValueError, AttributeError):
        return 0.0
    if not math.isfinite(start_fraction) or not math.isfinite(end_fraction):
        return 0.0
    return max(0.0, min(1.0, end_fraction) - max(0.0, min(1.0, start_fraction)))


def distribute_solar_forecast(
    slots: Sequence[PriceSlot],
    forecast_kwh: float,
    fraction_fn: Callable[[float], float] | None = None,
    *,
    normalize_future: bool = False,
) -> dict[PriceSlot, float]:
    """Distribute a daily solar forecast over normalized price slots.

    The production model supplies a cumulative fraction.  If it cannot be
    supplied, the function uses a duration-weighted uniform fallback rather
    than inventing a peak.  Only non-negative finite forecast values are used.
    """
    if not _finite(forecast_kwh) or float(forecast_kwh) <= 0 or not slots:
        return {slot: 0.0 for slot in slots}

    forecast = float(forecast_kwh)
    if fraction_fn is not None:
        shares = {slot: _cumulative_share(fraction_fn, slot.start, slot.end) for slot in slots}
        total_share = sum(shares.values())
        if total_share > EPSILON:
            if normalize_future:
                # ``forecast`` is already the energy from now on, so allocate
                # all of it over the provided future slots.  This is the one
                # intentional renormalisation; legacy ``today`` keeps the old
                # full-day behaviour above.
                return {slot: forecast * share / total_share for slot, share in shares.items()}
            return {slot: forecast * share for slot, share in shares.items()}
        return {slot: 0.0 for slot in slots}

    total_hours = sum(_duration_hours(slot) for slot in slots)
    if total_hours <= EPSILON:
        return {slot: 0.0 for slot in slots}
    return {
        slot: forecast * _duration_hours(slot) / total_hours
        for slot in slots
    }


def estimate_consumption_by_slot(
    slots: Sequence[PriceSlot],
    daily_consumption_kwh: float,
    fraction_fn: Callable[[float], float] | None = None,
    *,
    normalize_future: bool = False,
) -> dict[PriceSlot, float]:
    """Estimate consumption per slot, optionally from a remaining total."""
    if not _finite(daily_consumption_kwh) or float(daily_consumption_kwh) <= 0:
        return {slot: 0.0 for slot in slots}

    consumption = float(daily_consumption_kwh)
    if fraction_fn is not None:
        shares = {slot: _cumulative_share(fraction_fn, slot.start, slot.end) for slot in slots}
        total_share = sum(shares.values())
        if total_share > EPSILON:
            return {slot: consumption * share / total_share for slot, share in shares.items()}

    total_hours = sum(_duration_hours(slot) for slot in slots)
    if total_hours <= EPSILON:
        return {slot: 0.0 for slot in slots}
    denominator = total_hours if normalize_future else 24.0
    return {
        slot: consumption * _duration_hours(slot) / denominator
        for slot in slots
    }


def _make_blocks(slots: Sequence[PriceSlot]) -> list[list[PriceSlot]]:
    """Group adjacent same-duration slots into roughly one-hour blocks."""
    ordered = sorted(slots, key=lambda slot: slot.start)
    blocks: list[list[PriceSlot]] = []
    run: list[PriceSlot] = []
    run_duration: float | None = None

    def flush() -> None:
        nonlocal run
        if not run:
            return
        duration = run_duration or _duration_hours(run[0])
        block_size = max(1, int(round(1.0 / duration))) if duration > EPSILON else 1
        for index in range(0, len(run), block_size):
            blocks.append(run[index:index + block_size])
        run = []

    for slot in ordered:
        duration = _duration_hours(slot)
        contiguous = bool(run) and abs((slot.start - run[-1].end).total_seconds()) < 1
        same_duration = run_duration is not None and abs(duration - run_duration) < 1e-6
        if run and (not contiguous or not same_duration):
            flush()
            run_duration = None
        if not run:
            run_duration = duration
        run.append(slot)
    flush()
    return blocks


def select_most_valuable_discharge_slots(
    candidates: Sequence[PriceSlot],
    energy_needed_kwh: float,
    max_discharge_power_w: float,
    *,
    max_export_power_w: float | None = 0.0,
    export_mode: str | None = None,
    available_energy_by_slot: Mapping[PriceSlot, float] | None = None,
) -> list[PreDischargeSlot]:
    """Choose expensive contiguous blocks before risk windows.

    For 15-minute feeds the block size is four slots; for hourly feeds it is
    one.  Blocks are ranked by price, with later blocks winning ties so the
    discharge remains close to the protected solar window.
    """
    if energy_needed_kwh <= EPSILON or max_discharge_power_w <= EPSILON:
        return []

    normalized_mode = normalize_export_mode(export_mode, max_export_power_w)
    try:
        export_limit = max(0.0, float(max_export_power_w or 0.0))
    except (TypeError, ValueError):
        export_limit = 0.0

    blocks = _make_blocks(candidates)
    ranked: list[tuple[float, datetime, list[PriceSlot]]] = []
    for block in blocks:
        if not block:
            continue
        duration = sum(_duration_hours(slot) for slot in block)
        if duration <= EPSILON:
            continue
        average_price = sum(slot.price * _duration_hours(slot) for slot in block) / duration
        ranked.append((average_price, block[-1].end, block))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)

    selected: list[PreDischargeSlot] = []
    remaining = float(energy_needed_kwh)
    for _average_price, _end, block in ranked:
        if remaining <= EPSILON:
            break
        power_w = max(0.0, float(max_discharge_power_w))
        slot_capacities = {
            slot: min(
                power_w * _duration_hours(slot) / 1000.0,
                max(0.0, float(available_energy_by_slot.get(slot, 0.0))),
            )
            if available_energy_by_slot is not None
            else power_w * _duration_hours(slot) / 1000.0
            for slot in block
        }
        block_energy = sum(slot_capacities.values())
        if block_energy <= EPSILON:
            continue
        allocated_energy = min(remaining, block_energy)
        # A block is kept whole to avoid relay chatter; the live controller
        # later adapts the setpoint to the actual remaining headroom.
        selected.extend(
            PreDischargeSlot(
                start=slot.start,
                end=slot.end,
                price=float(slot.price),
                planned_energy_kwh=allocated_energy * slot_capacities[slot] / block_energy,
                power_w=(
                    allocated_energy * slot_capacities[slot] / block_energy
                    / _duration_hours(slot) * 1000.0
                    if _duration_hours(slot) > EPSILON
                    else 0.0
                ),
                export_target_w=(
                    export_limit
                    if normalized_mode == EXPORT_MODE_CUSTOM
                    else 0.0
                ),
            )
            for slot in block
            if slot_capacities[slot] > EPSILON
        )
        remaining -= allocated_energy
    return sorted(selected, key=lambda slot: slot.start)


def _invalid_battery(snapshot: BatterySnapshot) -> bool:
    return not (
        snapshot.eligible
        and _finite(snapshot.soc_pct)
        and _finite(snapshot.capacity_kwh)
        and _finite(snapshot.max_soc_pct)
        and _finite(snapshot.floor_soc_pct)
        and _finite(snapshot.max_discharge_power_w)
        and snapshot.capacity_kwh > 0
        and snapshot.max_soc_pct >= snapshot.floor_soc_pct
        and snapshot.max_discharge_power_w >= 0
    )


def plan_curtailment(
    price_slots: Sequence[PriceSlot],
    solar_forecast_kwh: float | None = None,
    daily_consumption_kwh: float | None = None,
    batteries: Sequence[BatterySnapshot] = (),
    *,
    negative_injection_threshold: float = 0.0,
    predischarge_reserve_soc: float = 0.0,
    headroom_margin_kwh: float = 0.0,
    charge_power_w: float | None = None,
    max_export_power_w: float | None = 0.0,
    export_mode: str | None = None,
    solar_fraction_fn: Callable[[float], float] | None = None,
    solar_forecast_is_remaining: bool = False,
    consumption_forecast_is_remaining: bool = False,
    consumption_fraction_fn: Callable[[float], float] | None = None,
    solar_by_slot: Mapping[PriceSlot, float] | None = None,
    consumption_by_slot: Mapping[PriceSlot, float] | None = None,
    reserved_slots: Iterable[PriceSlot] = (),
    now: datetime | None = None,
) -> CurtailmentPlan:
    """Build an anti-curtailment plan from normalized prices and live capacity."""
    evaluated_at = now or datetime.now()
    plan = CurtailmentPlan(evaluation_time=evaluated_at)

    normalized_export_mode = normalize_export_mode(export_mode, max_export_power_w)
    try:
        export_limit_w = max(0.0, float(max_export_power_w or 0.0))
    except (TypeError, ValueError):
        export_limit_w = 0.0
    if normalized_export_mode != EXPORT_MODE_CUSTOM:
        export_limit_w = 0.0
    plan.export_mode = normalized_export_mode
    plan.export_limit_w = export_limit_w

    if not price_slots:
        plan.status, plan.reason = "fail_safe", "missing_prices"
        return plan
    if solar_forecast_kwh is None or not _finite(solar_forecast_kwh):
        plan.status, plan.reason = "fail_safe", "missing_solar_forecast"
        return plan
    if daily_consumption_kwh is None or not _finite(daily_consumption_kwh):
        plan.status, plan.reason = "fail_safe", "missing_consumption"
        return plan

    try:
        valid_slots = [
            slot
            for slot in price_slots
            if slot.end > slot.start and _finite(slot.price)
        ]
        ordered_slots = sorted(
            (slot for slot in valid_slots if slot.end > evaluated_at),
            key=lambda slot: slot.start,
        )
    except (AttributeError, TypeError, ValueError):
        valid_slots = []
        ordered_slots = []
    if not ordered_slots:
        reason = "no_future_slots" if valid_slots else "invalid_or_missing_price_slots"
        plan.status, plan.reason = "fail_safe", reason
        return plan

    valid_batteries = [snapshot for snapshot in batteries if not _invalid_battery(snapshot)]
    if not valid_batteries:
        plan.status, plan.reason = "fail_safe", "missing_battery_capacity_or_soc"
        return plan
    if charge_power_w is None or not _finite(charge_power_w) or float(charge_power_w) <= 0:
        plan.status, plan.reason = "fail_safe", "missing_charge_capacity"
        return plan

    solar = dict(solar_by_slot or distribute_solar_forecast(
        ordered_slots, float(solar_forecast_kwh), solar_fraction_fn,
        normalize_future=solar_forecast_is_remaining,
    ))
    consumption = dict(consumption_by_slot or estimate_consumption_by_slot(
        ordered_slots, float(daily_consumption_kwh), consumption_fraction_fn,
        normalize_future=consumption_forecast_is_remaining,
    ))
    if not _finite(negative_injection_threshold):
        plan.status, plan.reason = "fail_safe", "invalid_negative_injection_threshold"
        return plan
    threshold = float(negative_injection_threshold)
    risk_slots: list[PriceSlot] = []
    required_kwh = 0.0
    surplus_kwh = 0.0
    reserve_by_slot: dict[PriceSlot, float] = {}
    for slot in ordered_slots:
        slot_solar = max(0.0, float(solar.get(slot, 0.0) or 0.0))
        slot_consumption = max(0.0, float(consumption.get(slot, 0.0) or 0.0))
        surplus = max(0.0, slot_solar - slot_consumption)
        if float(slot.price) <= threshold and surplus > EPSILON:
            risk_slots.append(slot)
            surplus_kwh += surplus
            # The export cap applies while creating headroom before the risk
            # window; it does not permit export during that protected window.
            # Convert AC-side PV surplus to the battery-side headroom that it
            # can consume, accounting for the integration's charge efficiency.
            slot_reserve = min(
                surplus,
                float(charge_power_w) * _duration_hours(slot) / 1000.0,
            ) * CHARGE_EFFICIENCY
            reserve_by_slot[slot] = slot_reserve
            required_kwh += slot_reserve

    plan.risk_slots = risk_slots
    plan.solar_surplus_kwh = surplus_kwh
    plan.solar_forecast_kwh = max(0.0, float(solar_forecast_kwh))
    plan.solar_forecast_is_remaining = solar_forecast_is_remaining
    plan.solar_reserve_by_slot = reserve_by_slot
    plan.solar_forecast_by_slot = {
        slot: max(0.0, float(solar.get(slot, 0.0) or 0.0)) for slot in ordered_slots
    }
    plan.consumption_forecast_by_slot = {
        slot: max(0.0, float(consumption.get(slot, 0.0) or 0.0))
        for slot in ordered_slots
    }

    # Calculate this even on no-risk days.  It makes the runtime diagnostic
    # explicit and correctly reports all free SOC as usable opportunistic space
    # when no solar reserve has to be protected.
    current_headroom = sum(
        max(0.0, (snapshot.max_soc_pct - snapshot.soc_pct) / 100.0 * snapshot.capacity_kwh)
        for snapshot in valid_batteries
    )
    plan.current_headroom_kwh = current_headroom
    if not risk_slots:
        plan.solar_reserve_remaining_kwh = 0.0
        plan.opportunistic_space_kwh = calculate_opportunistic_space_kwh(
            current_headroom, 0.0
        )
        plan.opportunistic_charge_reason = "no_solar_risk_reserve"
        plan.status, plan.reason = "no_risk", "no_negative_injection_window"
        return plan

    # Reuse the controller's common solar-forecast safety margin. It is an
    # energy buffer, so anti-curtailment applies it additively rather than as
    # a percentage of the forecast surplus.
    if not _finite(headroom_margin_kwh):
        plan.status, plan.reason = "fail_safe", "invalid_headroom_margin"
        return plan
    margin_kwh = min(
        max(0.0, float(headroom_margin_kwh or 0.0)),
        sum(snapshot.capacity_kwh for snapshot in valid_batteries),
    )
    required_kwh += margin_kwh
    plan.required_headroom_kwh = required_kwh
    plan.headroom_margin_kwh = margin_kwh
    plan.solar_reserve_remaining_kwh = required_kwh
    plan.opportunistic_space_kwh = calculate_opportunistic_space_kwh(
        current_headroom, required_kwh
    )
    plan.opportunistic_charge_reason = (
        "solar_reserve_protected"
        if plan.opportunistic_space_kwh <= EPSILON
        else "solar_reserve_space_available"
    )

    if current_headroom + EPSILON >= required_kwh:
        plan.status, plan.reason = "protected", "headroom_sufficient"
        plan.target_soc_by_battery = {
            snapshot.name: float(snapshot.soc_pct) for snapshot in valid_batteries
        }
        return plan

    extra_needed = required_kwh - current_headroom
    reserve = max(0.0, float(predischarge_reserve_soc or 0.0))
    discharge_batteries = [
        snapshot
        for snapshot in valid_batteries
        if snapshot.can_discharge and snapshot.max_discharge_power_w > EPSILON
    ]
    available_by_battery = {
        snapshot.name: max(
            0.0,
            (snapshot.soc_pct - max(snapshot.floor_soc_pct, reserve))
            / 100.0 * snapshot.capacity_kwh,
        )
        for snapshot in discharge_batteries
    }
    available_discharge_kwh = sum(available_by_battery.values())
    max_discharge_power_w = sum(
        snapshot.max_discharge_power_w for snapshot in discharge_batteries
    )

    reserved = list(reserved_slots)
    earliest_risk_start = min(slot.start for slot in risk_slots)
    candidates = [
        slot for slot in ordered_slots
        if float(slot.price) > threshold
        and not any(_overlaps(slot, risk) for risk in risk_slots)
        and not any(_overlaps(slot, reserved_slot) for reserved_slot in reserved)
        # The runtime protects against the aggregate daily surplus, so all of
        # that headroom must exist before the first risk window.  A later slot
        # cannot retroactively satisfy an earlier deadline.
        and slot.end <= earliest_risk_start
    ]
    available_energy_by_slot = {}
    for slot in candidates:
        slot_duration = _duration_hours(slot)
        net_load_energy = max(
            0.0,
            float(consumption.get(slot, 0.0) or 0.0)
            - float(solar.get(slot, 0.0) or 0.0),
        )
        # Self-consumption only may discharge into the household load.  Custom
        # mode adds exactly its deliberate-export limit.  Automatic mode has no
        # fixed export cap: it may use the battery power needed to create the
        # calculated headroom, but never more than that need is selected.
        if normalized_export_mode == EXPORT_MODE_AUTOMATIC:
            deliberate_export = max_discharge_power_w * slot_duration / 1000.0
        else:
            deliberate_export = export_limit_w * slot_duration / 1000.0
        available_energy_by_slot[slot] = net_load_energy + deliberate_export
    selected = select_most_valuable_discharge_slots(
        candidates,
        min(extra_needed, available_discharge_kwh),
        max_discharge_power_w,
        max_export_power_w=export_limit_w,
        export_mode=normalized_export_mode,
        available_energy_by_slot=available_energy_by_slot,
    )
    planned_kwh = min(
        min(extra_needed, available_discharge_kwh),
        sum(slot.planned_energy_kwh for slot in selected),
    )
    plan.selected_discharge_slots = selected
    plan.planned_discharge_kwh = planned_kwh
    plan.shortfall_kwh = max(0.0, extra_needed - planned_kwh)
    if plan.shortfall_kwh > EPSILON:
        plan.status, plan.reason = "shortfall", "insufficient_pre_discharge_power_or_slots"
    else:
        plan.status, plan.reason = "planned", "headroom_required"

    # Allocate the planned energy proportionally, clamped to each battery's
    # configured floor.  Runtime re-reads SOC and applies the same floors.
    remaining = planned_kwh
    targets: dict[str, float] = {}
    for snapshot in discharge_batteries:
        available = available_by_battery[snapshot.name]
        allocation = 0.0 if available_discharge_kwh <= EPSILON else planned_kwh * available / available_discharge_kwh
        allocation = min(available, max(0.0, allocation), remaining)
        targets[snapshot.name] = max(
            max(snapshot.floor_soc_pct, reserve),
            snapshot.soc_pct - allocation / snapshot.capacity_kwh * 100.0,
        )
        remaining -= allocation
    plan.target_soc_by_battery = targets
    projected_headroom = current_headroom + planned_kwh
    plan.opportunistic_space_kwh = calculate_opportunistic_space_kwh(
        projected_headroom, plan.solar_reserve_remaining_kwh
    )
    if plan.opportunistic_space_kwh > EPSILON:
        plan.opportunistic_charge_reason = "solar_reserve_space_available"
    elif plan.shortfall_kwh > EPSILON:
        plan.opportunistic_charge_reason = "solar_reserve_shortfall"
    else:
        plan.opportunistic_charge_reason = "solar_reserve_protected"
    return plan


# Descriptive alias used by callers that prefer the noun-first name.
build_curtailment_plan = plan_curtailment
