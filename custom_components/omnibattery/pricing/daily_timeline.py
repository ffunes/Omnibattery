"""Pure data model and projection helpers for the daily operation timeline.

This module is deliberately a boundary between the pricing/control code and a
future dashboard sensor.  It does not import Home Assistant, read controller
state, or mutate a plan.  Callers pass small snapshots (or the existing
``EnergyInterval``/``SlotAllocation`` shaped objects) and receive immutable,
JSON-safe data.

The public energy convention is explicit:

* ``solar_kwh`` and ``consumption_kwh`` are AC-side energy for the interval.
* ``*_to_battery_kwh`` are AC-side input energy.  The battery's stored energy
  increases by that input multiplied by ``charge_efficiency``.
* ``battery_to_home_kwh`` is AC-side output energy.  Stored energy decreases
  by that output divided by ``discharge_efficiency``.
* ``stored_energy_*_kwh`` is battery-side energy.

The simulator is intentionally conservative.  Grid charging is absent unless
the caller supplies an allocation (or an explicit grid-charge schedule), and
unknown runtime blockers are not extrapolated into the future.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..const import CHARGE_EFFICIENCY

SCHEMA_VERSION = 1
INTERVAL_MINUTES = 15
INTERVAL_COUNT = 24 * 60 // INTERVAL_MINUTES
INTERVAL_SECONDS = INTERVAL_MINUTES * 60
_EPSILON = 1e-9


# Action flags describe physical energy flows.  They must not be reused for a
# planning decision such as "grid charge is not needed".
ACTION_SOLAR_CHARGE = 1
ACTION_GRID_CHARGE = 2
ACTION_DISCHARGE = 4
ACTION_MASK_ALL = ACTION_SOLAR_CHARGE | ACTION_GRID_CHARGE | ACTION_DISCHARGE
ACTION_NONE = 0
ACTION_SOLAR = ACTION_SOLAR_CHARGE
ACTION_GRID = ACTION_GRID_CHARGE

# Context flags describe why a flow/decision is present.
CONTEXT_SETPOINT = 1
CONTEXT_CHARGE_DELAY = 2
CONTEXT_DYNAMIC_PRICE = 4
CONTEXT_TIME_SLOT = 8
CONTEXT_REALTIME_PRICE = 16
CONTEXT_HOURLY_BALANCE = 32
CONTEXT_NONE = 0
CONTEXT_MASK_ALL = (
    CONTEXT_SETPOINT
    | CONTEXT_CHARGE_DELAY
    | CONTEXT_DYNAMIC_PRICE
    | CONTEXT_TIME_SLOT
    | CONTEXT_REALTIME_PRICE
    | CONTEXT_HOURLY_BALANCE
)

STATE_PAST = "past"
STATE_CURRENT = "current"
STATE_FUTURE = "future"
TIMELINE_STATES = (STATE_PAST, STATE_CURRENT, STATE_FUTURE)

DST_NORMAL = "normal"
DST_SKIPPED = "skipped"
DST_REPEATED = "repeated"
DST_STATES = (DST_NORMAL, DST_SKIPPED, DST_REPEATED)

GRID_CHARGE_NOT_APPLICABLE = "not_applicable"
GRID_CHARGE_SCHEDULED = "scheduled"
GRID_CHARGE_NOT_NEEDED = "not_needed"
GRID_CHARGE_UNKNOWN = "unknown"
GRID_CHARGE_DECISIONS = (
    GRID_CHARGE_NOT_APPLICABLE,
    GRID_CHARGE_SCHEDULED,
    GRID_CHARGE_NOT_NEEDED,
    GRID_CHARGE_UNKNOWN,
)

ACTION_NAMES = {
    ACTION_SOLAR_CHARGE: "solar_charge",
    ACTION_GRID_CHARGE: "grid_charge",
    ACTION_DISCHARGE: "discharge",
}
_ACTION_ALIASES = {
    "solar": ACTION_SOLAR_CHARGE,
    "pv": ACTION_SOLAR_CHARGE,
    "solar_charge": ACTION_SOLAR_CHARGE,
    "charge_solar": ACTION_SOLAR_CHARGE,
    "grid": ACTION_GRID_CHARGE,
    "mains": ACTION_GRID_CHARGE,
    "grid_charge": ACTION_GRID_CHARGE,
    "charge_grid": ACTION_GRID_CHARGE,
    "discharge": ACTION_DISCHARGE,
    "battery_to_home": ACTION_DISCHARGE,
}
CONTEXT_NAMES = {
    CONTEXT_SETPOINT: "setpoint",
    CONTEXT_CHARGE_DELAY: "charge_delay",
    CONTEXT_DYNAMIC_PRICE: "dynamic_price",
    CONTEXT_TIME_SLOT: "time_slot",
    CONTEXT_REALTIME_PRICE: "realtime_price",
    CONTEXT_HOURLY_BALANCE: "hourly_balance",
}
_CONTEXT_ALIASES = {
    "setpoint": CONTEXT_SETPOINT,
    "charge_to_setpoint": CONTEXT_SETPOINT,
    "charging_to_setpoint": CONTEXT_SETPOINT,
    "delay": CONTEXT_CHARGE_DELAY,
    "charge_delay": CONTEXT_CHARGE_DELAY,
    "dynamic": CONTEXT_DYNAMIC_PRICE,
    "dynamic_pricing": CONTEXT_DYNAMIC_PRICE,
    "time_slot": CONTEXT_TIME_SLOT,
    "timeslot": CONTEXT_TIME_SLOT,
    "realtime": CONTEXT_REALTIME_PRICE,
    "real_time_price": CONTEXT_REALTIME_PRICE,
    "hourly_balance": CONTEXT_HOURLY_BALANCE,
    "hourly_net_balance": CONTEXT_HOURLY_BALANCE,
    "net_balance": CONTEXT_HOURLY_BALANCE,
}


def _finite(value: Any, default: float = 0.0) -> float:
    """Return a finite float without allowing runtime telemetry to leak NaN."""
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _non_negative(value: Any, default: float = 0.0) -> float:
    return max(0.0, _finite(value, default))


def _bounded(value: Any, lower: float, upper: float, default: float) -> float:
    parsed = _finite(value, default)
    return min(upper, max(lower, parsed))


def _timestamp(value: datetime) -> float:
    """Return an absolute timestamp while treating naive values as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt_timezone.utc).timestamp()
    return value.timestamp()


def _duration_seconds(start: datetime | None, end: datetime | None) -> float:
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return 0.0
    try:
        if start.tzinfo is None and end.tzinfo is None:
            result = (end - start).total_seconds()
        else:
            result = _timestamp(end) - _timestamp(start)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    return max(0.0, _finite(result))


def _coerce_mask(value: Any, known_bits: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed & known_bits


def _normalise_state(value: Any) -> str:
    return value if value in TIMELINE_STATES else STATE_FUTURE


def _normalise_dst(value: Any) -> str:
    return value if value in DST_STATES else DST_NORMAL


def normalize_grid_charge_decision(value: Any) -> str:
    """Normalize the independent grid-charge decision field.

    Invalid or missing decisions become ``unknown`` only when a caller
    supplied a non-empty value.  ``None`` means that no grid decision applies
    to this interval and maps to ``not_applicable``.
    """
    if isinstance(value, bool):
        return GRID_CHARGE_SCHEDULED if value else GRID_CHARGE_NOT_NEEDED
    if value is None or value == "":
        return GRID_CHARGE_NOT_APPLICABLE
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "scheduled_charge": GRID_CHARGE_SCHEDULED,
        "schedule": GRID_CHARGE_SCHEDULED,
        "selected": GRID_CHARGE_SCHEDULED,
        "active": GRID_CHARGE_SCHEDULED,
        "unneeded": GRID_CHARGE_NOT_NEEDED,
        "no": GRID_CHARGE_NOT_NEEDED,
        "none": GRID_CHARGE_NOT_APPLICABLE,
    }
    if normalized in GRID_CHARGE_DECISIONS:
        return normalized
    if normalized in aliases:
        return aliases[normalized]
    return GRID_CHARGE_UNKNOWN


def _flag_value(value: Any, names: Mapping[str, int]) -> int:
    if isinstance(value, str):
        normalized = value.strip().lower().replace("-", "_")
        try:
            if normalized.startswith("0x") or normalized.isdigit():
                return _coerce_mask(int(normalized, 0), sum(names))
        except ValueError:
            return 0
        aliases = _ACTION_ALIASES if names is ACTION_NAMES else _CONTEXT_ALIASES
        mask = 0
        for token in normalized.replace("|", ",").replace("+", ",").split(","):
            mask |= aliases.get(token.strip(), names.get(token.strip(), 0))
        return mask
    return _coerce_mask(value, sum(names))


def compose_action_mask(
    *actions: Any,
    solar_charge: bool = False,
    grid_charge: bool = False,
    discharge: bool = False,
) -> int:
    """Compose a physical action mask from flags, names, masks, or iterables."""
    mask = 0
    for action in actions:
        if isinstance(action, (list, tuple, set, frozenset)):
            mask |= compose_action_mask(*action)
        else:
            mask |= _flag_value(action, ACTION_NAMES)
    if solar_charge:
        mask |= ACTION_SOLAR_CHARGE
    if grid_charge:
        mask |= ACTION_GRID_CHARGE
    if discharge:
        mask |= ACTION_DISCHARGE
    return mask & ACTION_MASK_ALL


