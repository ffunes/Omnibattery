"""Pure inputs of the chronological planner.

Extracted from ``PricingManager._build_chronological_plan_for_horizon`` (S4):
each function turns plain values into one input of
``ChronologicalEvaluationRequest`` without touching the controller.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from .chronological import normalize_energy_shape


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
