"""Read solar forecast sensors with an explicit forecast horizon.

``solar_forecast_sensor`` is the legacy whole-day (``today``) value.  Newer
providers also expose a ``remaining today`` value.  Keeping the distinction in
one place prevents callers from accidentally subtracting production twice.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .const import (
    CONF_SOLAR_FORECAST_REMAINING_SENSOR,
    CONF_SOLAR_FORECAST_SENSOR,
    T_START_FALLBACK_HOUR,
)


ForecastSource = Literal["remaining", "today"]
_FORECAST_EPSILON_KWH = 1e-9


@dataclass(frozen=True)
class SolarForecastPeriod:
    """A dated, energy-valued period supplied by a forecast provider.

    The planner deliberately accepts periods rather than positional arrays.
    A timestamp is the only safe way to align a provider curve across partial
    ranges and daylight-saving transitions.
    """

    start: datetime
    end: datetime
    energy_kwh: float

    def __post_init__(self) -> None:
        if (
            not isinstance(self.start, datetime)
            or not isinstance(self.end, datetime)
            or self.start.tzinfo is None
            or self.end.tzinfo is None
        ):
            raise ValueError("solar forecast periods require aware timestamps")
        if self.end.timestamp() <= self.start.timestamp():
            raise ValueError("solar forecast period must have positive duration")
        try:
            energy = float(self.energy_kwh)
        except (TypeError, ValueError) as exc:
            raise ValueError("solar forecast period energy must be numeric") from exc
        if not math.isfinite(energy) or energy < 0.0:
            raise ValueError("solar forecast period energy must be finite and non-negative")
        object.__setattr__(self, "energy_kwh", energy)


@dataclass(frozen=True)
class SolarForecast:
    """A normalized solar forecast and the horizon it represents."""

    kwh: float
    source: ForecastSource
    sensor: str
    periods: tuple[SolarForecastPeriod, ...] = ()
    conversion: str = "none"

    @property
    def remaining_kwh(self) -> float:
        """Normalized future energy consumed by all control decisions."""
        return self.kwh

    @property
    def diagnostic_source(self) -> str:
        """Stable diagnostic label distinguishing the migration paths."""
        return "remaining_sensor" if self.source == "remaining" else "today_legacy"


@dataclass(frozen=True)
class SolarForecastInput:
    """Consumer-facing solar contract with an optional normalized curve."""

    remaining_kwh: float
    source: str
    temporal_shape: tuple[float, ...] | None = None
    periods: tuple[SolarForecastPeriod, ...] | None = None
    original_source: str | None = None
    conversion: str = "none"
    horizon: str = "remaining"

    def __post_init__(self) -> None:
        """Keep the normalized contract finite even with a loose sensor value."""
        try:
            value = float(self.remaining_kwh)
        except (TypeError, ValueError):
            value = 0.0
        object.__setattr__(
            self,
            "remaining_kwh",
            value if math.isfinite(value) and value >= 0.0 else 0.0,
        )
        if self.periods is not None:
            object.__setattr__(self, "periods", tuple(self.periods))

    def normalized_shape(self) -> list[float] | None:
        """Return a shape whose values sum exactly to ``remaining_kwh``."""
        if self.temporal_shape is None:
            return None
        values = []
        for value in self.temporal_shape:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                parsed = 0.0
            values.append(parsed if math.isfinite(parsed) and parsed >= 0.0 else 0.0)
        total = math.fsum(values)
        if total <= 0.0:
            return [0.0] * len(values)
        factor = max(0.0, self.remaining_kwh) / total
        normalized = [value * factor for value in values]
        if normalized:
            normalized[-1] += max(0.0, self.remaining_kwh) - math.fsum(normalized)
        return normalized


def get_configured_solar_forecast_sensor(
    controller: Any,
    source: ForecastSource,
) -> str | None:
    """Return the effective configured entity for a forecast horizon.

    The controller keeps a runtime copy of these values, but an options update
    can briefly leave that copy behind the config entry.  Read the persisted
    entry first so horizon selection cannot silently fall back to the daily
    legacy path while the remaining-today sensor is configured.
    """
    if source == "remaining":
        key = CONF_SOLAR_FORECAST_REMAINING_SENSOR
        attribute = "solar_forecast_remaining_sensor"
    else:
        key = CONF_SOLAR_FORECAST_SENSOR
        attribute = "solar_forecast_sensor"

    config_entry = getattr(controller, "config_entry", None)
    if config_entry is not None:
        has_persisted_config = False
        for config in (
            getattr(config_entry, "data", None),
            getattr(config_entry, "options", None),
        ):
            if config is None:
                continue
            has_persisted_config = True
            if key not in config:
                continue
            value = config.get(key)
            return value if value else None
        # A real config entry is authoritative even when the key is absent.
        # This prevents a stale runtime attribute from resurrecting a sensor
        # that was cleared in the options flow.
        if has_persisted_config:
            return None

    # Small unit-test doubles and lightweight consumers may not carry a config
    # entry; preserve their existing runtime-only contract.
    value = getattr(controller, attribute, None)
    return value if value else None


def normalize_solar_forecast_config(data: dict[str, Any]) -> dict[str, Any]:
    """Keep at most one persisted solar forecast horizon.

    A configured remaining-today sensor supersedes ``today``. Empty values are
    removed rather than stored as config keys, which lets Repairs distinguish a
    real legacy configuration from a cleared field.
    """
    normalized = dict(data)
    remaining = normalized.get(CONF_SOLAR_FORECAST_REMAINING_SENSOR)
    if remaining:
        normalized.pop(CONF_SOLAR_FORECAST_SENSOR, None)
    else:
        normalized.pop(CONF_SOLAR_FORECAST_REMAINING_SENSOR, None)
        if not normalized.get(CONF_SOLAR_FORECAST_SENSOR):
            normalized.pop(CONF_SOLAR_FORECAST_SENSOR, None)
    return normalized


def _state_kwh(state: Any) -> float | None:
    """Return a finite non-negative sensor state in kWh, converting Wh."""
    if state is None or getattr(state, "state", None) in ("unknown", "unavailable"):
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    unit = str(getattr(state, "attributes", {}).get("unit_of_measurement", "kWh")).strip().lower()
    if unit == "wh":
        value /= 1000.0
    elif unit != "kwh":
        return None
    return value


def _parse_period_timestamp(value: Any) -> datetime | None:
    """Parse one explicit provider timestamp, rejecting naive values."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _period_energy_kwh(raw: dict[str, Any]) -> float | None:
    """Read an explicitly unit-labelled period energy value."""
    if "energy_kwh" in raw:
        candidate = raw["energy_kwh"]
    elif "energy_wh" in raw:
        candidate = raw["energy_wh"]
        try:
            return float(candidate) / 1000.0
        except (TypeError, ValueError):
            return None
    elif "energy" in raw:
        candidate = raw["energy"]
        unit = str(raw.get("unit", raw.get("energy_unit", ""))).strip().lower()
        if unit == "wh":
            try:
                return float(candidate) / 1000.0
            except (TypeError, ValueError):
                return None
        if unit != "kwh":
            return None
    else:
        return None
    try:
        value = float(candidate)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0.0 else None