def compose_context_mask(
    *contexts: Any,
    setpoint: bool = False,
    charge_delay: bool = False,
    dynamic_price: bool = False,
    time_slot: bool = False,
    realtime_price: bool = False,
) -> int:
    """Compose a context mask from flags, names, masks, or iterables."""
    mask = 0
    for context in contexts:
        if isinstance(context, (list, tuple, set, frozenset)):
            mask |= compose_context_mask(*context)
        else:
            mask |= _flag_value(context, CONTEXT_NAMES)
    if setpoint:
        mask |= CONTEXT_SETPOINT
    if charge_delay:
        mask |= CONTEXT_CHARGE_DELAY
    if dynamic_price:
        mask |= CONTEXT_DYNAMIC_PRICE
    if time_slot:
        mask |= CONTEXT_TIME_SLOT
    if realtime_price:
        mask |= CONTEXT_REALTIME_PRICE
    return mask & CONTEXT_MASK_ALL


def has_action(mask: int, action: int) -> bool:
    return bool(_coerce_mask(mask, ACTION_MASK_ALL) & action)


def has_context(mask: int, context: int) -> bool:
    return bool(_coerce_mask(mask, CONTEXT_MASK_ALL) & context)


def actions_for_mask(mask: int) -> tuple[str, ...]:
    safe = _coerce_mask(mask, ACTION_MASK_ALL)
    return tuple(name for bit, name in ACTION_NAMES.items() if safe & bit)


def contexts_for_mask(mask: int) -> tuple[str, ...]:
    safe = _coerce_mask(mask, CONTEXT_MASK_ALL)
    return tuple(name for bit, name in CONTEXT_NAMES.items() if safe & bit)


# Friendly aliases for callers that use "decode" terminology.
make_action_mask = compose_action_mask
make_context_mask = compose_context_mask
decode_action_mask = actions_for_mask
decode_context_mask = contexts_for_mask


