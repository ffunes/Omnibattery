"""Pure construction and selection of a future solar timeline.

This module intentionally knows nothing about Home Assistant, entities or
coordinators.  It receives dated periods and normalized learned weights, then
returns one finite curve whose sum is the effective solar budget used by the
chronological planner.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from ..solar_forecast import SolarForecastPeriod

INTERVAL_MINUTES = 15
PROGRESS_BIN_COUNT = 96
MAX_PROVIDER_GAP_SECONDS = 60 * 60
_EPSILON = 1e-12


def _timestamp(value: datetime) -> float:
    """Return an absolute timestamp; naive values are treated as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).timestamp()
    return value.timestamp()


def _from_timestamp(value: float, tz: Any) -> datetime:
    if tz is None:
        return datetime.fromtimestamp(value, timezone.utc)
    return datetime.fromtimestamp(value, tz)


def _next_local_quarter_timestamp(cursor_ts: float, tz: Any) -> float:
    """Find the next quarter boundary in absolute time, including DST folds."""
    cursor = _from_timestamp(cursor_ts, tz)
    wall = cursor.replace(tzinfo=None)
    quarter = (wall.minute // INTERVAL_MINUTES) * INTERVAL_MINUTES
    boundary = wall.replace(minute=quarter, second=0, microsecond=0)
    if boundary <= wall:
        boundary += timedelta(minutes=INTERVAL_MINUTES)

    candidates: list[float] = []
    for fold in (0, 1):
        try:
            candidate = boundary.replace(tzinfo=tz, fold=fold).timestamp()
        except (AttributeError, OverflowError, ValueError):
            continue
        if candidate > cursor_ts + 1e-7:
            candidates.append(candidate)
    if candidates:
        next_boundary = min(candidates)
    else:
        next_boundary = cursor_ts + INTERVAL_MINUTES * 60

    # Split at an offset transition so the repeated hour is integrated twice
    # and a skipped hour is represented by one absolute segment.
    current_offset = cursor.utcoffset()
    if current_offset is not None and next_boundary - cursor_ts > 1.0:
        probe = cursor_ts + INTERVAL_MINUTES * 60
        while probe < next_boundary - 1e-7 and probe <= cursor_ts + 2 * 3600:
            if _from_timestamp(probe, tz).utcoffset() != current_offset:
                low, high = cursor_ts, probe
                for _ in range(40):
                    middle = (low + high) / 2.0
                    if _from_timestamp(middle, tz).utcoffset() == current_offset:
                        low = middle
                    else:
                        high = middle
                next_boundary = min(next_boundary, high)
                break
            probe += INTERVAL_MINUTES * 60
    return max(cursor_ts + 1e-7, next_boundary)


def build_boundaries(
    start: datetime,
    end: datetime,
    *,
    interval_minutes: int = INTERVAL_MINUTES,
) -> list[tuple[datetime, datetime]]:
    """Build dated, local-quarter boundaries with a possibly partial first bin."""
    if interval_minutes != INTERVAL_MINUTES:
        raise ValueError("solar timelines currently use 15-minute intervals")
    start_ts = _timestamp(start)
    end_ts = _timestamp(end)
    if not math.isfinite(start_ts) or not math.isfinite(end_ts) or end_ts <= start_ts:
        return []
    return_naive = start.tzinfo is None and end.tzinfo is None
    tz = start.tzinfo or end.tzinfo or timezone.utc
    result: list[tuple[datetime, datetime]] = []
    cursor = start_ts
    while cursor < end_ts - 1e-7:
        boundary = _next_local_quarter_timestamp(cursor, tz)
        segment_end = min(end_ts, max(cursor + 1e-7, boundary))
        segment_start = _from_timestamp(cursor, tz)
        segment_finish = _from_timestamp(segment_end, tz)
        if return_naive:
            # Runtime price slots intentionally use local wall-clock datetimes.
            # Preserve that contract when both horizon boundaries are naive so
            # the derived energy deadlines remain comparable with those slots.
            segment_start = segment_start.replace(tzinfo=None)
            segment_finish = segment_finish.replace(tzinfo=None)
        result.append((segment_start, segment_finish))
        cursor = segment_end
    return result


def _overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def _safe_non_negative(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0.0 else None


def _coerce_period(value: Any) -> SolarForecastPeriod | None:
    """Accept an already adapted period or an explicit mapping fixture."""
    if isinstance(value, SolarForecastPeriod):
        return value
    if not isinstance(value, dict):
        return None
    start = value.get("start", value.get("start_time", value.get("period_start")))
    end = value.get("end", value.get("end_time", value.get("period_end")))
    if isinstance(start, str):
        try:
            start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(end, str):
        try:
            end = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    if "energy_kwh" in value:
        energy = value["energy_kwh"]
    elif "energy_wh" in value:
        try:
            energy = float(value["energy_wh"]) / 1000.0
        except (TypeError, ValueError):
            return None
    elif "energy" in value and str(value.get("unit", "")).lower() == "wh":
        try:
            energy = float(value["energy"]) / 1000.0
        except (TypeError, ValueError):
            return None
    elif "energy" in value and str(value.get("unit", "")).lower() == "kwh":
        energy = value["energy"]
    else:
        return None
    try:
        return SolarForecastPeriod(start, end, energy)
    except (TypeError, ValueError):
        return None


def normalize_timeline_weights(
    weights: Sequence[float],
    total_kwh: float,
) -> list[float] | None:
    """Scale non-negative weights to an exact total, or reject an empty shape."""
    total = _safe_non_negative(total_kwh)
    if total is None:
        return None
    safe: list[float] = []
    for value in weights:
        parsed = _safe_non_negative(value)
        if parsed is None:
            return None
        safe.append(parsed)
    if not safe:
        return []
    weight_sum = math.fsum(safe)
    if weight_sum <= _EPSILON:
        return None
    scaled = [value * total / weight_sum for value in safe]
    residual = total - math.fsum(scaled)
    target = max(range(len(scaled)), key=scaled.__getitem__)
    corrected = scaled[target] + residual
    if corrected >= -1e-12:
        scaled[target] = max(0.0, corrected)
    else:
        # A negative residual this large indicates an invalid numeric input;
        # preserving a non-negative curve is safer than leaking it downstream.
        return None
    return scaled


def _validate_periods(
    periods: Sequence[SolarForecastPeriod],
    start: datetime,
    end: datetime,
    solar_start: datetime | None,
    solar_end: datetime | None,
) -> tuple[bool, str | None]:
    """Validate dated periods and their future daylight coverage."""
    if not periods:
        return False, "provider_periods_missing"
    ordered = sorted(periods, key=lambda period: _timestamp(period.start))
    previous_end = None
    for period in ordered:
        if period.start.tzinfo is None or period.end.tzinfo is None:
            return False, "provider_timestamp_naive"
        p_start, p_end = _timestamp(period.start), _timestamp(period.end)
        energy = _safe_non_negative(period.energy_kwh)
        if energy is None or p_end <= p_start:
            return False, "provider_period_invalid"
        if previous_end is not None and p_start < previous_end - 1e-7:
            return False, "provider_period_overlap"
        previous_end = p_end

    light_start = max(_timestamp(start), _timestamp(solar_start)) if solar_start else _timestamp(start)
    light_end = min(_timestamp(end), _timestamp(solar_end)) if solar_end else _timestamp(end)
    if light_end <= light_start:
        return False, "solar_window_invalid"
    covered = 0.0
    cursor = light_start
    for period in ordered:
        p_start, p_end = _timestamp(period.start), _timestamp(period.end)
        if p_end <= cursor:
            continue
        if p_start > cursor:
            # A gap larger than one hour inside the daylight horizon is unsafe.
            if p_start - cursor > MAX_PROVIDER_GAP_SECONDS:
                return False, "provider_gap"
        covered += _overlap(p_start, p_end, light_start, light_end)
        cursor = max(cursor, p_end)
    if light_end - cursor > MAX_PROVIDER_GAP_SECONDS:
        return False, "provider_gap"
    covered = min(covered, light_end - light_start)
    if covered / (light_end - light_start) < 0.80 - 1e-9:
        return False, "provider_coverage"
    return True, None


def provider_weights(
    boundaries: Sequence[tuple[datetime, datetime]],
    periods: Sequence[SolarForecastPeriod] | None,
    *,
    solar_start: datetime | None = None,
    solar_end: datetime | None = None,
) -> tuple[list[float] | None, str | None]:
    """Resample explicit provider periods to arbitrary dated boundaries."""
    if not boundaries:
        return [], "empty_horizon"
    start, end = boundaries[0][0], boundaries[-1][1]
    coerced = [_coerce_period(period) for period in (periods or ())]
    if any(period is None for period in coerced):
        return None, "provider_period_invalid"
    periods = tuple(period for period in coerced if period is not None)
    valid, reason = _validate_periods(periods, start, end, solar_start, solar_end)
    if not valid:
        return None, reason
    weights: list[float] = []
    for boundary_start, boundary_end in boundaries:
        b_start, b_end = _timestamp(boundary_start), _timestamp(boundary_end)
        energy = 0.0
        for period in periods:
            p_start, p_end = _timestamp(period.start), _timestamp(period.end)
            overlap = _overlap(b_start, b_end, p_start, p_end)
            if overlap:
                energy += period.energy_kwh * overlap / (p_end - p_start)
        weights.append(energy)
    if not any(value > _EPSILON for value in weights):
        return None, "provider_zero_energy"
    return weights, None


def legacy_shape_weights(
    boundaries: Sequence[tuple[datetime, datetime]],
    shape: Sequence[float] | None,
) -> tuple[list[float] | None, str | None]:
    """Validate the pre-period positional shape during the transition.

    Positional shapes are accepted only when they already match this exact
    dated horizon.  They are never resized, which avoids silently shifting an
    old curve across a DST boundary or a partial first interval.
    """
    if shape is None:
        return None, "legacy_shape_missing"
    if len(shape) != len(boundaries):
        return None, "legacy_shape_length"
    values: list[float] = []
    for value in shape:
        parsed = _safe_non_negative(value)
        if parsed is None:
            return None, "legacy_shape_invalid"
        values.append(parsed)
    if not any(value > _EPSILON for value in values):
        return None, "legacy_shape_zero"
    return values, None


def progress_shape_weights(
    boundaries: Sequence[tuple[datetime, datetime]],
    shape: Sequence[float] | None,
    solar_start: datetime | None,
    solar_end: datetime | None,
) -> tuple[list[float] | None, str | None]:
    """Map 96 normalized solar-progress bins to dated future intervals."""
    if not boundaries:
        return [], "empty_horizon"
    if solar_start is None or solar_end is None:
        return None, "solar_window_missing"
    start_ts, end_ts = _timestamp(solar_start), _timestamp(solar_end)
    if end_ts <= start_ts:
        return None, "solar_window_invalid"
    if shape is None or len(shape) != PROGRESS_BIN_COUNT:
        return None, "learned_shape_length"
    safe_shape = []
    for value in shape:
        parsed = _safe_non_negative(value)
        if parsed is None:
            return None, "learned_shape_invalid"
        safe_shape.append(parsed)
    weights: list[float] = []
    duration = end_ts - start_ts
    for boundary_start, boundary_end in boundaries:
        b_start, b_end = _timestamp(boundary_start), _timestamp(boundary_end)
        energy = 0.0
        for index, value in enumerate(safe_shape):
            bin_start = start_ts + duration * index / PROGRESS_BIN_COUNT
            bin_end = start_ts + duration * (index + 1) / PROGRESS_BIN_COUNT
            overlap = _overlap(b_start, b_end, bin_start, bin_end)
            if overlap:
                energy += value * overlap / (bin_end - bin_start)
        weights.append(energy)
    if not any(value > _EPSILON for value in weights):
        return None, "learned_shape_no_future_energy"
    return weights, None


def sinusoidal_weights(
    boundaries: Sequence[tuple[datetime, datetime]],
    solar_start: datetime | None,
    solar_end: datetime | None,
) -> tuple[list[float] | None, str | None]:
    """Return exact differences of the existing sinusoidal CDF."""
    if not boundaries:
        return [], "empty_horizon"
    if solar_start is None or solar_end is None:
        return None, "solar_window_missing"
    start_ts, end_ts = _timestamp(solar_start), _timestamp(solar_end)
    if end_ts <= start_ts:
        return None, "solar_window_invalid"

    def cumulative(timestamp: float) -> float:
        if timestamp <= start_ts:
            return 0.0
        if timestamp >= end_ts:
            return 1.0
        progress = (timestamp - start_ts) / (end_ts - start_ts)
        return (1.0 - math.cos(math.pi * progress)) / 2.0

    return [
        max(0.0, cumulative(_timestamp(end)) - cumulative(_timestamp(start)))
        for start, end in boundaries
    ], None


@dataclass(frozen=True)
class SolarTimelineResult:
    """Selected future curve and bounded provenance for one planner evaluation."""

    intervals_kwh: tuple[float, ...]
    source: str
    remaining_raw_kwh: float
    safety_margin_kwh: float
    remaining_effective_kwh: float
    timeline_effective_kwh: float
    fallback_reason: str | None = None
    candidate_reasons: tuple[str, ...] = ()
    shadow_selected_source: str | None = None
    shadow_intervals_kwh: tuple[float, ...] = ()

    @property
    def solar_kwh(self) -> tuple[float, ...]:
        """Compatibility alias used by timeline consumers."""
        return self.intervals_kwh

    @property
    def energy_error_kwh(self) -> float:
        return math.fsum(self.intervals_kwh) - self.timeline_effective_kwh


def _safe_budget(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0.0 else None


def build_solar_timeline(
    boundaries: Sequence[tuple[datetime, datetime]],
    remaining_raw_kwh: Any,
    *,
    safety_margin_kwh: Any = 0.0,
    provider_periods: Sequence[SolarForecastPeriod] | None = None,
    temporal_shape: Sequence[float] | None = None,
    learned_shape: Sequence[float] | None = None,
    learned_mature: bool = False,
    solar_start: datetime | None = None,
    solar_end: datetime | None = None,
    mode: str = "active",
) -> SolarTimelineResult:
    """Select provider > learned > sinusoidal and normalize exactly once.

    ``active`` is the normal automatic mode: provider periods, a mature local
    profile and finally the historical sinusoid are selected in that order.
    ``shadow`` remains a compatibility mode for callers from the rollout
    release and continues to report the best candidate without applying it.
    In ``off`` mode no new source is selected and the historical sinusoid is
    used directly.
    """
    raw = _safe_budget(remaining_raw_kwh)
    margin = _safe_budget(safety_margin_kwh)
    if raw is None:
        raw = 0.0
        invalid_budget = True
    else:
        invalid_budget = False
    margin = margin or 0.0
    effective = max(0.0, raw - margin)
    # Keep every rejected-candidate diagnostic, but only expose a fallback
    # reason when the selected timeline actually moved to a lower-priority
    # source. A healthy provider must not be labelled as fallback merely
    # because an optional learned candidate was rejected.
    reasons: list[str] = []
    provider_reasons: list[str] = []
    learned_reasons: list[str] = []
    candidates: list[tuple[str, list[float] | None, int]] = []

    if mode not in ("off", "shadow", "active"):
        reasons.append("invalid_mode")
        mode = "active"

    if mode != "off":
        provider, reason = provider_weights(
            boundaries,
            provider_periods,
            solar_start=solar_start,
            solar_end=solar_end,
        )
        if provider is not None:
            candidates.append(("provider", provider, 0))
        elif provider_periods:
            reason = reason or "provider_invalid"
            provider_reasons.append(reason)
            reasons.append(reason)

        legacy, reason = legacy_shape_weights(boundaries, temporal_shape)
        if legacy is not None:
            candidates.append(("provider", legacy, 0))
        elif temporal_shape is not None:
            reason = reason or "legacy_shape_invalid"
            provider_reasons.append(reason)
            reasons.append(reason)

        if learned_mature:
            learned, reason = progress_shape_weights(
                boundaries, learned_shape, solar_start, solar_end
            )
            if learned is not None:
                candidates.append(("learned_profile", learned, 1))
            else:
                reason = reason or "learned_invalid"
                learned_reasons.append(reason)
                reasons.append(reason)
        elif learned_shape is not None:
            reason = "profile_not_mature"
            learned_reasons.append(reason)
            reasons.append(reason)

    sinusoid, reason = sinusoidal_weights(boundaries, solar_start, solar_end)
    if sinusoid is not None:
        candidates.append(("sinusoidal", sinusoid, 2))
    else:
        reasons.append(reason or "sinusoidal_invalid")

    if invalid_budget:
        reasons.append("forecast_invalid")

    selected_source = "zero_fallback"
    selected_weights: list[float] | None = None
    selected_priority: int | None = None
    for source, weights, priority in candidates:
        if weights is not None and any(value > _EPSILON for value in weights):
            selected_source = source
            selected_weights = weights
            selected_priority = priority
            break

    selected_fallback_reasons: list[str] = []
    if selected_priority is None:
        # No candidate survived: every rejection contributed to the actual
        # zero fallback and remains useful to diagnose the missing timeline.
        selected_fallback_reasons.extend(reasons)
    else:
        if selected_priority >= 1:
            selected_fallback_reasons.extend(provider_reasons)
        if selected_priority >= 2:
            selected_fallback_reasons.extend(learned_reasons)

    if invalid_budget:
        zero = tuple(0.0 for _ in boundaries)
        return SolarTimelineResult(
            intervals_kwh=zero,
            source="zero_fallback",
            remaining_raw_kwh=raw,
            safety_margin_kwh=margin,
            remaining_effective_kwh=0.0,
            timeline_effective_kwh=0.0,
            fallback_reason=";".join(dict.fromkeys(reasons)),
            candidate_reasons=tuple(dict.fromkeys(reasons)),
            shadow_selected_source=None,
            shadow_intervals_kwh=(),
        )

    if selected_weights is None or effective <= _EPSILON:
        zero = tuple(0.0 for _ in boundaries)
        fallback_reason = (
            "zero_budget"
            if effective <= _EPSILON
            else ";".join(dict.fromkeys(selected_fallback_reasons))
            or "unsafe_temporal_shape"
        )
        return SolarTimelineResult(
            intervals_kwh=zero,
            source="zero_fallback" if selected_weights is None else selected_source,
            remaining_raw_kwh=raw,
            safety_margin_kwh=margin,
            remaining_effective_kwh=effective,
            timeline_effective_kwh=0.0,
            fallback_reason=fallback_reason,
            candidate_reasons=tuple(dict.fromkeys(reasons)),
            shadow_selected_source=selected_source if mode == "shadow" else None,
            shadow_intervals_kwh=(),
        )

    selected = normalize_timeline_weights(selected_weights, effective)
    if selected is None:
        normalization_reason = f"{selected_source}_normalization"
        reasons.append(normalization_reason)
        selected_fallback_reasons.append(normalization_reason)
        zero = tuple(0.0 for _ in boundaries)
        return SolarTimelineResult(
            zero,
            "zero_fallback",
            raw,
            margin,
            effective,
            0.0,
            ";".join(dict.fromkeys(selected_fallback_reasons)),
            tuple(dict.fromkeys(reasons)),
        )

    shadow_values: tuple[float, ...] = ()
    shadow_source: str | None = None
    if mode == "shadow":
        shadow_source = selected_source
        shadow_values = tuple(selected)
        sinusoid_candidate = next(
            (weights for source, weights, _ in candidates if source == "sinusoidal"),
            None,
        )
        active = normalize_timeline_weights(sinusoid_candidate or [], effective)
        if active is not None:
            selected = active
        else:
            shadow_source = None

    return SolarTimelineResult(
        intervals_kwh=tuple(selected),
        source="sinusoidal" if mode == "shadow" and shadow_source is not None else selected_source,
        remaining_raw_kwh=raw,
        safety_margin_kwh=margin,
        remaining_effective_kwh=effective,
        timeline_effective_kwh=effective,
        fallback_reason=";".join(dict.fromkeys(selected_fallback_reasons)) or None,
        candidate_reasons=tuple(dict.fromkeys(reasons)),
        shadow_selected_source=shadow_source,
        shadow_intervals_kwh=shadow_values,
    )


# Names used by callers/tests that prefer the shorter selector terminology.
select_solar_timeline = build_solar_timeline
resample_provider_periods = provider_weights
map_progress_shape = progress_shape_weights


__all__ = [
    "INTERVAL_MINUTES",
    "PROGRESS_BIN_COUNT",
    "SolarForecastPeriod",
    "SolarTimelineResult",
    "build_boundaries",
    "build_solar_timeline",
    "map_progress_shape",
    "normalize_timeline_weights",
    "legacy_shape_weights",
    "progress_shape_weights",
    "provider_weights",
    "resample_provider_periods",
    "select_solar_timeline",
    "sinusoidal_weights",
]