def _extract_forecast_periods(state: Any) -> tuple[SolarForecastPeriod, ...]:
    """Adapt the small set of explicit period schemas supported by the contract.

    A bare list of numbers is intentionally ignored.  Providers can expose
    their own adapter later, but positional values cannot be made DST-safe.
    """
    attributes = getattr(state, "attributes", {}) or {}
    raw_periods = None
    for key in ("solar_forecast_periods", "forecast_periods", "periods"):
        if key in attributes:
            raw_periods = attributes[key]
            break
    if not isinstance(raw_periods, (list, tuple)):
        return ()

    parsed: list[SolarForecastPeriod] = []
    for raw in raw_periods:
        if not isinstance(raw, dict):
            return ()
        start = _parse_period_timestamp(
            raw.get("start", raw.get("start_time", raw.get("period_start")))
        )
        end = _parse_period_timestamp(
            raw.get("end", raw.get("end_time", raw.get("period_end")))
        )
        energy = _period_energy_kwh(raw)
        if start is None or end is None or energy is None:
            return ()
        try:
            parsed.append(SolarForecastPeriod(start, end, energy))
        except ValueError:
            return ()
    return tuple(parsed)


def read_solar_forecast_kwh(
    hass: Any,
    controller: Any,
    *,
    update_controller: bool = True,
) -> SolarForecast | None:
    """Read the preferred forecast: remaining first, legacy today second.

    An unavailable remaining sensor deliberately falls back to the configured
    legacy sensor during the migration.  A valid remaining value is never
    transformed; consumers can rely on it already being the future horizon.
    """
    candidates = (
        (
            "remaining",
            get_configured_solar_forecast_sensor(controller, "remaining"),
        ),
        ("today", get_configured_solar_forecast_sensor(controller, "today")),
    )
    for source, sensor in candidates:
        if not sensor:
            continue
        state = hass.states.get(sensor)
        value = _state_kwh(state)
        if value is not None:
            forecast = SolarForecast(
                value,
                source,
                sensor,
                periods=_extract_forecast_periods(state),
                conversion="none",
            )
            # Most control callers retain the source for diagnostics. Dashboard
            # projections use the same adapter read-only, so they can refresh
            # the live value without changing controller-owned runtime state.
            if update_controller:
                controller.solar_forecast_source = source
                controller.solar_forecast_diagnostic_source = forecast.diagnostic_source
                controller.solar_forecast_periods = forecast.periods
            return forecast
    if update_controller:
        controller.solar_forecast_source = None
        controller.solar_forecast_diagnostic_source = None
        controller.solar_forecast_periods = ()
    return None


