"""Direct-PV temporal profile capture and learning.

The solar profile stores only evidence from direct PV power telemetry.  It
learns a normalized distribution over solar-window progress; the weather
forecast remains the sole source of the future kWh budget.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import IntFlag
from time import monotonic
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    DEFAULT_SOLAR_PROFILE_MODE,
    DOMAIN,
    normalize_solar_profile_mode,
)
from ..pricing.solar_timeline import PROGRESS_BIN_COUNT
from ..drivers.base import has_connected_mppt_pv
from .consumption_profile import (
    INTERVAL_COUNT,
    INTERVAL_SECONDS,
    MAX_SAMPLE_GAP_SECONDS,
    _as_timestamp,
    _interval_index,
    _local_segments,
    _state_timestamp,
    _state_to_power_kw,
    split_sample_across_bins,
)
from .backfill import BackfillToken, RecorderBackfillCoordinator, local_day_bounds

_LOGGER = logging.getLogger(__name__)

SOLAR_PROFILE_INTERVAL_MINUTES = 15
SOLAR_PROFILE_INTERVAL_COUNT = 96
SOLAR_PROFILE_RETENTION_DAYS = 42
SOLAR_PROFILE_STORE_VERSION = 1
SOLAR_PROFILE_CAPTURE_VERSION = 1
SOLAR_PROFILE_STORE_KEY = "solar_profile"
SOLAR_POWER_NOISE_KW = 0.03
SOLAR_MIN_DAY_ENERGY_KWH = 0.5
SOLAR_MIN_COVERAGE_RATIO = 0.90
SOLAR_MIN_RECENT_DAYS = 7
SOLAR_MIN_HIGH_QUALITY_DAYS = 5
SOLAR_MIN_BIN_CONTRIBUTIONS = 3
SOLAR_CLIPPING_STRUCTURAL_DAYS = 3
SOLAR_MAX_SAMPLE_GAP_SECONDS = MAX_SAMPLE_GAP_SECONDS
SOLAR_AGE_HALF_LIFE_DAYS = 14.0
SOLAR_MIN_SUSTAINED_SECONDS = 60.0
SOLAR_MAX_CDF_DISPERSION = 0.35
SOLAR_CAPACITY_SHIFT_THRESHOLD = 0.25
SOLAR_CAPACITY_RECENT_CLEAR_DAYS = 3
SOLAR_CAPACITY_BASELINE_CLEAR_DAYS = 14
# Public compatibility aliases mirror the consumption-profile vocabulary.
INTERVAL_MINUTES = SOLAR_PROFILE_INTERVAL_MINUTES
INTERVAL_COUNT = SOLAR_PROFILE_INTERVAL_COUNT
PROFILE_RETENTION_DAYS = SOLAR_PROFILE_RETENTION_DAYS


class SolarQualityFlag(IntFlag):
    """Compact per-bin quality flags persisted with each raw day."""

    MISSING = 1
    CLIPPING = 2
    CURTAILMENT_SUSPECTED = 4
    BATTERY_FULL_RISK = 8
    SENSOR_JUMP = 16
    CONFIGURATION_TRANSITION = 32
    CLOUDY = 64


SolarQualityFlags = SolarQualityFlag


@dataclass
class SolarProfileDay:
    """Raw direct-PV evidence for one local date."""

    local_date: date
    energy_kwh: list[float] = field(
        default_factory=lambda: [0.0] * SOLAR_PROFILE_INTERVAL_COUNT
    )
    coverage_s: list[float] = field(
        default_factory=lambda: [0.0] * SOLAR_PROFILE_INTERVAL_COUNT
    )
    quality_flags: list[int] = field(
        default_factory=lambda: [0] * SOLAR_PROFILE_INTERVAL_COUNT
    )
    solar_start: datetime | None = None
    solar_end: datetime | None = None
    forecast_reference_kwh: float | None = None
    complete: bool = False
    generation: int = 1

    def __post_init__(self) -> None:
        self.energy_kwh = _fit_float_list(self.energy_kwh, 0.0)
        self.coverage_s = _fit_float_list(self.coverage_s, 0.0)
        self.quality_flags = _fit_int_list(self.quality_flags, 0)
        self.generation = _safe_generation(self.generation)
        if self.forecast_reference_kwh is not None:
            parsed = _finite_non_negative(self.forecast_reference_kwh)
            self.forecast_reference_kwh = parsed

    @property
    def total_energy_kwh(self) -> float:
        return math.fsum(self.energy_kwh)

    @property
    def daylight_seconds(self) -> float:
        if self.solar_start is None or self.solar_end is None:
            return 0.0
        return max(0.0, _as_timestamp(self.solar_end) - _as_timestamp(self.solar_start))

    @property
    def daylight_hours(self) -> float:
        return self.daylight_seconds / 3600.0

    def interval_power_kw(self, index: int) -> float | None:
        if not 0 <= index < SOLAR_PROFILE_INTERVAL_COUNT:
            return None
        coverage = self.coverage_s[index]
        energy = self.energy_kwh[index]
        if coverage <= 0.0 or not math.isfinite(coverage) or not math.isfinite(energy):
            return None
        return max(0.0, energy / coverage * 3600.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.local_date.isoformat(),
            "energy_kwh": [round(max(0.0, value), 9) for value in self.energy_kwh],
            "coverage_s": [round(max(0.0, value), 3) for value in self.coverage_s],
            "quality_flags": [int(value) for value in self.quality_flags],
            "solar_start": self.solar_start.isoformat() if self.solar_start else None,
            "solar_end": self.solar_end.isoformat() if self.solar_end else None,
            "forecast_reference_kwh": self.forecast_reference_kwh,
            "complete": bool(self.complete),
            "generation": int(self.generation),
        }


@dataclass(frozen=True)
class SolarProfileSnapshot:
    """A bounded learned shape plus its eligibility metadata."""

    shape: tuple[float, ...] = ()
    mature: bool = False
    coverage_ratio: float = 0.0
    future_coverage_ratio: float = 0.0
    available_days: int = 0
    eligible_days: int = 0
    high_quality_days: int = 0
    bin_contributions: tuple[int, ...] = ()
    newest_valid_date: date | None = None
    profile_age_days: int | None = None
    fallback_reason: str | None = "profile_not_mature"
    generation: int = 1
    shape_dispersion: float = 0.0

    @property
    def intervals(self) -> tuple[float, ...]:
        return self.shape


def _finite_non_negative(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0.0 else None


def _safe_generation(value: Any, default: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(1, parsed)


def _fit_float_list(values: Any, default: float) -> list[float]:
    if not isinstance(values, (list, tuple)):
        values = []
    result: list[float] = []
    for index in range(SOLAR_PROFILE_INTERVAL_COUNT):
        parsed = _finite_non_negative(values[index]) if index < len(values) else None
        result.append(default if parsed is None else parsed)
    return result


def _fit_int_list(values: Any, default: int) -> list[int]:
    if not isinstance(values, (list, tuple)):
        values = []
    result: list[int] = []
    for index in range(SOLAR_PROFILE_INTERVAL_COUNT):
        try:
            parsed = int(values[index]) if index < len(values) else default
        except (TypeError, ValueError):
            parsed = default
        result.append(max(0, parsed))
    return result


def _weighted_median(values: Sequence[tuple[float, float]]) -> float | None:
    valid = [(value, weight) for value, weight in values if weight > 0 and math.isfinite(value)]
    if not valid:
        return None
    valid.sort(key=lambda item: item[0])
    total = math.fsum(weight for _, weight in valid)
    if total <= 0.0:
        return None
    cursor = 0.0
    for value, weight in valid:
        cursor += weight
        if cursor >= total / 2.0:
            return value
    return valid[-1][0]


def weighted_median(values: Sequence[tuple[float, float]]) -> float | None:
    """Public pure helper for robust shape aggregation."""
    return _weighted_median(values)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _profile_volatility(day: SolarProfileDay) -> float:
    """Estimate high-frequency cloudiness without penalising the daily shape."""
    energy, coverage, _flags = _progress_day(day)
    powers = [
        energy[index] / coverage[index] * 3600.0
        for index in range(PROGRESS_BIN_COUNT)
        if coverage[index] > 0.0 and energy[index] >= 0.0
    ]
    if len(powers) < 5:
        return 0.0
    residuals: list[float] = []
    for index in range(1, len(powers) - 1):
        local = _median(powers[index - 1:index + 2])
        if local <= SOLAR_POWER_NOISE_KW:
            continue
        residuals.append(abs(powers[index] - local) / local)
    return _median(residuals)


def _daylight_indices(day: SolarProfileDay) -> list[int]:
    """Return covered local bins that belong to the observed light window."""
    covered = [index for index, value in enumerate(day.coverage_s) if value > 0.0]
    if day.solar_start is None or day.solar_end is None:
        return covered
    start_seconds = (
        day.solar_start.hour * 3600
        + day.solar_start.minute * 60
        + day.solar_start.second
        + day.solar_start.microsecond / 1_000_000
    )
    end_seconds = (
        day.solar_end.hour * 3600
        + day.solar_end.minute * 60
        + day.solar_end.second
        + day.solar_end.microsecond / 1_000_000
    )
    if end_seconds <= start_seconds:
        return covered
    first = max(0, int(math.floor(start_seconds / INTERVAL_SECONDS)))
    last = min(SOLAR_PROFILE_INTERVAL_COUNT, int(math.ceil(end_seconds / INTERVAL_SECONDS)))
    return [index for index in covered if first <= index < last]


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def _progress_day(day: SolarProfileDay) -> tuple[list[float], list[float], list[int]]:
    """Remap local quarter-hour evidence to 96 solar-progress bins."""
    if day.solar_start is None or day.solar_end is None or day.daylight_seconds <= 0:
        return [0.0] * PROGRESS_BIN_COUNT, [0.0] * PROGRESS_BIN_COUNT, [0] * PROGRESS_BIN_COUNT
    start_ts = _as_timestamp(day.solar_start)
    end_ts = _as_timestamp(day.solar_end)
    duration = end_ts - start_ts
    energy = [0.0] * PROGRESS_BIN_COUNT
    coverage = [0.0] * PROGRESS_BIN_COUNT
    flags = [0] * PROGRESS_BIN_COUNT

    local_start = day.solar_start.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    for segment_start, segment_end, midpoint in _local_segments(local_start, local_end):
        index = _interval_index(midpoint.timetz().replace(tzinfo=None))
        bin_coverage = day.coverage_s[index]
        if bin_coverage <= 0.0:
            continue
        segment_seconds = segment_end - segment_start
        fraction = segment_seconds / bin_coverage
        segment_energy = day.energy_kwh[index] * fraction
        segment_start = max(segment_start, start_ts)
        segment_end = min(segment_end, end_ts)
        if segment_end <= segment_start:
            continue
        progress_start = (segment_start - start_ts) / duration
        progress_end = (segment_end - start_ts) / duration
        for progress_index in range(PROGRESS_BIN_COUNT):
            p_start = progress_index / PROGRESS_BIN_COUNT
            p_end = (progress_index + 1) / PROGRESS_BIN_COUNT
            overlap = max(0.0, min(progress_end, p_end) - max(progress_start, p_start))
            if overlap <= 0.0:
                continue
            progress_duration = max(1e-12, progress_end - progress_start)
            energy[progress_index] += segment_energy * overlap / progress_duration
            coverage[progress_index] += (segment_end - segment_start) * overlap / progress_duration
            flags[progress_index] |= day.quality_flags[index]
    return energy, coverage, flags


def remap_day_to_progress(day: SolarProfileDay) -> tuple[list[float], list[float], list[int]]:
    """Public alias used by tests and diagnostics tooling."""
    return _progress_day(day)


def _median_filter(values: list[float]) -> list[float]:
    if len(values) < 3:
        return values[:]
    result = values[:]
    for index in range(1, len(values) - 1):
        result[index] = _median(values[index - 1:index + 2])
    return result


def _interpolate_isolated(values: list[float], valid: list[bool]) -> list[float]:
    result = values[:]
    index = 0
    while index < len(values):
        if valid[index]:
            index += 1
            continue
        start = index
        while index < len(values) and not valid[index]:
            index += 1
        length = index - start
        if length == 1 and start > 0 and index < len(values) and valid[start - 1] and valid[index]:
            result[start] = (result[start - 1] + result[index]) / 2.0
    return result


def cdf_mae(predicted: Sequence[float], actual: Sequence[float]) -> float:
    """Compare normalized cumulative curves with a bounded mean absolute error."""
    if len(predicted) != len(actual) or not predicted:
        return math.inf
    predicted_total = math.fsum(max(0.0, float(value)) for value in predicted)
    actual_total = math.fsum(max(0.0, float(value)) for value in actual)
    if predicted_total <= 0.0 or actual_total <= 0.0:
        return math.inf
    predicted_cdf = 0.0
    actual_cdf = 0.0
    errors: list[float] = []
    for p, a in zip(predicted, actual):
        predicted_cdf += max(0.0, float(p)) / predicted_total
        actual_cdf += max(0.0, float(a)) / actual_total
        errors.append(abs(predicted_cdf - actual_cdf))
    return math.fsum(errors) / len(errors)


def _day_quality(day: SolarProfileDay, recent_totals: Sequence[float]) -> tuple[bool, float, str | None]:
    """Classify one closed day without inventing missing solar."""
    if not day.complete:
        return False, 0.0, "day_incomplete"
    if day.daylight_seconds <= 0.0:
        return False, 0.0, "solar_window_invalid"
    _energy, progress_coverage, _flags = _progress_day(day)
    daylight_coverage = math.fsum(progress_coverage)
    coverage_ratio = min(1.0, daylight_coverage / max(1.0, day.daylight_seconds))
    if coverage_ratio < SOLAR_MIN_COVERAGE_RATIO:
        return False, coverage_ratio, "insufficient_coverage"
    total = day.total_energy_kwh
    if total < SOLAR_MIN_DAY_ENERGY_KWH:
        return False, coverage_ratio, "insufficient_energy"
    daylight_indices = _daylight_indices(day)
    curtailment_bins = sum(
        bool(day.quality_flags[index] & int(SolarQualityFlag.CURTAILMENT_SUSPECTED))
        for index in daylight_indices
    )
    if daylight_indices and curtailment_bins / len(daylight_indices) > 0.10:
        return False, coverage_ratio, "curtailment_suspected"

    quality = coverage_ratio
    if recent_totals:
        recent_median = _median([value for value in recent_totals if value > 0.0])
        if recent_median > 0.0 and total < recent_median * 0.10:
            return False, coverage_ratio, "insufficient_relative_energy"
    if day.forecast_reference_kwh and day.forecast_reference_kwh > 0.0:
        ratio = total / day.forecast_reference_kwh
        if ratio < 0.35 or ratio > 1.75:
            return False, coverage_ratio, "forecast_ratio_outlier"
        if ratio < 0.75:
            quality *= (ratio - 0.35) / 0.40
            for index, covered in enumerate(day.coverage_s):
                if covered > 0.0:
                    day.quality_flags[index] |= int(SolarQualityFlag.CLOUDY)
        elif ratio > 1.25:
            quality *= (1.75 - ratio) / 0.50
    elif recent_totals:
        q3 = sorted(recent_totals)[max(0, int(math.ceil(len(recent_totals) * 0.75)) - 1)]
        if q3 > 0.0 and total < q3 * 0.10:
            return False, coverage_ratio, "low_without_forecast"
    volatility = _profile_volatility(day)
    if volatility > 0.25:
        quality *= max(0.20, 1.0 - min(0.80, volatility * 0.40))
        for index, covered in enumerate(day.coverage_s):
            if covered > 0.0:
                day.quality_flags[index] |= int(SolarQualityFlag.CLOUDY)
    return True, max(0.0, min(1.0, quality)), None


class SolarProfileTracker:
    """Own direct-PV capture, persistence and normalized shape learning."""

    def __init__(self, hass: Any, config_entry: Any, controller: Any) -> None:
        self._hass = hass
        self._config_entry = config_entry
        self._controller = controller
        entry_id = getattr(config_entry, "entry_id", "unknown")
        self._store: Store = Store(
            hass,
            SOLAR_PROFILE_STORE_VERSION,
            f"{DOMAIN}.{entry_id}.{SOLAR_PROFILE_STORE_KEY}",
        )
        self._days: dict[date, SolarProfileDay] = {}
        self._last_sample_time: datetime | None = None
        self._last_sample_monotonic: float | None = None
        self._last_power_kw: float | None = None
        self._last_local_date: date | None = None
        self._last_save_monotonic = 0.0
        self._save_task: asyncio.Task | None = None
        self._backfill_task: asyncio.Task | None = None
        self._last_error: str | None = None
        self._backfill_status = "not_started"
        self._backfill_coordinator: RecorderBackfillCoordinator | None = None
        self._backfill_runtime_generation = 1
        self._backfill_current_date: date | None = None
        self._backfill_blocks = 0
        self._backfill_last_duration_s = 0.0
        self._loaded = False
        self._active_fingerprint = self.configuration_fingerprint()
        self._generation = 1
        self._positive_candidate_start: datetime | None = None
        self._positive_run_seconds = 0.0
        self._runtime_battery_full_risk = False
        self._runtime_export_zero = False
        self._runtime_expected_high = False
        self._runtime_explicit_curtailment = False
        self._runtime_curtailment_active = False
        self._active_mode = normalize_solar_profile_mode(
            (getattr(config_entry, "data", {}) or {}).get(
                "solar_profile_mode", DEFAULT_SOLAR_PROFILE_MODE
            )
        )

    def set_backfill_coordinator(
        self, coordinator: RecorderBackfillCoordinator
    ) -> None:
        """Attach the entry-owned Recorder coordinator."""
        self._backfill_coordinator = coordinator

    def cancel_backfill(self) -> None:
        """Invalidate this profile's pending Recorder result application."""
        self._backfill_runtime_generation += 1
        if self._backfill_task is not None and not self._backfill_task.done():
            self._backfill_task.cancel()
        self._backfill_status = "cancelled"

    # ------------------------------------------------------------------
    # Time, source and persistence
    # ------------------------------------------------------------------

    def _timezone(self) -> Any:
        configured = getattr(getattr(self._hass, "config", None), "time_zone", None)
        if configured:
            try:
                return dt_util.get_time_zone(configured) or ZoneInfo(configured)
            except Exception:  # noqa: BLE001
                pass
        return timezone.utc

    def _now(self) -> datetime:
        try:
            current = dt_util.now()
        except Exception:  # noqa: BLE001
            current = datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=self._timezone())
        return current.astimezone(self._timezone())

    def _today(self) -> date:
        return self._now().date()

    @property
    def days(self) -> list[SolarProfileDay]:
        return [self._days[key] for key in sorted(self._days)]

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def mode(self) -> str:
        return self._active_mode

    def refresh_mode(self, mode: str | None = None) -> None:
        """Refresh the automatic profile mode without touching evidence."""
        candidate = mode or getattr(self._controller, "solar_profile_mode", None)
        self._active_mode = normalize_solar_profile_mode(candidate)

    def telemetry_source(self) -> str:
        external = bool(getattr(self._controller, "solar_production_sensor", None))
        mppt = False
        aggregate = False
        for coordinator in getattr(self._controller, "coordinators", ()) or ():
            capabilities = getattr(coordinator, "capabilities", None)
            mppt = mppt or has_connected_mppt_pv(coordinator)
            aggregate = aggregate or bool(
                getattr(capabilities, "has_solar_telemetry", False)
                or getattr(coordinator, "has_solar_telemetry", False)
            )
        if external and mppt:
            return "external_plus_mppt"
        if external and aggregate:
            return "external_plus_aggregate"
        if external:
            return "external"
        if mppt:
            return "mppt"
        if aggregate:
            return "aggregate"
        return "none"

    def configuration_fingerprint(self) -> str:
        data = getattr(self._config_entry, "data", {}) or {}
        mppt_sources = []
        aggregate_sources = []
        for coordinator in getattr(self._controller, "coordinators", ()) or ():
            capabilities = getattr(coordinator, "capabilities", None)
            has_mppt = has_connected_mppt_pv(coordinator)
            has_aggregate = bool(
                getattr(capabilities, "has_solar_telemetry", False)
                or getattr(coordinator, "has_solar_telemetry", False)
            )
            if not has_mppt and not has_aggregate:
                continue
            channels = tuple(
                key
                for key in ("mppt1_power", "mppt2_power", "mppt3_power", "mppt4_power")
                if not hasattr(coordinator, "data") or key in (getattr(coordinator, "data", {}) or {})
            )
            capacity = getattr(coordinator, "battery_capacity_kwh", None)
            if capacity is None:
                capacity = (getattr(coordinator, "data", {}) or {}).get("battery_total_energy")
            try:
                capacity = float(capacity)
            except (TypeError, ValueError):
                capacity = None
            if capacity is not None and (not math.isfinite(capacity) or capacity <= 0.0):
                capacity = None
            source = {
                "id": getattr(coordinator, "device_key", None) or getattr(coordinator, "name", None) or str(coordinator),
                "model": getattr(coordinator, "battery_version", None),
                "channels": channels,
                "capacity_kwh": capacity,
            }
            if has_mppt:
                mppt_sources.append(source)
            elif has_aggregate:
                aggregate_sources.append({
                    "id": source["id"],
                    "model": source["model"],
                    "key": "solar_power",
                    "capacity_kwh": source["capacity_kwh"],
                })
        payload = {
            "capture_version": SOLAR_PROFILE_CAPTURE_VERSION,
            "external_sensor": data.get("solar_production_sensor") or getattr(self._controller, "solar_production_sensor", None),
            "timezone": getattr(getattr(self._hass, "config", None), "time_zone", None),
            "mppt": sorted(mppt_sources, key=lambda item: json.dumps(item, sort_keys=True, default=str)),
        }
        # Keep the fingerprint byte-for-byte compatible for existing external/
        # MPPT-only profiles. The extra payload is only needed once a verified
        # independent aggregate PV source (Anker E5000 Pro) is configured.
        if aggregate_sources:
            payload["aggregate_pv"] = sorted(
                aggregate_sources,
                key=lambda item: json.dumps(item, sort_keys=True, default=str),
            )
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_day(raw: Any) -> SolarProfileDay | None:
        if not isinstance(raw, dict):
            return None
        try:
            local_date = date.fromisoformat(str(raw["date"]))
        except (KeyError, TypeError, ValueError):
            return None
        energy = raw.get("energy_kwh")
        coverage = raw.get("coverage_s")
        flags = raw.get("quality_flags", [0] * SOLAR_PROFILE_INTERVAL_COUNT)
        if not isinstance(energy, list) or not isinstance(coverage, list):
            return None
        if len(energy) != SOLAR_PROFILE_INTERVAL_COUNT or len(coverage) != SOLAR_PROFILE_INTERVAL_COUNT:
            return None
        if not isinstance(flags, list) or len(flags) != SOLAR_PROFILE_INTERVAL_COUNT:
            return None
        parsed_energy = [_finite_non_negative(value) for value in energy]
        parsed_coverage = [_finite_non_negative(value) for value in coverage]
        if any(value is None for value in parsed_energy + parsed_coverage):
            return None
        parsed_start = _parse_datetime(raw.get("solar_start"))
        parsed_end = _parse_datetime(raw.get("solar_end"))
        return SolarProfileDay(
            local_date=local_date,
            energy_kwh=[float(value) for value in parsed_energy],
            coverage_s=[float(value) for value in parsed_coverage],
            quality_flags=_fit_int_list(flags, 0),
            solar_start=parsed_start,
            solar_end=parsed_end,
            forecast_reference_kwh=raw.get("forecast_reference_kwh"),
            complete=bool(raw.get("complete", False)),
            generation=_safe_generation(raw.get("generation", 1) or 1),
        )

    def _retention_floor(self, reference_date: date | None = None) -> date:
        return (reference_date or self._today()) - timedelta(days=SOLAR_PROFILE_RETENTION_DAYS)

    def _prune(self, reference_date: date | None = None) -> None:
        reference_date = reference_date or self._today()
        floor = self._retention_floor(reference_date)
        self._days = {
            local_date: day
            for local_date, day in self._days.items()
            if floor <= local_date <= reference_date
        }

    async def async_load(self) -> bool:
        try:
            data = await self._store.async_load()
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"load: {exc}"
            self._loaded = True
            return False
        self._loaded = True
        if not isinstance(data, dict):
            if data is not None:
                self._last_error = "load: invalid_store"
            return False
        expected = self.configuration_fingerprint()
        stored = data.get("source_fingerprint", data.get("configuration_fingerprint"))
        stored_generation = _safe_generation(data.get("generation", 1) or 1)
        if stored and stored != expected:
            self._days = {}
            self._generation = stored_generation + 1
            self._active_fingerprint = expected
            self._last_error = "source_fingerprint_changed"
            await self.async_save()
            self._last_error = "source_fingerprint_changed"
            return False
        self._generation = stored_generation
        loaded: dict[date, SolarProfileDay] = {}
        for raw_day in data.get("days", []) if isinstance(data.get("days", []), list) else []:
            parsed = self._parse_day(raw_day)
            if parsed is None or parsed.generation != self._generation:
                continue
            loaded[parsed.local_date] = parsed
        self._days = loaded
        self._prune()
        self._active_fingerprint = expected
        self._last_error = None
        return bool(self._days)

    def invalidate_if_configuration_changed(self) -> bool:
        current = self.configuration_fingerprint()
        if current == self._active_fingerprint:
            return False
        self.cancel_backfill()
        self._days = {}
        self._generation += 1
        self._active_fingerprint = current
        self._last_error = "source_fingerprint_changed"
        self._reset_continuity()
        self.request_save()
        return True

    def _store_payload(self) -> dict[str, Any]:
        self._prune()
        return {
            "capture_version": SOLAR_PROFILE_CAPTURE_VERSION,
            "interval_minutes": SOLAR_PROFILE_INTERVAL_MINUTES,
            "retention_days": SOLAR_PROFILE_RETENTION_DAYS,
            "timezone": getattr(getattr(self._hass, "config", None), "time_zone", None),
            "source_fingerprint": self.configuration_fingerprint(),
            "generation": self._generation,
            "days": [self._days[key].as_dict() for key in sorted(self._days)],
        }

    async def async_save(self) -> None:
        try:
            await self._store.async_save(self._store_payload())
            self._last_error = None
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"save: {exc}"
            _LOGGER.error("Solar profile: failed to save Store: %s", exc)

    def request_save(self) -> None:
        if self._save_task is not None and not self._save_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._save_task = None
            return
        self._save_task = loop.create_task(self.async_save())

    async def async_save_all(self) -> None:
        self.cancel_backfill()
        if self._backfill_task is not None and not self._backfill_task.done():
            try:
                await self._backfill_task
            except asyncio.CancelledError:
                pass
        self._backfill_task = None
        if self._save_task is not None and not self._save_task.done():
            try:
                await self._save_task
            except asyncio.CancelledError:
                pass
        self._save_task = None
        await self.async_save()

    # ------------------------------------------------------------------
    # Direct-power capture
    # ------------------------------------------------------------------

    def _reset_continuity(self) -> None:
        self._last_sample_time = None
        self._last_sample_monotonic = None
        self._last_power_kw = None
        self._positive_candidate_start = None
        self._positive_run_seconds = 0.0

    def update_runtime_context(
        self,
        *,
        battery_full_risk: bool = False,
        export_zero: bool = False,
        expected_high: bool = False,
        explicit_curtailment: bool = False,
    ) -> None:
        """Supply conservative context for curtailment classification.

        Direct PV remains the measured source. These signals only decide when a
        falling/limited sample should be excluded; battery fullness alone is
        deliberately insufficient because a full battery may still export.
        """
        self._runtime_battery_full_risk = bool(battery_full_risk)
        self._runtime_export_zero = bool(export_zero)
        self._runtime_expected_high = bool(expected_high)
        self._runtime_explicit_curtailment = bool(explicit_curtailment)

    def _day(self, local_date: date) -> SolarProfileDay:
        return self._days.setdefault(
            local_date,
            SolarProfileDay(local_date, generation=self._generation),
        )

    def _mark_missing(self, timestamp: datetime) -> None:
        day = self._day(timestamp.date())
        day.quality_flags[_interval_index(timestamp.timetz().replace(tzinfo=None))] |= int(SolarQualityFlag.MISSING)

    def record_power_sample(
        self,
        power_kw: float | None,
        *,
        local_time: datetime | None = None,
        monotonic_time: float | None = None,
        forecast_reference_kwh: float | None = None,
    ) -> None:
        """Integrate one direct-PV sample into local 15-minute bins."""
        if local_time is None:
            local_time = self._now()
        elif local_time.tzinfo is None:
            local_time = local_time.replace(tzinfo=self._timezone())
        else:
            local_time = local_time.astimezone(self._timezone())
        current_date = local_time.date()
        day = self._day(current_date)
        if self._last_local_date != current_date:
            for local_date, previous in self._days.items():
                if local_date < current_date:
                    previous.complete = True
                    self._classify_day(previous)
            self._last_local_date = current_date
            self._prune(current_date)
            self.request_save()

        parsed_power = _finite_non_negative(power_kw)
        if parsed_power is None:
            self._reset_continuity()
            return
        if forecast_reference_kwh is not None and day.forecast_reference_kwh is None:
            day.forecast_reference_kwh = _finite_non_negative(forecast_reference_kwh)

        runtime_battery_full_risk = getattr(self, "_runtime_battery_full_risk", False)
        runtime_export_zero = getattr(self, "_runtime_export_zero", False)
        runtime_expected_high = getattr(self, "_runtime_expected_high", False)
        runtime_explicit_curtailment = getattr(self, "_runtime_explicit_curtailment", False)
        runtime_curtailment_active = getattr(self, "_runtime_curtailment_active", False)
        previous_power = self._last_power_kw
        previous_time = self._last_sample_time
        if previous_power is not None and previous_time is not None:
            context_elapsed = _as_timestamp(local_time) - _as_timestamp(previous_time)
        else:
            context_elapsed = 0.0
        current_index = _interval_index(local_time.timetz().replace(tzinfo=None))
        if 0.0 < context_elapsed <= 60.0 and previous_power is not None:
            if parsed_power >= 2.0 and previous_power <= max(0.5, parsed_power / 4.0):
                day.quality_flags[current_index] |= int(SolarQualityFlag.SENSOR_JUMP)

            power_drop = previous_power >= 0.5 and parsed_power <= previous_power * 0.75
            contextual_curtailment = (
                runtime_battery_full_risk
                and runtime_export_zero
                and runtime_expected_high
                and (power_drop or runtime_curtailment_active)
            )
            if contextual_curtailment or runtime_explicit_curtailment:
                day.quality_flags[current_index] |= int(SolarQualityFlag.CURTAILMENT_SUSPECTED)
                if runtime_battery_full_risk:
                    day.quality_flags[current_index] |= int(SolarQualityFlag.BATTERY_FULL_RISK)
                runtime_curtailment_active = contextual_curtailment or runtime_explicit_curtailment
            elif not runtime_explicit_curtailment:
                runtime_curtailment_active = False
            self._runtime_curtailment_active = runtime_curtailment_active

        if self._last_sample_time is not None and self._last_power_kw is not None:
            if monotonic_time is not None and self._last_sample_monotonic is not None:
                elapsed = monotonic_time - self._last_sample_monotonic
            else:
                elapsed = _as_timestamp(local_time) - _as_timestamp(self._last_sample_time)
            if 0.0 < elapsed <= SOLAR_MAX_SAMPLE_GAP_SECONDS:
                for contribution in split_sample_across_bins(
                    self._last_sample_time,
                    local_time,
                    self._last_power_kw,
                    parsed_power,
                ):
                    target = self._day(contribution.local_date)
                    index = contribution.interval_index
                    target.energy_kwh[index] += contribution.energy_kwh
                    target.coverage_s[index] += contribution.coverage_s
            elif elapsed > SOLAR_MAX_SAMPLE_GAP_SECONDS:
                self._mark_missing(local_time)
                self._reset_continuity()

        # Direct PV above the noise floor is the only source used to align the
        # solar window.  The last positive sample is retained as the observed
        # end; a later zero does not invent a future production tail.
        if parsed_power >= SOLAR_POWER_NOISE_KW:
            if self._positive_candidate_start is None:
                self._positive_candidate_start = local_time
                self._positive_run_seconds = 0.0
            elif self._last_sample_time is not None:
                positive_elapsed = _as_timestamp(local_time) - _as_timestamp(self._last_sample_time)
                if 0.0 < positive_elapsed <= SOLAR_MAX_SAMPLE_GAP_SECONDS:
                    self._positive_run_seconds += positive_elapsed
            if self._positive_run_seconds >= SOLAR_MIN_SUSTAINED_SECONDS:
                if day.solar_start is None:
                    day.solar_start = self._positive_candidate_start
                day.solar_end = local_time
        else:
            self._positive_candidate_start = None
            self._positive_run_seconds = 0.0

        self._last_sample_time = local_time
        self._last_sample_monotonic = monotonic_time
        self._last_power_kw = parsed_power
        self._prune(current_date)
        if self._last_save_monotonic == 0.0 or monotonic() - self._last_save_monotonic >= 300:
            self._last_save_monotonic = monotonic()
            self.request_save()

    def add_day(self, day: SolarProfileDay) -> None:
        parsed = self._parse_day(day.as_dict())
        if parsed is None:
            return
        existing = self._days.get(parsed.local_date)
        if existing is None or sum(parsed.coverage_s) > sum(existing.coverage_s):
            self._days[parsed.local_date] = parsed
        else:
            for index in range(SOLAR_PROFILE_INTERVAL_COUNT):
                if parsed.coverage_s[index] > existing.coverage_s[index]:
                    existing.energy_kwh[index] = parsed.energy_kwh[index]
                    existing.coverage_s[index] = parsed.coverage_s[index]
                    existing.quality_flags[index] = parsed.quality_flags[index]
            existing.complete = existing.complete or parsed.complete

    def close_day(self, local_date: date | None = None) -> bool:
        local_date = local_date or self._today()
        day = self._days.get(local_date)
        if day is None:
            return False
        day.complete = True
        self._classify_day(day)
        self.request_save()
        return True

    def _classify_day(self, day: SolarProfileDay) -> None:
        powers = [
            power
            for index in range(SOLAR_PROFILE_INTERVAL_COUNT)
            if (power := day.interval_power_kw(index)) is not None
        ]
        if len(powers) < 3:
            return
        robust_top = sorted(powers)[int(0.90 * (len(powers) - 1))]
        if robust_top <= 0.0:
            return
        plateau_indices: list[int] = []
        for index in range(SOLAR_PROFILE_INTERVAL_COUNT):
            current = day.interval_power_kw(index)
            if current is not None and current >= robust_top * 0.98:
                plateau_indices.append(index)
                continue
            if len(plateau_indices) >= 3:
                for plateau_index in plateau_indices:
                    if 8 <= plateau_index <= 88:
                        day.quality_flags[plateau_index] |= int(SolarQualityFlag.CLIPPING)
            plateau_indices = []
        if len(plateau_indices) >= 3:
            for plateau_index in plateau_indices:
                if 8 <= plateau_index <= 88:
                    day.quality_flags[plateau_index] |= int(SolarQualityFlag.CLIPPING)
        self._detect_capacity_regime(day.local_date)

    @staticmethod
    def _recent_totals(days: Sequence[SolarProfileDay]) -> list[float]:
        """Reference totals for the relative-energy gates: newest days only.

        Weighing a day against every retained day makes the reference drift
        with the season and, worse, lets days recorded under an incompatible
        scale veto every correct day that follows them: the relative gate
        rejects the new days, so ``_day_quality`` never marks them clear, so
        ``_detect_capacity_regime`` -- the machinery that exists to retire the
        old scale -- never sees the regime it is supposed to notice.  Bounding
        the reference to the most recent days breaks that deadlock and lets a
        changed scale age out on its own.
        """
        ordered = sorted(
            (day for day in days if day.daylight_seconds > 0.0),
            key=lambda day: day.local_date,
            reverse=True,
        )
        return [day.total_energy_kwh for day in ordered[:SOLAR_MIN_RECENT_DAYS]]

    @staticmethod
    def _robust_day_peak(day: SolarProfileDay) -> float | None:
        """Return a peak estimate resistant to isolated sensor spikes."""
        powers = [
            power
            for index in range(SOLAR_PROFILE_INTERVAL_COUNT)
            if (power := day.interval_power_kw(index)) is not None
        ]
        if len(powers) < 3:
            return None
        ordered = sorted(powers)
        return ordered[int(0.90 * (len(ordered) - 1))]

    def _detect_capacity_regime(self, reference_date: date | None = None) -> bool:
        """Start a generation after a sustained clear-day peak shift.

        Only the active generation participates in the comparison.  Once a
        shift is confirmed, the three confirming days move together into a new
        generation and are marked as a transition, so incompatible regimes
        cannot be mixed by the learner.
        """
        reference_date = reference_date or self._today()
        complete = [
            day
            for day in self._days.values()
            if day.complete
            and day.generation == self._generation
            and day.local_date <= reference_date
        ]
        minimum_days = SOLAR_CAPACITY_RECENT_CLEAR_DAYS + SOLAR_CAPACITY_BASELINE_CLEAR_DAYS
        if len(complete) < minimum_days:
            return False
        recent_totals = self._recent_totals(complete)
        clear: list[tuple[date, float]] = []
        for day in sorted(complete, key=lambda item: item.local_date):
            valid, quality, _reason = _day_quality(day, recent_totals)
            peak = self._robust_day_peak(day)
            if valid and quality >= 0.75 and peak is not None and peak > 0.0:
                clear.append((day.local_date, peak))
        if len(clear) < minimum_days:
            return False
        recent = clear[-SOLAR_CAPACITY_RECENT_CLEAR_DAYS:]
        baseline_end = len(clear) - SOLAR_CAPACITY_RECENT_CLEAR_DAYS
        baseline = clear[
            max(0, baseline_end - SOLAR_CAPACITY_BASELINE_CLEAR_DAYS):baseline_end
        ]
        if len(baseline) < SOLAR_CAPACITY_BASELINE_CLEAR_DAYS:
            return False
        baseline_peak = _median([peak for _day, peak in baseline])
        recent_peak = _median([peak for _day, peak in recent])
        if baseline_peak <= 0.0:
            return False
        shift = recent_peak / baseline_peak - 1.0
        if abs(shift) <= SOLAR_CAPACITY_SHIFT_THRESHOLD:
            return False

        new_generation = self._generation + 1
        # The confirming days are the newest ones, so the regime boundary sits
        # at the *oldest* end of the trailing run that already matches the new
        # peak.  Carrying that whole run over keeps a usable shape instead of
        # blanking the profile until seven fresh days accumulate, and only the
        # boundary day is marked as a transition -- it is the one that may
        # straddle the change, while the days after it describe the new regime.
        carried = list(recent)
        for entry in reversed(clear[:baseline_end]):
            if abs(entry[1] / recent_peak - 1.0) > SOLAR_CAPACITY_SHIFT_THRESHOLD:
                break
            carried.insert(0, entry)
        carried_dates = {local_date for local_date, _peak in carried}
        boundary_date = carried[0][0]
        for day in self._days.values():
            if day.local_date not in carried_dates or day.generation != self._generation:
                continue
            day.generation = new_generation
            if day.local_date != boundary_date:
                continue
            for index, coverage in enumerate(day.coverage_s):
                if coverage > 0.0:
                    day.quality_flags[index] |= int(SolarQualityFlag.CONFIGURATION_TRANSITION)
        self._generation = new_generation
        self._reset_continuity()
        self._last_error = "capacity_regime_changed"
        self.request_save()
        _LOGGER.info(
            "Solar profile: capacity regime changed %.1f%% (%.3f -> %.3f kW); generation=%d",
            shift * 100.0,
            baseline_peak,
            recent_peak,
            new_generation,
        )
        return True

    def detect_capacity_regime(self, reference_date: date | None = None) -> bool:
        """Public diagnostic hook for testing or an explicit recalculation."""
        return self._detect_capacity_regime(reference_date)

    def mark_curtailment_intervals(
        self,
        local_date: date,
        indices: Sequence[int],
        *,
        battery_full_risk: bool = False,
        explicit: bool = False,
    ) -> None:
        """Apply conservative runtime curtailment context to captured bins."""
        day = self._days.get(local_date)
        if day is None:
            return
        flag = int(SolarQualityFlag.CURTAILMENT_SUSPECTED)
        if battery_full_risk:
            flag |= int(SolarQualityFlag.BATTERY_FULL_RISK)
        if explicit:
            flag |= int(SolarQualityFlag.CURTAILMENT_SUSPECTED)
        for index in indices:
            if 0 <= index < SOLAR_PROFILE_INTERVAL_COUNT:
                day.quality_flags[index] |= flag

    def current_day_capture(self, local_date: date | None = None) -> dict[str, Any] | None:
        local_date = local_date or self._today()
        day = self._days.get(local_date)
        if day is None or sum(day.coverage_s) <= 0.0:
            return None
        return {
            "date": local_date.isoformat(),
            "complete": day.complete,
            "energy_kwh": round(day.total_energy_kwh, 6),
            "interval_energy_kwh": [round(value, 6) for value in day.energy_kwh],
            "interval_coverage_s": [round(value, 3) for value in day.coverage_s],
            "quality_flags": list(day.quality_flags),
            "solar_start": day.solar_start.isoformat() if day.solar_start else None,
            "solar_end": day.solar_end.isoformat() if day.solar_end else None,
            "coverage_ratio": round(
                min(1.0, sum(day.coverage_s) / (24 * 3600)),
                6,
            ),
        }

    # ------------------------------------------------------------------
    # Shape learning and timeline snapshot
    # ------------------------------------------------------------------

    def _eligible_days(
        self,
        target_date: date,
        target_daylight_hours: float | None = None,
    ) -> list[tuple[SolarProfileDay, float, float]]:
        complete = [day for day in self._days.values() if day.complete and day.generation == self._generation]
        recent_totals = self._recent_totals(complete)
        result: list[tuple[SolarProfileDay, float, float]] = []
        for day in complete:
            age_days = (target_date - day.local_date).days
            if day.local_date >= target_date or age_days > SOLAR_PROFILE_RETENTION_DAYS:
                continue
            valid, quality, _reason = _day_quality(day, recent_totals)
            if not valid:
                continue
            age = max(0, age_days)
            age_weight = math.exp(-math.log(2.0) * age / SOLAR_AGE_HALF_LIFE_DAYS)
            target_daylight = day.daylight_hours
            season_weight = (
                math.exp(-abs(target_daylight - target_daylight_hours) / 2.0)
                if target_daylight_hours is not None and target_daylight > 0.0
                else 1.0
            )
            result.append((day, age_weight * season_weight * quality, quality))
        return result

    def learn_shape(
        self,
        target_date: date | None = None,
        *,
        target_daylight_hours: float | None = None,
    ) -> SolarProfileSnapshot:
        target_date = target_date or self._today()
        candidates = self._eligible_days(target_date, target_daylight_hours)
        progress_days: list[tuple[list[float], list[float], list[int], float, SolarProfileDay]] = []
        for day, weight, _quality in candidates:
            energy, coverage, flags = _progress_day(day)
            if math.fsum(energy) <= 0.0:
                continue
            total = math.fsum(energy)
            normalized = [value / total for value in energy]
            progress_days.append((normalized, coverage, flags, weight, day))

        clipping_counts = [
            sum(
                bool(flags[index] & int(SolarQualityFlag.CLIPPING))
                for _values, _coverage, flags, _weight, _day in progress_days
            )
            for index in range(PROGRESS_BIN_COUNT)
        ]
        excluded_flags = int(
            SolarQualityFlag.MISSING
            | SolarQualityFlag.CURTAILMENT_SUSPECTED
            | SolarQualityFlag.SENSOR_JUMP
            | SolarQualityFlag.CONFIGURATION_TRANSITION
        )
        values: list[float] = []
        contributions: list[int] = []
        valid_bins: list[bool] = []
        for index in range(PROGRESS_BIN_COUNT):
            candidates_for_bin = [
                (day_values[index], weight)
                for day_values, _coverage, flags, weight, _day in progress_days
                if (
                    day_values[index] > 0.0
                    and not flags[index] & excluded_flags
                    and not (
                        flags[index] & int(SolarQualityFlag.CLIPPING)
                        and clipping_counts[index] < SOLAR_CLIPPING_STRUCTURAL_DAYS
                    )
                )
            ]
            value = _weighted_median(candidates_for_bin)
            values.append(0.0 if value is None else value)
            contributions.append(len(candidates_for_bin))
            valid_bins.append(value is not None)
        values = _interpolate_isolated(values, valid_bins)
        values = _median_filter(values)
        total = math.fsum(values)
        if total > 0.0:
            values = [value / total for value in values]

        dispersion = _median(
            [cdf_mae(day_values, values) for day_values, *_rest in progress_days]
        ) if values and total > 0.0 else math.inf

        newest = max((day.local_date for *_, day in progress_days), default=None)
        age = (target_date - newest).days if newest else None
        high_quality = sum(quality >= 0.75 for _, _, quality in candidates)
        coverage_ratio = sum(count > 0 for count in contributions) / PROGRESS_BIN_COUNT
        mature = (
            len(progress_days) >= SOLAR_MIN_RECENT_DAYS
            and high_quality >= SOLAR_MIN_HIGH_QUALITY_DAYS
            and coverage_ratio >= 0.80
            and all(count >= SOLAR_MIN_BIN_CONTRIBUTIONS for count in contributions if count > 0)
            and age is not None
            and age <= 7
            and total > 0.0
            and dispersion <= SOLAR_MAX_CDF_DISPERSION
        )
        if mature:
            reason = None
        elif len(progress_days) < SOLAR_MIN_RECENT_DAYS:
            reason = "insufficient_days"
        elif high_quality < SOLAR_MIN_HIGH_QUALITY_DAYS:
            reason = "insufficient_high_quality_days"
        elif coverage_ratio < 0.80:
            reason = "insufficient_coverage"
        elif any(0 < count < SOLAR_MIN_BIN_CONTRIBUTIONS for count in contributions):
            reason = "insufficient_bin_contributions"
        elif age is None or age > 7:
            reason = "stale_profile"
        elif dispersion > SOLAR_MAX_CDF_DISPERSION:
            reason = "inconsistent_profile"
        else:
            reason = "profile_not_mature"
        return SolarProfileSnapshot(
            shape=tuple(values),
            mature=mature,
            coverage_ratio=coverage_ratio,
            future_coverage_ratio=coverage_ratio,
            available_days=len(self._days),
            eligible_days=len(progress_days),
            high_quality_days=high_quality,
            bin_contributions=tuple(contributions),
            newest_valid_date=newest,
            profile_age_days=age,
            fallback_reason=reason,
            generation=self._generation,
            shape_dispersion=0.0 if not math.isfinite(dispersion) else dispersion,
        )

    def get_snapshot(
        self,
        *,
        target_date: date | None = None,
        target_daylight_hours: float | None = None,
        future_progress_start: float | None = None,
        future_progress_end: float | None = None,
    ) -> SolarProfileSnapshot:
        snapshot = self.learn_shape(
            target_date,
            target_daylight_hours=target_daylight_hours,
        )
        if future_progress_start is None and future_progress_end is None:
            return snapshot
        start = max(0.0, min(1.0, future_progress_start or 0.0))
        end = max(start, min(1.0, future_progress_end if future_progress_end is not None else 1.0))
        first = max(0, int(math.floor(start * PROGRESS_BIN_COUNT)))
        last = min(PROGRESS_BIN_COUNT, int(math.ceil(end * PROGRESS_BIN_COUNT)))
        requested = range(first, last)
        requested_counts = [snapshot.bin_contributions[index] for index in requested]
        if not requested_counts:
            # After the solar window there are no future progress bins whose
            # coverage could invalidate an otherwise mature profile. Treat the
            # empty request as fully covered and preserve the global verdict.
            return SolarProfileSnapshot(
                **{
                    **snapshot.__dict__,
                    "future_coverage_ratio": 1.0,
                }
            )
        future_coverage = (
            sum(count >= SOLAR_MIN_BIN_CONTRIBUTIONS for count in requested_counts)
            / max(1, len(requested_counts))
        )
        mature = snapshot.mature and future_coverage >= 0.80 and all(
            count >= SOLAR_MIN_BIN_CONTRIBUTIONS for count in requested_counts
        )
        reason = None if mature else snapshot.fallback_reason or "insufficient_future_coverage"
        return SolarProfileSnapshot(
            **{
                **snapshot.__dict__,
                "mature": mature,
                "future_coverage_ratio": future_coverage,
                "fallback_reason": reason,
            }
        )

    def get_learned_shape(self, target_date: date | None = None) -> SolarProfileSnapshot:
        """Return the current learned snapshot without exposing raw days."""
        return self.learn_shape(target_date)

    def snapshot(self, target_date: date | None = None) -> SolarProfileSnapshot:
        """Short alias for integrations that treat the profile as a model."""
        return self.learn_shape(target_date)

    capture_power_sample = record_power_sample

    # ------------------------------------------------------------------
    # Recorder backfill and diagnostics
    # ------------------------------------------------------------------

    def _day_needs_backfill(self, local_date: date) -> bool:
        """Return whether a day lacks enough raw coverage to be trusted."""
        day = self._days.get(local_date)
        if day is None or not day.complete or day.generation != self._generation:
            return True
        return sum(value > 0.0 for value in day.coverage_s) < math.ceil(
            SOLAR_PROFILE_INTERVAL_COUNT * 0.75
        )

    def _backfill_token_valid(
        self, token: BackfillToken | None, generation: int
    ) -> bool:
        return self._backfill_runtime_generation == generation and (
            token is None or token.is_valid()
        )

    def _build_backfill_day(
        self,
        local_date: date,
        states: list[Any],
        today: date,
    ) -> SolarProfileDay | None:
        """Convert one detached Recorder list into one temporary solar day."""
        day = SolarProfileDay(local_date, generation=self._generation)
        previous_time: datetime | None = None
        previous_power: float | None = None
        positive_start: datetime | None = None
        positive_run_s = 0.0
        local_tz = self._timezone()

        for state in states:
            timestamp = _state_timestamp(state)
            power = _state_to_power_kw(state)
            if timestamp is None or power is None:
                previous_time = None
                previous_power = None
                positive_start = None
                positive_run_s = 0.0
                continue
            timestamp = (
                timestamp.astimezone(local_tz)
                if timestamp.tzinfo
                else timestamp.replace(tzinfo=local_tz)
            )
            if previous_time is not None and previous_power is not None:
                gap = _as_timestamp(timestamp) - _as_timestamp(previous_time)
                if 0.0 < gap <= SOLAR_MAX_SAMPLE_GAP_SECONDS:
                    for contribution in split_sample_across_bins(
                        previous_time,
                        timestamp,
                        previous_power,
                        power,
                    ):
                        target = day if contribution.local_date == local_date else None
                        if target is None:
                            continue
                        index = contribution.interval_index
                        target.energy_kwh[index] += contribution.energy_kwh
                        target.coverage_s[index] += contribution.coverage_s

            if power >= SOLAR_POWER_NOISE_KW:
                if positive_start is None:
                    positive_start = timestamp
                    positive_run_s = 0.0
                elif previous_time is not None:
                    gap = _as_timestamp(timestamp) - _as_timestamp(previous_time)
                    if 0.0 < gap <= SOLAR_MAX_SAMPLE_GAP_SECONDS:
                        positive_run_s += gap
                if positive_run_s >= SOLAR_MIN_SUSTAINED_SECONDS:
                    if day.solar_start is None:
                        day.solar_start = positive_start
                    day.solar_end = timestamp
            else:
                positive_start = None
                positive_run_s = 0.0
            previous_time, previous_power = timestamp, power

        if sum(day.coverage_s) <= 0.0:
            return None
        day.complete = local_date < today
        if day.complete:
            self._classify_day(day)
        return day

    def _merge_backfill_day(self, day: SolarProfileDay) -> bool:
        """Merge a better day while retaining existing quality evidence."""
        before = self._days.get(day.local_date)
        before_signature = before.as_dict() if before is not None else None
        self.add_day(day)
        merged = self._days.get(day.local_date)
        if merged is None:
            return False
        if day.solar_start is not None and (
            merged.solar_start is None or day.solar_start < merged.solar_start
        ):
            merged.solar_start = day.solar_start
        if day.solar_end is not None and (
            merged.solar_end is None or day.solar_end > merged.solar_end
        ):
            merged.solar_end = day.solar_end
        merged.complete = merged.complete or day.complete
        merged.generation = self._generation
        return before_signature != merged.as_dict()

    async def async_backfill_from_recorder(
        self, token: BackfillToken | None = None
    ) -> bool:
        """Populate missing raw days using one local-day query at a time."""
        started = monotonic()
        generation = self._backfill_runtime_generation
        self._backfill_status = "running"
        changed = False
        try:
            entity_id = getattr(self._controller, "solar_production_sensor", None)
            if not entity_id:
                self._backfill_status = "no_direct_source"
                return False
            coordinator = self._backfill_coordinator
            if token is None and coordinator is not None:
                token = coordinator.new_token()
            if coordinator is None:
                self._backfill_status = "coordinator_unavailable"
                self._last_error = "backfill: coordinator unavailable"
                return False

            today = self._today()
            local_tz = self._timezone()
            floor = self._retention_floor(today)
            for offset in range((today - floor).days + 1):
                local_date = floor + timedelta(days=offset)
                self._backfill_current_date = local_date
                if not self._backfill_token_valid(token, generation):
                    self._backfill_status = "cancelled"
                    return False
                if not self._day_needs_backfill(local_date):
                    coordinator.note_skipped()
                    continue
                start, end = local_day_bounds(local_date, local_tz, now=self._now())
                states = await coordinator.async_query(
                    token,
                    str(entity_id),
                    start,
                    end,
                    block=local_date.isoformat(),
                )
                if states is None:
                    self._backfill_status = "cancelled"
                    return False
                day = self._build_backfill_day(local_date, states, today)
                del states
                if day is None:
                    await asyncio.sleep(0)
                    continue
                if not self._backfill_token_valid(token, generation):
                    self._backfill_status = "cancelled"
                    return False
                day_changed = self._merge_backfill_day(day)
                if day_changed:
                    changed = True
                self._backfill_blocks += 1
                self._prune(today)
                if local_date < today and day_changed:
                    await self.async_save()
                await asyncio.sleep(0)

            if changed and self._backfill_token_valid(token, generation):
                await self.async_save()
            self._backfill_status = "completed" if changed else "no_better_intervals"
            self._last_error = None if changed else "backfill: no better intervals"
            return changed
        except asyncio.CancelledError:
            self._backfill_status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - learning is best effort
            self._backfill_status = f"error: {exc}"
            self._last_error = self._backfill_status
            _LOGGER.warning("Solar profile: Recorder backfill failed: %s", exc)
            return False
        finally:
            self._backfill_current_date = None
            self._backfill_last_duration_s = max(0.0, monotonic() - started)

    def start_backfill(
        self, coordinator: RecorderBackfillCoordinator | None = None
    ) -> None:
        """Start at most one entry-owned background Recorder backfill."""
        if coordinator is not None:
            self.set_backfill_coordinator(coordinator)
        if self._backfill_task is not None and not self._backfill_task.done():
            is_cancelling = getattr(self._backfill_task, "cancelling", lambda: 0)()
            if not is_cancelling:
                return
        self._backfill_status = "running"
        if self._backfill_coordinator is None:
            self._backfill_status = "coordinator_unavailable"
            self._last_error = "backfill: coordinator unavailable"
            return
        self._backfill_task = self._backfill_coordinator.submit(
            "solar_profile", self.async_backfill_from_recorder
        )

    def _day_reports(self) -> list[dict[str, Any]]:
        """Per-day admission verdict so a rejected day names its own reason.

        The aggregate counters say how many days were admitted but never why
        the rest were dropped, which leaves a silently rotting profile with no
        way to tell a coverage hole from an invalid solar window.
        """
        complete = [
            day
            for day in self._days.values()
            if day.complete and day.generation == self._generation
        ]
        recent_totals = self._recent_totals(complete)
        reports: list[dict[str, Any]] = []
        for local_date in sorted(self._days, reverse=True):
            day = self._days[local_date]
            valid, quality, reason = _day_quality(day, recent_totals)
            _energy, progress_coverage, _flags = _progress_day(day)
            daylight = day.daylight_seconds
            reports.append(
                {
                    "date": local_date.isoformat(),
                    "valid": valid,
                    "reason": reason,
                    "complete": day.complete,
                    "generation": day.generation,
                    "total_kwh": round(day.total_energy_kwh, 3),
                    "daylight_h": round(day.daylight_hours, 2),
                    "solar_start": day.solar_start.isoformat() if day.solar_start else None,
                    "solar_end": day.solar_end.isoformat() if day.solar_end else None,
                    "coverage_ratio": (
                        round(math.fsum(progress_coverage) / daylight, 3)
                        if daylight > 0.0
                        else 0.0
                    ),
                    "forecast_reference_kwh": day.forecast_reference_kwh,
                    "quality": round(quality, 3),
                }
            )
        return reports

    def diagnostics(self, target_date: date | None = None) -> dict[str, Any]:
        target_date = target_date or self._today()
        snapshot = self.learn_shape(target_date)
        counts = {flag.name.lower(): 0 for flag in SolarQualityFlag}
        for day in self._days.values():
            for raw_flags in day.quality_flags:
                for flag in SolarQualityFlag:
                    if raw_flags & int(flag):
                        counts[flag.name.lower()] += 1
        shape = list(snapshot.shape)
        summary = [
            round(math.fsum(shape[index:index + 4]), 6)
            for index in range(0, len(shape), 4)
        ]
        return {
            "mode": self._active_mode,
            "telemetry_source": self.telemetry_source(),
            "interval_minutes": SOLAR_PROFILE_INTERVAL_MINUTES,
            "retention_days": SOLAR_PROFILE_RETENTION_DAYS,
            "available_days": len(self._days),
            "eligible_days": snapshot.eligible_days,
            "high_quality_days": snapshot.high_quality_days,
            "active_generation": self._generation,
            "source_fingerprint_changed": self._last_error == "source_fingerprint_changed",
            "coverage_ratio": round(snapshot.coverage_ratio, 3),
            "future_coverage_ratio": round(snapshot.future_coverage_ratio, 3),
            "shape_dispersion": round(snapshot.shape_dispersion, 4),
            "mature": snapshot.mature,
            "newest_valid_date": snapshot.newest_valid_date.isoformat() if snapshot.newest_valid_date else None,
            "profile_age_days": snapshot.profile_age_days,
            **counts,
            "fallback_reason": snapshot.fallback_reason,
            "last_error": self._last_error,
            "backfill_status": self._backfill_status,
            "backfill_date": (
                self._backfill_current_date.isoformat()
                if self._backfill_current_date is not None
                else None
            ),
            "backfill_blocks": self._backfill_blocks,
            "backfill_duration_last_s": round(self._backfill_last_duration_s, 3),
            "shape_progress_24": summary[:24],
            "days": self._day_reports(),
        }


__all__ = [
    "SOLAR_PROFILE_INTERVAL_COUNT",
    "SOLAR_PROFILE_INTERVAL_MINUTES",
    "SOLAR_PROFILE_RETENTION_DAYS",
    "INTERVAL_COUNT",
    "INTERVAL_MINUTES",
    "SolarProfileDay",
    "SolarProfileSnapshot",
    "SolarProfileTracker",
    "SolarQualityFlag",
    "SolarQualityFlags",
    "cdf_mae",
    "remap_day_to_progress",
    "weighted_median",
]
