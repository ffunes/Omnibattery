"""Runtime and intraday persistence for the daily operation timeline.

The timeline is deliberately a small boundary object between the controller
and the dashboard.  It records what the runtime observed and accepts a future
projection that was already produced by an authoritative planner.  It does
not select prices, write battery registers, or infer a future schedule.

Only the local day currently being displayed is kept as a diary.  A bounded
next-day forecast is stored separately for the dashboard extension.  Once a
quarter-hour has elapsed its cell is closed and later refreshes may not rewrite
it.  The current cell remains open so cumulative telemetry and the latest
runtime decision can be refreshed without changing the historical evidence.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

INTERVAL_MINUTES = 15
INTERVAL_SECONDS = INTERVAL_MINUTES * 60
INTERVAL_COUNT = 96
EXTENDED_HORIZON_HOURS = 12
EXTENDED_INTERVAL_COUNT = EXTENDED_HORIZON_HOURS * 60 // INTERVAL_MINUTES

DAILY_TIMELINE_SCHEMA_VERSION = 1
DAILY_TIMELINE_STORE_VERSION = 1
DAILY_TIMELINE_STORE_KEY = "daily_operation_timeline"
DAILY_OPERATION_TIMELINE_SCHEMA_VERSION = DAILY_TIMELINE_SCHEMA_VERSION
DAILY_OPERATION_TIMELINE_STORE_VERSION = DAILY_TIMELINE_STORE_VERSION
STORE_KEY = DAILY_TIMELINE_STORE_KEY
SCHEMA_VERSION = DAILY_TIMELINE_SCHEMA_VERSION
TIMELINE_SCHEMA_VERSION = DAILY_TIMELINE_SCHEMA_VERSION
TIMELINE_STORE_VERSION = DAILY_TIMELINE_STORE_VERSION
DAILY_TIMELINE_INTERVAL_COUNT = INTERVAL_COUNT
DAILY_TIMELINE_INTERVAL_MINUTES = INTERVAL_MINUTES
# A full minute bounds continuous-activity persistence to one Store write per
# minute while still keeping structural changes on the immediate path.
DEFAULT_TIMELINE_DEBOUNCE_SECONDS = 60.0

# Names shared with the pure ``pricing.daily_timeline`` DTO.  They are copied
# here intentionally: this runtime module can be integrated before the pricing
# package is exported from ``pricing.__init__``.
STATE_PAST = "past"
STATE_CURRENT = "current"
STATE_FUTURE = "future"
DST_NORMAL = "normal"
DST_SKIPPED = "skipped"
DST_REPEATED = "repeated"

# Action flags are intentionally stable: the frontend and future pricing
# module can consume the masks without importing runtime classes.
ACTION_NONE = 0
ACTION_SOLAR_CHARGE = 1
ACTION_GRID_CHARGE = 2
ACTION_DISCHARGE = 4
ACTION_MASK_ALL = ACTION_SOLAR_CHARGE | ACTION_GRID_CHARGE | ACTION_DISCHARGE

# Short aliases make the DTO usable by callers that use ``solar``/``grid``
# terminology rather than the longer contract names.
ACTION_SOLAR = ACTION_SOLAR_CHARGE
ACTION_GRID = ACTION_GRID_CHARGE

CONTEXT_NONE = 0
CONTEXT_SETPOINT = 1
CONTEXT_CHARGE_DELAY = 2
CONTEXT_DYNAMIC_PRICE = 4
CONTEXT_TIME_SLOT = 8
CONTEXT_REALTIME_PRICE = 16
CONTEXT_HOURLY_BALANCE = 32
CONTEXT_MASK_ALL = (
    CONTEXT_SETPOINT
    | CONTEXT_CHARGE_DELAY
    | CONTEXT_DYNAMIC_PRICE
    | CONTEXT_TIME_SLOT
    | CONTEXT_REALTIME_PRICE
    | CONTEXT_HOURLY_BALANCE
)

GRID_CHARGE_NOT_APPLICABLE = "not_applicable"
GRID_CHARGE_SCHEDULED = "scheduled"
GRID_CHARGE_NOT_NEEDED = "not_needed"
GRID_CHARGE_UNKNOWN = "unknown"
GRID_CHARGE_DECISIONS = frozenset(
    {
        GRID_CHARGE_NOT_APPLICABLE,
        GRID_CHARGE_SCHEDULED,
        GRID_CHARGE_NOT_NEEDED,
        GRID_CHARGE_UNKNOWN,
    }
)

REALTIME_PRICE_MODES = frozenset(
    {
        "realtime_price",
        "real_time_price",
        "real-time-price",
        "realtime",
        "real_time",
    }
)

MAX_TEXT_LENGTH = 128
MAX_METADATA_ITEMS = 8
MAX_DECISION_EVENTS_PER_CELL = 8
POWER_DEADBAND_W = 10.0
_EPSILON = 1e-9

_ACTION_NAMES = {
    ACTION_SOLAR_CHARGE: "solar_charge",
    ACTION_GRID_CHARGE: "grid_charge",
    ACTION_DISCHARGE: "discharge",
}
ACTION_NAMES = dict(_ACTION_NAMES)
_ACTION_NAME_TO_MASK = {
    "solar": ACTION_SOLAR_CHARGE,
    "pv": ACTION_SOLAR_CHARGE,
    "solar_charge": ACTION_SOLAR_CHARGE,
    "solar-charging": ACTION_SOLAR_CHARGE,
    "solar charging": ACTION_SOLAR_CHARGE,
    "charge_solar": ACTION_SOLAR_CHARGE,
    "grid": ACTION_GRID_CHARGE,
    "mains": ACTION_GRID_CHARGE,
    "network": ACTION_GRID_CHARGE,
    "grid_charge": ACTION_GRID_CHARGE,
    "grid-charging": ACTION_GRID_CHARGE,
    "grid charging": ACTION_GRID_CHARGE,
    "charge_grid": ACTION_GRID_CHARGE,
    "discharge": ACTION_DISCHARGE,
    "battery_to_home": ACTION_DISCHARGE,
    "battery-to-home": ACTION_DISCHARGE,
    "export_from_battery": ACTION_DISCHARGE,
}
_CONTEXT_NAME_TO_MASK = {
    "setpoint": CONTEXT_SETPOINT,
    "charge_to_setpoint": CONTEXT_SETPOINT,
    "charging_to_setpoint": CONTEXT_SETPOINT,
    "delay": CONTEXT_CHARGE_DELAY,
    "charge_delay": CONTEXT_CHARGE_DELAY,
    "charging_delay": CONTEXT_CHARGE_DELAY,
    "dynamic": CONTEXT_DYNAMIC_PRICE,
    "dynamic_price": CONTEXT_DYNAMIC_PRICE,
    "dynamic_pricing": CONTEXT_DYNAMIC_PRICE,
    "time_slot": CONTEXT_TIME_SLOT,
    "timeslot": CONTEXT_TIME_SLOT,
    "time-slot": CONTEXT_TIME_SLOT,
    "realtime": CONTEXT_REALTIME_PRICE,
    "real_time": CONTEXT_REALTIME_PRICE,
    "realtime_price": CONTEXT_REALTIME_PRICE,
    "real-time-price": CONTEXT_REALTIME_PRICE,
    "hourly_balance": CONTEXT_HOURLY_BALANCE,
    "hourly_net_balance": CONTEXT_HOURLY_BALANCE,
    "net_balance": CONTEXT_HOURLY_BALANCE,
}
CONTEXT_NAMES = {
    CONTEXT_SETPOINT: "setpoint",
    CONTEXT_CHARGE_DELAY: "charge_delay",
    CONTEXT_DYNAMIC_PRICE: "dynamic_price",
    CONTEXT_TIME_SLOT: "time_slot",
    CONTEXT_REALTIME_PRICE: "realtime_price",
    CONTEXT_HOURLY_BALANCE: "hourly_balance",
}


def _finite_float(value: Any, default: float | None = None) -> float | None:
    """Return a finite float, optionally requiring a non-negative value."""
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


def _finite_non_negative(value: Any, default: float | None = None) -> float | None:
    parsed = _finite_float(value, default)
    if parsed is None or parsed < 0.0:
        return default
    return parsed


def _safe_text(value: Any, *, max_length: int = MAX_TEXT_LENGTH) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        value = value.isoformat()
    elif not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value[:max_length] if value else None


def _as_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _safe_text(value)


def _parse_datetime(value: Any, tz: Any = timezone.utc) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=tz)
        except (TypeError, ValueError):
            parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _object_mapping(value: Any) -> Mapping[str, Any] | None:
    """Read a dict or a small duck-typed DTO without retaining the object."""
    if isinstance(value, Mapping):
        return value
    for method_name in ("as_dict", "to_dict", "as_mapping"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                result = method()
            except Exception:  # noqa: BLE001, S112 - telemetry must not block control
                continue
            if isinstance(result, Mapping):
                return result
    try:
        values = vars(value)
    except TypeError:
        return None
    return values if isinstance(values, Mapping) else None


def _value(value: Any, *names: str, default: Any = None) -> Any:
    mapping = _object_mapping(value)
    if mapping is not None:
        for name in names:
            if name in mapping:
                return mapping[name]
    for name in names:
        try:
            return getattr(value, name)
        except AttributeError:
            continue
    return default


def _json_safe(value: Any) -> Any:
    """Convert bounded public data to values accepted by strict JSON encoders."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
            if _safe_text(key, max_length=MAX_TEXT_LENGTH) is not None
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return [_json_safe(item) for item in sorted(value, key=str)]
    return _safe_text(value)


def _fit_list(values: Any, default: Any) -> list[Any] | None:
    if not isinstance(values, (list, tuple)) or len(values) != INTERVAL_COUNT:
        return None
    return list(values)


