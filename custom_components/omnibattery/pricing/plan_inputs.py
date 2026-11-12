"""Pure inputs of the chronological planner.

Extracted from ``PricingManager._build_chronological_plan_for_horizon`` (S4):
each function turns plain values into one input of
``ChronologicalEvaluationRequest`` without touching the controller.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from ..solar_forecast import solar_forecast_period_energy_between
from .chronological import EnergyInterval, normalize_energy_shape
from .solar_timeline import SolarTimelineResult, build_solar_timeline


def consumption_shape(
    forecast: Any,
    boundaries: Sequence[tuple[datetime, datetime]],
    *,
    horizon_end: datetime,
    daily_horizon_end: datetime,
    scalar_total_kwh: float | None,
) -> list[float]:
    """Consumption (kWh) of every planning interval, summing to the horizon total.

    ``forecast`` exposes ``intervals_kwh``, ``intervals_by_date`` and
    ``energy_kwh``; ``scalar_total_kwh`` is the controller's remaining-day balance.
    """
    consumption_raw: list[float] = []
    intervals_by_date = getattr(forecast, "intervals_by_date", None) or {}
    for start, end in boundaries:
        index = start.hour * 4 + start.minute // 15
        # The range API preserves its original one-day contract by
        # aggregating matching wall-clock bins.  A cross-midnight
        # dashboard horizon may contain the same bin on two dates,
        # though, so use the retained date-specific shape whenever it
        # is available.  This also leaves repeated DST bins as two
        # physical intervals of their nominal daily value.
        date_intervals = intervals_by_date.get(
            start.date(), forecast.intervals_kwh
        )
        bucket = (
            float(date_intervals[index])
            if index < len(date_intervals)
            else 0.0
        )
        fraction = max(
            0.0,
            (end.timestamp() - start.timestamp()) / 900.0,
        )
        consumption_raw.append(max(0.0, bucket * fraction))

    # The normal planner receives a remaining-day balance from the
    # controller.  An explicitly extended diagnostic horizon instead
    # needs the profile's energy for the whole requested range;
    # otherwise tomorrow's intervals would merely dilute today's
    # remainder across the extra hours.
    forecast_total = (
        forecast.energy_kwh
        if horizon_end > daily_horizon_end
        else scalar_total_kwh
    )
    consumption_total = max(0.0, float(forecast_total or 0.0))
    return normalize_energy_shape(consumption_raw, consumption_total)


def _apply_excluded_demand_claim(
    boundaries: list[tuple[datetime, datetime]],
    intervals_kwh: list[float],
    claim_kwh: float,
    today_end: datetime,
) -> tuple[list[float], float]:
    """Reserve an excluded device's remaining demand from today's solar.

    The reservation is spread over the intervals that start before
    ``today_end``, proportional to their energy, and capped at what those
    intervals hold. Intervals beyond it belong to tomorrow's forecast in a
    cross-midnight projection: a sensor reporting demand remaining *today*
    must never reduce them.

    Returns the adjusted intervals and the claim actually applied.
    """
    claim = max(0.0, claim_kwh)
    if claim <= 0.0:
        return intervals_kwh, 0.0
    today_indices = [
        index
        for index, (start, _end) in enumerate(boundaries)
        if start < today_end and intervals_kwh[index] > 0.0
    ]
    available = sum(intervals_kwh[index] for index in today_indices)
    if available <= 0.0:
        return intervals_kwh, 0.0
    claim = min(claim, available)
    factor = (available - claim) / available
    adjusted = list(intervals_kwh)
    for index in today_indices:
        adjusted[index] *= factor
    return adjusted, claim


@dataclass(frozen=True)
class SolarTimeline:
    """Solar curve of the plan: the selected timeline and the claim applied to it."""

    timeline: SolarTimelineResult
    intervals_kwh: list[float]
    excluded_claim_kwh: float
    source: str


def solar_timeline(
    boundaries: list[tuple[datetime, datetime]],
    solar_input: Any,
    learned_snapshot: Any,
    *,
    solar_start: datetime | None,
    solar_end: datetime | None,
    mode: str,
    safety_margin_kwh: float,
    extended_timezone: Any,
    excluded_claim_kwh: float,
    today_end: datetime,
) -> SolarTimeline:
    """Per-interval solar of the horizon, net of an excluded device's claim.

    ``solar_input`` exposes ``remaining_kwh``, ``periods`` and
    ``temporal_shape``; ``learned_snapshot`` (or ``None``) exposes ``shape`` and
    ``mature``.  ``extended_timezone`` is the local timezone when the horizon
    is extended past the control one, else ``None``.
    """
    provider_periods = solar_input.periods
    temporal_shape = solar_input.temporal_shape
    if extended_timezone is not None and provider_periods:
        # A cross-midnight provider curve may contain a normal overnight
        # gap. Map its dated periods directly onto the exact boundaries;
        # the single-day daylight coverage validator cannot represent
        # two separate solar windows safely.
        temporal_shape = tuple(
            solar_forecast_period_energy_between(
                provider_periods,
                start,
                end,
                timezone=extended_timezone,
            )
            for start, end in boundaries
        )
        provider_periods = None

    timeline = build_solar_timeline(
        boundaries,
        solar_input.remaining_kwh,
        safety_margin_kwh=safety_margin_kwh,
        provider_periods=provider_periods,
        temporal_shape=temporal_shape,
        learned_shape=(learned_snapshot.shape if learned_snapshot else None),
        learned_mature=bool(learned_snapshot and learned_snapshot.mature),
        solar_start=solar_start,
        solar_end=solar_end,
        mode=mode,
    )
    # Excluded devices reserve part of the remaining forecast (see
    # _should_activate_grid_charging). The timeline must project the
    # same solar the scalar balance used, otherwise it would place
    # deadlines against sunshine another load is going to take. The
    # reservation is taken out of today's intervals only, proportional
    # to their energy: the sensor reports demand remaining *today*, and
    # a cross-midnight projection also carries tomorrow's forecast,
    # which the claim must never touch. Within today every interval
    # gives up the same share of its own energy, because we do not know
    # when the device will draw, which is uniformly conservative.
    solar, claim = _apply_excluded_demand_claim(
        boundaries,
        list(timeline.intervals_kwh),
        excluded_claim_kwh,
        today_end,
    )
    return SolarTimeline(timeline, solar, claim, timeline.source)


def battery_idle_intervals(
    boundaries: Sequence[tuple[datetime, datetime]],
    consumption: Sequence[float],
    solar: Sequence[float],
    *,
    held_until: datetime | None,
    limit_kw: float,
    fleet_blocks: Sequence[Any],
    block_matches: Callable[[Any, Any], bool],
    blocked_price_slots: Sequence[Any] | None,
    tz: Any,
) -> list[EnergyInterval]:
    """Planning intervals with the battery idled where nothing may discharge.

    ``held_until`` (peak shaving conserving now) trims each earlier
    interval's load to the part above ``limit_kw``; ``fleet_blocks`` are the
    fleet-wide no-discharge windows, matched with ``block_matches(interval,
    block)``; ``blocked_price_slots`` are the price slots at or below the
    discharge-control threshold (``None`` when that control is inactive) and
    ``tz`` the timezone their naive wall time is compared in.
    """
    # Peak shaving that is conserving now holds the battery to the
    # excess above its limit; the grid carries the rest of the load.
    # Projecting a full overnight drain instead booked a phantom
    # guaranteed-floor charge for a battery that never moved.
    intervals = [
        EnergyInterval(
            start,
            end,
            max(
                0.0,
                consumption[index]
                - limit_kw * (end - start).total_seconds() / 3600.0,
            )
            if held_until is not None and start < held_until
            else consumption[index],
            solar[index],
        )
        for index, (start, end) in enumerate(boundaries)
    ]
    # A no-discharge window blocks the battery outright, peaks
    # included, so the grid carries all of that interval's load.
    if fleet_blocks:
        intervals = [
            item.with_battery_idle()
            if any(block_matches(item, block) for block in fleet_blocks)
            else item
            for item in intervals
        ]
    # Price-based discharge control idles the battery whenever the
    # slot price is at or below its threshold (#594), so those
    # intervals drain nothing either.
    if blocked_price_slots is not None:

        def wall(value: datetime) -> datetime:
            # Price slots are naive local wall time.
            if value.tzinfo is None:
                return value
            return value.astimezone(tz).replace(tzinfo=None)

        intervals = [
            item.with_battery_idle()
            if any(
                wall(slot.start) <= wall(item.start) < wall(slot.end)
                for slot in blocked_price_slots
            )
            else item
            for item in intervals
        ]
    return intervals


def time_slot_window_hold(
    intervals: Sequence[EnergyInterval],
    windows: Sequence[Any],
) -> tuple[list[EnergyInterval], float | None]:
    """Idle the battery inside the Time Slot ``windows`` (each with ``start``/``end``).

    Returns the intervals and the load the grid carries inside the windows;
    that load is ``None`` when there are no windows, and the intervals pass
    through untouched.
    """
    if not windows:
        return intervals, None
    window_load_on_grid = 0.0
    held = []
    for item in intervals:
        if any(w.start <= item.start < w.end for w in windows):
            window_load_on_grid += max(
                0.0, item.consumption_kwh - item.solar_kwh
            )
            item = item.with_battery_idle()
        held.append(item)
    return held, window_load_on_grid


@dataclass(frozen=True)
class FleetPlanState:
    """Fleet energy figures the plan is sized with."""

    total_capacity_kwh: float
    weighted_min_soc: float
    usable_kwh: float
    room_kwh: tuple[float, ...]
    headroom_kwh: float


def fleet_plan_state(
    batteries: Sequence[tuple[float, float, float, float]],
) -> FleetPlanState:
    """Capacity, usable energy and room below the ceiling of the fleet.

    Each battery is ``(capacity_kwh, soc, floor_soc, ceiling_soc)``, SOCs in
    percent; ``room_kwh`` keeps the order of ``batteries``.
    """
    total_capacity = sum(cap for cap, _soc, _floor, _ceiling in batteries)
    weighted_min_soc = (
        sum(floor * cap for cap, _soc, floor, _ceiling in batteries)
        / total_capacity
        if total_capacity > 0
        else 0.0
    )
    usable = sum(
        max(0.0, (soc - floor) / 100.0 * cap)
        for cap, soc, floor, _ceiling in batteries
    )
    room_kwh = tuple(
        max(0.0, (ceiling - soc) / 100.0 * cap)
        for cap, soc, _floor, ceiling in batteries
    )
    return FleetPlanState(
        total_capacity, weighted_min_soc, usable, room_kwh, sum(room_kwh)
    )