def json_safe(value: Any) -> Any:
    """Return a recursively JSON-safe representation.

    Datetimes are ISO strings, non-finite floats become ``None``, mapping keys
    become strings, and dataclasses are traversed without exposing objects or
    enums from the runtime.  The function is intentionally best-effort at the
    boundary: an unavailable diagnostic value must not break the sensor.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    if is_dataclass(value):
        return {
            item.name: json_safe(getattr(value, item.name)) for item in fields(value)
        }
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return json_safe(to_dict())
        except (AttributeError, TypeError, ValueError):
            pass
    return str(value)


def _timezone_name(tz: Any) -> str:
    key = getattr(tz, "key", None)
    if key:
        return str(key)
    try:
        name = tz.tzname(None)
    except (AttributeError, TypeError, ValueError):
        name = None
    return str(name or "UTC")


def _coerce_timezone(value: Any, fallback: Any = None) -> Any:
    candidate = value if value is not None else fallback
    if isinstance(candidate, str):
        try:
            return ZoneInfo(candidate)
        except (KeyError, ValueError):
            raise ValueError(f"unknown timezone: {candidate}") from None
    if candidate is not None and hasattr(candidate, "utcoffset"):
        return candidate
    return dt_timezone.utc


def _coerce_local_date(value: date | datetime | str) -> tuple[date, datetime | None]:
    if isinstance(value, datetime):
        return value.date(), value
    if isinstance(value, date):
        return value, None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value), None
        except ValueError:
            raise ValueError("local_date must be an ISO date") from None
    raise TypeError("local_date must be a date, datetime, or ISO date")


def _round_trip_wall(candidate: datetime, tz: Any) -> datetime:
    return datetime.fromtimestamp(_timestamp(candidate), tz).replace(tzinfo=None)


def _first_offset_transition(
    start_timestamp: float,
    end_timestamp: float,
    tz: Any,
) -> float | None:
    """Find the first UTC instant at which the offset changes in a segment."""
    if end_timestamp <= start_timestamp + 1e-6:
        return None
    start_offset = datetime.fromtimestamp(start_timestamp, tz).utcoffset()
    end_offset = datetime.fromtimestamp(end_timestamp, tz).utcoffset()
    if start_offset == end_offset:
        return None
    low, high = start_timestamp, end_timestamp
    for _ in range(50):
        middle = (low + high) / 2.0
        if datetime.fromtimestamp(middle, tz).utcoffset() == start_offset:
            low = middle
        else:
            high = middle
    return high


def _wall_occurrences(
    wall_start: datetime,
    wall_end: datetime,
    tz: Any,
) -> tuple[tuple[datetime, datetime], ...]:
    """Return valid absolute occurrences of one local wall-clock quarter.

    ``zoneinfo`` deliberately allows construction of nonexistent local times.
    Round-tripping through a timestamp is therefore used to distinguish a
    skipped wall time from a real one.  Endpoints immediately around a spring
    transition are allowed to round-trip to the post-transition wall time so
    the physical quarter remains 15 minutes long.
    """
    starts: list[tuple[float, datetime]] = []
    for fold in (0, 1):
        candidate = wall_start.replace(tzinfo=tz, fold=fold)
        try:
            timestamp = _timestamp(candidate)
            if _round_trip_wall(candidate, tz) != wall_start:
                continue
        except (OverflowError, TypeError, ValueError):
            continue
        if not any(abs(timestamp - previous) < 1e-6 for previous, _ in starts):
            starts.append((timestamp, datetime.fromtimestamp(timestamp, tz)))

    occurrences: list[tuple[datetime, datetime]] = []
    for start_timestamp, start in sorted(starts):
        end_candidates: list[float] = []
        for fold in (0, 1):
            candidate = wall_end.replace(tzinfo=tz, fold=fold)
            try:
                timestamp = _timestamp(candidate)
            except (OverflowError, TypeError, ValueError):
                continue
            if timestamp > start_timestamp + 1e-6:
                end_candidates.append(timestamp)
        if not end_candidates:
            continue
        end_timestamp = min(end_candidates)
        transition = _first_offset_transition(start_timestamp, end_timestamp, tz)
        if transition is not None:
            end_timestamp = transition
        occurrences.append((start, datetime.fromtimestamp(end_timestamp, tz)))
    return tuple(occurrences)


@dataclass(frozen=True)
class LocalTimelineInterval:
    """One of the 96 fixed wall-clock positions shown by the dashboard."""

    index: int
    label: str
    start: datetime | None
    end: datetime | None
    duration_seconds: float
    state: str
    dst_status: str = DST_NORMAL
    occurrences: tuple[tuple[datetime, datetime], ...] = ()
    local_date: date | None = None
    timezone: str = "UTC"

    @property
    def dst_skipped(self) -> bool:
        return self.dst_status == DST_SKIPPED

    @property
    def dst_repeated(self) -> bool:
        return self.dst_status == DST_REPEATED

    def to_dict(self) -> dict[str, Any]:
        return json_safe(
            {
                "index": self.index,
                "label": self.label,
                "start": self.start,
                "end": self.end,
                "duration_seconds": self.duration_seconds,
                "duration_s": self.duration_seconds,
                "state": self.state,
                "dst_status": self.dst_status,
                "dst_skipped": self.dst_skipped,
                "dst_repeated": self.dst_repeated,
                "occurrences": self.occurrences,
            }
        )


def _state_for_interval(
    occurrences: Sequence[tuple[datetime, datetime]],
    wall_start: datetime,
    wall_end: datetime,
    now: datetime,
    tz: Any,
) -> str:
    now_local = now.astimezone(tz) if now.tzinfo is not None else now.replace(tzinfo=tz)
    now_timestamp = _timestamp(now_local)
    if occurrences:
        first_start = _timestamp(occurrences[0][0])
        last_end = _timestamp(occurrences[-1][1])
        if now_timestamp < first_start - 1e-6:
            return STATE_FUTURE
        if now_timestamp >= last_end - 1e-6:
            return STATE_PAST
        return STATE_CURRENT

    # There is no absolute interval to compare for a skipped quarter.  Its
    # wall position still has a useful past/current/future state for tooltips.
    current_wall = now_local.replace(tzinfo=None)
    if current_wall >= wall_end:
        return STATE_PAST
    if current_wall < wall_start:
        return STATE_FUTURE
    return STATE_CURRENT


def build_local_grid(
    local_date: date | datetime | str,
    tz: Any = None,
    *,
    now: datetime | None = None,
    timezone: Any = None,
) -> list[LocalTimelineInterval]:
    """Build exactly 96 local wall-clock quarters, including DST metadata.

    ``tz`` may be a ``tzinfo`` or an IANA timezone name.  ``timezone`` is a
    keyword alias for readability.  The returned grid always has 96 entries;
    spring-forward positions have no timestamps and fall-back positions hold
    two absolute occurrences in one wall-clock cell.
    """
    if tz is not None and timezone is not None:
        raise TypeError("pass either tz or timezone, not both")
    calendar_date, source_datetime = _coerce_local_date(local_date)
    timezone_value = _coerce_timezone(
        timezone if timezone is not None else tz,
        source_datetime.tzinfo if source_datetime else None,
    )
    current = now or datetime.now(timezone_value)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone_value)
    else:
        current = current.astimezone(timezone_value)

    result: list[LocalTimelineInterval] = []
    timezone_name = _timezone_name(timezone_value)
    midnight = datetime.combine(calendar_date, time.min)
    for index in range(INTERVAL_COUNT):
        wall_start = midnight + timedelta(minutes=index * INTERVAL_MINUTES)
        wall_end = wall_start + timedelta(minutes=INTERVAL_MINUTES)
        occurrences = _wall_occurrences(wall_start, wall_end, timezone_value)
        if not occurrences:
            status = DST_SKIPPED
            start = end = None
            duration = 0.0
        else:
            status = DST_REPEATED if len(occurrences) > 1 else DST_NORMAL
            start = occurrences[0][0]
            end = occurrences[-1][1]
            duration = sum(_duration_seconds(item[0], item[1]) for item in occurrences)
        result.append(
            LocalTimelineInterval(
                index=index,
                label=f"{index // 4:02d}:{(index % 4) * 15:02d}",
                start=start,
                end=end,
                duration_seconds=duration,
                state=_state_for_interval(
                    occurrences, wall_start, wall_end, current, timezone_value
                ),
                dst_status=status,
                occurrences=occurrences,
                local_date=calendar_date,
                timezone=timezone_name,
            )
        )
    return result


# Names used by different layers during the planned implementation.
build_local_timeline = build_local_grid
build_local_quarter_grid = build_local_grid


@dataclass(frozen=True)
class BatteryProjectionInput:
    """Battery-only snapshot consumed by the pure projection.

    Values are intentionally accepted as supplied and clamped by the
    simulator.  This lets a stale SOC or an over-capacity restore degrade to a
    bounded projection instead of raising from the control loop.  A capacity
    of zero or a non-finite limit simply makes that battery unavailable.
    """

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
class ProjectionIntervalInput:
    """Small, optional adapter for callers that do not use ``EnergyInterval``."""

    start: datetime
    end: datetime
    consumption_kwh: float = 0.0
    solar_kwh: float = 0.0
    state: str = STATE_FUTURE
    context_mask: int = 0
    grid_charge_decision: str = GRID_CHARGE_NOT_APPLICABLE
    dst_status: str = DST_NORMAL
    coverage_seconds: float | None = None
    projected: bool | None = None
    duration_seconds: float | None = None


@dataclass(frozen=True)
class ProjectedIntervalFlow:
    """Aggregate physical flows and rendering metadata for one interval."""

    start: datetime | None
    end: datetime | None
    solar_kwh: float = 0.0
    consumption_kwh: float = 0.0
    solar_to_battery_kwh: float = 0.0
    grid_to_battery_kwh: float = 0.0
    battery_to_home_kwh: float = 0.0
    grid_to_home_kwh: float = 0.0
    stored_energy_end_kwh: float = 0.0
    action_mask: int = 0
    context_mask: int = 0
    grid_charge_decision: str = GRID_CHARGE_NOT_APPLICABLE
    state: str = STATE_FUTURE
    dst_status: str = DST_NORMAL
    duration_seconds: float | None = None
    coverage_seconds: float | None = None
    stored_energy_start_kwh: float | None = None
    solar_to_home_kwh: float | None = None
    curtailed_solar_kwh: float | None = None
    stored_energy_charged_kwh: float | None = None
    stored_energy_discharged_kwh: float | None = None
    charge_power_w: float | None = None
    discharge_power_w: float | None = None
    delay_until: datetime | str | None = None
    projected: bool | None = None
    wall_index: int | None = None
    reason: str | None = None
    source: str | None = None
    slot: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "action_mask", _coerce_mask(self.action_mask, ACTION_MASK_ALL)
        )
        object.__setattr__(
            self, "context_mask", _coerce_mask(self.context_mask, CONTEXT_MASK_ALL)
        )
        object.__setattr__(
            self,
            "grid_charge_decision",
            normalize_grid_charge_decision(self.grid_charge_decision),
        )
        object.__setattr__(self, "state", _normalise_state(self.state))
        object.__setattr__(self, "dst_status", _normalise_dst(self.dst_status))

    @property
    def actions(self) -> tuple[str, ...]:
        return actions_for_mask(self.action_mask)

    @property
    def contexts(self) -> tuple[str, ...]:
        return contexts_for_mask(self.context_mask)

    def to_dict(self) -> dict[str, Any]:
        return json_safe(
            {
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
                "actions": self.actions,
                "contexts": self.contexts,
                "grid_charge_decision": self.grid_charge_decision,
                "state": self.state,
                "dst_status": self.dst_status,
                "duration_seconds": self.duration_seconds,
                "coverage_seconds": self.coverage_seconds,
                "stored_energy_start_kwh": self.stored_energy_start_kwh,
                "solar_to_home_kwh": self.solar_to_home_kwh,
                "curtailed_solar_kwh": self.curtailed_solar_kwh,
                "stored_energy_charged_kwh": self.stored_energy_charged_kwh,
                "stored_energy_discharged_kwh": self.stored_energy_discharged_kwh,
                "charge_power_w": self.charge_power_w,
                "discharge_power_w": self.discharge_power_w,
                "delay_until": self.delay_until,
                "projected": self.projected,
                "wall_index": self.wall_index,
                "reason": self.reason,
                "source": self.source,
                "slot": self.slot,
            }
        )

    as_dict = to_dict


@dataclass(frozen=True)
class ProjectedBatteryFlow:
    """Per-battery detail retained by ``BatteryProjectionResult``."""

    battery_key: str
    start: datetime | None
    end: datetime | None
    solar_to_battery_kwh: float
    grid_to_battery_kwh: float
    battery_to_home_kwh: float
    stored_energy_start_kwh: float
    stored_energy_end_kwh: float
    stored_energy_charged_kwh: float
    stored_energy_discharged_kwh: float
    action_mask: int = 0

    def to_dict(self) -> dict[str, Any]:
        return json_safe(self)

    as_dict = to_dict


@dataclass(frozen=True)
class BatteryProjectionResult:
    """Projection result with aggregate flows and per-battery accounting."""

    intervals: tuple[ProjectedIntervalFlow, ...]
    battery_flows: Mapping[str, tuple[ProjectedBatteryFlow, ...]]
    final_stored_kwh_by_battery: Mapping[str, float]

    @property
    def flows(self) -> tuple[ProjectedIntervalFlow, ...]:
        return self.intervals

    def to_dict(self) -> dict[str, Any]:
        return json_safe(
            {
                "intervals": self.intervals,
                "battery_flows": self.battery_flows,
                "final_stored_kwh_by_battery": self.final_stored_kwh_by_battery,
            }
        )

    as_dict = to_dict


def _coerce_interval(value: Any, index: int) -> ProjectionIntervalInput:
    if isinstance(value, ProjectionIntervalInput):
        return value
    if isinstance(value, ProjectedIntervalFlow):
        if value.start is None or value.end is None:
            raise ValueError("projected flow without timestamps cannot be simulated")
        return ProjectionIntervalInput(
            value.start,
            value.end,
            value.consumption_kwh,
            value.solar_kwh,
            value.state,
            value.context_mask,
            value.grid_charge_decision,
            value.dst_status,
            value.coverage_seconds,
            value.projected,
            value.duration_seconds,
        )
    if isinstance(value, Mapping):
        start = value.get("start", value.get("start_time"))
        end = value.get("end", value.get("end_time"))
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            raise TypeError(f"interval {index} needs datetime start/end")
        return ProjectionIntervalInput(
            start,
            end,
            value.get("consumption_kwh", value.get("consumption", 0.0)),
            value.get("solar_kwh", value.get("solar", 0.0)),
            value.get("state", STATE_FUTURE),
            value.get("context_mask", 0),
            value.get("grid_charge_decision", GRID_CHARGE_NOT_APPLICABLE),
            value.get("dst_status", DST_NORMAL),
            value.get("coverage_seconds"),
            value.get("projected"),
            value.get("duration_seconds", value.get("duration_s")),
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) < 4:
            raise TypeError("tuple interval needs start, end, consumption, and solar")
        start, end = value[0], value[1]
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            raise TypeError(f"interval {index} needs datetime start/end")
        return ProjectionIntervalInput(start, end, value[2], value[3])
    start = getattr(value, "start", None)
    end = getattr(value, "end", None)
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        raise TypeError(f"interval {index} needs datetime start/end")
    return ProjectionIntervalInput(
        start,
        end,
        getattr(value, "consumption_kwh", 0.0),
        getattr(value, "solar_kwh", 0.0),
        getattr(value, "state", STATE_FUTURE),
        getattr(value, "context_mask", 0),
        getattr(value, "grid_charge_decision", GRID_CHARGE_NOT_APPLICABLE),
        getattr(value, "dst_status", DST_NORMAL),
        getattr(value, "coverage_seconds", None),
        getattr(value, "projected", None),
        getattr(value, "duration_seconds", None),
    )


def _window_from_value(value: Any) -> tuple[datetime, datetime] | None:
    candidate = value
    if isinstance(value, Mapping) and "slot" in value:
        candidate = value["slot"]
    else:
        candidate = getattr(value, "slot", value)
    start = (
        candidate.get("start")
        if isinstance(candidate, Mapping)
        else getattr(candidate, "start", None)
    )
    end = (
        candidate.get("end")
        if isinstance(candidate, Mapping)
        else getattr(candidate, "end", None)
    )
    if (
        isinstance(start, datetime)
        and isinstance(end, datetime)
        and _duration_seconds(start, end) > 0
    ):
        return start, end
    return None


def _entry_amount(
    value: Any, interval: ProjectionIntervalInput, battery_key: str | None
) -> float:
    """Read a stored-energy quota from a scalar, mapping, or slot object."""
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return _non_negative(value)

    scope = None
    if isinstance(value, Mapping):
        scope = value.get("battery_scope", value.get("battery_key"))
        if (
            scope not in (None, "all", "*")
            and battery_key is not None
            and str(scope) != battery_key
        ):
            return 0.0
        amount = None
        for name in (
            "planned_battery_kwh",
            "grid_charge_kwh",
            "amount_kwh",
            "energy_kwh",
            "amount",
            "value",
        ):
            if name in value:
                amount = value[name]
                break
    else:
        scope = getattr(value, "battery_scope", getattr(value, "battery_key", None))
        if (
            scope not in (None, "all", "*")
            and battery_key is not None
            and str(scope) != battery_key
        ):
            return 0.0
        amount = None
        for name in (
            "planned_battery_kwh",
            "grid_charge_kwh",
            "amount_kwh",
            "energy_kwh",
            "amount",
            "value",
        ):
            candidate = getattr(value, name, None)
            if candidate is not None:
                amount = candidate
                break
    if amount is None:
        return 0.0

    window = _window_from_value(value)
    if window is None:
        return _non_negative(amount)
    overlap = max(
        0.0,
        min(_timestamp(window[1]), _timestamp(interval.end))
        - max(_timestamp(window[0]), _timestamp(interval.start)),
    )
    window_seconds = _duration_seconds(window[0], window[1])
    return (
        _non_negative(amount) * overlap / window_seconds
        if window_seconds > _EPSILON
        else 0.0
    )


def _amount_from_source(
    source: Any,
    index: int,
    intervals: Sequence[ProjectionIntervalInput],
    battery_key: str | None = None,
) -> float:
    if source is None:
        return 0.0
    interval = intervals[index]
    if isinstance(source, Mapping):
        # A single slot/allocation mapping is handled as one entry.  A mapping
        # keyed by index/start is treated as an aligned schedule.
        if any(
            name in source
            for name in (
                "planned_battery_kwh",
                "grid_charge_kwh",
                "amount_kwh",
                "energy_kwh",
                "amount",
                "value",
            )
        ):
            return _entry_amount(source, interval, battery_key)
        for key in (index, str(index), interval.start, interval.end):
            try:
                if key in source:
                    return _entry_amount(source[key], interval, battery_key)
            except TypeError:
                continue
        return 0.0
    if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
        if len(source) == len(intervals):
            return _entry_amount(source[index], interval, battery_key)
        return sum(_entry_amount(item, interval, battery_key) for item in source)
    return _entry_amount(source, interval, battery_key)


def _per_battery_sources(
    source: Any,
    battery_keys: set[str],
) -> tuple[Any, dict[str, Any]]:
    if not isinstance(source, Mapping):
        return source, {}
    per_battery = {
        str(key): value for key, value in source.items() if str(key) in battery_keys
    }
    if not per_battery:
        return source, {}
    global_source = source.get("all", source.get("*"))
    return global_source, per_battery


def _metadata_value(
    source: Any, index: int, intervals: Sequence[ProjectionIntervalInput]
) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        interval = intervals[index]
        for key in (index, str(index), interval.start):
            try:
                if key in source:
                    return source[key]
            except TypeError:
                continue
        return None
    if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
        return source[index] if index < len(source) else None
    return source


def _effective_efficiency(value: Any, default: float) -> float:
    parsed = _finite(value, default)
    if parsed <= _EPSILON:
        return 0.0
    return min(1.0, parsed)


def _battery_limits(
    battery: BatteryProjectionInput,
) -> tuple[float, float, float, float, float, float] | None:
    capacity = _finite(battery.capacity_kwh, 0.0)
    if capacity <= _EPSILON or not battery.eligible:
        return None
    minimum_soc = _bounded(battery.min_soc_pct, 0.0, 100.0, 0.0)
    maximum_soc = _bounded(battery.max_soc_pct, minimum_soc, 100.0, minimum_soc)
    minimum = capacity * minimum_soc / 100.0
    maximum = capacity * maximum_soc / 100.0
    stored = min(maximum, max(minimum, _non_negative(battery.stored_kwh)))
    charge_power = _non_negative(battery.charge_power_w)
    discharge_power = _non_negative(battery.discharge_power_w)
    return stored, minimum, maximum, charge_power, discharge_power, capacity


def simulate_battery_projection(
    intervals: Iterable[Any],
    batteries: Sequence[BatteryProjectionInput],
    *,
    allocations: Any = None,
    grid_charge_kwh: Any = None,
    allocation_energy_kind: str = "stored",
    charge_efficiency: float = CHARGE_EFFICIENCY,
    discharge_efficiency: float = 1.0,
    context_masks: Any = None,
    grid_charge_decisions: Any = None,
    charge_availability: Any = None,
    system_charge_power_w: float | None = None,
    system_discharge_power_w: float | None = None,
) -> BatteryProjectionResult:
    """Project aggregate and per-battery flows without mutating any input.

    ``allocations`` and ``grid_charge_kwh`` are optional grid-charge quotas.
    They may be aligned numeric sequences, mappings keyed by interval index, or
    existing ``SlotAllocation``-shaped objects.  Existing chronological
    allocations use stored battery-side kWh, so that is the default.  Pass
    ``allocation_energy_kind="input"`` for AC-side quota values.

    A mapping keyed by battery key supplies per-battery quotas; ``all``/``*``
    can be used for a system-wide quota.  This helper does not infer price
    gates, whitelist windows, controller blockers, or new allocations.  Those
    must be resolved by the authoritative planner before calling it.  Optional
    ``charge_availability`` values from 0 to 1 describe the fraction of each
    interval in which charging is permitted.  System-wide charge/discharge
    limits cap the aggregate AC-side flow in each interval in addition to every
    battery's own power limit.
    """
    interval_list = [
        _coerce_interval(value, index) for index, value in enumerate(intervals)
    ]
    battery_list = list(batteries or ())
    allocation_source = allocations if allocations is not None else grid_charge_kwh
    if allocation_energy_kind not in {"stored", "input"}:
        raise ValueError("allocation_energy_kind must be 'stored' or 'input'")

    valid: list[
        tuple[BatteryProjectionInput, float, float, float, float, float, float]
    ] = []
    for battery in battery_list:
        limits = _battery_limits(battery)
        if limits is None:
            continue
        stored, minimum, maximum, charge_power, discharge_power, capacity = limits
        valid.append(
            (
                battery,
                stored,
                minimum,
                maximum,
                charge_power,
                discharge_power,
                capacity,
            )
        )

    states = {str(battery.key): stored for battery, stored, *_rest in valid}
    battery_results: dict[str, list[ProjectedBatteryFlow]] = {
        str(battery.key): [] for battery, *_rest in valid
    }
    result_flows: list[ProjectedIntervalFlow] = []
    battery_keys = {str(battery.key) for battery, *_rest in valid}
    global_allocations, per_battery_allocations = _per_battery_sources(
        allocation_source, battery_keys
    )

    for index, interval in enumerate(interval_list):
        physical_seconds = (
            _non_negative(interval.duration_seconds)
            if interval.duration_seconds is not None
            else _duration_seconds(interval.start, interval.end)
        )
        duration_hours = physical_seconds / 3600.0
        solar = _non_negative(interval.solar_kwh)
        consumption = _non_negative(interval.consumption_kwh)
        direct_solar = min(solar, consumption)
        solar_surplus = max(0.0, solar - direct_solar)
        charge_fraction = _bounded(
            _metadata_value(charge_availability, index, interval_list),
            0.0,
            1.0,
            1.0,
        )
        solar_charge_available = solar_surplus * charge_fraction
        deficit = max(0.0, consumption - direct_solar)

        solar_to_battery = 0.0
        grid_to_battery = 0.0
        battery_to_home = 0.0
        remaining_deficit = deficit
        stored_energy_start = sum(states.values())
        stored_energy_charged = 0.0
        stored_energy_discharged = 0.0
        per_interval: list[ProjectedBatteryFlow] = []
        per_battery: dict[str, dict[str, float]] = {}
        global_quota_remaining = (
            _amount_from_source(global_allocations, index, interval_list)
            if not per_battery_allocations
            else 0.0
        )
        system_charge_remaining = (
            _non_negative(system_charge_power_w)
            * duration_hours
            * charge_fraction
            / 1000.0
            if system_charge_power_w is not None
            else math.inf
        )
        system_discharge_remaining = (
            _non_negative(system_discharge_power_w) * duration_hours / 1000.0
            if system_discharge_power_w is not None
            else math.inf
        )

        # Solar is consumed locally before it is offered to the batteries.
        for (
            battery,
            _initial,
            minimum,
            maximum,
            charge_power,
            discharge_power,
            _capacity,
        ) in valid:
            key = str(battery.key)
            start_stored = states[key]
            charge_eff = _effective_efficiency(
                battery.charge_efficiency, charge_efficiency
            )
            discharge_eff = _effective_efficiency(
                battery.discharge_efficiency, discharge_efficiency
            )
            if (
                not battery.can_charge
                or charge_eff <= _EPSILON
                or duration_hours <= _EPSILON
            ):
                solar_input = 0.0
            else:
                power_cap = (
                    charge_power * duration_hours * charge_fraction / 1000.0
                )
                headroom_input = max(0.0, (maximum - states[key]) / charge_eff)
                solar_input = min(
                    solar_charge_available,
                    power_cap,
                    headroom_input,
                    system_charge_remaining,
                )
            solar_charge_available -= solar_input
            solar_surplus -= solar_input
            system_charge_remaining = max(0.0, system_charge_remaining - solar_input)
            states[key] += solar_input * charge_eff
            solar_to_battery += solar_input
            stored_energy_charged += solar_input * charge_eff

            # Grid quota is applied after solar.  The per-battery charge power
            # limit is shared by both sources in this interval.
            if per_battery_allocations:
                quota_value = _amount_from_source(
                    per_battery_allocations.get(key), index, interval_list, key
                )
            else:
                quota_value = global_quota_remaining
            if allocation_energy_kind == "input":
                quota_input = quota_value
            else:
                quota_input = quota_value / charge_eff if charge_eff > _EPSILON else 0.0
            if (
                not battery.can_charge
                or charge_eff <= _EPSILON
                or duration_hours <= _EPSILON
            ):
                grid_input = 0.0
            else:
                power_cap = max(
                    0.0,
                    charge_power * duration_hours * charge_fraction / 1000.0
                    - solar_input,
                )
                headroom_input = max(0.0, (maximum - states[key]) / charge_eff)
                grid_input = min(
                    max(0.0, quota_input),
                    power_cap,
                    headroom_input,
                    system_charge_remaining,
                )
            if not per_battery_allocations:
                global_quota_remaining = max(
                    0.0,
                    global_quota_remaining
                    - (
                        grid_input
                        if allocation_energy_kind == "input"
                        else grid_input * charge_eff
                    ),
                )
            states[key] += grid_input * charge_eff
            system_charge_remaining = max(0.0, system_charge_remaining - grid_input)
            grid_to_battery += grid_input
            stored_energy_charged += grid_input * charge_eff

            per_battery[key] = {
                "start_stored": start_stored,
                "solar_input": solar_input,
                "grid_input": grid_input,
                "charge_eff": charge_eff,
                "discharge_eff": discharge_eff,
                "minimum": minimum,
                "discharge_power": discharge_power,
                "discharge_output": 0.0,
            }

        # Run discharge after all charge allocation.  A given battery cannot
        # charge and discharge in the same interval, but distinct batteries
        # may still coexist in opposite directions.
        for (
            battery,
            _initial,
            _minimum,
            _maximum,
            _charge_power,
            _discharge_power,
            _capacity,
        ) in valid:
            key = str(battery.key)
            values = per_battery[key]
            discharge_eff = values["discharge_eff"]
            if values["solar_input"] > _EPSILON or values["grid_input"] > _EPSILON:
                discharge_output = 0.0
            elif (
                not battery.can_discharge
                or discharge_eff <= _EPSILON
                or duration_hours <= _EPSILON
            ):
                discharge_output = 0.0
            else:
                power_cap = values["discharge_power"] * duration_hours / 1000.0
                available_output = max(
                    0.0, (states[key] - values["minimum"]) * discharge_eff
                )
                discharge_output = min(
                    remaining_deficit,
                    power_cap,
                    available_output,
                    system_discharge_remaining,
                )
            values["discharge_output"] = discharge_output
            system_discharge_remaining = max(
                0.0, system_discharge_remaining - discharge_output
            )
            states[key] -= (
                discharge_output / discharge_eff if discharge_eff > _EPSILON else 0.0
            )
            battery_to_home += discharge_output
            remaining_deficit = max(0.0, remaining_deficit - discharge_output)
            stored_energy_discharged += (
                discharge_output / discharge_eff if discharge_eff > _EPSILON else 0.0
            )

        for (
            battery,
            _initial,
            _minimum,
            _maximum,
            _charge_power,
            _discharge_power,
            _capacity,
        ) in valid:
            key = str(battery.key)
            values = per_battery[key]
            solar_input = values["solar_input"]
            grid_input = values["grid_input"]
            discharge_output = values["discharge_output"]
            charge_eff = values["charge_eff"]
            discharge_eff = values["discharge_eff"]
            per_interval.append(
                ProjectedBatteryFlow(
                    battery_key=key,
                    start=interval.start,
                    end=interval.end,
                    solar_to_battery_kwh=solar_input,
                    grid_to_battery_kwh=grid_input,
                    battery_to_home_kwh=discharge_output,
                    stored_energy_start_kwh=values["start_stored"],
                    stored_energy_end_kwh=states[key],
                    stored_energy_charged_kwh=(solar_input + grid_input) * charge_eff,
                    stored_energy_discharged_kwh=(
                        discharge_output / discharge_eff
                        if discharge_eff > _EPSILON
                        else 0.0
                    ),
                    action_mask=compose_action_mask(
                        solar_charge=solar_input > _EPSILON,
                        grid_charge=grid_input > _EPSILON,
                        discharge=discharge_output > _EPSILON,
                    ),
                )
            )

        # All remaining household demand is supplied by the grid.  Any solar
        # surplus which could not fit is curtailed; no export is invented.
        grid_to_home = max(0.0, deficit - battery_to_home)
        curtailed_solar = max(0.0, solar_surplus)
        decision = _metadata_value(grid_charge_decisions, index, interval_list)
        if decision is None:
            decision = (
                GRID_CHARGE_SCHEDULED
                if grid_to_battery > _EPSILON and allocation_source is not None
                else interval.grid_charge_decision
            )
        context = _metadata_value(context_masks, index, interval_list)
        if context is None:
            context = interval.context_mask
        action_mask = compose_action_mask(
            solar_charge=solar_to_battery > _EPSILON,
            grid_charge=grid_to_battery > _EPSILON,
            discharge=battery_to_home > _EPSILON,
        )
        projected = interval.projected
        if projected is None:
            projected = interval.state == STATE_FUTURE
        charge_power = (
            (solar_to_battery + grid_to_battery) / duration_hours * 1000.0
            if duration_hours > _EPSILON
            else 0.0
        )
        discharge_power = (
            battery_to_home / duration_hours * 1000.0
            if duration_hours > _EPSILON
            else 0.0
        )
        result_flows.append(
            ProjectedIntervalFlow(
                start=interval.start,
                end=interval.end,
                solar_kwh=solar,
                consumption_kwh=consumption,
                solar_to_battery_kwh=solar_to_battery,
                grid_to_battery_kwh=grid_to_battery,
                battery_to_home_kwh=battery_to_home,
                grid_to_home_kwh=grid_to_home,
                stored_energy_end_kwh=sum(states.values()),
                action_mask=action_mask,
                context_mask=context,
                grid_charge_decision=decision,
                state=interval.state,
                dst_status=interval.dst_status,
                duration_seconds=physical_seconds,
                coverage_seconds=interval.coverage_seconds,
                stored_energy_start_kwh=stored_energy_start,
                solar_to_home_kwh=direct_solar,
                curtailed_solar_kwh=curtailed_solar,
                stored_energy_charged_kwh=stored_energy_charged,
                stored_energy_discharged_kwh=stored_energy_discharged,
                charge_power_w=charge_power,
                discharge_power_w=discharge_power,
                projected=projected,
            )
        )
        for item in per_interval:
            battery_results[item.battery_key].append(item)

    return BatteryProjectionResult(
        intervals=tuple(result_flows),
        battery_flows={key: tuple(value) for key, value in battery_results.items()},
        final_stored_kwh_by_battery=dict(states),
    )


def simulate_battery_flows(
    intervals: Iterable[Any],
    batteries: Sequence[BatteryProjectionInput],
    **kwargs: Any,
) -> list[ProjectedIntervalFlow]:
    """Return aggregate projected flows as a list for simple callers."""
    return list(simulate_battery_projection(intervals, batteries, **kwargs).intervals)


project_battery_flows = simulate_battery_flows


def simulate_per_battery_flows(
    intervals: Iterable[Any],
    batteries: Sequence[BatteryProjectionInput],
    **kwargs: Any,
) -> Mapping[str, tuple[ProjectedBatteryFlow, ...]]:
    """Return the detailed immutable flow series grouped by battery key."""
    return simulate_battery_projection(intervals, batteries, **kwargs).battery_flows


@dataclass(frozen=True)
class ChargeDelayProjection:
    """Side-effect-free, bounded estimate of the setpoint/delay milestones."""

    setpoint_reached_at: datetime | None
    delay_starts_at: datetime | None
    estimated_unlock_at: datetime | None
    source: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return json_safe(self)

    as_dict = to_dict


def project_charge_delay(
    intervals: Iterable[Any],
    batteries: Sequence[BatteryProjectionInput],
    *,
    setpoint_soc_pct: float | None = None,
    target_soc_pct: float | None = None,
    enabled: bool = True,
    charge_delay_enabled: bool = True,
    now: datetime | None = None,
    allocations: Any = None,
    grid_charge_kwh: Any = None,
    allocation_energy_kind: str = "stored",
    charge_efficiency: float = CHARGE_EFFICIENCY,
    discharge_efficiency: float = 1.0,
    unlock_at: datetime | None = None,
    solar_t_end: datetime | None = None,
    safety_margin_h: float | None = None,
    system_charge_power_w: float | None = None,
    system_discharge_power_w: float | None = None,
) -> ChargeDelayProjection:
    """Project only observable ChargeDelay milestones without controller calls.

    The function can estimate when every eligible battery reaches a supplied
    SOC setpoint using the same interval and quota model as the flow simulator.
    When ``target_soc_pct``, ``solar_t_end`` and ``safety_margin_h`` are also
    supplied, the projected unlock includes the same time-backup boundary used
    by the live charge-delay controller: the final target must be reachable
    before the end of solar production minus the configured margin.  An unlock
    timestamp is authoritative only when supplied explicitly.  Otherwise the
    fallback is the earliest available boundary between that time-backup edge
    and the first post-setpoint interval whose forecasted solar is below
    household consumption; it does not model prices, mutable latches, or
    reactive blockers.
    """
    if not enabled or not charge_delay_enabled:
        return ChargeDelayProjection(None, None, None, "disabled", "disabled")
    interval_list = [
        _coerce_interval(value, index) for index, value in enumerate(intervals)
    ]
    valid = [
        battery for battery in batteries or () if _battery_limits(battery) is not None
    ]
    if not valid:
        return ChargeDelayProjection(
            None, None, None, "pure_projection", "no_batteries"
        )
    setpoint_target = setpoint_soc_pct
    if setpoint_target is None:
        setpoint_target = target_soc_pct
    if setpoint_target is None:
        setpoint_target = min(
            _bounded(battery.max_soc_pct, 0.0, 100.0, 0.0) for battery in valid
        )
    setpoint_target = _bounded(setpoint_target, 0.0, 100.0, 0.0)
    final_target = _bounded(
        target_soc_pct if target_soc_pct is not None else setpoint_target,
        0.0,
        100.0,
        setpoint_target,
    )

    result = simulate_battery_projection(
        interval_list,
        valid,
        allocations=allocations,
        grid_charge_kwh=grid_charge_kwh,
        allocation_energy_kind=allocation_energy_kind,
        charge_efficiency=charge_efficiency,
        discharge_efficiency=discharge_efficiency,
        system_charge_power_w=system_charge_power_w,
        system_discharge_power_w=system_discharge_power_w,
    )
    target_energy = {
        str(battery.key): _finite(battery.capacity_kwh)
        * min(
            setpoint_target,
            _bounded(battery.max_soc_pct, 0.0, 100.0, setpoint_target),
        )
        / 100.0
        for battery in valid
    }
    start_time = now
    if start_time is None and interval_list:
        start_time = interval_list[0].start
    initial_reached = all(
        _battery_limits(battery) is not None
        and _battery_limits(battery)[0] + _EPSILON >= target_energy[str(battery.key)]
        for battery in valid
    )
    reached_at = start_time if initial_reached else None
    if reached_at is None:
        for index, flow in enumerate(result.intervals):
            if all(
                result.battery_flows[str(battery.key)][index].stored_energy_end_kwh
                + _EPSILON
                >= target_energy[str(battery.key)]
                for battery in valid
                if index < len(result.battery_flows[str(battery.key)])
            ):
                reached_at = flow.end
                break
    if reached_at is None:
        return ChargeDelayProjection(
            None, None, None, "pure_projection", "setpoint_not_reached"
        )

    unlock = unlock_at
    reason = "explicit_unlock" if unlock is not None else "no_projected_unlock"
    if unlock is None:
        time_backup_unlock = None
        if isinstance(solar_t_end, datetime) and safety_margin_h is not None:
            margin_h = _finite(safety_margin_h, -1.0)
            if margin_h >= 0.0:
                remaining_input_kwh = 0.0
                charge_power_w = 0.0
                can_estimate_time = True
                for battery in valid:
                    limits = _battery_limits(battery)
                    if limits is None:
                        continue
                    initial_stored, _minimum, maximum, power_w, _discharge, capacity = limits
                    setpoint_stored = target_energy[str(battery.key)]
                    stored_at_delay_start = max(initial_stored, setpoint_stored)
                    final_stored = min(
                        maximum,
                        capacity * min(
                            final_target,
                            _bounded(battery.max_soc_pct, 0.0, 100.0, final_target),
                        )
                        / 100.0,
                    )
                    remaining_stored_kwh = max(
                        0.0, final_stored - stored_at_delay_start
                    )
                    if remaining_stored_kwh <= _EPSILON:
                        continue
                    efficiency = _effective_efficiency(
                        battery.charge_efficiency, charge_efficiency
                    )
                    if not battery.can_charge or efficiency <= _EPSILON:
                        can_estimate_time = False
                        break
                    remaining_input_kwh += remaining_stored_kwh / efficiency
                    charge_power_w += power_w

                if can_estimate_time:
                    if remaining_input_kwh <= _EPSILON:
                        time_backup_unlock = reached_at
                    else:
                        if system_charge_power_w is not None:
                            charge_power_w = min(
                                charge_power_w,
                                _non_negative(system_charge_power_w),
                            )
                        if charge_power_w > _EPSILON:
                            charge_time_h = remaining_input_kwh / (
                                charge_power_w / 1000.0
                            )
                            time_backup_unlock = solar_t_end - timedelta(
                                hours=charge_time_h + margin_h
                            )
                            if _timestamp(time_backup_unlock) < _timestamp(reached_at):
                                time_backup_unlock = reached_at

        if time_backup_unlock is not None:
            unlock = time_backup_unlock
            reason = "projected_time_backup"

        for flow in result.intervals:
            if flow.end is None or _timestamp(flow.end) <= _timestamp(reached_at):
                continue
            if flow.solar_kwh + _EPSILON < flow.consumption_kwh:
                solar_deficit_unlock = flow.start
                if (
                    unlock is None
                    or _timestamp(solar_deficit_unlock) < _timestamp(unlock)
                ):
                    unlock = solar_deficit_unlock
                    reason = "projected_solar_deficit"
                break
    if unlock is not None and _timestamp(unlock) < _timestamp(reached_at):
        unlock = reached_at
    return ChargeDelayProjection(
        setpoint_reached_at=reached_at,
        delay_starts_at=reached_at,
        estimated_unlock_at=unlock,
        source="pure_projection",
        reason=reason,
    )


estimate_charge_delay = project_charge_delay


def _merge_decisions(values: Sequence[str]) -> str:
    decisions = [normalize_grid_charge_decision(value) for value in values]
    if GRID_CHARGE_SCHEDULED in decisions:
        return GRID_CHARGE_SCHEDULED
    if GRID_CHARGE_UNKNOWN in decisions:
        return GRID_CHARGE_UNKNOWN
    if GRID_CHARGE_NOT_NEEDED in decisions:
        return GRID_CHARGE_NOT_NEEDED
    return GRID_CHARGE_NOT_APPLICABLE


@dataclass(frozen=True)
class DailyOperationTimelineCell:
    """A grid cell plus its optional aggregate flow."""

    grid: LocalTimelineInterval
    flow: ProjectedIntervalFlow | None = None

    @property
    def index(self) -> int:
        return self.grid.index

    @property
    def state(self) -> str:
        return self.grid.state

    def to_dict(self) -> dict[str, Any]:
        data = self.grid.to_dict()
        data["flow"] = self.flow.to_dict() if self.flow is not None else None
        return json_safe(data)


def _flow_from_value(value: Any) -> ProjectedIntervalFlow | None:
    if value is None:
        return None
    if isinstance(value, ProjectedIntervalFlow):
        return value
    if isinstance(value, Mapping):
        names = {item.name for item in fields(ProjectedIntervalFlow)}
        data = {key: value[key] for key in names if key in value}
        for key in ("start", "end"):
            if isinstance(data.get(key), str):
                data[key] = _parse_datetime(data[key])
        if isinstance(data.get("delay_until"), str):
            parsed_delay = _parse_datetime(data["delay_until"])
            if parsed_delay is not None:
                data["delay_until"] = parsed_delay
        return ProjectedIntervalFlow(**data)
    return None


def _merge_flows(
    values: Sequence[ProjectedIntervalFlow],
    cell: LocalTimelineInterval,
) -> ProjectedIntervalFlow | None:
    if not values or cell.dst_skipped:
        return None
    if len(values) == 1:
        return replace(
            values[0],
            state=cell.state,
            dst_status=cell.dst_status,
            duration_seconds=cell.duration_seconds,
            wall_index=cell.index,
        )

    sum_fields = (
        "solar_kwh",
        "consumption_kwh",
        "solar_to_battery_kwh",
        "grid_to_battery_kwh",
        "battery_to_home_kwh",
        "grid_to_home_kwh",
        "solar_to_home_kwh",
        "curtailed_solar_kwh",
        "stored_energy_charged_kwh",
        "stored_energy_discharged_kwh",
        "coverage_seconds",
    )
    merged: dict[str, Any] = {
        name: sum(
            _non_negative(getattr(value, name))
            for value in values
            if getattr(value, name) is not None
        )
        if any(getattr(value, name) is not None for value in values)
        else None
        for name in sum_fields
    }
    ordered = sorted(
        values,
        key=lambda value: (
            _timestamp(value.start) if value.start is not None else float("inf")
        ),
    )
    starts = [
        value.stored_energy_start_kwh
        for value in ordered
        if value.stored_energy_start_kwh is not None
    ]
    ends = [
        value.stored_energy_end_kwh
        for value in ordered
        if value.stored_energy_end_kwh is not None
    ]
    charge_power = sum(
        _non_negative(value.charge_power_w)
        for value in values
        if value.charge_power_w is not None
    )
    discharge_power = sum(
        _non_negative(value.discharge_power_w)
        for value in values
        if value.discharge_power_w is not None
    )
    projected_values = [
        value.projected for value in values if value.projected is not None
    ]
    action_mask = 0
    context_mask = 0
    for value in values:
        action_mask |= value.action_mask
        context_mask |= value.context_mask
    return ProjectedIntervalFlow(
        start=cell.start,
        end=cell.end,
        solar_kwh=merged["solar_kwh"] or 0.0,
        consumption_kwh=merged["consumption_kwh"] or 0.0,
        solar_to_battery_kwh=merged["solar_to_battery_kwh"] or 0.0,
        grid_to_battery_kwh=merged["grid_to_battery_kwh"] or 0.0,
        battery_to_home_kwh=merged["battery_to_home_kwh"] or 0.0,
        grid_to_home_kwh=merged["grid_to_home_kwh"] or 0.0,
        stored_energy_end_kwh=ends[-1] if ends else 0.0,
        action_mask=action_mask,
        context_mask=context_mask,
        grid_charge_decision=_merge_decisions(
            [value.grid_charge_decision for value in values]
        ),
        state=cell.state,
        dst_status=cell.dst_status,
        duration_seconds=cell.duration_seconds,
        coverage_seconds=merged["coverage_seconds"],
        stored_energy_start_kwh=starts[0] if starts else None,
        solar_to_home_kwh=merged["solar_to_home_kwh"],
        curtailed_solar_kwh=merged["curtailed_solar_kwh"],
        stored_energy_charged_kwh=merged["stored_energy_charged_kwh"],
        stored_energy_discharged_kwh=merged["stored_energy_discharged_kwh"],
        charge_power_w=charge_power,
        discharge_power_w=discharge_power,
        delay_until=next(
            (value.delay_until for value in ordered if value.delay_until is not None),
            None,
        ),
        projected=True
        if any(projected_values)
        else (False if projected_values else None),
        wall_index=cell.index,
        reason=next((value.reason for value in ordered if value.reason), None),
    )


def _align_flows(
    grid: Sequence[LocalTimelineInterval],
    flows: Any,
) -> list[list[ProjectedIntervalFlow]]:
    aligned: list[list[ProjectedIntervalFlow]] = [[] for _ in grid]
    if flows is None:
        return aligned
    if isinstance(flows, BatteryProjectionResult):
        flows = flows.intervals
    mapping_values: dict[int, Any] = {}
    sequence_values: list[Any] = []
    if isinstance(flows, Mapping):
        for key, value in flows.items():
            try:
                mapping_values[int(key)] = value
            except (TypeError, ValueError):
                continue
    elif isinstance(flows, Sequence) and not isinstance(flows, (str, bytes)):
        sequence_values = list(flows)
    else:
        sequence_values = list(flows) if isinstance(flows, Iterable) else []

    if mapping_values:
        for index, value in mapping_values.items():
            if 0 <= index < len(grid):
                flow = _flow_from_value(value)
                if flow is not None:
                    aligned[index].append(flow)
        return aligned

    flat_occurrences = [
        (cell.index, occurrence) for cell in grid for occurrence in cell.occurrences
    ]
    flat_occurrences.sort(key=lambda item: _timestamp(item[1][0]))
    if len(sequence_values) == len(grid):
        for index, value in enumerate(sequence_values):
            flow = _flow_from_value(value)
            if flow is not None:
                aligned[index].append(flow)
        return aligned
    if sequence_values and len(sequence_values) == len(flat_occurrences):
        for value, (index, _occurrence) in zip(sequence_values, flat_occurrences):
            flow = _flow_from_value(value)
            if flow is not None:
                aligned[index].append(flow)
        return aligned

    for value in sequence_values:
        flow = _flow_from_value(value)
        if flow is None:
            continue
        if flow.wall_index is not None and 0 <= flow.wall_index < len(grid):
            aligned[flow.wall_index].append(flow)
            continue
        if flow.start is None:
            continue
        flow_timestamp = _timestamp(flow.start)
        for index, cell in enumerate(grid):
            if any(
                _timestamp(start) <= flow_timestamp < _timestamp(end)
                for start, end in cell.occurrences
            ):
                aligned[index].append(flow)
                break
    return aligned


def _snapshot_series(
    cells: Sequence[DailyOperationTimelineCell],
) -> dict[str, list[Any]]:
    keys = (
        "solar_actual_kwh",
        "solar_forecast_kwh",
        "consumption_actual_kwh",
        "consumption_forecast_kwh",
        "actual_coverage_s",
    )
    series = {key: [None] * INTERVAL_COUNT for key in keys}
    for cell in cells:
        flow = cell.flow
        if flow is None or cell.grid.dst_skipped:
            continue
        index = cell.index
        is_future = cell.state == STATE_FUTURE or flow.projected is True
        if is_future:
            series["solar_forecast_kwh"][index] = flow.solar_kwh
            series["consumption_forecast_kwh"][index] = flow.consumption_kwh
        else:
            series["solar_actual_kwh"][index] = flow.solar_kwh
            series["consumption_actual_kwh"][index] = flow.consumption_kwh
            series["actual_coverage_s"][index] = flow.coverage_seconds
    return json_safe(series)


def _snapshot_operations(
    cells: Sequence[DailyOperationTimelineCell],
) -> dict[str, list[Any]]:
    operations = {
        "actual_action_mask": [None] * INTERVAL_COUNT,
        "planned_action_mask": [None] * INTERVAL_COUNT,
        "actual_context_mask": [None] * INTERVAL_COUNT,
        "planned_context_mask": [None] * INTERVAL_COUNT,
        "grid_charge_decision": [None] * INTERVAL_COUNT,
        "delay_until": [None] * INTERVAL_COUNT,
        "charge_power_w": [None] * INTERVAL_COUNT,
        "discharge_power_w": [None] * INTERVAL_COUNT,
    }
    for cell in cells:
        flow = cell.flow
        if flow is None or cell.grid.dst_skipped:
            continue
        index = cell.index
        planned = cell.state == STATE_FUTURE or flow.projected is True
        if planned:
            operations["planned_action_mask"][index] = flow.action_mask
            operations["planned_context_mask"][index] = flow.context_mask
        else:
            operations["actual_action_mask"][index] = flow.action_mask
            operations["actual_context_mask"][index] = flow.context_mask
        operations["grid_charge_decision"][index] = flow.grid_charge_decision
        operations["delay_until"][index] = flow.delay_until
        operations["charge_power_w"][index] = flow.charge_power_w
        operations["discharge_power_w"][index] = flow.discharge_power_w
    return json_safe(operations)


def local_grid_to_dict(grid: Sequence[LocalTimelineInterval]) -> dict[str, list[Any]]:
    """Serialize the fixed grid in the compact array form used by HA DTOs."""
    values = tuple(grid)
    if len(values) != INTERVAL_COUNT:
        raise ValueError(
            f"daily timeline requires exactly {INTERVAL_COUNT} grid intervals"
        )
    return json_safe(
        {
            "labels": [item.label for item in values],
            "starts": [item.start for item in values],
            "ends": [item.end for item in values],
            "duration_s": [item.duration_seconds for item in values],
            "dst_skipped": [item.dst_skipped for item in values],
            "dst_repeated": [item.dst_repeated for item in values],
        }
    )


build_interval_grid = build_local_grid


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class DailyOperationTimelineSnapshot:
    """Versioned, fixed-size snapshot suitable for a Home Assistant attribute."""

    local_date: date | str
    timezone: str
    intervals: tuple[DailyOperationTimelineCell, ...] = ()
    schema_version: int = SCHEMA_VERSION
    interval_minutes: int = INTERVAL_MINUTES
    interval_count: int = INTERVAL_COUNT
    generated_at: datetime | str | None = None
    plan_evaluated_at: datetime | str | None = None
    current_index: int | None = None
    current_progress: float | None = None
    mode: str | None = None
    stale: bool = False
    stale_reason: str | None = None
    sources: Mapping[str, Any] | None = None
    interval_grid: Mapping[str, Any] | None = None
    series: Mapping[str, Any] | None = None
    operations: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        values = tuple(self.intervals)
        if not values:
            parsed_date = (
                _parse_date(self.local_date) or datetime.now(dt_timezone.utc).date()
            )
            generated = _parse_datetime(self.generated_at)
            values = tuple(
                DailyOperationTimelineCell(item)
                for item in build_local_grid(parsed_date, self.timezone, now=generated)
            )
        if len(values) != INTERVAL_COUNT:
            raise ValueError(
                f"daily timeline requires exactly {INTERVAL_COUNT} intervals"
            )
        object.__setattr__(self, "intervals", values)
        object.__setattr__(self, "interval_count", INTERVAL_COUNT)
        object.__setattr__(self, "interval_minutes", INTERVAL_MINUTES)
        if self.current_index is None:
            current = next(
                (cell.index for cell in values if cell.state == STATE_CURRENT), None
            )
            object.__setattr__(self, "current_index", current)
        if self.current_progress is not None:
            object.__setattr__(
                self,
                "current_progress",
                _bounded(self.current_progress, 0.0, 1.0, 0.0),
            )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DailyOperationTimelineSnapshot:
        """Rehydrate the typed view from this module's JSON-safe payload."""
        parsed_date = (
            _parse_date(payload.get("local_date"))
            or datetime.now(dt_timezone.utc).date()
        )
        timezone_name = str(payload.get("timezone", "UTC"))
        raw_intervals = payload.get("intervals")
        if (
            not isinstance(raw_intervals, Sequence)
            or isinstance(raw_intervals, (str, bytes))
            or len(raw_intervals) != INTERVAL_COUNT
        ):
            generated = _parse_datetime(payload.get("generated_at"))
            grid = build_local_grid(parsed_date, timezone_name, now=generated)
            cells = tuple(DailyOperationTimelineCell(item) for item in grid)
        else:
            cells_list: list[DailyOperationTimelineCell] = []
            for index, raw in enumerate(raw_intervals):
                item = raw if isinstance(raw, Mapping) else {}
                status = item.get("dst_status")
                if status is None:
                    status = (
                        DST_SKIPPED
                        if item.get("dst_skipped")
                        else (DST_REPEATED if item.get("dst_repeated") else DST_NORMAL)
                    )
                start = _parse_datetime(item.get("start"))
                end = _parse_datetime(item.get("end"))
                occurrences: list[tuple[datetime, datetime]] = []
                for occurrence in item.get("occurrences", ()):
                    if isinstance(occurrence, Sequence) and len(occurrence) == 2:
                        occurrence_start = _parse_datetime(occurrence[0])
                        occurrence_end = _parse_datetime(occurrence[1])
                        if occurrence_start is not None and occurrence_end is not None:
                            occurrences.append((occurrence_start, occurrence_end))
                if not occurrences and start is not None and end is not None:
                    occurrences.append((start, end))
                grid_item = LocalTimelineInterval(
                    index=int(item.get("index", index)),
                    label=str(
                        item.get("label", f"{index // 4:02d}:{(index % 4) * 15:02d}")
                    ),
                    start=start,
                    end=end,
                    duration_seconds=_non_negative(
                        item.get("duration_seconds", item.get("duration_s", 0.0))
                    ),
                    state=_normalise_state(item.get("state")),
                    dst_status=_normalise_dst(status),
                    occurrences=tuple(occurrences),
                    local_date=parsed_date,
                    timezone=timezone_name,
                )
                cells_list.append(
                    DailyOperationTimelineCell(
                        grid_item, _flow_from_value(item.get("flow"))
                    )
                )
            cells = tuple(cells_list)
        return cls(
            local_date=payload.get("local_date", parsed_date),
            timezone=timezone_name,
            intervals=cells,
            schema_version=int(payload.get("schema_version", SCHEMA_VERSION)),
            generated_at=_parse_datetime(payload.get("generated_at")),
            plan_evaluated_at=_parse_datetime(payload.get("plan_evaluated_at")),
            current_index=payload.get("current_index"),
            current_progress=payload.get("current_progress"),
            mode=payload.get("mode"),
            stale=bool(payload.get("stale", False)),
            stale_reason=payload.get("stale_reason"),
            sources=payload.get("sources") or {},
            interval_grid=payload.get("interval_grid"),
            interval_minutes=payload.get("interval_minutes", INTERVAL_MINUTES),
            interval_count=payload.get("interval_count", INTERVAL_COUNT),
            series=payload.get("series"),
            operations=payload.get("operations"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
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
            "stale": bool(self.stale),
            "stale_reason": self.stale_reason,
            "sources": self.sources or {},
            "intervals": [cell.to_dict() for cell in self.intervals],
            "series": self.series
            if self.series is not None
            else _snapshot_series(self.intervals),
            "operations": self.operations
            if self.operations is not None
            else _snapshot_operations(self.intervals),
            "interval_grid": self.interval_grid
            or local_grid_to_dict([cell.grid for cell in self.intervals]),
        }
        return json_safe(payload)

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self):
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