def _safe_mask(value: Any, allowed: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed & allowed


def _mask_from_value(value: Any, names: Mapping[str, int], allowed: int) -> int:
    """Parse integer, string, iterable, or mapping action/context values."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value & allowed
    if isinstance(value, float) and value.is_integer():
        return int(value) & allowed
    if isinstance(value, str):
        normalized = value.strip().lower()
        try:
            if normalized.startswith("0x"):
                return int(normalized, 16) & allowed
            if normalized.isdigit():
                return int(normalized) & allowed
        except ValueError:
            pass
        mask = 0
        for token in normalized.replace("|", ",").replace("+", ",").split(","):
            token = token.strip()
            if token in names:
                mask |= names[token]
        return mask & allowed
    if isinstance(value, Mapping):
        mask = 0
        for key, enabled in value.items():
            if enabled:
                mask |= _mask_from_value(key, names, allowed)
        return mask & allowed
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        mask = 0
        for item in value:
            mask |= _mask_from_value(item, names, allowed)
        return mask & allowed
    return 0


def _is_realtime_mode(mode: Any) -> bool:
    normalized = _safe_text(mode, max_length=64)
    if normalized is None:
        return False
    return normalized.lower().replace(" ", "_") in REALTIME_PRICE_MODES


def _normalize_mode(mode: Any) -> str:
    normalized = _safe_text(mode, max_length=64)
    if normalized is None:
        return "unknown"
    return normalized.lower().replace(" ", "_")


def _normalize_grid_decision(value: Any) -> str:
    if isinstance(value, bool):
        return GRID_CHARGE_SCHEDULED if value else GRID_CHARGE_NOT_NEEDED
    normalized = _safe_text(value, max_length=32)
    if normalized is None:
        return GRID_CHARGE_NOT_APPLICABLE
    normalized = normalized.lower().replace("-", "_").replace(" ", "_")
    if normalized in GRID_CHARGE_DECISIONS:
        return normalized
    aliases = {
        "scheduled_charge": GRID_CHARGE_SCHEDULED,
        "schedule": GRID_CHARGE_SCHEDULED,
        "selected": GRID_CHARGE_SCHEDULED,
        "active": GRID_CHARGE_SCHEDULED,
        "not_needed": GRID_CHARGE_NOT_NEEDED,
        "unneeded": GRID_CHARGE_NOT_NEEDED,
        "no": GRID_CHARGE_NOT_NEEDED,
        "unknown": GRID_CHARGE_UNKNOWN,
        "not_applicable": GRID_CHARGE_NOT_APPLICABLE,
        "none": GRID_CHARGE_NOT_APPLICABLE,
        "": GRID_CHARGE_NOT_APPLICABLE,
    }
    return aliases.get(normalized, GRID_CHARGE_UNKNOWN)


def _datetime_candidates(wall: datetime, tz: Any) -> list[datetime]:
    """Return valid aware representations of a wall time, handling DST folds."""
    if tz is None:
        return [wall.replace(tzinfo=timezone.utc)]
    result: list[datetime] = []
    timestamps: set[float] = set()
    for fold in (0, 1):
        try:
            candidate = wall.replace(tzinfo=tz, fold=fold)
            timestamp = candidate.timestamp()
            round_trip = datetime.fromtimestamp(timestamp, tz).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            continue
        # A nonexistent wall time round-trips to a different wall time.
        if round_trip != wall.replace(tzinfo=None) or timestamp in timestamps:
            continue
        timestamps.add(timestamp)
        result.append(candidate)
    return result


def _wall_interval_info(local_date: date, index: int, tz: Any) -> dict[str, Any]:
    hour, quarter = divmod(index, 4)
    minute = quarter * INTERVAL_MINUTES
    start_wall = datetime.combine(local_date, time(hour, minute))
    end_wall = start_wall + timedelta(minutes=INTERVAL_MINUTES)
    starts = _datetime_candidates(start_wall, tz)
    ends = _datetime_candidates(end_wall, tz)
    repeated = len(starts) > 1
    if repeated:
        # The wall endpoint immediately after a fall-back quarter has only
        # one valid representation, but each repeated start still owns one
        # physical 15-minute occurrence.
        ends = [
            datetime.fromtimestamp(start.timestamp() + INTERVAL_SECONDS, tz)
            for start in starts
        ]
    # At a spring-forward transition the quarter immediately before the
    # missing wall hour has a valid start but a nonexistent wall endpoint.  It
    # still occupies one physical quarter and ends at the transition instant.
    if starts and not ends:
        ends = [
            datetime.fromtimestamp(start.timestamp() + INTERVAL_SECONDS, tz)
            for start in starts
        ]
    skipped = not starts
    durations: list[float] = []
    if not skipped:
        if repeated:
            durations = [
                max(0.0, (end.timestamp() - start.timestamp()))
                for start, end in zip(starts, ends)
            ]
        else:
            durations = [max(0.0, ends[0].timestamp() - starts[0].timestamp())]
    start = starts[0].isoformat() if starts else None
    end = ends[-1].isoformat() if ends else None
    return {
        "label": f"{hour:02d}:{minute:02d}",
        "start": start,
        "end": end,
        "duration_s": round(sum(durations), 3),
        "dst_skipped": skipped,
        "dst_repeated": repeated,
    }


@dataclass(frozen=True)
class BatteryProjectionInput:
    """Small DTO accepted by future pure projection code."""

    key: str
    stored_kwh: float
    capacity_kwh: float
    min_soc_pct: float
    max_soc_pct: float
    charge_power_w: float
    discharge_power_w: float
    charge_efficiency: float | None = None
    discharge_efficiency: float | None = None
    eligible: bool = True
    can_charge: bool = True
    can_discharge: bool = True


@dataclass(frozen=True)
class ProjectedIntervalFlow:
    """DTO for one already-calculated future interval.

    The field names mirror the planned ``pricing.daily_timeline`` contract, so
    this module can be used before that package module is integrated.
    """

    start: datetime | None
    end: datetime | None
    solar_kwh: float = 0.0
    consumption_kwh: float = 0.0
    solar_to_battery_kwh: float = 0.0
    grid_to_battery_kwh: float = 0.0
    battery_to_home_kwh: float = 0.0
    grid_to_home_kwh: float = 0.0
    stored_energy_end_kwh: float = 0.0
    action_mask: int = ACTION_NONE
    context_mask: int = CONTEXT_NONE
    grid_charge_decision: str = GRID_CHARGE_NOT_APPLICABLE
    charge_power_w: float = 0.0
    discharge_power_w: float = 0.0
    delay_until: datetime | str | None = None
    source: str | None = None
    slot: str | None = None
    state: str = STATE_FUTURE
    dst_status: str = DST_NORMAL
    duration_seconds: float | None = None
    coverage_seconds: float | None = None
    stored_energy_start_kwh: float | None = None
    solar_to_home_kwh: float | None = None
    curtailed_solar_kwh: float | None = None
    stored_energy_charged_kwh: float | None = None
    stored_energy_discharged_kwh: float | None = None
    projected: bool | None = None
    wall_index: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_mask", _safe_mask(self.action_mask, ACTION_MASK_ALL))
        object.__setattr__(self, "context_mask", _safe_mask(self.context_mask, CONTEXT_MASK_ALL))
        object.__setattr__(self, "grid_charge_decision", _normalize_grid_decision(self.grid_charge_decision))
        object.__setattr__(self, "state", _safe_text(self.state, max_length=16) or STATE_FUTURE)
        object.__setattr__(self, "dst_status", _safe_text(self.dst_status, max_length=16) or DST_NORMAL)

    @property
    def actions(self) -> tuple[str, ...]:
        return tuple(
            name for bit, name in ACTION_NAMES.items() if self.action_mask & bit
        )

    @property
    def contexts(self) -> tuple[str, ...]:
        return tuple(
            name for bit, name in CONTEXT_NAMES.items() if self.context_mask & bit
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "solar_kwh": self.solar_kwh,
            "consumption_kwh": self.consumption_kwh,
            "solar_to_battery_kwh": self.solar_to_battery_kwh,
            "grid_to_battery_kwh": self.grid_to_battery_kwh,
            "battery_to_home_kwh": self.battery_to_home_kwh,
            "grid_to_home_kwh": self.grid_to_home_kwh,
            "stored_energy_end_kwh": self.stored_energy_end_kwh,
            "action_mask": self.action_mask,
            "context_mask": self.context_mask,
            "grid_charge_decision": self.grid_charge_decision,
            "charge_power_w": self.charge_power_w,
            "discharge_power_w": self.discharge_power_w,
            "delay_until": self.delay_until,
            "source": self.source,
            "slot": self.slot,
            "state": self.state,
            "dst_status": self.dst_status,
            "duration_seconds": self.duration_seconds,
            "coverage_seconds": self.coverage_seconds,
            "stored_energy_start_kwh": self.stored_energy_start_kwh,
            "solar_to_home_kwh": self.solar_to_home_kwh,
            "curtailed_solar_kwh": self.curtailed_solar_kwh,
            "stored_energy_charged_kwh": self.stored_energy_charged_kwh,
            "stored_energy_discharged_kwh": self.stored_energy_discharged_kwh,
            "projected": self.projected,
            "wall_index": self.wall_index,
            "reason": self.reason,
        }

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self.as_dict())


# Compatibility aliases for the names used by the plan and likely callers.
DailyTimelineInterval = ProjectedIntervalFlow
ProjectedOperationInterval = ProjectedIntervalFlow


@dataclass(frozen=True)
class ChargeDelayProjection:
    """Pure projection result that can be attached to future cells."""

    setpoint_reached_at: datetime | None = None
    delay_starts_at: datetime | None = None
    estimated_unlock_at: datetime | None = None
    source: str = "runtime"
    reason: str | None = None


@dataclass(frozen=True)
class DailyOperationTimelineSnapshot(Mapping[str, Any]):
    """Typed view over the JSON contract published by the manager."""

    schema_version: int
    local_date: str
    timezone: str
    interval_minutes: int
    interval_count: int
    generated_at: str
    plan_evaluated_at: str | None
    current_index: int
    current_progress: float
    mode: str
    stale: bool
    stale_reason: str | None
    series: Mapping[str, Any]
    operations: Mapping[str, Any]
    sources: Mapping[str, Any]
    interval_grid: Mapping[str, Any] = field(default_factory=dict)
    extended_horizon: Mapping[str, Any] = field(default_factory=dict)
    extended_projection: tuple[Mapping[str, Any], ...] = ()
    revision: int = 0
    snapshot_build_count: int = 0
    notification_count: int = 0
    save_count: int = 0
    snapshot_age_s: float | None = None
    last_save_age_s: float | None = None
    publications_last_minute: int = 0
    writes_last_minute: int = 0

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DailyOperationTimelineSnapshot:
        return cls(
            schema_version=int(payload.get("schema_version", SCHEMA_VERSION)),
            local_date=str(payload.get("local_date", "")),
            timezone=str(payload.get("timezone", "UTC")),
            interval_minutes=int(payload.get("interval_minutes", INTERVAL_MINUTES)),
            interval_count=int(payload.get("interval_count", INTERVAL_COUNT)),
            generated_at=str(payload.get("generated_at", "")),
            plan_evaluated_at=payload.get("plan_evaluated_at"),
            current_index=int(payload.get("current_index", 0)),
            current_progress=float(payload.get("current_progress", 0.0)),
            mode=str(payload.get("mode", "unknown")),
            stale=bool(payload.get("stale", False)),
            stale_reason=payload.get("stale_reason"),
            series=payload.get("series", {}),
            operations=payload.get("operations", {}),
            sources=payload.get("sources", {}),
            interval_grid=payload.get("interval_grid", {}),
            extended_horizon=payload.get("extended_horizon", {}),
            extended_projection=tuple(
                item
                for item in (payload.get("extended_projection") or ())
                if isinstance(item, Mapping)
            ),
            revision=int(payload.get("revision", 0)),
            snapshot_build_count=int(payload.get("snapshot_build_count", 0)),
            notification_count=int(payload.get("notification_count", 0)),
            save_count=int(payload.get("save_count", 0)),
            snapshot_age_s=_finite_non_negative(payload.get("snapshot_age_s")),
            last_save_age_s=_finite_non_negative(payload.get("last_save_age_s")),
            publications_last_minute=int(payload.get("publications_last_minute", 0)),
            writes_last_minute=int(payload.get("writes_last_minute", 0)),
        )

    def as_dict(self) -> dict[str, Any]:
        return _json_safe(
            {
                "schema_version": self.schema_version,
                "local_date": self.local_date,
                "timezone": self.timezone,
                "interval_minutes": self.interval_minutes,
                "interval_count": self.interval_count,
                "generated_at": self.generated_at,
                "plan_evaluated_at": self.plan_evaluated_at,
                "current_index": self.current_index,
                "current_progress": self.current_progress,
                "mode": self.mode,
                "stale": self.stale,
                "stale_reason": self.stale_reason,
                "series": dict(self.series),
                "operations": dict(self.operations),
                "sources": dict(self.sources),
                "interval_grid": dict(self.interval_grid),
                "extended_horizon": dict(self.extended_horizon),
                "extended_projection": list(self.extended_projection),
                "revision": self.revision,
                "snapshot_build_count": self.snapshot_build_count,
                "notification_count": self.notification_count,
                "save_count": self.save_count,
                "snapshot_age_s": self.snapshot_age_s,
                "last_save_age_s": self.last_save_age_s,
                "publications_last_minute": self.publications_last_minute,
                "writes_last_minute": self.writes_last_minute,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return self.as_dict()

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())


DailyTimelineSnapshot = DailyOperationTimelineSnapshot
DailyTimelineDTO = DailyOperationTimelineSnapshot


@dataclass
class _TimelineCell:
    """Mutable runtime state for one wall-clock quarter-hour."""

    actual_action_mask: int = ACTION_NONE
    planned_action_mask: int = ACTION_NONE
    actual_context_mask: int = CONTEXT_NONE
    planned_context_mask: int = CONTEXT_NONE
    actual_coexistence_mask: int = ACTION_NONE
    planned_coexistence_mask: int = ACTION_NONE
    actual_grid_charge_decision: str = GRID_CHARGE_NOT_APPLICABLE
    planned_grid_charge_decision: str = GRID_CHARGE_NOT_APPLICABLE
    actual_charge_power_w: float | None = None
    planned_charge_power_w: float | None = None
    actual_discharge_power_w: float | None = None
    planned_discharge_power_w: float | None = None
    actual_charge_to_battery_kwh: float | None = None
    actual_discharge_from_battery_kwh: float | None = None
    actual_soc_pct: float | None = None
    planned_solar_to_battery_kwh: float | None = None
    planned_grid_to_battery_kwh: float | None = None
    planned_battery_to_home_kwh: float | None = None
    planned_grid_to_home_kwh: float | None = None
    planned_solar_to_home_kwh: float | None = None
    planned_stored_energy_end_kwh: float | None = None
    planned_soc_end_pct: float | None = None
    actual_delay_until: str | None = None
    planned_delay_until: str | None = None
    actual_source: str | None = None
    planned_source: str | None = None
    actual_slot: str | None = None
    planned_slot: str | None = None
    observed_seconds_by_action: dict[str, float] = field(default_factory=dict)
    observed_seconds_by_context: dict[str, float] = field(default_factory=dict)
    decision_events: list[dict[str, Any]] = field(default_factory=list)

    def clear_planned(self) -> None:
        self.planned_action_mask = ACTION_NONE
        self.planned_context_mask = CONTEXT_NONE
        self.planned_coexistence_mask = ACTION_NONE
        self.planned_grid_charge_decision = GRID_CHARGE_NOT_APPLICABLE
        self.planned_charge_power_w = None
        self.planned_discharge_power_w = None
        self.planned_solar_to_battery_kwh = None
        self.planned_grid_to_battery_kwh = None
        self.planned_battery_to_home_kwh = None
        self.planned_grid_to_home_kwh = None
        self.planned_solar_to_home_kwh = None
        self.planned_stored_energy_end_kwh = None
        self.planned_soc_end_pct = None
        self.planned_delay_until = None
        self.planned_source = None
        self.planned_slot = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "actual_action_mask": self.actual_action_mask,
            "planned_action_mask": self.planned_action_mask,
            "actual_context_mask": self.actual_context_mask,
            "planned_context_mask": self.planned_context_mask,
            "actual_coexistence_mask": self.actual_coexistence_mask,
            "planned_coexistence_mask": self.planned_coexistence_mask,
            "actual_grid_charge_decision": self.actual_grid_charge_decision,
            "planned_grid_charge_decision": self.planned_grid_charge_decision,
            "actual_charge_power_w": self.actual_charge_power_w,
            "planned_charge_power_w": self.planned_charge_power_w,
            "actual_discharge_power_w": self.actual_discharge_power_w,
            "planned_discharge_power_w": self.planned_discharge_power_w,
            "actual_charge_to_battery_kwh": self.actual_charge_to_battery_kwh,
            "actual_discharge_from_battery_kwh": self.actual_discharge_from_battery_kwh,
            "actual_soc_pct": self.actual_soc_pct,
            "planned_solar_to_battery_kwh": self.planned_solar_to_battery_kwh,
            "planned_grid_to_battery_kwh": self.planned_grid_to_battery_kwh,
            "planned_battery_to_home_kwh": self.planned_battery_to_home_kwh,
            "planned_grid_to_home_kwh": self.planned_grid_to_home_kwh,
            "planned_solar_to_home_kwh": self.planned_solar_to_home_kwh,
            "planned_stored_energy_end_kwh": self.planned_stored_energy_end_kwh,
            "planned_soc_end_pct": self.planned_soc_end_pct,
            "actual_delay_until": self.actual_delay_until,
            "planned_delay_until": self.planned_delay_until,
            "actual_source": self.actual_source,
            "planned_source": self.planned_source,
            "actual_slot": self.actual_slot,
            "planned_slot": self.planned_slot,
            "observed_seconds_by_action": dict(self.observed_seconds_by_action),
            "observed_seconds_by_context": dict(self.observed_seconds_by_context),
            "decision_events": list(self.decision_events[-MAX_DECISION_EVENTS_PER_CELL:]),
        }


class DailyOperationTimelineManager:
    """Own one ConfigEntry's current-day runtime diary and public snapshot."""

    def __init__(
        self,
        hass: Any,
        config_entry: Any,
        controller: Any = None,
        *,
        store: Any = None,
        now_provider: Callable[[], datetime] | datetime | None = None,
        clock: Callable[[], datetime] | datetime | None = None,
        debounce_seconds: float = DEFAULT_TIMELINE_DEBOUNCE_SECONDS,
    ) -> None:
        self._hass = hass
        self._config_entry = config_entry
        self._controller = controller
        entry_id = getattr(config_entry, "entry_id", "unknown")
        self._store = (
            store
            if store is not None
            else Store(
                hass,
                DAILY_TIMELINE_STORE_VERSION,
                f"{DOMAIN}.{entry_id}.{DAILY_TIMELINE_STORE_KEY}",
            )
        )
        provider = now_provider if now_provider is not None else clock
        if isinstance(provider, datetime):
            self._now_provider: Callable[[], datetime] = lambda: provider
        elif callable(provider):
            self._now_provider = provider
        else:
            self._now_provider = self._default_now

        parsed_debounce = _finite_non_negative(
            debounce_seconds, DEFAULT_TIMELINE_DEBOUNCE_SECONDS
        )
        self._debounce_seconds = (
            parsed_debounce
            if parsed_debounce is not None
            else DEFAULT_TIMELINE_DEBOUNCE_SECONDS
        )
        self._save_handle: asyncio.TimerHandle | None = None
        self._save_task: asyncio.Task | None = None
        self._save_revision = 0
        self._save_reschedule_requested = False
        self._listeners: list[Callable[..., Any]] = []
        self._update_batch_depth = 0
        self._update_notification_pending = False
        self._dirty = False
        self._loaded = False
        self._last_error: str | None = None
        self._restore_status = "not_loaded"
        self._local_date: date = self._now().date()
        self._timezone_name = self._configured_timezone_name()
        self._cells: list[_TimelineCell] = []
        self._closed: list[bool] = []
        self._actual_solar_kwh: list[float | None] = []
        self._actual_consumption_kwh: list[float | None] = []
        self._actual_solar_coverage_s: list[float] = []
        self._actual_consumption_coverage_s: list[float] = []
        self._planned_solar_kwh: list[float | None] = []
        self._planned_consumption_kwh: list[float | None] = []
        self._planned_stored_energy_end_kwh: list[float | None] = []
        self._extended_projection: list[dict[str, Any]] = []
        self._current_index = 0
        self._current_progress = 0.0
        self._last_clock_timestamp: float | None = None
        self._interval_end_timestamp_cache: list[tuple[float, ...] | None] = []
        self._mode = self._controller_mode()
        self._plan_evaluated_at: str | None = None
        self._generated_at: str | None = None
        self._stale = False
        self._stale_reason: str | None = None
        self._setpoint_info: dict[str, Any] = {}
        self._delay_info: dict[str, Any] = {}
        self._freshness_info: dict[str, Any] = {}
        self._sources: dict[str, str | None] = {
            "solar_actual": None,
            "solar_forecast": None,
            "solar_fallback_reason": None,
            "consumption_actual": None,
            "consumption_forecast": None,
            "consumption_fallback_reason": None,
            "operation_plan": None,
        }
        self._revision = 0
        self._snapshot_cache_revision = -1
        self._snapshot_cache: dict[str, Any] | None = None
        self._snapshot_build_count = 0
        self._last_snapshot_monotonic: float | None = None
        self._notification_count = 0
        self._last_notification_monotonic: float | None = None
        self._notification_times: deque[float] = deque(maxlen=256)
        self._save_count = 0
        self._last_save_monotonic: float | None = None
        self._save_times: deque[float] = deque(maxlen=256)
        self._last_notification_kind = "activity"
        self._last_notification_immediate = False
        self._configuration_fingerprint = self.configuration_fingerprint()
        self._reset_arrays(self._local_date)

    # ------------------------------------------------------------------
    # Public properties and time/fingerprint helpers
    # ------------------------------------------------------------------

    @property
    def store(self) -> Any:
        return self._store

    @property
    def local_date(self) -> date:
        return self._local_date

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def is_stale(self) -> bool:
        return self._stale

    @property
    def closed_intervals(self) -> tuple[bool, ...]:
        return tuple(self._closed)

    @property
    def revision(self) -> int:
        """Return the monotonic revision of the public timeline contract."""
        return self._revision

    @property
    def snapshot_build_count(self) -> int:
        """Return the number of expensive public snapshot builds."""
        return self._snapshot_build_count

    @property
    def notification_count(self) -> int:
        """Return the number of listener publications since setup."""
        return self._notification_count

    @property
    def save_count(self) -> int:
        """Return the number of completed Store writes since setup."""
        return self._save_count

    @property
    def snapshot_age_s(self) -> float | None:
        """Return the age of the last expensive snapshot build."""
        if self._last_snapshot_monotonic is None:
            return None
        return max(0.0, monotonic() - self._last_snapshot_monotonic)

    @property
    def last_save_age_s(self) -> float | None:
        """Return the age of the last successful Store write."""
        if self._last_save_monotonic is None:
            return None
        return max(0.0, monotonic() - self._last_save_monotonic)

    @staticmethod
    def _events_last_minute(events: deque[float]) -> int:
        """Return a bounded event count for the rolling one-minute window."""
        now = monotonic()
        while events and now - events[0] > 60.0:
            events.popleft()
        return len(events)

    @property
    def publications_last_minute(self) -> int:
        """Return timeline listener publications in the last minute."""
        return self._events_last_minute(self._notification_times)

    @property
    def writes_last_minute(self) -> int:
        """Return successful timeline Store writes in the last minute."""
        return self._events_last_minute(self._save_times)

    @property
    def last_notification_kind(self) -> str:
        return self._last_notification_kind

    @property
    def last_notification_immediate(self) -> bool:
        return self._last_notification_immediate

    def async_add_listener(self, listener: Callable[..., Any]) -> Callable[[], None]:
        """Register a lightweight synchronous listener for entity updates."""
        if not callable(listener):
            return lambda: None
        self._listeners.append(listener)
        removed = False

        def remove() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return remove

    add_listener = async_add_listener

    def _mark_public_change(
        self, kind: str = "activity", *, immediate: bool = False
    ) -> None:
        """Invalidate the public snapshot only after a material mutation."""
        self._revision += 1
        self._snapshot_cache_revision = -1
        self._snapshot_cache = None
        self._last_notification_kind = kind
        self._last_notification_immediate = immediate

    def _notify_listeners(self) -> None:
        if self._update_batch_depth > 0:
            self._update_notification_pending = True
            return
        self._notification_count += 1
        self._last_notification_monotonic = monotonic()
        self._notification_times.append(self._last_notification_monotonic)
        for listener in tuple(self._listeners):
            try:
                listener(self)
            except TypeError:
                try:
                    listener()
                except Exception:
                    _LOGGER.debug("Daily operation listener failed", exc_info=True)
            except Exception:
                _LOGGER.debug("Daily operation listener failed", exc_info=True)

    def begin_update_batch(self) -> None:
        """Defer listener publication until a coherent refresh is complete."""
        self._update_batch_depth += 1

    def end_update_batch(self) -> None:
        """Publish one listener update after the outermost refresh batch."""
        if self._update_batch_depth <= 0:
            return
        self._update_batch_depth -= 1
        if self._update_batch_depth == 0 and self._update_notification_pending:
            self._update_notification_pending = False
            self._notify_listeners()

    @staticmethod
    def _safe_metadata(value: Any, *, max_items: int = 16) -> dict[str, Any]:
        mapping = _object_mapping(value)
        if mapping is None:
            return {}
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(mapping.items()):
            if index >= max_items:
                break
            safe_key = _safe_text(key, max_length=64)
            if safe_key is None:
                continue
            safe_item = _json_safe(item)
            if isinstance(safe_item, dict):
                safe_item = {
                    str(nested_key): nested_value
                    for nested_index, (nested_key, nested_value)
                    in enumerate(safe_item.items())
                    if nested_index < 16
                }
            elif isinstance(safe_item, list):
                safe_item = safe_item[:16]
            result[safe_key] = safe_item
        return result

    def update_runtime_metadata(
        self,
        *,
        setpoint: Any = None,
        delay: Any = None,
        freshness: Any = None,
        restoration: Any = None,
        sources: Mapping[str, Any] | None = None,
        stale: bool | None = None,
        stale_reason: Any = None,
    ) -> bool:
        """Update small status metadata without touching interval evidence."""
        old = (
            self._setpoint_info,
            self._delay_info,
            self._freshness_info,
            self._restore_status,
            self._last_error,
            dict(self._sources),
            self._stale,
            self._stale_reason,
        )
        if setpoint is not None:
            self._setpoint_info = self._safe_metadata(setpoint)
        if delay is not None:
            self._delay_info = self._safe_metadata(delay)
        if freshness is not None:
            incoming_freshness = self._safe_metadata(freshness)
            # ``updated_at`` is useful in the DTO, but the control loop calls
            # this method every cycle. Keep the previous timestamp while the
            # freshness state is unchanged so it does not invalidate the
            # public snapshot on every tick.
            if (
                incoming_freshness.get("state")
                == self._freshness_info.get("state")
                and "updated_at" in incoming_freshness
                and "updated_at" in self._freshness_info
            ):
                incoming_freshness["updated_at"] = self._freshness_info["updated_at"]
            self._freshness_info = incoming_freshness
        if restoration is not None:
            restore = self._safe_metadata(restoration)
            status = restore.get("status")
            if status is not None:
                self._restore_status = _safe_text(status, max_length=32) or self._restore_status
            error = restore.get("error")
            if error is not None:
                self._last_error = _safe_text(error, max_length=128)
        if sources:
            for key in self._sources:
                if key in sources:
                    self._sources[key] = _safe_text(sources[key], max_length=128)
        if stale is not None:
            self._stale = bool(stale)
        if stale_reason is not None:
            self._stale_reason = _safe_text(stale_reason, max_length=128)
        current = (
            self._setpoint_info,
            self._delay_info,
            self._freshness_info,
            self._restore_status,
            self._last_error,
            dict(self._sources),
            self._stale,
            self._stale_reason,
        )
        changed = old != current
        if changed:
            self._dirty = True
            stale_changed = old[6] != self._stale or old[7] != self._stale_reason
            restoration_changed = old[3] != self._restore_status
            kind = "stale" if stale_changed else "recovery" if restoration_changed else "plan"
            self._mark_public_change(kind, immediate=True)
            self.request_save(immediate=True)
            self._notify_listeners()
        return changed

    def _default_now(self) -> datetime:
        try:
            current = dt_util.now()
        except Exception:  # noqa: BLE001
            current = datetime.now(timezone.utc)
        return current

    def _timezone(self) -> Any:
        configured = getattr(getattr(self._hass, "config", None), "time_zone", None)
        if isinstance(configured, timezone):
            return configured
        if hasattr(configured, "utcoffset") and not isinstance(configured, str):
            return configured
        if configured:
            try:
                return dt_util.get_time_zone(configured) or ZoneInfo(str(configured))
            except Exception:  # noqa: BLE001
                try:
                    return ZoneInfo(str(configured))
                except Exception:  # noqa: BLE001, S110
                    pass
        return timezone.utc

    def _configured_timezone_name(self) -> str:
        configured = getattr(getattr(self._hass, "config", None), "time_zone", None)
        if configured:
            return _safe_text(configured, max_length=64) or "UTC"
        tz = self._timezone()
        return _safe_text(getattr(tz, "key", None) or str(tz), max_length=64) or "UTC"

    def _now(self) -> datetime:
        try:
            current = self._now_provider()
        except Exception:  # noqa: BLE001
            current = self._default_now()
        if not isinstance(current, datetime):
            current = self._default_now()
        tz = self._timezone()
        if current.tzinfo is None:
            current = current.replace(tzinfo=tz)
        return current.astimezone(tz)

    def _as_local_datetime(self, value: Any = None) -> datetime:
        parsed = self._now() if value is None else _parse_datetime(value, self._timezone())
        if parsed is None:
            parsed = self._now()
        return parsed.astimezone(self._timezone())

    def as_local_datetime(self, value: Any = None) -> datetime:
        """Normalize an external callback timestamp to Home Assistant local time."""
        return self._as_local_datetime(value)

    def configuration_fingerprint(self) -> str:
        """Hash the configuration used to build a future projection.

        Runtime observations are a diary of what already happened and remain
        valid when an option changes. The fingerprint therefore protects only
        the persisted projection, which must be rebuilt after a reload with
        different settings.
        """
        data = getattr(self._config_entry, "data", {}) or {}
        options = getattr(self._config_entry, "options", {}) or {}
        payload = {
            "entry_id": getattr(self._config_entry, "entry_id", None),
            "data": data,
            "options": options,
            "timezone": self._configured_timezone_name(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    # Compatibility spelling used by the profile trackers.
    source_fingerprint = configuration_fingerprint

    def _controller_mode(self) -> str:
        for name in ("pricing_mode", "mode", "predictive_mode", "charging_mode"):
            value = getattr(self._controller, name, None)
            if value is not None:
                return _normalize_mode(value)
        entry_data = getattr(self._config_entry, "data", {}) or {}
        for name in ("pricing_mode", "mode", "predictive_mode"):
            if name in entry_data:
                return _normalize_mode(entry_data[name])
        return "unknown"

    def _index_for_datetime(self, value: datetime) -> int:
        return min(INTERVAL_COUNT - 1, (value.hour * 60 + value.minute) // INTERVAL_MINUTES)

    def _progress_for_datetime(self, value: datetime) -> float:
        seconds = value.minute % INTERVAL_MINUTES * 60 + value.second
        seconds += value.microsecond / 1_000_000
        return min(1.0, max(0.0, seconds / INTERVAL_SECONDS))

    def _ensure_current_day(self, current: datetime) -> bool:
        if current.date() == self._local_date:
            return False
        self._local_date = current.date()
        self._reset_arrays(self._local_date)
        self._mode = self._controller_mode()
        self._restore_status = "new_day"
        self._dirty = True
        return True

    def _reset_arrays(self, local_date: date) -> None:
        self._local_date = local_date
        self._cells = [_TimelineCell() for _ in range(INTERVAL_COUNT)]
        self._closed = [False] * INTERVAL_COUNT
        self._actual_solar_kwh = [None] * INTERVAL_COUNT
        self._actual_consumption_kwh = [None] * INTERVAL_COUNT
        self._actual_solar_coverage_s = [0.0] * INTERVAL_COUNT
        self._actual_consumption_coverage_s = [0.0] * INTERVAL_COUNT
        self._planned_solar_kwh = [None] * INTERVAL_COUNT
        self._planned_consumption_kwh = [None] * INTERVAL_COUNT
        self._planned_stored_energy_end_kwh = [None] * INTERVAL_COUNT
        self._extended_projection = []
        self._current_index = 0
        self._current_progress = 0.0
        self._last_clock_timestamp = None
        self._interval_end_timestamp_cache = [None] * INTERVAL_COUNT
        self._plan_evaluated_at = None
        self._generated_at = None
        self._stale = False
        self._stale_reason = None
        self._setpoint_info = {}
        self._delay_info = {}
        self._freshness_info = {}
        self._sources = {
            "solar_actual": None,
            "solar_forecast": None,
            "solar_fallback_reason": None,
            "consumption_actual": None,
            "consumption_forecast": None,
            "consumption_fallback_reason": None,
            "operation_plan": None,
        }

    def _clear_projection(self) -> None:
        """Discard forecast data while retaining measured current-day evidence."""
        for cell in self._cells:
            cell.clear_planned()
        self._planned_solar_kwh = [None] * INTERVAL_COUNT
        self._planned_consumption_kwh = [None] * INTERVAL_COUNT
        self._planned_stored_energy_end_kwh = [None] * INTERVAL_COUNT
        self._extended_projection = []
        self._mode = self._controller_mode()
        self._plan_evaluated_at = None
        self._stale = False
        self._stale_reason = None
        self._sources["solar_forecast"] = None
        self._sources["solar_fallback_reason"] = None
        self._sources["consumption_forecast"] = None
        self._sources["consumption_fallback_reason"] = None
        self._sources["operation_plan"] = None

    def _interval_end_timestamps(self, index: int) -> tuple[float, ...]:
        """Return every physical end instant owned by one wall-clock cell.

        The dashboard deliberately has 96 *wall-clock* cells.  On an autumn
        DST change the four 02:xx cells therefore each own two physical
        occurrences.  A cell is historical only after its final occurrence
        has ended, not after the first pass through that wall time.
        """
        cached = self._interval_end_timestamp_cache[index]
        if cached is not None:
            return cached

        hour, quarter = divmod(index, 4)
        start_wall = datetime.combine(
            self._local_date, time(hour, quarter * INTERVAL_MINUTES)
        )
        starts = _datetime_candidates(start_wall, self._timezone())
        if not starts:
            self._interval_end_timestamp_cache[index] = ()
            return ()
        if len(starts) > 1:
            result = tuple(start.timestamp() + INTERVAL_SECONDS for start in starts)
            self._interval_end_timestamp_cache[index] = result
            return result

        end_wall = start_wall + timedelta(minutes=INTERVAL_MINUTES)
        ends = _datetime_candidates(end_wall, self._timezone())
        start_timestamp = starts[0].timestamp()
        valid_ends = sorted(
            candidate.timestamp()
            for candidate in ends
            if candidate.timestamp() > start_timestamp
        )
        if valid_ends:
            result = (valid_ends[0],)
            self._interval_end_timestamp_cache[index] = result
            return result
        # The wall endpoint of the quarter before spring-forward is
        # nonexistent, while the physical quarter still lasts fifteen minutes.
        result = (start_timestamp + INTERVAL_SECONDS,)
        self._interval_end_timestamp_cache[index] = result
        return result

    def _advance_clock(self, current: datetime, *, close_elapsed: bool = True) -> bool:
        """Advance from an absolute instant without confusing a DST fold.

        Wall-clock indexes legitimately move from 11 back to 8 during the
        second 02:00 hour in Europe/Madrid.  Ordering is instead based on the
        absolute timestamp.  Late callbacks are ignored, but the repeated
        hour remains writable until every physical occurrence has elapsed.
        """
        timestamp = current.timestamp()
        if (
            self._last_clock_timestamp is not None
            and timestamp < self._last_clock_timestamp
        ):
            return False
        index = self._index_for_datetime(current)
        progress = self._progress_for_datetime(current)
        # Progress is deliberately not a public revision: it changes every
        # control tick and would defeat the snapshot cache. The interval index
        # and closure state are material timeline changes.
        changed = index != self._current_index
        self._current_index = index
        self._current_progress = progress
        self._last_clock_timestamp = timestamp
        if not close_elapsed:
            return changed
        for closed_index in range(INTERVAL_COUNT):
            end_timestamps = self._interval_end_timestamps(closed_index)
            if (
                not self._closed[closed_index]
                and end_timestamps
                and timestamp >= max(end_timestamps)
            ):
                self._closed[closed_index] = True
                changed = True
        return changed

    # ------------------------------------------------------------------
    # Capture normalization and actual runtime refresh
    # ------------------------------------------------------------------

    @staticmethod
    def _call_capture(source: Any, local_date: date) -> Any:
        if source is None:
            return None
        method = getattr(source, "current_day_capture", None)
        if callable(method):
            try:
                return method(local_date)
            except TypeError:
                try:
                    return method()
                except Exception:  # noqa: BLE001
                    return None
            except Exception:  # noqa: BLE001
                return None
        return source

    @staticmethod
    def _capture_series(
        capture: Any,
        kind: str,
        current_index: int,
    ) -> tuple[list[float | None], list[float], str | None, str | None]:
        """Extract a bounded 96-bin capture from a profile or simple mapping."""
        mapping = _object_mapping(capture)
        if mapping is None:
            values: list[float | None] = [None] * INTERVAL_COUNT
            coverage = [0.0] * INTERVAL_COUNT
            if isinstance(capture, (list, tuple)):
                for index in range(min(INTERVAL_COUNT, len(capture))):
                    values[index] = _finite_non_negative(capture[index])
                    if values[index] is not None:
                        coverage[index] = float(INTERVAL_SECONDS)
            else:
                parsed = _finite_non_negative(capture)
                if parsed is not None:
                    values[current_index] = parsed
                    coverage[current_index] = float(INTERVAL_SECONDS)
            return values, coverage, None, None
        nested = mapping.get(kind) or mapping.get(f"{kind}_capture")
        if nested is not None and _object_mapping(nested) is not None:
            mapping = _object_mapping(nested) or mapping

        if kind == "solar":
            value_names = (
                "interval_solar_kwh",
                "solar_interval_energy_kwh",
                "interval_energy_kwh",
                "solar_kwh",
                "energy_kwh",
            )
            coverage_names = ("interval_coverage_s", "solar_coverage_s", "coverage_s")
        else:
            value_names = (
                "interval_consumption_kwh",
                "consumption_interval_energy_kwh",
                "interval_energy_kwh",
                "consumption_kwh",
                "energy_kwh",
            )
            coverage_names = (
                "interval_coverage_s",
                "consumption_coverage_s",
                "coverage_s",
            )

        raw_values = None
        for name in value_names:
            if name in mapping:
                raw_values = mapping[name]
                break
        raw_coverage = None
        for name in coverage_names:
            if name in mapping:
                raw_coverage = mapping[name]
                break

        values: list[float | None] = [None] * INTERVAL_COUNT
        coverage: list[float] = [0.0] * INTERVAL_COUNT
        if isinstance(raw_values, (list, tuple)):
            for index in range(min(INTERVAL_COUNT, len(raw_values))):
                values[index] = _finite_non_negative(raw_values[index])
        else:
            parsed = _finite_non_negative(raw_values)
            if parsed is not None:
                values[current_index] = parsed
        if isinstance(raw_coverage, (list, tuple)):
            for index in range(min(INTERVAL_COUNT, len(raw_coverage))):
                parsed = _finite_non_negative(raw_coverage[index])
                coverage[index] = min(float(INTERVAL_SECONDS), parsed or 0.0)
        else:
            parsed = _finite_non_negative(raw_coverage)
            if parsed is not None:
                coverage[current_index] = min(float(INTERVAL_SECONDS), parsed)

        source = _safe_text(
            mapping.get("source")
            or mapping.get(f"{kind}_source")
            or mapping.get("telemetry_source"),
            max_length=64,
        )
        fallback_reason = _safe_text(
            mapping.get("fallback_reason") or mapping.get(f"{kind}_fallback_reason"),
            max_length=128,
        )
        return values, coverage, source, fallback_reason

    def _merge_actual_series(
        self,
        values: list[float | None],
        coverage: list[float],
        destination: list[float | None],
        coverage_destination: list[float],
        current_index: int,
    ) -> bool:
        changed = False
        for index in range(min(INTERVAL_COUNT, current_index + 1)):
            if self._closed[index]:
                continue
            value = values[index]
            if value is not None and destination[index] != value:
                destination[index] = value
                changed = True
            if coverage_destination[index] != coverage[index]:
                coverage_destination[index] = coverage[index]
                changed = True
        return changed

    def refresh_actual_partial(
        self,
        consumption_capture: Any = None,
        solar_capture: Any = None,
        *,
        now: datetime | None = None,
        at: datetime | None = None,
        consumption: Any = None,
        solar: Any = None,
        actual: Any = None,
        **kwargs: Any,
    ) -> bool:
        """Refresh only real samples, preserving every closed cell.

        ``consumption_capture`` and ``solar_capture`` may be profile trackers,
        their ``current_day_capture()`` result, or a simple dict.  Scalar
        ``consumption_kwh``/``solar_kwh`` kwargs update only the open interval.
        Missing telemetry remains ``None`` rather than being turned into zero.
        """
        current = self._as_local_datetime(now or at)
        day_changed = self._ensure_current_day(current)
        current_index = self._index_for_datetime(current)

        if consumption is not None:
            consumption_capture = consumption
        if solar is not None:
            solar_capture = solar
        if actual is not None:
            actual_mapping = _object_mapping(actual)
            if actual_mapping is not None:
                consumption_capture = actual_mapping.get(
                    "consumption", actual_mapping.get("consumption_capture", consumption_capture)
                )
                solar_capture = actual_mapping.get(
                    "solar", actual_mapping.get("solar_capture", solar_capture)
                )
        if "consumption_profile" in kwargs:
            consumption_capture = kwargs["consumption_profile"]
        if "solar_profile" in kwargs:
            solar_capture = kwargs["solar_profile"]

        # A combined capture is convenient for simple callers.
        if solar_capture is None and consumption_capture is not None:
            combined = _object_mapping(consumption_capture)
            if combined is not None and (
                "solar_kwh" in combined
                or "interval_solar_kwh" in combined
                or "solar" in combined
            ):
                solar_capture = combined.get("solar", combined)
                consumption_capture = combined.get("consumption", combined)

        consumption_capture = self._call_capture(consumption_capture, self._local_date)
        solar_capture = self._call_capture(solar_capture, self._local_date)
        consumption_values, consumption_coverage, consumption_source, consumption_fallback = (
            self._capture_series(consumption_capture, "consumption", current_index)
        )
        solar_values, solar_coverage, solar_source, solar_fallback = self._capture_series(
            solar_capture, "solar", current_index
        )

        # Explicit scalar kwargs are accepted even when a capture object is not
        # available.  They intentionally affect the open cell only.
        if "consumption_kwh" in kwargs:
            parsed = _finite_non_negative(kwargs["consumption_kwh"])
            if parsed is not None:
                consumption_values[current_index] = parsed
        if "solar_kwh" in kwargs:
            parsed = _finite_non_negative(kwargs["solar_kwh"])
            if parsed is not None:
                solar_values[current_index] = parsed
        for field_name, values in (
            ("consumption_coverage_s", consumption_coverage),
            ("solar_coverage_s", solar_coverage),
        ):
            if field_name in kwargs:
                parsed = _finite_non_negative(kwargs[field_name])
                if parsed is not None:
                    values[current_index] = min(float(INTERVAL_SECONDS), parsed)
        if "coverage_s" in kwargs:
            parsed = _finite_non_negative(kwargs["coverage_s"])
            if parsed is not None:
                consumption_coverage[current_index] = min(float(INTERVAL_SECONDS), parsed)
                solar_coverage[current_index] = min(float(INTERVAL_SECONDS), parsed)

        sources_before = tuple(self._sources.items())
        changed = day_changed
        changed |= self._merge_actual_series(
            consumption_values,
            consumption_coverage,
            self._actual_consumption_kwh,
            self._actual_consumption_coverage_s,
            current_index,
        )
        changed |= self._merge_actual_series(
            solar_values,
            solar_coverage,
            self._actual_solar_kwh,
            self._actual_solar_coverage_s,
            current_index,
        )
        if consumption_source:
            self._sources["consumption_actual"] = consumption_source
        if solar_source:
            self._sources["solar_actual"] = solar_source
        if consumption_fallback:
            self._sources["consumption_fallback_reason"] = consumption_fallback
        if solar_fallback:
            self._sources["solar_fallback_reason"] = solar_fallback

        closed_before_clock = tuple(self._closed)
        changed |= self._advance_clock(current)
        interval_closed = closed_before_clock != tuple(self._closed)
        changed |= sources_before != tuple(self._sources.items())
        if changed:
            self._dirty = True
            self._mark_public_change(
                "rollover" if day_changed else "activity",
                immediate=day_changed or interval_closed,
            )
            self.request_save(immediate=day_changed or interval_closed)
            self._notify_listeners()
        return bool(changed)

    # ------------------------------------------------------------------
    # Runtime decision diary
    # ------------------------------------------------------------------

    @staticmethod
    def _action_mask_from_decision(decision: Any) -> int:
        mapping = _object_mapping(decision)
        if mapping is None:
            return _mask_from_value(decision, _ACTION_NAME_TO_MASK, ACTION_MASK_ALL)
        for name in (
            "actual_action_mask",
            "action_mask",
            "actions",
            "action",
            "operation",
            "direction",
        ):
            if name in mapping:
                mask = _mask_from_value(mapping[name], _ACTION_NAME_TO_MASK, ACTION_MASK_ALL)
                if mask:
                    return mask
        mask = 0
        for keys, bit in (
            (("solar_to_battery_kwh", "pv_to_battery_kwh", "solar_excess_kwh"), ACTION_SOLAR_CHARGE),
            (("grid_to_battery_kwh", "grid_charge_kwh"), ACTION_GRID_CHARGE),
            (("battery_to_home_kwh", "battery_discharge_kwh"), ACTION_DISCHARGE),
        ):
            if any(
                (parsed := _finite_non_negative(mapping.get(key))) is not None
                and parsed > _EPSILON
                for key in keys
            ):
                mask |= bit
        if mask:
            return mask
        batteries = mapping.get("batteries") or mapping.get("battery_actions")
        if isinstance(batteries, Sequence) and not isinstance(batteries, (str, bytes)):
            for battery in batteries:
                mask |= DailyOperationTimelineManager._action_mask_from_decision(battery)
        return mask

    @staticmethod
    def _context_mask_from_decision(decision: Any) -> int:
        mapping = _object_mapping(decision)
        if mapping is None:
            return _mask_from_value(decision, _CONTEXT_NAME_TO_MASK, CONTEXT_MASK_ALL)
        for name in ("actual_context_mask", "context_mask", "contexts", "context"):
            if name in mapping:
                mask = _mask_from_value(mapping[name], _CONTEXT_NAME_TO_MASK, CONTEXT_MASK_ALL)
                if mask:
                    return mask
        mask = 0
        if mapping.get("setpoint_active") or mapping.get("charging_to_setpoint"):
            mask |= CONTEXT_SETPOINT
        if mapping.get("charge_delay") or mapping.get("delay_active") or mapping.get("is_delayed"):
            mask |= CONTEXT_CHARGE_DELAY
        return mask

    @staticmethod
    def _duration_mapping(value: Any) -> dict[str, float]:
        mapping = _object_mapping(value)
        if mapping is None:
            return {}
        result: dict[str, float] = {}
        for key, raw in mapping.items():
            name = _safe_text(key, max_length=32)
            parsed = _finite_non_negative(raw)
            if name is None or parsed is None:
                continue
            canonical = name.lower().replace("-", "_").replace(" ", "_")
            if canonical in _ACTION_NAME_TO_MASK:
                canonical = _ACTION_NAMES[_ACTION_NAME_TO_MASK[canonical]]
            elif canonical in ("setpoint", "charge_to_setpoint"):
                canonical = "setpoint"
            elif canonical in ("delay", "charge_delay"):
                canonical = "charge_delay"
            result[canonical] = min(86_400.0, parsed)
        return result

    def record_runtime_decision(
        self,
        decision: Any = None,
        *,
        at: datetime | None = None,
        timestamp: datetime | None = None,
        interval_index: int | None = None,
        action_mask: Any = None,
        context_mask: Any = None,
        grid_charge_decision: Any = None,
        duration_s: Any = None,
        observed_seconds_by_action: Any = None,
        observed_seconds_by_context: Any = None,
        simultaneous: bool = False,
        coexistence_mask: Any = None,
        source: Any = None,
        slot: Any = None,
        **kwargs: Any,
    ) -> bool:
        """Record one observed runtime decision in the open interval.

        Inputs may be a dict, dataclass, or lightweight object.  Actions are
        accumulated as a union, while coexistence is set only when the sample
        explicitly says actions were simultaneous (sequential actions in one
        quarter remain distinguishable through their durations).
        """
        event_time = self._as_local_datetime(at or timestamp)
        day_changed = False
        if event_time.date() != self._local_date:
            if event_time.date() != self._now().date():
                return False
            day_changed = self._ensure_current_day(event_time)
        closed_before_clock = tuple(self._closed)
        clock_changed = self._advance_clock(event_time)
        interval_closed = closed_before_clock != tuple(self._closed)
        index = self._index_for_datetime(event_time)
        if interval_index is not None:
            try:
                candidate = int(interval_index)
            except (TypeError, ValueError, OverflowError):
                candidate = index
            if 0 <= candidate < INTERVAL_COUNT:
                index = candidate
        if index != self._current_index or self._closed[index]:
            return False

        mapping = _object_mapping(decision) or {}
        # Explicit keyword arguments win over DTO fields, including zero masks.
        if action_mask is None:
            action_mask = kwargs.get("actual_action_mask", mapping.get("actual_action_mask"))
        if action_mask is None:
            action_mask = mapping.get("action_mask", mapping.get("actions"))
        parsed_action_mask = (
            _mask_from_value(action_mask, _ACTION_NAME_TO_MASK, ACTION_MASK_ALL)
            if action_mask is not None
            else self._action_mask_from_decision(mapping)
        )
        if context_mask is None:
            context_mask = kwargs.get("actual_context_mask", mapping.get("actual_context_mask"))
        if context_mask is None:
            context_mask = mapping.get("context_mask", mapping.get("contexts"))
        parsed_context_mask = (
            _mask_from_value(context_mask, _CONTEXT_NAME_TO_MASK, CONTEXT_MASK_ALL)
            if context_mask is not None
            else self._context_mask_from_decision(mapping)
        )
        mode_before = self._mode
        mode = _normalize_mode(mapping.get("mode", kwargs.get("mode", self._mode)))
        self._mode = mode if mode != "unknown" else self._mode
        if mode == "dynamic_pricing" or mode == "dynamic":
            parsed_context_mask |= CONTEXT_DYNAMIC_PRICE
        elif mode in {"time_slot", "timeslot", "time-slot"}:
            parsed_context_mask |= CONTEXT_TIME_SLOT
        elif _is_realtime_mode(mode):
            parsed_context_mask |= CONTEXT_REALTIME_PRICE

        grid_value = (
            grid_charge_decision
            if grid_charge_decision is not None
            else kwargs.get("grid_decision", mapping.get("grid_charge_decision"))
        )
        if grid_value is None:
            grid_value = mapping.get("grid_decision", mapping.get("charge_decision"))
        grid_decision = _normalize_grid_decision(grid_value)
        should_charge = mapping.get("should_charge", kwargs.get("should_charge"))
        if grid_value is None and should_charge is not None:
            grid_decision = _normalize_grid_decision(bool(should_charge))
        if (
            grid_value is None
            and parsed_action_mask & ACTION_GRID_CHARGE
        ):
            grid_decision = GRID_CHARGE_SCHEDULED
        if _is_realtime_mode(mode) and grid_value is None and not parsed_action_mask & ACTION_GRID_CHARGE:
            grid_decision = GRID_CHARGE_NOT_APPLICABLE

        if mapping.get("setpoint_active") or mapping.get("charging_to_setpoint"):
            parsed_context_mask |= CONTEXT_SETPOINT
        if mapping.get("charge_delay") or mapping.get("delay_active") or mapping.get("is_delayed"):
            parsed_context_mask |= CONTEXT_CHARGE_DELAY

        # Power/flow fields supply a safe fallback classification when callers
        # have not precomputed the mask.
        charge_power = _finite_non_negative(
            kwargs.get("charge_power_w", mapping.get("charge_power_w"))
        )
        discharge_power = _finite_non_negative(
            kwargs.get("discharge_power_w", mapping.get("discharge_power_w"))
        )
        soc_pct = _finite_non_negative(
            kwargs.get(
                "actual_soc_pct",
                mapping.get(
                    "actual_soc_pct", mapping.get("soc_pct", mapping.get("system_soc"))
                ),
            )
        )
        if soc_pct is not None:
            soc_pct = min(100.0, soc_pct)
        solar_to_battery = next(
            (
                parsed
                for key in ("solar_to_battery_kwh", "pv_to_battery_kwh", "solar_excess_kwh")
                if (parsed := _finite_non_negative(mapping.get(key))) is not None
                and parsed > _EPSILON
            ),
            0.0,
        )
        grid_to_battery = next(
            (
                parsed
                for key in ("grid_to_battery_kwh", "grid_charge_kwh")
                if (parsed := _finite_non_negative(mapping.get(key))) is not None
                and parsed > _EPSILON
            ),
            0.0,
        )
        battery_to_home = next(
            (
                parsed
                for key in ("battery_to_home_kwh", "battery_discharge_kwh")
                if (parsed := _finite_non_negative(mapping.get(key))) is not None
                and parsed > _EPSILON
            ),
            0.0,
        )
        if parsed_action_mask == ACTION_NONE:
            if solar_to_battery and solar_to_battery > _EPSILON:
                parsed_action_mask |= ACTION_SOLAR_CHARGE
            if grid_to_battery and grid_to_battery > _EPSILON:
                parsed_action_mask |= ACTION_GRID_CHARGE
            if (
                (discharge_power is not None and discharge_power > POWER_DEADBAND_W)
                or (battery_to_home and battery_to_home > _EPSILON)
            ):
                parsed_action_mask |= ACTION_DISCHARGE
            if (
                charge_power is not None
                and charge_power > POWER_DEADBAND_W
                and grid_decision == GRID_CHARGE_SCHEDULED
            ):
                parsed_action_mask |= ACTION_GRID_CHARGE
        if (
            grid_value is None
            and parsed_action_mask & ACTION_GRID_CHARGE
        ):
            grid_decision = GRID_CHARGE_SCHEDULED
        if grid_decision == GRID_CHARGE_SCHEDULED:
            parsed_context_mask |= CONTEXT_DYNAMIC_PRICE if mode in {"dynamic", "dynamic_pricing"} else 0

        raw_duration = duration_s
        if raw_duration is None:
            raw_duration = kwargs.get("observed_seconds", mapping.get("duration_s"))
        duration = _finite_non_negative(raw_duration, 0.0) or 0.0
        duration = min(86_400.0, duration)
        durations = self._duration_mapping(
            observed_seconds_by_action
            if observed_seconds_by_action is not None
            else mapping.get("observed_seconds_by_action")
        )
        if not durations and duration > 0.0:
            for bit, name in _ACTION_NAMES.items():
                if parsed_action_mask & bit:
                    durations[name] = duration
        context_durations = self._duration_mapping(
            observed_seconds_by_context
            if observed_seconds_by_context is not None
            else mapping.get("observed_seconds_by_context")
        )
        if not context_durations and duration > 0.0:
            for bit, name in (
                (CONTEXT_SETPOINT, "setpoint"),
                (CONTEXT_CHARGE_DELAY, "charge_delay"),
            ):
                if parsed_context_mask & bit:
                    context_durations[name] = duration

        cell = self._cells[index]
        old = cell.as_dict()
        cell.actual_action_mask |= parsed_action_mask
        cell.actual_context_mask |= parsed_context_mask
        explicit_coexistence = coexistence_mask
        if explicit_coexistence is None:
            explicit_coexistence = mapping.get("coexistence_mask", mapping.get("simultaneous_action_mask"))
        if explicit_coexistence is not None:
            cell.actual_coexistence_mask |= _mask_from_value(
                explicit_coexistence, _ACTION_NAME_TO_MASK, ACTION_MASK_ALL
            )
        elif simultaneous or mapping.get("simultaneous"):
            cell.actual_coexistence_mask |= parsed_action_mask
        if grid_decision != GRID_CHARGE_NOT_APPLICABLE:
            cell.actual_grid_charge_decision = grid_decision
        if charge_power is not None:
            cell.actual_charge_power_w = charge_power
        if discharge_power is not None:
            cell.actual_discharge_power_w = discharge_power
        if soc_pct is not None:
            cell.actual_soc_pct = soc_pct
        if (
            duration > 0.0
            and charge_power is not None
            and charge_power > POWER_DEADBAND_W
            and parsed_action_mask & (ACTION_SOLAR_CHARGE | ACTION_GRID_CHARGE)
        ):
            cell.actual_charge_to_battery_kwh = min(
                1_000_000.0,
                (cell.actual_charge_to_battery_kwh or 0.0)
                + charge_power * duration / 3_600_000.0,
            )
        if (
            duration > 0.0
            and discharge_power is not None
            and discharge_power > POWER_DEADBAND_W
            and parsed_action_mask & ACTION_DISCHARGE
        ):
            cell.actual_discharge_from_battery_kwh = min(
                1_000_000.0,
                (cell.actual_discharge_from_battery_kwh or 0.0)
                + discharge_power * duration / 3_600_000.0,
            )
        delay_value = kwargs.get("delay_until", mapping.get("delay_until"))
        if delay_value is not None:
            cell.actual_delay_until = self._format_delay_until(delay_value, event_time)
        elif not (parsed_context_mask & CONTEXT_CHARGE_DELAY):
            # A later runtime decision can release a delay that was recorded
            # earlier in the same quarter. Do not leave its unlock clock visible.
            cell.actual_delay_until = None
        source_value = source if source is not None else mapping.get("source", mapping.get("decision_source"))
        slot_value = slot if slot is not None else mapping.get("slot", mapping.get("slot_id"))
        if source_value is not None:
            cell.actual_source = _safe_text(source_value, max_length=64)
        if slot_value is not None:
            cell.actual_slot = _safe_text(slot_value, max_length=64)
        for name, value in durations.items():
            cell.observed_seconds_by_action[name] = min(
                86_400.0,
                cell.observed_seconds_by_action.get(name, 0.0) + value,
            )
        for name, value in context_durations.items():
            cell.observed_seconds_by_context[name] = min(
                86_400.0,
                cell.observed_seconds_by_context.get(name, 0.0) + value,
            )

        event = {
            "at": event_time.isoformat(),
            "action_mask": parsed_action_mask,
            "context_mask": parsed_context_mask,
            "grid_charge_decision": grid_decision,
            "duration_s": duration,
            "simultaneous": bool(
                explicit_coexistence is not None or simultaneous or mapping.get("simultaneous")
            ),
            "source": cell.actual_source,
            "slot": cell.actual_slot,
        }
        cell.decision_events.append(_json_safe(event))
        del cell.decision_events[:-MAX_DECISION_EVENTS_PER_CELL]

        if _is_realtime_mode(self._mode):
            # RT mode has no future plan; the current cell's planned overlay is
            # merely the last real activation so the UI can render it.
            cell.planned_action_mask = cell.actual_action_mask
            cell.planned_context_mask = cell.actual_context_mask
            cell.planned_coexistence_mask = cell.actual_coexistence_mask
            cell.planned_grid_charge_decision = cell.actual_grid_charge_decision
            cell.planned_charge_power_w = cell.actual_charge_power_w
            cell.planned_discharge_power_w = cell.actual_discharge_power_w
            cell.planned_delay_until = cell.actual_delay_until
            cell.planned_source = cell.actual_source
            cell.planned_slot = cell.actual_slot
        changed = old != cell.as_dict() or clock_changed or day_changed
        if changed:
            self._dirty = True
            mode_changed = mode_before != self._mode
            self._mark_public_change(
                "mode" if mode_changed else "rollover" if day_changed else "activity",
                immediate=mode_changed or day_changed or interval_closed,
            )
            self.request_save(
                immediate=mode_changed or day_changed or interval_closed
            )
            self._notify_listeners()
        return changed

    # ------------------------------------------------------------------
    # Future projection replacement
    # ------------------------------------------------------------------

    @staticmethod
    def _projection_items(projection: Any) -> list[Any]:
        if projection is None:
            return []
        if isinstance(projection, Sequence) and not isinstance(projection, (str, bytes, bytearray)):
            return list(projection)
        mapping = _object_mapping(projection)
        if mapping is None:
            return []
        for name in ("intervals", "flows", "projected_intervals", "planned_intervals", "timeline"):
            value = mapping.get(name)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                return list(value)
        series = mapping.get("series")
        operations = mapping.get("operations")
        if isinstance(series, Mapping) or isinstance(operations, Mapping):
            return [{"index": index} for index in range(INTERVAL_COUNT)]
        return []

    def _projection_index(self, item: Any, fallback: int) -> int | None:
        raw_index = _value(item, "index", "interval_index", "bin_index", default=None)
        if raw_index is not None:
            try:
                index = int(raw_index)
            except (TypeError, ValueError, OverflowError):
                index = fallback
            return index if 0 <= index < INTERVAL_COUNT else None
        start = _parse_datetime(_value(item, "start", "interval_start", default=None), self._timezone())
        if start is not None:
            return self._index_for_datetime(start.astimezone(self._timezone()))
        return fallback if 0 <= fallback < INTERVAL_COUNT else None

    @staticmethod
    def _projection_extension_items(projection: Any) -> list[Any]:
        """Return the optional cross-midnight dashboard projection items."""
        mapping = _object_mapping(projection)
        if mapping is None:
            return []
        for name in (
            "extended_intervals",
            "extended_projection",
            "forecast_extension",
        ):
            value = mapping.get(name)
            if isinstance(value, Sequence) and not isinstance(
                value, (str, bytes, bytearray)
            ):
                return list(value)
        return []

    def _normalise_projection_extension_item(
        self, item: Any
    ) -> dict[str, Any] | None:
        """Keep one bounded, JSON-safe interval after the local day."""
        mapping = _object_mapping(item)
        if mapping is None:
            return None
        start = _parse_datetime(
            mapping.get("start", mapping.get("interval_start")), self._timezone()
        )
        end = _parse_datetime(
            mapping.get("end", mapping.get("interval_end")), self._timezone()
        )
        if start is None or end is None or end <= start:
            return None
        local_start = start.astimezone(self._timezone())
        local_end = end.astimezone(self._timezone())
        extension_start = datetime.combine(
            self._local_date + timedelta(days=1),
            time.min,
            tzinfo=self._timezone(),
        )
        extension_end = extension_start + timedelta(hours=EXTENDED_HORIZON_HOURS)
        if (
            local_start < extension_start
            or local_start >= extension_end
            or local_end > extension_end
        ):
            return None

        raw_index = mapping.get(
            "extension_index",
            mapping.get("index", local_start.hour * 4 + local_start.minute // INTERVAL_MINUTES),
        )
        try:
            extension_index = int(raw_index)
        except (TypeError, ValueError, OverflowError):
            extension_index = local_start.hour * 4 + local_start.minute // INTERVAL_MINUTES
        if not 0 <= extension_index < EXTENDED_INTERVAL_COUNT:
            extension_index = local_start.hour * 4 + local_start.minute // INTERVAL_MINUTES
        if not 0 <= extension_index < EXTENDED_INTERVAL_COUNT:
            return None

        result: dict[str, Any] = {
            "index": extension_index,
            "extension_index": extension_index,
            "start": local_start.isoformat(),
            "end": local_end.isoformat(),
        }
        for name in (
            "solar_kwh",
            "consumption_kwh",
            "solar_to_battery_kwh",
            "grid_to_battery_kwh",
            "battery_to_home_kwh",
            "grid_to_home_kwh",
            "solar_to_home_kwh",
            "charge_to_battery_kwh",
            "discharge_from_battery_kwh",
            "stored_energy_end_kwh",
            "soc_end_pct",
            "charge_power_w",
            "discharge_power_w",
        ):
            if name in mapping:
                parsed = _finite_non_negative(mapping.get(name))
                if parsed is not None:
                    result[name] = parsed
        for name in (
            "action_mask",
            "planned_action_mask",
            "context_mask",
            "planned_context_mask",
            "coexistence_mask",
            "planned_coexistence_mask",
        ):
            if name in mapping:
                result[name] = _safe_mask(
                    mapping.get(name),
                    ACTION_MASK_ALL
                    if "action" in name or "coexistence" in name
                    else CONTEXT_MASK_ALL,
                )
        for name in (
            "grid_charge_decision",
            "planned_grid_charge_decision",
        ):
            if name in mapping:
                result[name] = _normalize_grid_decision(mapping.get(name))
        for name in ("source", "slot"):
            if name in mapping:
                result[name] = _safe_text(mapping.get(name), max_length=64)
        if mapping.get("delay_until") is not None:
            result["delay_until"] = self._format_delay_until(
                mapping.get("delay_until"), local_start
            )
        for name in ("setpoint_active", "delay_active", "simultaneous"):
            if name in mapping:
                result[name] = bool(mapping.get(name))
        return result

    @staticmethod
    def _projection_array_value(
        projection: Any,
        section_names: Sequence[str],
        names: Sequence[str],
        index: int,
    ) -> Any:
        mapping = _object_mapping(projection)
        if mapping is None:
            return None
        for section_name in section_names:
            section = mapping.get(section_name)
            if not isinstance(section, Mapping):
                continue
            for name in names:
                values = section.get(name)
                if isinstance(values, (list, tuple)) and index < len(values):
                    return values[index]
        return None

    def _format_delay_until(self, value: Any, reference: datetime) -> str | None:
        parsed = _parse_datetime(value, self._timezone())
        if parsed is not None:
            local = parsed.astimezone(self._timezone())
            if local.date() == self._local_date:
                return local.strftime("%H:%M")
            return local.isoformat()
        text = _safe_text(value, max_length=32)
        return text

    def rebuild_future_projection(
        self,
        projection: Any = None,
        *,
        now: datetime | None = None,
        at: datetime | None = None,
        mode: Any = None,
        evaluated_at: Any = None,
        plan_evaluated_at: Any = None,
        stale: bool | None = None,
        stale_reason: Any = None,
        **kwargs: Any,
    ) -> bool:
        """Replace only the open/current and future projection.

        ``projection`` can be a list of flow DTOs or a mapping containing the
        contract's ``series`` and ``operations`` arrays.  For real-time price
        mode all future planned values are forcibly cleared; real activations
        are retained through ``record_runtime_decision``.
        """
        if projection is None:
            projection = kwargs.get("future_projection", kwargs.get("plan"))
        current = self._as_local_datetime(now or at)
        day_changed = False
        if current.date() != self._local_date:
            day_changed = self._ensure_current_day(current)
        clock_changed = self._advance_clock(current)
        index = self._current_index
        old_projection = self._projection_signature()
        old_sources = tuple(self._sources.items())
        old_plan_evaluated_at = self._plan_evaluated_at
        old_mode = self._mode
        old_stale = self._stale
        old_stale_reason = self._stale_reason
        mapping = _object_mapping(projection)
        selected_mode = mode
        if selected_mode is None and mapping is not None:
            selected_mode = mapping.get("mode", mapping.get("pricing_mode"))
        if selected_mode is None:
            selected_mode = self._controller_mode()
        normalized_mode = _normalize_mode(selected_mode)
        if normalized_mode == "unknown" and self._mode != "unknown":
            normalized_mode = self._mode
        self._mode = normalized_mode

        if evaluated_at is None:
            evaluated_at = plan_evaluated_at
        if evaluated_at is None and mapping is not None:
            evaluated_at = mapping.get("plan_evaluated_at", mapping.get("evaluated_at"))
        if evaluated_at is not None:
            self._plan_evaluated_at = _as_iso(evaluated_at)
        elif projection is not None:
            self._plan_evaluated_at = current.isoformat()

        if stale is None:
            stale = bool(mapping.get("stale", False)) if mapping is not None else False
        self._stale = bool(stale)
        if stale_reason is None and mapping is not None:
            stale_reason = mapping.get("stale_reason")
        self._stale_reason = _safe_text(stale_reason, max_length=128)

        if mapping is not None:
            sources = mapping.get("sources")
            if isinstance(sources, Mapping):
                for key in self._sources:
                    if key in sources:
                        self._sources[key] = _safe_text(sources[key], max_length=128)
            for key in self._sources:
                if key in mapping and key not in {"solar_actual", "consumption_actual"}:
                    self._sources[key] = _safe_text(mapping[key], max_length=128)
        if kwargs.get("source") is not None:
            self._sources["operation_plan"] = _safe_text(kwargs["source"], max_length=128)

        self._extended_projection = []
        for cell_index in range(index, INTERVAL_COUNT):
            if self._closed[cell_index]:
                continue
            self._cells[cell_index].clear_planned()
            self._planned_solar_kwh[cell_index] = None
            self._planned_consumption_kwh[cell_index] = None
            self._planned_stored_energy_end_kwh[cell_index] = None

        if _is_realtime_mode(normalized_mode):
            # The current cell shows a real activation if one exists.  No
            # forecast, action, gray decision, or slot is created after it.
            for future_index in range(index + 1, INTERVAL_COUNT):
                if not self._closed[future_index]:
                    self._cells[future_index].clear_planned()
            current_cell = self._cells[index]
            current_cell.planned_action_mask = current_cell.actual_action_mask
            current_cell.planned_context_mask = current_cell.actual_context_mask
            current_cell.planned_coexistence_mask = current_cell.actual_coexistence_mask
            current_cell.planned_grid_charge_decision = current_cell.actual_grid_charge_decision
            current_cell.planned_charge_power_w = current_cell.actual_charge_power_w
            current_cell.planned_discharge_power_w = current_cell.actual_discharge_power_w
            current_cell.planned_delay_until = current_cell.actual_delay_until
            current_cell.planned_source = current_cell.actual_source
            current_cell.planned_slot = current_cell.actual_slot
            self._sources["operation_plan"] = "realtime_runtime_only"
        else:
            items = self._projection_items(projection)
            for fallback, item in enumerate(items):
                item_mapping = _object_mapping(item) or {}
                item_index = self._projection_index(item, fallback)
                if item_index is None or item_index < index or self._closed[item_index]:
                    continue
                cell = self._cells[item_index]

                def item_value(
                    *names: str,
                    default: Any = None,
                    _item_mapping: Mapping[str, Any] = item_mapping,
                    _item_index: int = item_index,
                ) -> Any:
                    for name in names:
                        if name in _item_mapping:
                            return _item_mapping[name]
                    return self._projection_array_value(
                        projection,
                        ("series", "operations"),
                        names,
                        _item_index,
                    ) if mapping is not None else default

                solar_value = _finite_non_negative(
                    item_value("solar_forecast_kwh", "solar_kwh", default=None)
                )
                consumption_value = _finite_non_negative(
                    item_value("consumption_forecast_kwh", "consumption_kwh", default=None)
                )
                if solar_value is not None:
                    self._planned_solar_kwh[item_index] = solar_value
                if consumption_value is not None:
                    self._planned_consumption_kwh[item_index] = consumption_value
                stored_value = _finite_non_negative(
                    item_value("stored_energy_end_kwh", "stored_energy_kwh", default=None)
                )
                if stored_value is not None:
                    self._planned_stored_energy_end_kwh[item_index] = stored_value
                cell.planned_stored_energy_end_kwh = stored_value
                for field_name, aliases in (
                    (
                        "planned_solar_to_battery_kwh",
                        ("solar_to_battery_kwh", "planned_solar_to_battery_kwh"),
                    ),
                    (
                        "planned_grid_to_battery_kwh",
                        ("grid_to_battery_kwh", "planned_grid_to_battery_kwh"),
                    ),
                    (
                        "planned_battery_to_home_kwh",
                        ("battery_to_home_kwh", "planned_battery_to_home_kwh"),
                    ),
                    (
                        "planned_grid_to_home_kwh",
                        ("grid_to_home_kwh", "planned_grid_to_home_kwh"),
                    ),
                    (
                        "planned_solar_to_home_kwh",
                        ("solar_to_home_kwh", "planned_solar_to_home_kwh"),
                    ),
                    (
                        "planned_soc_end_pct",
                        ("soc_end_pct", "stored_soc_end_pct", "planned_soc_end_pct"),
                    ),
                ):
                    setattr(
                        cell,
                        field_name,
                        _finite_non_negative(item_value(*aliases, default=None)),
                    )

                action_value = item_value("planned_action_mask", "action_mask", default=None)
                action_mask = (
                    _mask_from_value(action_value, _ACTION_NAME_TO_MASK, ACTION_MASK_ALL)
                    if action_value is not None
                    else 0
                )
                flow_fields = (
                    ("solar_to_battery_kwh", ACTION_SOLAR_CHARGE),
                    ("grid_to_battery_kwh", ACTION_GRID_CHARGE),
                    ("battery_to_home_kwh", ACTION_DISCHARGE),
                )
                for field_name, bit in flow_fields:
                    flow = _finite_non_negative(item_value(field_name, default=None))
                    if flow is not None and flow > _EPSILON:
                        action_mask |= bit
                cell.planned_action_mask = action_mask
                context_value = item_value("planned_context_mask", "context_mask", default=None)
                context_mask = (
                    _mask_from_value(context_value, _CONTEXT_NAME_TO_MASK, CONTEXT_MASK_ALL)
                    if context_value is not None
                    else CONTEXT_NONE
                )
                if normalized_mode in {"dynamic", "dynamic_pricing"}:
                    context_mask |= CONTEXT_DYNAMIC_PRICE
                elif normalized_mode in {"time_slot", "timeslot", "time-slot"}:
                    context_mask |= CONTEXT_TIME_SLOT
                if item_value("setpoint_active", "charging_to_setpoint", default=False):
                    context_mask |= CONTEXT_SETPOINT
                if item_value("delay_active", "charge_delay", default=False):
                    context_mask |= CONTEXT_CHARGE_DELAY
                cell.planned_context_mask = context_mask
                coexistence_value = item_value(
                    "planned_coexistence_mask", "coexistence_mask", "simultaneous_action_mask", default=None
                )
                if coexistence_value is not None:
                    cell.planned_coexistence_mask = _mask_from_value(
                        coexistence_value, _ACTION_NAME_TO_MASK, ACTION_MASK_ALL
                    )
                elif item_value("simultaneous", default=False):
                    cell.planned_coexistence_mask = action_mask
                elif action_mask.bit_count() > 1:
                    # A single authoritative projected flow can contain
                    # multiple positive physical directions (for example
                    # solar plus grid charging); those are simultaneous in
                    # the rendering cell.
                    cell.planned_coexistence_mask = action_mask
                cell.planned_grid_charge_decision = _normalize_grid_decision(
                    item_value("planned_grid_charge_decision", "grid_charge_decision", "grid_decision", default=None)
                )
                cell.planned_charge_power_w = _finite_non_negative(
                    item_value("charge_power_w", "planned_charge_power_w", default=None)
                )
                cell.planned_discharge_power_w = _finite_non_negative(
                    item_value("discharge_power_w", "planned_discharge_power_w", default=None)
                )
                delay_value = item_value("delay_until", "planned_delay_until", default=None)
                cell.planned_delay_until = (
                    self._format_delay_until(delay_value, current)
                    if delay_value is not None
                    else None
                )
                cell.planned_source = _safe_text(
                    item_value("source", "decision_source", default=None), max_length=64
                )
                cell.planned_slot = _safe_text(
                    item_value("slot", "slot_id", default=None), max_length=64
                )

            extension_items: list[dict[str, Any]] = []
            for raw_item in self._projection_extension_items(projection):
                parsed_item = self._normalise_projection_extension_item(raw_item)
                if parsed_item is not None:
                    extension_items.append(parsed_item)
            extension_items.sort(key=lambda value: value["extension_index"])
            self._extended_projection = extension_items[:EXTENDED_INTERVAL_COUNT]
            if self._sources.get("operation_plan") is None:
                self._sources["operation_plan"] = "projection"

        projection_changed = old_projection != self._projection_signature()
        if not projection_changed:
            # The planner commonly stamps an otherwise identical projection
            # with ``now``. That timestamp is diagnostic metadata, not a new
            # plan, and must not turn the control loop into a Store writer or
            # force a dashboard publication every time it is evaluated.
            self._plan_evaluated_at = old_plan_evaluated_at
        plan_timestamp_changed = old_plan_evaluated_at != self._plan_evaluated_at
        changed = (
            day_changed
            or clock_changed
            or projection_changed
            or plan_timestamp_changed
            or old_sources != tuple(self._sources.items())
        )
        if changed:
            self._dirty = True
            mode_changed = old_mode != self._mode
            stale_changed = old_stale != self._stale or old_stale_reason != self._stale_reason
            self._mark_public_change(
                "mode" if mode_changed else "stale" if stale_changed else "plan",
                immediate=True,
            )
            self.request_save(immediate=True)
            self._notify_listeners()
        return changed

    def _projection_signature(self) -> tuple[Any, ...]:
        return (
            tuple(self._planned_solar_kwh),
            tuple(self._planned_consumption_kwh),
            tuple(self._planned_stored_energy_end_kwh),
            tuple(
                (
                    cell.planned_action_mask,
                    cell.planned_context_mask,
                    cell.planned_coexistence_mask,
                    cell.planned_grid_charge_decision,
                    cell.planned_charge_power_w,
                    cell.planned_discharge_power_w,
                    cell.planned_solar_to_battery_kwh,
                    cell.planned_grid_to_battery_kwh,
                    cell.planned_battery_to_home_kwh,
                    cell.planned_grid_to_home_kwh,
                    cell.planned_solar_to_home_kwh,
                    cell.planned_stored_energy_end_kwh,
                    cell.planned_soc_end_pct,
                    cell.planned_delay_until,
                    cell.planned_source,
                    cell.planned_slot,
                )
                for cell in self._cells
            ),
            tuple(
                json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
                for item in self._extended_projection
            ),
            self._mode,
            self._stale,
            self._stale_reason,
        )

    # ------------------------------------------------------------------
    # JSON-safe snapshot and persistence
    # ------------------------------------------------------------------

    def _store_payload(self) -> dict[str, Any]:
        now = self._now()
        return _json_safe(
            {
                "schema_version": DAILY_TIMELINE_SCHEMA_VERSION,
                "interval_minutes": INTERVAL_MINUTES,
                "interval_count": INTERVAL_COUNT,
                "local_date": self._local_date.isoformat(),
                "date": self._local_date.isoformat(),
                "timezone": self._configured_timezone_name(),
                "configuration_fingerprint": self.configuration_fingerprint(),
                "closed": list(self._closed),
                "actual": {
                    "solar_kwh": list(self._actual_solar_kwh),
                    "consumption_kwh": list(self._actual_consumption_kwh),
                    "solar_coverage_s": list(self._actual_solar_coverage_s),
                    "consumption_coverage_s": list(self._actual_consumption_coverage_s),
                },
                "planned": {
                    "solar_kwh": list(self._planned_solar_kwh),
                    "consumption_kwh": list(self._planned_consumption_kwh),
                    "stored_energy_end_kwh": list(self._planned_stored_energy_end_kwh),
                },
                "extended_projection": list(self._extended_projection),
                "cells": [cell.as_dict() for cell in self._cells],
                "metadata": {
                    "mode": self._mode,
                    "plan_evaluated_at": self._plan_evaluated_at,
                    "generated_at": self._generated_at or now.isoformat(),
                    "stale": self._stale,
                    "stale_reason": self._stale_reason,
                    "sources": dict(self._sources),
                    "setpoint": dict(self._setpoint_info),
                    "delay": dict(self._delay_info),
                    "freshness": dict(self._freshness_info),
                },
                "last_open_sample": {
                    "index": self._current_index,
                    "solar_kwh": self._actual_solar_kwh[self._current_index],
                    "consumption_kwh": self._actual_consumption_kwh[self._current_index],
                    "solar_coverage_s": self._actual_solar_coverage_s[self._current_index],
                    "consumption_coverage_s": self._actual_consumption_coverage_s[self._current_index],
                },
            }
        )

    @staticmethod
    def _parse_numeric_list(raw: Any, *, allow_none: bool = False) -> list[float | None] | None:
        values = _fit_list(raw, None)
        if values is None:
            return None
        result: list[float | None] = []
        for value in values:
            if value is None and allow_none:
                result.append(None)
                continue
            parsed = _finite_non_negative(value)
            if parsed is None:
                return None
            result.append(parsed)
        return result

    @staticmethod
    def _parse_cell(raw: Any) -> _TimelineCell | None:
        if not isinstance(raw, Mapping):
            return None
        cell = _TimelineCell()
        for name in (
            "actual_action_mask",
            "planned_action_mask",
            "actual_context_mask",
            "planned_context_mask",
            "actual_coexistence_mask",
            "planned_coexistence_mask",
        ):
            setattr(
                cell,
                name,
                _safe_mask(
                    raw.get(name, 0),
                    ACTION_MASK_ALL if "action" in name or "coexistence" in name else CONTEXT_MASK_ALL,
                ),
            )
        for name in (
            "actual_grid_charge_decision",
            "planned_grid_charge_decision",
        ):
            setattr(cell, name, _normalize_grid_decision(raw.get(name)))
        for name in (
            "actual_charge_power_w",
            "planned_charge_power_w",
            "actual_discharge_power_w",
            "planned_discharge_power_w",
            "actual_charge_to_battery_kwh",
            "actual_discharge_from_battery_kwh",
            "actual_soc_pct",
        ):
            parsed = _finite_non_negative(raw.get(name))
            setattr(cell, name, parsed)
        for name in (
            "planned_solar_to_battery_kwh",
            "planned_grid_to_battery_kwh",
            "planned_battery_to_home_kwh",
            "planned_grid_to_home_kwh",
            "planned_solar_to_home_kwh",
            "planned_stored_energy_end_kwh",
            "planned_soc_end_pct",
        ):
            setattr(cell, name, _finite_non_negative(raw.get(name)))
        for name in (
            "actual_delay_until",
            "planned_delay_until",
            "actual_source",
            "planned_source",
            "actual_slot",
            "planned_slot",
        ):
            setattr(cell, name, _safe_text(raw.get(name), max_length=64))
        for field_name in ("observed_seconds_by_action", "observed_seconds_by_context"):
            value = raw.get(field_name, {})
            parsed_mapping = DailyOperationTimelineManager._duration_mapping(value)
            setattr(cell, field_name, parsed_mapping)
        events = raw.get("decision_events", [])
        if isinstance(events, list):
            safe_events = []
            for event in events[-MAX_DECISION_EVENTS_PER_CELL:]:
                if isinstance(event, Mapping):
                    safe_events.append(
                        _json_safe(
                            {
                                "at": _safe_text(event.get("at"), max_length=64),
                                "action_mask": _safe_mask(event.get("action_mask"), ACTION_MASK_ALL),
                                "context_mask": _safe_mask(event.get("context_mask"), CONTEXT_MASK_ALL),
                                "grid_charge_decision": _normalize_grid_decision(
                                    event.get("grid_charge_decision")
                                ),
                                "duration_s": _finite_non_negative(event.get("duration_s"), 0.0) or 0.0,
                                "simultaneous": bool(event.get("simultaneous", False)),
                                "source": _safe_text(event.get("source"), max_length=64),
                                "slot": _safe_text(event.get("slot"), max_length=64),
                            }
                        )
                    )
            cell.decision_events = safe_events
        return cell

    async def async_load(self) -> bool:
        """Restore a valid current-day diary; never propagate Store corruption."""
        try:
            loaded = self._store.async_load()
            data = await loaded if inspect.isawaitable(loaded) else loaded
        except Exception as exc:  # noqa: BLE001
            self._reset_arrays(self._now().date())
            self._last_error = f"load: {exc}"
            self._restore_status = "error"
            self._loaded = True
            _LOGGER.warning("Daily operation timeline: failed to load Store: %s", exc)
            return False

        self._loaded = True
        current = self._now()
        expected_date = current.date()
        expected_timezone = self._configured_timezone_name()
        expected_fingerprint = self.configuration_fingerprint()
        if not isinstance(data, Mapping):
            self._reset_arrays(expected_date)
            self._last_error = "load: invalid_store"
            self._restore_status = "empty" if data is None else "corrupt"
            return False
        if (
            data.get("schema_version") != DAILY_TIMELINE_SCHEMA_VERSION
            or data.get("interval_count") != INTERVAL_COUNT
            or str(data.get("local_date", data.get("date", ""))) != expected_date.isoformat()
            or data.get("timezone") != expected_timezone
        ):
            self._reset_arrays(expected_date)
            self._last_error = "load: identity_mismatch"
            self._restore_status = "discarded"
            return False
        projection_configuration_changed = (
            data.get("configuration_fingerprint") != expected_fingerprint
        )

        closed = _fit_list(data.get("closed"), False)
        cells_raw = data.get("cells")
        actual = data.get("actual")
        planned = data.get("planned")
        if (
            closed is None
            or not isinstance(cells_raw, list)
            or len(cells_raw) != INTERVAL_COUNT
            or not isinstance(actual, Mapping)
            or not isinstance(planned, Mapping)
        ):
            self._reset_arrays(expected_date)
            self._last_error = "load: invalid_shape"
            self._restore_status = "corrupt"
            return False
        actual_solar = self._parse_numeric_list(actual.get("solar_kwh"), allow_none=True)
        actual_consumption = self._parse_numeric_list(
            actual.get("consumption_kwh"), allow_none=True
        )
        solar_coverage = self._parse_numeric_list(actual.get("solar_coverage_s"))
        consumption_coverage = self._parse_numeric_list(actual.get("consumption_coverage_s"))
        planned_solar = self._parse_numeric_list(planned.get("solar_kwh"), allow_none=True)
        planned_consumption = self._parse_numeric_list(
            planned.get("consumption_kwh"), allow_none=True
        )
        stored_end = self._parse_numeric_list(
            planned.get("stored_energy_end_kwh"), allow_none=True
        )
        if any(
            value is None
            for value in (
                actual_solar,
                actual_consumption,
                solar_coverage,
                consumption_coverage,
                planned_solar,
                planned_consumption,
                stored_end,
            )
        ):
            self._reset_arrays(expected_date)
            self._last_error = "load: invalid_series"
            self._restore_status = "corrupt"
            return False

        parsed_cells: list[_TimelineCell] = []
        for raw_cell in cells_raw:
            parsed_cell = self._parse_cell(raw_cell)
            # A damaged individual cell should not discard an otherwise valid
            # current-day diary; its evidence is conservatively empty.
            parsed_cells.append(parsed_cell or _TimelineCell())
        self._local_date = expected_date
        restored_extension: list[dict[str, Any]] = []
        raw_extension = data.get("extended_projection")
        if isinstance(raw_extension, list):
            for raw_item in raw_extension[:EXTENDED_INTERVAL_COUNT]:
                parsed_item = self._normalise_projection_extension_item(raw_item)
                if parsed_item is not None:
                    restored_extension.append(parsed_item)
        restored_extension.sort(key=lambda value: value["extension_index"])
        self._closed = [bool(value) for value in closed]
        self._cells = parsed_cells
        self._actual_solar_kwh = actual_solar
        self._actual_consumption_kwh = actual_consumption
        self._actual_solar_coverage_s = solar_coverage
        self._actual_consumption_coverage_s = consumption_coverage
        self._planned_solar_kwh = planned_solar
        self._planned_consumption_kwh = planned_consumption
        self._planned_stored_energy_end_kwh = stored_end
        self._extended_projection = restored_extension
        metadata = data.get("metadata")
        if isinstance(metadata, Mapping):
            self._mode = _normalize_mode(metadata.get("mode", self._controller_mode()))
            self._plan_evaluated_at = _as_iso(metadata.get("plan_evaluated_at"))
            self._generated_at = _as_iso(metadata.get("generated_at"))
            self._stale = bool(metadata.get("stale", False))
            self._stale_reason = _safe_text(metadata.get("stale_reason"), max_length=128)
            sources = metadata.get("sources")
            if isinstance(sources, Mapping):
                for key in self._sources:
                    self._sources[key] = _safe_text(sources.get(key), max_length=128)
            self._setpoint_info = self._safe_metadata(metadata.get("setpoint"))
            self._delay_info = self._safe_metadata(metadata.get("delay"))
            self._freshness_info = self._safe_metadata(metadata.get("freshness"))
        if projection_configuration_changed:
            # A config update can legitimately reload this integration during
            # the day. Do not erase actual battery activity that occurred
            # before the reload; only discard the forecast derived from the
            # previous configuration. Setup immediately builds a new one.
            self._clear_projection()
        self._configuration_fingerprint = expected_fingerprint
        self._current_index = self._index_for_datetime(current)
        self._current_progress = self._progress_for_datetime(current)
        self._advance_clock(current)
        self._dirty = False
        self._last_error = None
        self._restore_status = "restored"
        self._mark_public_change("recovery", immediate=True)
        if projection_configuration_changed:
            # Rewrite the stored projection identity so later reloads do not
            # keep treating this otherwise restored diary as stale.
            self.request_save()
        return True

    async def async_restore(self) -> bool:
        """Alias used by setup code that calls restoration explicitly."""
        return await self.async_load()

    def invalidate_if_configuration_changed(self) -> bool:
        """Discard only stale projections when configuration changes."""
        current = self.configuration_fingerprint()
        if current == self._configuration_fingerprint:
            return False
        self._clear_projection()
        self._configuration_fingerprint = current
        self._last_error = None
        self._mark_public_change("plan", immediate=True)
        self.request_save(immediate=True)
        self._notify_listeners()
        return True

    async def async_save(self) -> bool:
        """Write the bounded payload without ever blocking the control caller."""
        revision = self._save_revision
        payload = self._store_payload()
        try:
            saved = self._store.async_save(payload)
            if inspect.isawaitable(saved):
                await saved
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"save: {exc}"
            _LOGGER.error("Daily operation timeline: failed to save Store: %s", exc)
            return False
        self._configuration_fingerprint = self.configuration_fingerprint()
        # A mutation may have happened while Store was awaiting I/O.  Its
        # payload was not part of this write, so retain dirty state and queue
        # a follow-up instead of silently losing the final runtime evidence.
        if self._save_revision == revision:
            self._dirty = False
        else:
            self._dirty = True
            self._save_reschedule_requested = True
        self._last_error = None
        self._save_count += 1
        self._last_save_monotonic = monotonic()
        self._save_times.append(self._last_save_monotonic)
        return True

    def _reschedule_after_save(self, _task: asyncio.Task) -> None:
        if self._save_reschedule_requested:
            self._schedule_pending_save()

    def _schedule_pending_save(self) -> None:
        """Start one follow-up save after an in-flight write has completed."""
        if not self._save_reschedule_requested or not self._dirty:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._save_task is not None and not self._save_task.done():
            return
        self._save_reschedule_requested = False
        self._save_task = loop.create_task(self.async_save())

    def request_save(self, *, immediate: bool = False) -> None:
        """Schedule one debounced background save and return immediately."""
        self._dirty = True
        self._save_revision += 1
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if immediate:
            if self._save_handle is not None:
                self._save_handle.cancel()
                self._save_handle = None
            if self._save_task is None or self._save_task.done():
                self._save_task = loop.create_task(self.async_save())
            else:
                self._save_reschedule_requested = True
                self._save_task.add_done_callback(self._reschedule_after_save)
            return
        if self._save_task is not None and not self._save_task.done():
            self._save_reschedule_requested = True
            self._save_task.add_done_callback(self._reschedule_after_save)
            return
        if self._save_handle is not None and not self._save_handle.cancelled():
            return

        def schedule() -> None:
            self._save_handle = None
            if self._save_task is None or self._save_task.done():
                self._save_task = loop.create_task(self.async_save())

        self._save_handle = loop.call_later(self._debounce_seconds, schedule)

    async def async_save_all(self) -> bool:
        """Flush pending debounced state, suitable for unload/shutdown."""
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_handle = None
        while True:
            task = self._save_task
            if task is not None and not task.done():
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                # Give the task completion callback a chance to enqueue a
                # coalesced follow-up write before deciding the flush is done.
                await asyncio.sleep(0)
                continue
            if not self._dirty:
                return True
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return await self.async_save()
            self._save_task = loop.create_task(self.async_save())

    async def async_shutdown(self) -> bool:
        return await self.async_save_all()

    async def async_unload(self) -> bool:
        return await self.async_save_all()

    def snapshot(self) -> dict[str, Any]:
        """Synchronous adapter used by sensors and diagnostics."""
        return self.build_public_snapshot()

    get_snapshot = snapshot
    current_snapshot = snapshot
    to_dict = snapshot

    def build_public_dto(self) -> DailyOperationTimelineSnapshot:
        return DailyOperationTimelineSnapshot.from_dict(self.build_public_snapshot())

    def _public_action_durations(self) -> list[dict[str, float]]:
        return [
            {
                name: round(max(0.0, float(cell.observed_seconds_by_action.get(name, 0.0))), 3)
                for name in _ACTION_NAMES.values()
                if _finite_non_negative(cell.observed_seconds_by_action.get(name), 0.0) is not None
            }
            for cell in self._cells
        ]

    def _public_intervals(self, grid: list[dict[str, Any]]) -> list[dict[str, Any]]:
        intervals: list[dict[str, Any]] = []
        for index, item in enumerate(grid):
            if self._closed[index]:
                state = STATE_PAST
            elif index == self._current_index:
                state = STATE_CURRENT
            else:
                state = STATE_FUTURE
            if item["dst_skipped"]:
                dst_status = DST_SKIPPED
            elif item["dst_repeated"]:
                dst_status = DST_REPEATED
            else:
                dst_status = DST_NORMAL
            intervals.append(
                {
                    "index": index,
                    "label": item["label"],
                    "start": item["start"],
                    "end": item["end"],
                    "duration_seconds": item["duration_s"],
                    "state": state,
                    "dst_status": dst_status,
                    "dst_skipped": bool(item["dst_skipped"]),
                    "dst_repeated": bool(item["dst_repeated"]),
                    "occurrences": [],
                    "flow": None,
                }
            )
        return intervals

    def build_public_snapshot(self, *, as_dto: bool = False) -> dict[str, Any] | DailyOperationTimelineSnapshot:
        """Return the versioned, bounded, JSON-safe dashboard contract."""
        current = self._now()
        # Snapshot reads must not perform the day rollover.  A polling entity
        # can otherwise clear yesterday's coherent payload before the controller
        # has captured telemetry and rebuilt today's projection.  Mutating
        # refresh methods perform the rollover atomically instead.
        # Within the same local day it is safe to refresh the public marker,
        # but a read never closes an interval.  At midnight retain the last
        # coherent day until a mutating control refresh performs the rollover.
        if current.date() == self._local_date:
            if self._advance_clock(current, close_elapsed=False):
                self._mark_public_change("activity")
        if (
            self._snapshot_cache is not None
            and self._snapshot_cache_revision == self._revision
        ):
            # Progress is intentionally excluded from the material revision,
            # but it is still cheap and useful to keep the cached scalar fresh
            # without rebuilding any of the 96-cell arrays.
            self._snapshot_cache["current_progress"] = round(
                self._current_progress, 6
            )
            self._snapshot_cache["notification_count"] = self._notification_count
            self._snapshot_cache["save_count"] = self._save_count
            self._snapshot_cache["snapshot_age_s"] = self.snapshot_age_s
            self._snapshot_cache["last_save_age_s"] = self.last_save_age_s
            self._snapshot_cache["publications_last_minute"] = (
                self.publications_last_minute
            )
            self._snapshot_cache["writes_last_minute"] = self.writes_last_minute
            if as_dto:
                return DailyOperationTimelineSnapshot.from_dict(self._snapshot_cache)
            return self._snapshot_cache
        self._generated_at = current.isoformat()
        action_durations = self._public_action_durations()
        actual_coverage = [
            round(
                min(
                    float(INTERVAL_SECONDS),
                    max(self._actual_solar_coverage_s[index], self._actual_consumption_coverage_s[index]),
                ),
                3,
            )
            for index in range(INTERVAL_COUNT)
        ]
        actual_grid: list[str] = []
        planned_grid: list[str] = []
        visible_grid: list[str] = []
        delay_until: list[str | None] = []
        for index, cell in enumerate(self._cells):
            actual_grid.append(cell.actual_grid_charge_decision)
            planned_grid.append(cell.planned_grid_charge_decision)
            visible_grid.append(
                cell.actual_grid_charge_decision
                if index <= self._current_index and cell.actual_grid_charge_decision
                != GRID_CHARGE_NOT_APPLICABLE
                else cell.planned_grid_charge_decision
            )
            delay_until.append(
                cell.actual_delay_until
                if index <= self._current_index and cell.actual_delay_until is not None
                else cell.planned_delay_until
            )

        grid = [_wall_interval_info(self._local_date, index, self._timezone()) for index in range(INTERVAL_COUNT)]
        interval_grid = {
            "labels": [item["label"] for item in grid],
            "starts": [item["start"] for item in grid],
            "ends": [item["end"] for item in grid],
            "duration_s": [item["duration_s"] for item in grid],
            "dst_skipped": [item["dst_skipped"] for item in grid],
            "dst_repeated": [item["dst_repeated"] for item in grid],
        }
        extended_start = datetime.combine(
            self._local_date + timedelta(days=1),
            time.min,
            tzinfo=self._timezone(),
        )
        extended_grid = [
            _wall_interval_info(
                self._local_date + timedelta(days=1),
                index,
                self._timezone(),
            )
            for index in range(EXTENDED_INTERVAL_COUNT)
        ]
        extended_horizon = {
            "start": extended_start.isoformat(),
            "end": (extended_start + timedelta(hours=EXTENDED_HORIZON_HOURS)).isoformat(),
            "interval_minutes": INTERVAL_MINUTES,
            "interval_count": EXTENDED_INTERVAL_COUNT,
            "duration_s": [item["duration_s"] for item in extended_grid],
            "dst_skipped": [item["dst_skipped"] for item in extended_grid],
            "dst_repeated": [item["dst_repeated"] for item in extended_grid],
        }
        actual_samples = [
            {
                "solar_kwh": self._actual_solar_kwh[index],
                "consumption_kwh": self._actual_consumption_kwh[index],
                "coverage_s": actual_coverage[index],
            }
            if (
                self._actual_solar_kwh[index] is not None
                or self._actual_consumption_kwh[index] is not None
                or actual_coverage[index] > 0.0
            )
            else None
            for index in range(INTERVAL_COUNT)
        ]
        observed_totals = {
            name: round(
                sum(cell.observed_seconds_by_action.get(name, 0.0) for cell in self._cells),
                3,
            )
            for name in _ACTION_NAMES.values()
        }
        intervals = self._public_intervals(grid)
        actual_masks = [cell.actual_action_mask for cell in self._cells]
        planned_masks = [cell.planned_action_mask for cell in self._cells]
        counts = {
            "past_cells": int(sum(self._closed)),
            "current_cells": 1,
            "future_cells": max(0, INTERVAL_COUNT - sum(self._closed) - 1),
            "partial_cells": sum(
                1
                for index in range(INTERVAL_COUNT)
                if not self._closed[index]
                and (
                    self._actual_solar_coverage_s[index] > 0.0
                    or self._actual_consumption_coverage_s[index] > 0.0
                )
            ),
            "unknown_cells": sum(
                1
                for index in range(INTERVAL_COUNT)
                if self._cells[index].actual_action_mask == ACTION_NONE
                and self._cells[index].planned_action_mask == ACTION_NONE
            ),
            "by_action": {
                name: sum(1 for mask in actual_masks + planned_masks if mask & bit)
                for bit, name in _ACTION_NAMES.items()
            },
            "double_overlaps": sum(
                1
                for cell in self._cells
                if cell.actual_coexistence_mask.bit_count() == 2
                or cell.planned_coexistence_mask.bit_count() == 2
            ),
            "triple_overlaps": sum(
                1
                for cell in self._cells
                if cell.actual_coexistence_mask.bit_count() >= 3
                or cell.planned_coexistence_mask.bit_count() >= 3
            ),
        }
        visible_charge_power = [
            cell.actual_charge_power_w
            if index <= self._current_index and cell.actual_charge_power_w is not None
            else cell.planned_charge_power_w
            for index, cell in enumerate(self._cells)
        ]
        visible_discharge_power = [
            cell.actual_discharge_power_w
            if index <= self._current_index and cell.actual_discharge_power_w is not None
            else cell.planned_discharge_power_w
            for index, cell in enumerate(self._cells)
        ]
        planned_charge_to_battery = [
            (
                (cell.planned_solar_to_battery_kwh or 0.0)
                + (cell.planned_grid_to_battery_kwh or 0.0)
            )
            if cell.planned_solar_to_battery_kwh is not None
            or cell.planned_grid_to_battery_kwh is not None
            else None
            for cell in self._cells
        ]
        visible_charge_to_battery = [
            cell.actual_charge_to_battery_kwh
            if index <= self._current_index
            else planned_charge_to_battery[index]
            for index, cell in enumerate(self._cells)
        ]
        planned_discharge_from_battery = [
            cell.planned_battery_to_home_kwh for cell in self._cells
        ]
        visible_discharge_from_battery = [
            cell.actual_discharge_from_battery_kwh
            if index <= self._current_index
            else planned_discharge_from_battery[index]
            for index, cell in enumerate(self._cells)
        ]
        actual_soc_pct = [cell.actual_soc_pct for cell in self._cells]
        planned_soc_pct = [cell.planned_soc_end_pct for cell in self._cells]
        visible_soc_pct = [
            actual_soc_pct[index]
            if index <= self._current_index
            else planned_soc_pct[index]
            for index in range(INTERVAL_COUNT)
        ]
        payload = {
            "schema_version": DAILY_TIMELINE_SCHEMA_VERSION,
            "local_date": self._local_date.isoformat(),
            "timezone": self._configured_timezone_name(),
            "interval_minutes": INTERVAL_MINUTES,
            "interval_count": INTERVAL_COUNT,
            "generated_at": current.isoformat(),
            "revision": self._revision,
            "snapshot_build_count": self._snapshot_build_count + 1,
            "notification_count": self._notification_count,
            "save_count": self._save_count,
            "snapshot_age_s": 0.0,
            "last_save_age_s": self.last_save_age_s,
            "publications_last_minute": self.publications_last_minute,
            "writes_last_minute": self.writes_last_minute,
            "plan_evaluated_at": self._plan_evaluated_at,
            "current_index": self._current_index,
            "current_progress": round(self._current_progress, 6),
            "mode": self._mode,
            "stale": bool(self._stale),
            "stale_reason": self._stale_reason,
            "last_error": self._last_error,
            "series": {
                "solar_actual_kwh": list(self._actual_solar_kwh),
                "solar_forecast_kwh": list(self._planned_solar_kwh),
                "consumption_actual_kwh": list(self._actual_consumption_kwh),
                "consumption_forecast_kwh": list(self._planned_consumption_kwh),
                "actual_coverage_s": actual_coverage,
                "solar_actual_coverage_s": [round(value, 3) for value in self._actual_solar_coverage_s],
                "consumption_actual_coverage_s": [
                    round(value, 3) for value in self._actual_consumption_coverage_s
                ],
                "actual_samples": actual_samples,
            },
            "operations": {
                "actual_action_mask": actual_masks,
                "planned_action_mask": planned_masks,
                "actual_context_mask": [cell.actual_context_mask for cell in self._cells],
                "planned_context_mask": [cell.planned_context_mask for cell in self._cells],
                "actual_coexistence_mask": [cell.actual_coexistence_mask for cell in self._cells],
                "planned_coexistence_mask": [cell.planned_coexistence_mask for cell in self._cells],
                "grid_charge_decision": visible_grid,
                "actual_grid_charge_decision": actual_grid,
                "planned_grid_charge_decision": planned_grid,
                "delay_until": delay_until,
                "charge_power_w": visible_charge_power,
                "discharge_power_w": visible_discharge_power,
                "actual_charge_power_w": [cell.actual_charge_power_w for cell in self._cells],
                "planned_charge_power_w": [cell.planned_charge_power_w for cell in self._cells],
                "actual_discharge_power_w": [cell.actual_discharge_power_w for cell in self._cells],
                "planned_discharge_power_w": [cell.planned_discharge_power_w for cell in self._cells],
                "charge_to_battery_kwh": visible_charge_to_battery,
                "actual_charge_to_battery_kwh": [
                    cell.actual_charge_to_battery_kwh for cell in self._cells
                ],
                "planned_charge_to_battery_kwh": planned_charge_to_battery,
                "discharge_from_battery_kwh": visible_discharge_from_battery,
                "actual_discharge_from_battery_kwh": [
                    cell.actual_discharge_from_battery_kwh for cell in self._cells
                ],
                "planned_discharge_from_battery_kwh": planned_discharge_from_battery,
                "soc_pct": visible_soc_pct,
                "actual_soc_pct": actual_soc_pct,
                "planned_soc_pct": planned_soc_pct,
                "solar_to_battery_kwh": [cell.planned_solar_to_battery_kwh for cell in self._cells],
                "grid_to_battery_kwh": [cell.planned_grid_to_battery_kwh for cell in self._cells],
                "battery_to_home_kwh": [cell.planned_battery_to_home_kwh for cell in self._cells],
                "grid_to_home_kwh": [cell.planned_grid_to_home_kwh for cell in self._cells],
                "solar_to_home_kwh": [cell.planned_solar_to_home_kwh for cell in self._cells],
                "stored_energy_end_kwh": [cell.planned_stored_energy_end_kwh for cell in self._cells],
                "soc_end_pct": [cell.planned_soc_end_pct for cell in self._cells],
                "observed_seconds_by_action": observed_totals,
                "observed_seconds_by_action_by_interval": action_durations,
                "actual_source": [cell.actual_source for cell in self._cells],
                "planned_source": [cell.planned_source for cell in self._cells],
                "actual_slot": [cell.actual_slot for cell in self._cells],
                "planned_slot": [cell.planned_slot for cell in self._cells],
                "closed": list(self._closed),
            },
            "sources": dict(self._sources),
            "restoration": {
                "status": self._restore_status,
                "restored": self._restore_status == "restored",
                "date": self._local_date.isoformat(),
                "error": self._last_error,
            },
            "freshness": {
                "state": "stale" if self._stale else "fresh",
                "stale": bool(self._stale),
                **self._freshness_info,
            },
            "setpoint": dict(self._setpoint_info),
            "delay": dict(self._delay_info),
            "counts": counts,
            "intervals": intervals,
            "interval_grid": interval_grid,
            "extended_horizon": extended_horizon,
            "extended_projection": list(self._extended_projection),
        }
        safe_payload = _json_safe(payload)
        self._snapshot_cache = safe_payload
        self._snapshot_cache_revision = self._revision
        self._snapshot_build_count += 1
        self._last_snapshot_monotonic = monotonic()
        self._snapshot_cache["snapshot_age_s"] = 0.0
        if as_dto:
            return DailyOperationTimelineSnapshot.from_dict(safe_payload)
        return safe_payload


__all__ = [
    "ACTION_DISCHARGE",
    "ACTION_GRID",
    "ACTION_GRID_CHARGE",
    "ACTION_MASK_ALL",
    "ACTION_NAMES",
    "ACTION_NONE",
    "ACTION_SOLAR",
    "ACTION_SOLAR_CHARGE",
    "CONTEXT_CHARGE_DELAY",
    "CONTEXT_DYNAMIC_PRICE",
    "CONTEXT_HOURLY_BALANCE",
    "CONTEXT_MASK_ALL",
    "CONTEXT_NAMES",
    "CONTEXT_NONE",
    "CONTEXT_REALTIME_PRICE",
    "CONTEXT_SETPOINT",
    "CONTEXT_TIME_SLOT",
    "DAILY_OPERATION_TIMELINE_SCHEMA_VERSION",
    "DAILY_OPERATION_TIMELINE_STORE_VERSION",
    "DAILY_TIMELINE_INTERVAL_COUNT",
    "DAILY_TIMELINE_INTERVAL_MINUTES",
    "DEFAULT_TIMELINE_DEBOUNCE_SECONDS",
    "DAILY_TIMELINE_SCHEMA_VERSION",
    "DAILY_TIMELINE_STORE_KEY",
    "DAILY_TIMELINE_STORE_VERSION",
    "DST_NORMAL",
    "DST_REPEATED",
    "DST_SKIPPED",
    "GRID_CHARGE_DECISIONS",
    "GRID_CHARGE_NOT_APPLICABLE",
    "GRID_CHARGE_NOT_NEEDED",
    "GRID_CHARGE_SCHEDULED",
    "GRID_CHARGE_UNKNOWN",
    "INTERVAL_COUNT",
    "INTERVAL_MINUTES",
    "INTERVAL_SECONDS",
    "EXTENDED_HORIZON_HOURS",
    "EXTENDED_INTERVAL_COUNT",
    "SCHEMA_VERSION",
    "STATE_CURRENT",
    "STATE_FUTURE",
    "STATE_PAST",
    "STORE_KEY",
    "TIMELINE_SCHEMA_VERSION",
    "TIMELINE_STORE_VERSION",
    "BatteryProjectionInput",
    "ChargeDelayProjection",
    "DailyOperationTimelineManager",
    "DailyOperationTimelineSnapshot",
    "DailyTimelineDTO",
    "DailyTimelineInterval",
    "DailyTimelineSnapshot",
    "ProjectedIntervalFlow",
    "ProjectedOperationInterval",
]
