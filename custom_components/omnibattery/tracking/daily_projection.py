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
from typing import Any, Callable, Mapping, Sequence

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
    compose_action_mask,
    project_charge_delay,
    simulate_battery_projection,
)


_QUARTER_S = 15 * 60


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
    weekly_full_charge_tomorrow: bool = False
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
    # What the battery keeps at a slot (the plan's holds, a weekly hold, a
    # Time Slot window), given (slot start, usable kWh, free space kWh) as
    # projected there. Must be side-effect free.
    reserve_floor: Callable[[datetime, float, float], float | None] | None = None
    # (solar kWh, house kWh) of each plan interval by its start, before the
    # planner idles the battery (a Time Slot window, a no-discharge window, a
    # price block, peak shaving) or caps its drain: ``plan_intervals`` carry
    # what the battery sees, this is what the chart draws.
    house_forecast: Mapping[datetime, tuple[float, float]] = MappingProxyType({})

    def __post_init__(self) -> None:
        """Detach every collection from its controller-owned source."""
        object.__setattr__(self, "plan_intervals", tuple(self.plan_intervals))
        object.__setattr__(self, "allocations", tuple(self.allocations))
        object.__setattr__(self, "battery_inputs", tuple(self.battery_inputs))
        object.__setattr__(
            self, "house_forecast", MappingProxyType(dict(self.house_forecast))
        )
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
    # The planner's own first bin already starts now and holds only the rest
    # of its quarter; scale it back up, or the last value written before the
    # quarter closes freezes a sliver of it into the past.
    def _full_quarter_factor(interval: Any) -> float:
        covered_s = (interval.end - interval.start).total_seconds()
        if interval.start <= now and 0.0 < covered_s < _QUARTER_S:
            return _QUARTER_S / covered_s
        return 1.0

    def _house(interval: Any) -> tuple[float, float]:
        """(solar, house) of the whole interval, never below the plan's own."""
        planned = (
            _finite(getattr(interval, "solar_kwh", 0.0)),
            _finite(getattr(interval, "consumption_kwh", 0.0)),
        )
        real = request.house_forecast.get(interval.start) or planned
        return (
            max(planned[0], _finite(real[0])),
            max(planned[1], _finite(real[1])),
        )

    full_forecasts = [
        tuple(value * _full_quarter_factor(interval) for value in _house(interval))
        for interval, _remaining_start, _remaining_ratio in remaining_intervals
    ]
    # What the simulation did not see: the house load the grid carries while
    # the battery is idle or capped, and the sun that load takes first.
    unseen = [
        (
            (_house(interval)[0] - _finite(getattr(interval, "solar_kwh", 0.0)))
            * remaining_ratio,
            (_house(interval)[1] - _finite(getattr(interval, "consumption_kwh", 0.0)))
            * remaining_ratio,
        )
        for interval, _remaining_start, remaining_ratio in remaining_intervals
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
        # Tomorrow's weekly full charge lifts the ceiling from midnight on.
        charge_ceiling_pct=(
            [
                100.0 if interval.start >= local_midnight else None
                for interval in projection_inputs
            ]
            if request.weekly_full_charge_tomorrow
            else None
        ),
        discharge_reserve_fn=request.reserve_floor,
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
            unseen_solar, unseen_house = unseen[flow_index]
            item["solar_kwh"] += unseen_solar
            item["consumption_kwh"] += unseen_house
            item["solar_to_home_kwh"] += unseen_solar
            item["grid_to_home_kwh"] += max(0.0, unseen_house - unseen_solar)
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
            # The reason of the curve the panel draws (no safety margin).
            "solar_fallback_reason": request.decision_data.get(
                "solar_shown_fallback_reason",
                request.decision_data.get("solar_timeline_fallback_reason"),
            ),
            "consumption_forecast": request.decision_data.get("chronological_source")
            or "fallback",
            "operation_plan": request.operation_plan_source,
        },
        "_delay_projection": (
            delay_projection.to_dict() if delay_projection is not None else None
        ),
    }


def margin_sun_kwh(planned: Any, shown: Any, change: float, efficiency: float) -> float:
    """Stored kWh a quarter gains from the sun the safety margin took off.

    ``planned`` and ``shown`` are the battery's view of the quarter
    (``consumption_kwh`` drain, ``solar_kwh``) with and without the margin;
    ``change`` is the plan's stored change there. The extra sun first spares
    what the battery gave the house, then its new surplus charges it.
    """
    eff = max(efficiency, 1e-9)
    net_plan = planned.consumption_kwh - planned.solar_kwh
    net_shown = shown.consumption_kwh - shown.solar_kwh
    out = max(0.0, -change) * eff
    # Only the house's share of the drop: a sale's export stays.
    spared = min(out, max(0.0, net_plan)) - min(out, max(0.0, net_shown))
    surplus = max(0.0, max(0.0, -net_shown) - max(0.0, -net_plan))
    return spared / eff + surplus * eff


