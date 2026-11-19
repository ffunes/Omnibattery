"""The learned consumption profile on price slots.

High-Price Sale sizes the demand it protects from it. (The previous discharge
reserve planner that lived here is gone: the Dynamic Pricing plan holds the
battery itself, #888.)
"""
from __future__ import annotations

import math
from datetime import timedelta
from typing import Sequence

_QUARTER = timedelta(minutes=15)


def _positive(value, default: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, value) if math.isfinite(value) else default


def consumption_by_slot(
    price_slots: Sequence,
    intervals_by_date: dict,
    intervals_kwh: Sequence[float] | None = None,
) -> dict:
    """Map the learned 15-minute consumption profile onto price slots.

    The profile is 96 quarter-hour bins of local wall-clock time. A price slot
    that covers part of a bin is credited that part of its energy, so hourly and
    quarter-hourly tariffs both land on the same shape.

    ``intervals_by_date`` is preferred over the flat ``intervals_kwh``: a horizon
    that crosses midnight has one shape per date, and applying today's evening to
    tomorrow's evening is exactly the error the dated form exists to prevent.
    Returns an empty mapping when no profile is available, so the caller keeps
    its behaviour instead of protecting demand of an invented shape.
    """
    if not price_slots:
        return {}
    flat = list(intervals_kwh or [])
    dated = {
        local_date: list(values)
        for local_date, values in (intervals_by_date or {}).items()
        if values
    }
    if not dated and not flat:
        return {}

    demand: dict = {}
    for slot in price_slots:
        start = slot.start
        end = slot.end
        if end <= start:
            demand[slot] = 0.0
            continue
        total = 0.0
        cursor = start
        while cursor < end:
            index = (cursor.hour * 60 + cursor.minute) // 15
            bin_start = cursor.replace(
                minute=(index * 15) % 60,
                hour=(index * 15) // 60,
                second=0,
                microsecond=0,
            )
            bin_end = bin_start + _QUARTER
            step_end = min(end, bin_end)
            values = dated.get(cursor.date()) or flat
            if values and 0 <= index < len(values):
                share = (step_end - cursor).total_seconds() / _QUARTER.total_seconds()
                total += _positive(values[index]) * share
            cursor = step_end
        demand[slot] = total
    return demand