def build_daily_operation_snapshot(
    grid: Sequence[LocalTimelineInterval],
    flows: Any = None,
    *,
    generated_at: datetime | None = None,
    plan_evaluated_at: datetime | None = None,
    current_index: int | None = None,
    current_progress: float | None = None,
    mode: str | None = None,
    stale: bool = False,
    stale_reason: str | None = None,
    sources: Mapping[str, Any] | None = None,
    local_date: date | str | None = None,
    timezone: str | None = None,
) -> DailyOperationTimelineSnapshot:
    """Combine a local grid and aligned/occurrence flows into a 96-cell DTO."""
    grid_values = tuple(grid)
    if len(grid_values) != INTERVAL_COUNT:
        raise ValueError(
            f"daily timeline requires exactly {INTERVAL_COUNT} grid intervals"
        )
    aligned = _align_flows(grid_values, flows)
    cells = tuple(
        DailyOperationTimelineCell(
            grid=cell,
            flow=_merge_flows(aligned[index], cell),
        )
        for index, cell in enumerate(grid_values)
    )
    inferred_date = local_date
    if inferred_date is None:
        inferred_date = next(
            (cell.local_date for cell in grid_values if cell.local_date), None
        )
    if inferred_date is None:
        inferred_date = datetime.now(dt_timezone.utc).date()
    inferred_timezone = timezone or next(
        (cell.timezone for cell in grid_values if cell.timezone), "UTC"
    )
    return DailyOperationTimelineSnapshot(
        local_date=inferred_date,
        timezone=inferred_timezone,
        intervals=cells,
        generated_at=generated_at,
        plan_evaluated_at=plan_evaluated_at,
        current_index=current_index,
        current_progress=current_progress,
        mode=mode,
        stale=stale,
        stale_reason=stale_reason,
        sources=sources,
    )