def overlay_plan_rows(
    dto: dict[str, Any],
    rows: Sequence[tuple[datetime, datetime, float, float, float, float]],
    *,
    start_kwh: float,
    capacity_kwh: float,
    efficiency: float,
    now: datetime,
    floor_kwh: float = 0.0,
    top_kwh: float | None = None,
) -> dict[str, Any]:
    """Draw the published optimizer plan's own quarters over the projection.

    ``rows`` are the plan's ``(start, end, solar, house, delta_kwh, sun_kwh)``
    with the stored-energy change since the evaluation at each quarter's end
    and the stored kWh the margin's sun adds there (``margin_sun_kwh``). The
    line starts at the live ``start_kwh`` and replays each quarter's change
    plus that sun, kept within ``floor_kwh``..``top_kwh``: a plan evaluated
    hours ago, or with the margin eating the day's sun, would otherwise draw a
    cliff at now and a flat line under the drawn sun. Context masks stay the
    projection's; quarters past the plan's last point are dropped (the plan
    does not know them).
    """
    # ponytail: the replay keeps the plan's own changes; a grid charge to a
    # target overshoots it when the line runs above the plan, until the next
    # evaluation re-plans. No charge-rate limit on the margin's sun.
    top = capacity_kwh if top_kwh is None else top_kwh
    replay: dict[datetime, tuple[float, float]] = {}
    level = start_kwh
    previous = 0.0
    for row in sorted(rows, key=lambda r: r[1]):
        start, end, _solar, _house, delta, sun = row
        change = delta - previous + sun
        previous = delta
        if end <= now:
            continue
        length = (end - start).total_seconds()
        left = (end - max(start, now)).total_seconds()
        step = change * (max(0.0, min(1.0, left / length)) if length > 0.0 else 0.0)
        # Only the moving direction is clamped: a live level past a bound
        # stays where it is instead of jumping to it.
        after = (
            max(level, min(top, level + step))
            if step >= 0.0
            else min(level, max(floor_kwh, level + step))
        )
        replay[end] = (level, after)
        level = after
    # ponytail: flows are net per quarter, from the energy change: a rise is
    # bought from the solar surplus first and the grid for the rest, a fall
    # covers the house first. A sale's or a delay export's energy has no field,
    # so the SOC falls (or stays) without a drawn flow.
    by_end = {row[1]: row for row in rows}
    eff = max(efficiency, 1e-9)

    def redraw(item: dict[str, Any]) -> bool:
        row = by_end.get(item.get("end"))
        if row is None or row[1] not in replay:
            return False
        start, end, solar, house, _delta, _sun = row
        length = (end - start).total_seconds()
        left = (end - max(start, now)).total_seconds()
        ratio = max(0.0, min(1.0, left / length)) if length > 0.0 else 0.0
        before, after = replay[end]
        # The quarter's whole change, of which the rest (``ratio``) is left.
        change = (after - before) / ratio if ratio > 0.0 else 0.0
        inn = max(0.0, change) / eff
        out = max(0.0, -change) * eff
        solar_home = min(solar, house)
        solar_battery = min(inn, solar - solar_home)
        battery_home = min(out, house - solar_home)
        flows = {
            "solar_kwh": solar,
            "consumption_kwh": house,
            "solar_to_home_kwh": solar_home,
            "solar_to_battery_kwh": solar_battery,
            "grid_to_battery_kwh": inn - solar_battery,
            "battery_to_home_kwh": battery_home,
            "grid_to_home_kwh": house - solar_home - battery_home,
        }
        for key, value in flows.items():
            item[key] = value * ratio
        full = _QUARTER_S / length if 0.0 < length < _QUARTER_S else 1.0
        item["solar_forecast_kwh"] = solar * full
        item["consumption_forecast_kwh"] = house * full
        item["charge_to_battery_kwh"] = inn * ratio
        item["discharge_from_battery_kwh"] = battery_home * ratio
        hours = left / 3600.0
        item["charge_power_w"] = inn * ratio / hours * 1000.0 if hours > 0.0 else 0.0
        item["discharge_power_w"] = battery_home * ratio / hours * 1000.0 if hours > 0.0 else 0.0
        item["stored_energy_end_kwh"] = after
        item["soc_end_pct"] = item["stored_energy_end_kwh"] / capacity_kwh * 100.0
        item["action_mask"] = compose_action_mask(
            solar_charge=solar_battery > 1e-6,
            grid_charge=inn - solar_battery > 1e-6,
            discharge=battery_home > 1e-6,
        )
        if inn - solar_battery > 1e-6:
            item["grid_charge_decision"] = GRID_CHARGE_SCHEDULED
        return True

    for key in ("intervals", "extended_intervals"):
        dto[key] = [item for item in dto.get(key) or () if redraw(item)]
    return dto
