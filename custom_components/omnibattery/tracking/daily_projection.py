"""Pure transformation of a chronological plan into Daily Operation data.

This module is deliberately independent from the controller.  It accepts the
already-authoritative plan and a snapshot of runtime context, then returns the
JSON-shaped future projection consumed by the timeline sensor.  In particular,
rendering the dashboard cannot update controller diagnostics, schedules, or
device state through this boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ..pricing.daily_timeline import (
    CONTEXT_CHARGE_DELAY,
    CONTEXT_DYNAMIC_PRICE,
    CONTEXT_SETPOINT,
    CONTEXT_TIME_SLOT,
    GRID_CHARGE_NOT_APPLICABLE,
    GRID_CHARGE_NOT_NEEDED,
    GRID_CHARGE_SCHEDULED,
    BatteryProjectionInput,
    ProjectionIntervalInput,
    project_charge_delay,
    simulate_battery_projection,
)


def _finite(value: Any, default: float = 0.0) -> float:
    """Coerce untrusted snapshots without allowing non-finite sensor values."""
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


@dataclass(frozen=True)
class DailyOperationProjectionRequest:
    """Read-only inputs needed to materialize a Daily Operation projection."""

    now: datetime
    plan_intervals: Sequence[Any]
    allocations: Sequence[Any]
    battery_inputs: Sequence[BatteryProjectionInput]
    mode: str
    decision_data: Mapping[str, Any]
    predictive_charging_enabled: bool = False
    has_selected_schedule: bool = False
    setpoint_enabled: bool = False
    setpoint_reached: bool = False
    weekly_charge_bypasses_delay: bool = False
    delay_active: bool = False
    delay_planned: bool = False
    delay_unlock: datetime | None = None
    charge_delay_enabled: bool = False
    setpoint_soc_pct: float = 0.0
    target_soc_pct: float | None = None
    solar_t_end: datetime | None = None
    safety_margin_h: float | None = None
    system_charge_power_w: float | None = None
    system_discharge_power_w: float | None = None
    operation_plan_source: str = "profile_projection"
    plan_evaluated_at: datetime | None = None
    extension_hours: int = 12

    def __post_init__(self) -> None:
        """Detach every collection from its controller-owned source."""
        object.__setattr__(self, "plan_intervals", tuple(self.plan_intervals))
        object.__setattr__(self, "allocations", tuple(self.allocations))
        object.__setattr__(self, "battery_inputs", tuple(self.battery_inputs))
        object.__setattr__(
            self,
            "decision_data",
            MappingProxyType(dict(self.decision_data)),
        )


def build_daily_operation_projection(
    request: DailyOperationProjectionRequest,
) -> dict[str, Any] | None:
    """Return future timeline DTO from immutable/read-only plan snapshots.

    The function never alters ``plan_intervals``, allocations, batteries, or
    the diagnostic mapping.  It is safe to call for a wider display horizon
    than the predictive controller's own execution horizon.
    """
    now = request.now
    local_midnight = now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    projection_horizon_end = local_midnight + timedelta(
        hours=max(0, request.extension_hours)
    )
    plan_intervals = list(request.plan_intervals or ())
    if not plan_intervals:
        return None

    remaining_intervals: list[tuple[Any, datetime, float]] = []
    for interval in plan_intervals:
        start = getattr(interval, "start", None)
        end = getattr(interval, "end", None)
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            continue
        try:
            if end <= now:
                continue
            duration_s = (end - start).total_seconds()
            remaining_start = max(start, now)
            remaining_ratio = (
                max(0.0, (end - remaining_start).total_seconds()) / duration_s
                if duration_s > 0.0
                else 0.0
            )
        except (TypeError, ValueError):
            continue
        if remaining_ratio > 0.0:
            remaining_intervals.append(
                (interval, remaining_start, min(1.0, remaining_ratio))
            )
    if not remaining_intervals:
        return None

    mode_context = {
        "dynamic_pricing": CONTEXT_DYNAMIC_PRICE,
        "time_slot": CONTEXT_TIME_SLOT,
    }.get(request.mode, 0)
    explicit_should_charge = request.decision_data.get("should_charge")
    if request.mode == "time_slot" and "aggregate_should_charge" in request.decision_data:
        explicit_should_charge = request.decision_data["aggregate_should_charge"]
    context_masks: list[int] = []
    grid_decisions: list[str] = []
    for interval, remaining_start, _remaining_ratio in remaining_intervals:
        context = mode_context if request.predictive_charging_enabled else 0
        if (
            request.setpoint_enabled
            and not request.setpoint_reached
            and not request.weekly_charge_bypasses_delay
        ):
            context |= CONTEXT_SETPOINT
        scheduled = any(
            allocation.slot.start < interval.end
            and allocation.slot.end > remaining_start
            for allocation in request.allocations
        )
        if scheduled:
            grid_decisions.append(GRID_CHARGE_SCHEDULED)
        elif (
            request.mode in {"dynamic_pricing", "time_slot"}
            and not request.has_selected_schedule
            and explicit_should_charge is False
        ):
            grid_decisions.append(GRID_CHARGE_NOT_NEEDED)
        else:
            grid_decisions.append(GRID_CHARGE_NOT_APPLICABLE)
        context_masks.append(context)

    projection_inputs = [
        ProjectionIntervalInput(
            start=remaining_start,
            end=interval.end,
            consumption_kwh=_finite(getattr(interval, "consumption_kwh", 0.0))
            * remaining_ratio,
            solar_kwh=_finite(getattr(interval, "solar_kwh", 0.0)) * remaining_ratio,
            state="future",
            context_mask=context_masks[index],
            grid_charge_decision=grid_decisions[index],
            projected=True,
        )
        for index, (interval, remaining_start, remaining_ratio) in enumerate(
            remaining_intervals
        )
    ]
    # The simulation must only see the energy left in an in-progress interval,
    # but the published forecast series has to stay a full-quarter quantity.
    full_forecasts = [
        (
            _finite(getattr(interval, "solar_kwh", 0.0)),
            _finite(getattr(interval, "consumption_kwh", 0.0)),
        )
        for interval, _remaining_start, _remaining_ratio in remaining_intervals
    ]
    delay_projection = None
    delay_starts_at = (
        now
        if request.delay_active
        and (not request.setpoint_enabled or request.setpoint_reached)
        else None
    )
    delay_ends_at = request.delay_unlock if delay_starts_at is not None else None
    if (
        request.battery_inputs
        and request.setpoint_enabled
        and not request.setpoint_reached
        and not request.weekly_charge_bypasses_delay
    ):
        try:
            delay_projection = project_charge_delay(
                projection_inputs,
                request.battery_inputs,
                setpoint_soc_pct=request.setpoint_soc_pct,
                target_soc_pct=request.target_soc_pct,
                enabled=request.setpoint_enabled,
                charge_delay_enabled=request.charge_delay_enabled,
                now=now,
                allocations=request.allocations,
                unlock_at=request.delay_unlock,
                solar_t_end=request.solar_t_end,
                safety_margin_h=request.safety_margin_h,
                system_charge_power_w=request.system_charge_power_w,
                system_discharge_power_w=request.system_discharge_power_w,
            )
            if request.delay_planned:
                delay_starts_at = delay_projection.delay_starts_at
                delay_ends_at = delay_projection.estimated_unlock_at
        except Exception:  # noqa: BLE001 - projection is display-only
            delay_projection = None

    charge_availability: list[float] = []
    for interval in projection_inputs:
        duration_s = max(0.0, (interval.end - interval.start).total_seconds())
        blocked_s = 0.0
        if (
            delay_starts_at is not None
            and delay_ends_at is not None
            and duration_s > 0.0
        ):
            blocked_s = max(
                0.0,
                (
                    min(interval.end, delay_ends_at)
                    - max(interval.start, delay_starts_at)
                ).total_seconds(),
            )
        charge_availability.append(
            max(0.0, min(1.0, 1.0 - blocked_s / duration_s))
            if duration_s > 0.0
            else 0.0
        )

    result = simulate_battery_projection(
        projection_inputs,
        request.battery_inputs,
        allocations=request.allocations,
        context_masks=context_masks,
        grid_charge_decisions=grid_decisions,
        charge_availability=charge_availability,
        system_charge_power_w=request.system_charge_power_w,
        system_discharge_power_w=request.system_discharge_power_w,
    )

    capacity = sum(_finite(item.capacity_kwh) for item in request.battery_inputs)
    source = (
        request.decision_data.get("chronological_source")
        or request.decision_data.get("solar_timeline_source")
        or "profile_projection"
    )
    aggregates: dict[tuple[Any, int], dict[str, Any]] = {}
    for flow_index, flow in enumerate(result.intervals):
        if flow.start is None:
            continue
        flow_start = flow.start
        flow_end = flow.end
        if flow_start.tzinfo is None and now.tzinfo is not None:
            flow_start = flow_start.replace(tzinfo=now.tzinfo)
        elif flow_start.tzinfo is not None and now.tzinfo is not None:
            flow_start = flow_start.astimezone(now.tzinfo)
        if flow_end is not None:
            if flow_end.tzinfo is None and now.tzinfo is not None:
                flow_end = flow_end.replace(tzinfo=now.tzinfo)
            elif flow_end.tzinfo is not None and now.tzinfo is not None:
                flow_end = flow_end.astimezone(now.tzinfo)

        interval_index = flow_start.hour * 4 + flow_start.minute // 15
        is_extended = flow_start >= local_midnight and flow_start < projection_horizon_end
        if flow_start.date() != now.date() and not is_extended:
            continue
        aggregate_key = (flow_start.date(), interval_index)
        item = aggregates.setdefault(
            aggregate_key,
            {
                "index": interval_index,
                "extension_index": interval_index if is_extended else None,
                "_extended": is_extended,
                "solar_kwh": 0.0,
                "consumption_kwh": 0.0,
                "solar_forecast_kwh": 0.0,
                "consumption_forecast_kwh": 0.0,
                "solar_to_battery_kwh": 0.0,
                "grid_to_battery_kwh": 0.0,
                "battery_to_home_kwh": 0.0,
                "grid_to_home_kwh": 0.0,
                "solar_to_home_kwh": 0.0,
                "action_mask": 0,
                "context_mask": 0,
                "grid_charge_decision": GRID_CHARGE_NOT_APPLICABLE,
                "stored_energy_end_kwh": 0.0,
                "duration_s": 0.0,
                "source": str(source),
                "_start": flow_start,
                "_end": flow_end,
            },
        )
        if item["_start"] is None or flow_start < item["_start"]:
            item["_start"] = flow_start
        if flow_end is not None and (item["_end"] is None or flow_end > item["_end"]):
            item["_end"] = flow_end
        for key in (
            "solar_kwh",
            "consumption_kwh",
            "solar_to_battery_kwh",
            "grid_to_battery_kwh",
            "battery_to_home_kwh",
            "grid_to_home_kwh",
            "solar_to_home_kwh",
            "duration_seconds",
        ):
            target_key = "duration_s" if key == "duration_seconds" else key
            item[target_key] += _finite(getattr(flow, key, 0.0))
        if flow_index < len(full_forecasts):
            item["solar_forecast_kwh"] += full_forecasts[flow_index][0]
            item["consumption_forecast_kwh"] += full_forecasts[flow_index][1]
        item["action_mask"] |= int(getattr(flow, "action_mask", 0) or 0)
        item["context_mask"] |= int(getattr(flow, "context_mask", 0) or 0)
        if getattr(flow, "grid_charge_decision", None) == GRID_CHARGE_SCHEDULED:
            item["grid_charge_decision"] = GRID_CHARGE_SCHEDULED
        elif (
            item["grid_charge_decision"] == GRID_CHARGE_NOT_APPLICABLE
            and getattr(flow, "grid_charge_decision", None) == GRID_CHARGE_NOT_NEEDED
        ):
            item["grid_charge_decision"] = GRID_CHARGE_NOT_NEEDED
        item["stored_energy_end_kwh"] = _finite(
            getattr(flow, "stored_energy_end_kwh", 0.0)
        )

    if delay_projection is not None and delay_projection.setpoint_reached_at is not None:
        for item in aggregates.values():
            interval_start = item.get("_start")
            if (
                interval_start is not None
                and interval_start >= delay_projection.setpoint_reached_at
            ):
                item["context_mask"] &= ~CONTEXT_SETPOINT

    for item in aggregates.values():
        interval_start = item.pop("_start", None)
        interval_end = item.pop("_end", None)
        item["start"] = interval_start
        item["end"] = interval_end
        if (
            delay_starts_at is not None
            and delay_ends_at is not None
            and interval_start is not None
            and interval_end is not None
            and interval_end > delay_starts_at
            and interval_start < delay_ends_at
        ):
            item["context_mask"] |= CONTEXT_CHARGE_DELAY
            item["delay_until"] = delay_ends_at
        duration = item.pop("duration_s")
        item["charge_power_w"] = (
            (item["solar_to_battery_kwh"] + item["grid_to_battery_kwh"])
            / duration
            * 3600.0
            * 1000.0
            if duration > 0.0
            else 0.0
        )
        item["discharge_power_w"] = (
            item["battery_to_home_kwh"] / duration * 3600.0 * 1000.0
            if duration > 0.0
            else 0.0
        )
        item["charge_to_battery_kwh"] = (
            item["solar_to_battery_kwh"] + item["grid_to_battery_kwh"]
        )
        item["discharge_from_battery_kwh"] = item["battery_to_home_kwh"]
        if capacity > 0.0:
            item["soc_end_pct"] = item["stored_energy_end_kwh"] / capacity * 100.0
        item["setpoint_active"] = bool(item["context_mask"] & CONTEXT_SETPOINT)
        item["delay_active"] = bool(item["context_mask"] & CONTEXT_CHARGE_DELAY)

    base_intervals = [item for item in aggregates.values() if not item.get("_extended")]
    extended_intervals = [item for item in aggregates.values() if item.get("_extended")]
    for item in aggregates.values():
        item.pop("_extended", None)

    return {
        "intervals": sorted(base_intervals, key=lambda item: item["index"]),
        "extended_intervals": sorted(
            extended_intervals,
            key=lambda item: item.get("extension_index", item["index"]),
        ),
        "extended_horizon": {
            "start": local_midnight,
            "end": projection_horizon_end,
            "interval_minutes": 15,
            "interval_count": max(0, request.extension_hours) * 4,
        },
        "mode": request.mode,
        "plan_evaluated_at": request.plan_evaluated_at or now,
        "stale": False,
        "sources": {
            "solar_forecast": request.decision_data.get("solar_timeline_source")
            or "unavailable",
            "solar_fallback_reason": request.decision_data.get(
                "solar_timeline_fallback_reason"
            ),
            "consumption_forecast": request.decision_data.get("chronological_source")
            or "fallback",
            "operation_plan": request.operation_plan_source,
        },
        "_delay_projection": (
            delay_projection.to_dict() if delay_projection is not None else None
        ),
    }