def _controller_local_date(controller: Any) -> date:
    """Return today's local date without requiring a Home Assistant object."""
    profile = getattr(getattr(controller, "_consumption_tracker", None), "solar_profile", None)
    if profile is not None:
        today = getattr(profile, "_today", None)
        if callable(today):
            try:
                value = today()
                if isinstance(value, datetime):
                    return value.date()
                if isinstance(value, date):
                    return value
            except Exception:  # noqa: BLE001
                pass
    return datetime.now().date()


def solar_forecast_local_timezone(
    hass: Any,
    controller: Any,
    now: datetime | None = None,
):
    """Return the timezone used to interpret local forecast horizons."""
    profile = getattr(
        getattr(controller, "_consumption_tracker", None),
        "solar_profile",
        None,
    )
    timezone = getattr(profile, "_timezone", None)
    if callable(timezone):
        try:
            value = timezone()
            if value is not None:
                return value
        except Exception:  # noqa: BLE001 - timezone fallback must remain safe
            pass

    configured = getattr(getattr(hass, "config", None), "time_zone", None)
    if configured:
        try:
            return ZoneInfo(str(configured))
        except (KeyError, ValueError):
            pass
    if isinstance(now, datetime) and now.tzinfo is not None:
        return now.tzinfo
    return datetime.now().astimezone().tzinfo


def solar_forecast_period_energy_between(
    periods: tuple[SolarForecastPeriod, ...] | list[SolarForecastPeriod] | None,
    start: datetime,
    end: datetime,
    *,
    timezone: Any = None,
) -> float:
    """Return period energy overlapping one explicit horizon.

    Period energy is prorated by absolute-time overlap. Naive boundaries are
    local wall-clock values and therefore require the caller's local timezone.
    """
    if timezone is None:
        timezone = start.tzinfo or end.tzinfo or datetime.now().astimezone().tzinfo
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone)
    start_ts = start.timestamp()
    end_ts = end.timestamp()
    if end_ts <= start_ts:
        return 0.0

    energy = 0.0
    for period in periods or ():
        period_start = period.start.timestamp()
        period_end = period.end.timestamp()
        overlap = max(0.0, min(end_ts, period_end) - max(start_ts, period_start))
        if overlap > 0.0:
            energy += period.energy_kwh * overlap / (period_end - period_start)
    return max(0.0, energy)


def _remaining_period_energy_today(
    hass: Any,
    controller: Any,
    periods: tuple[SolarForecastPeriod, ...],
    now: datetime | float | None,
) -> float:
    """Return dated provider energy still expected before local midnight."""
    timezone = solar_forecast_local_timezone(
        hass,
        controller,
        now if isinstance(now, datetime) else None,
    )
    if isinstance(now, datetime):
        local_now = (
            now.replace(tzinfo=timezone)
            if now.tzinfo is None
            else now.astimezone(timezone)
        )
    else:
        midnight = datetime.combine(
            _controller_local_date(controller),
            time.min,
            tzinfo=timezone,
        )
        local_now = midnight + timedelta(hours=_current_hour(controller, now))
    day_end = datetime.combine(
        local_now.date() + timedelta(days=1),
        time.min,
        tzinfo=timezone,
    )
    return solar_forecast_period_energy_between(
        periods,
        local_now,
        day_end,
        timezone=timezone,
    )


