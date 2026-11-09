"""Validate Home Assistant per-phase active power measurements.

This module is deliberately read-only: no hardware commands or controller
ownership changes. Positive grid power means import, negative means export.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any


@dataclass(frozen=True)
class PhaseMeterReading:
    power_w: float | None
    reason: str | None = None
    age_s: float | None = None


def read_phase_power(
    state: Any,
    *,
    now: datetime | None = None,
    max_age_s: float = 15.0,
    inverted: bool = False,
) -> PhaseMeterReading:
    """Read a W/kW HA sensor, rejecting missing, stale or invalid values."""
    if not isfinite(max_age_s) or max_age_s < 0:
        raise ValueError("Invalid maximum age")
    if state is None:
        return PhaseMeterReading(None, "sensor_not_found")
    raw = getattr(state, "state", None)
    if raw in (None, "unknown", "unavailable"):
        return PhaseMeterReading(None, "sensor_unavailable")
    try:
        value = float(raw)
    except (ValueError, TypeError):
        return PhaseMeterReading(None, "sensor_not_numeric")
    if not isfinite(value):
        return PhaseMeterReading(None, "sensor_not_numeric")
    unit = (getattr(state, "attributes", None) or {}).get("unit_of_measurement")
    if unit == "kW":
        value *= 1000.0
    elif unit != "W":
        return PhaseMeterReading(None, "sensor_invalid_unit")
    timestamp = getattr(state, "last_reported", None) or getattr(
        state, "last_updated", None
    )
    # HA state objects have timestamps. Missing timestamps must fail closed,
    # rather than treating an unknown age as fresh.
    if timestamp is None or timestamp.tzinfo is None:
        return PhaseMeterReading(None, "sensor_missing_timestamp")
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    age = (reference - timestamp).total_seconds()
    if age < -5:
        return PhaseMeterReading(None, "sensor_future_timestamp")
    age = max(0.0, age)
    if age > max_age_s:
        return PhaseMeterReading(None, "sensor_stale", age)
    return PhaseMeterReading(-value if inverted else value, age_s=age)