build_timeline_snapshot = build_daily_operation_snapshot
snapshot_from_grid = build_daily_operation_snapshot

# Compatibility names used by the runtime diary and by early dashboard
# prototypes.  They point to the same immutable DTOs; no second contract is
# maintained here.
DailyTimelineInterval = ProjectedIntervalFlow
ProjectedOperationInterval = ProjectedIntervalFlow
DailyTimelineSnapshot = DailyOperationTimelineSnapshot
DailyTimelineDTO = DailyOperationTimelineSnapshot


def snapshot_to_dict(snapshot: DailyOperationTimelineSnapshot) -> dict[str, Any]:
    """Serialize a snapshot without exposing runtime dataclasses."""
    if isinstance(snapshot, DailyOperationTimelineSnapshot):
        return snapshot.to_dict()
    if isinstance(snapshot, Mapping):
        return json_safe(snapshot)
    raise TypeError("snapshot must be a DailyOperationTimelineSnapshot or mapping")


def serialize_snapshot(snapshot: DailyOperationTimelineSnapshot) -> str:
    return json.dumps(
        snapshot_to_dict(snapshot),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


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
    "SCHEMA_VERSION",
    "STATE_CURRENT",
    "STATE_FUTURE",
    "STATE_PAST",
    "BatteryProjectionInput",
    "BatteryProjectionResult",
    "ChargeDelayProjection",
    "DailyOperationTimelineCell",
    "DailyOperationTimelineSnapshot",
    "DailyTimelineDTO",
    "DailyTimelineInterval",
    "DailyTimelineSnapshot",
    "LocalTimelineInterval",
    "ProjectedBatteryFlow",
    "ProjectedIntervalFlow",
    "ProjectedOperationInterval",
    "ProjectionIntervalInput",
    "actions_for_mask",
    "build_daily_operation_snapshot",
    "build_interval_grid",
    "build_local_grid",
    "build_local_quarter_grid",
    "build_local_timeline",
    "build_timeline_snapshot",
    "compose_action_mask",
    "compose_context_mask",
    "contexts_for_mask",
    "decode_action_mask",
    "decode_context_mask",
    "estimate_charge_delay",
    "has_action",
    "has_context",
    "json_safe",
    "local_grid_to_dict",
    "make_action_mask",
    "make_context_mask",
    "normalize_grid_charge_decision",
    "project_battery_flows",
    "project_charge_delay",
    "serialize_snapshot",
    "simulate_battery_flows",
    "simulate_battery_projection",
    "simulate_per_battery_flows",
    "snapshot_from_grid",
    "snapshot_to_dict",
]