def _current_hour(controller: Any, now: datetime | float | None) -> float:
    if isinstance(now, datetime):
        return now.hour + now.minute / 60.0 + now.second / 3600.0
    if now is not None:
        try:
            value = float(now)
            if math.isfinite(value):
                return value
        except (TypeError, ValueError):
            pass
    try:
        value = getattr(controller, "_solar_forecast_now_hour")
        value = float(value)
        if math.isfinite(value):
            return value
    except (AttributeError, TypeError, ValueError):
        pass
    return datetime.now().hour + datetime.now().minute / 60.0


def read_remaining_solar_kwh(
    hass: Any,
    controller: Any,
    *,
    now: datetime | float | None = None,
    update_controller: bool = True,
) -> SolarForecastInput:
    """Return one normalized ``remaining`` budget for every caller.

    ``remaining`` sensors are passed through untouched.  A legacy ``today``
    sensor is converted once using the best reliable evidence available, and
    the conversion is carried on the result so downstream code cannot infer
    the horizon a second time.
    """
    forecast = read_solar_forecast_kwh(
        hass,
        controller,
        update_controller=update_controller,
    )
    if forecast is None:
        if update_controller:
            controller.solar_forecast_source = "fallback"
            controller.solar_forecast_diagnostic_source = "fallback"
        return SolarForecastInput(
            0.0,
            "fallback",
            periods=None,
            original_source=None,
            conversion="unsafe_zero",
        )

    # Some providers roll the scalar ``remaining today`` state a few minutes
    # after midnight while their explicitly dated periods already contain the
    # new day's production. A numeric zero is normally valid, but it must not
    # erase positive, timestamped evidence inside today's remaining horizon.
    period_remaining = _remaining_period_energy_today(
        hass,
        controller,
        forecast.periods,
        now,
    )
    if forecast.kwh <= _FORECAST_EPSILON_KWH and period_remaining > _FORECAST_EPSILON_KWH:
        if update_controller:
            controller.solar_forecast_conversion = "dated_periods_zero_scalar"
            controller.solar_forecast_diagnostic_source = forecast.diagnostic_source
        return SolarForecastInput(
            period_remaining,
            forecast.diagnostic_source,
            periods=forecast.periods,
            original_source=(
                "remaining" if forecast.source == "remaining" else "today_legacy"
            ),
            conversion="dated_periods_zero_scalar",
        )

    if forecast.source == "remaining":
        result = SolarForecastInput(
            forecast.kwh,
            forecast.diagnostic_source,
            periods=forecast.periods or None,
            original_source="remaining",
            conversion="none",
        )
        if update_controller:
            controller.solar_forecast_conversion = "none"
        return result

    # A whole-day scalar has already assigned part of its energy to elapsed
    # hours.  Subtracting actual production makes any cloudy/optimistic morning
    # reappear as fictional energy at sunset. Prefer dated provider periods;
    # otherwise map the total through the remaining part of the solar curve.
    if forecast.periods:
        remaining = period_remaining
        conversion = "dated_periods"
    else:
        tracker = getattr(controller, "_consumption_tracker", None)
        t_start = getattr(controller, "_solar_t_start", None)
        current_hour = _current_hour(controller, now)
        remaining = None
        conversion = ""
        if t_start is not None and tracker is not None:
            try:
                t_end = tracker.estimate_t_end()
                fraction_done = tracker.get_solar_fraction_done(
                    current_hour, float(t_start), float(t_end)
                )
                remaining = forecast.kwh * max(0.0, 1.0 - float(fraction_done))
                conversion = "temporal_fraction"
            except (AttributeError, TypeError, ValueError, ZeroDivisionError):
                remaining = None
        if remaining is None:
            if current_hour < T_START_FALLBACK_HOUR:
                remaining = forecast.kwh
                conversion = "pre_solar"
            else:
                remaining = 0.0
                conversion = "unsafe_zero"

    if update_controller:
        controller.solar_forecast_conversion = conversion
        controller.solar_forecast_diagnostic_source = "today_legacy"
    return SolarForecastInput(
        remaining,
        "today_legacy",
        periods=forecast.periods or None,
        original_source="today_legacy",
        conversion=conversion,
    )
