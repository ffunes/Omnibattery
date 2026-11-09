"""Price slots on DST nights (Europe/Amsterdam).

The parsers turn provider times into naive local wall clocks. On the spring
night the slot before the gap ran to the far side of it (2 h / 75 min long);
on the autumn night the slot before the repeated hour ended at or before its
own start and the repeated hour's starts appeared twice. A 0-minute slot made
``select_cheapest_hours`` raise ZeroDivisionError and abort the control cycle.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing import calculations as calc


@pytest.fixture(autouse=True)
def _amsterdam():
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Amsterdam"))
    yield
    dt_util.set_default_time_zone(previous)


def _nordpool(start_utc, count, step, price=lambda i: 0.2):
    raw = [
        {
            "start": start_utc + i * step,
            "end": start_utc + (i + 1) * step,
            "value": price(i),
        }
        for i in range(count)
    ]
    return calc.parse_nordpool_prices({"raw_today": raw})


def _durations(slots):
    return [slot.end - slot.start for slot in slots]


@pytest.mark.parametrize("step", [timedelta(hours=1), timedelta(minutes=15)])
def test_fall_back_night_has_no_empty_or_repeated_slots(step):
    # 2026-10-25: 03:00 CEST -> 02:00 CET. 22:00 UTC .. 06:00 UTC.
    start = datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc)
    raw = _nordpool(start, int(timedelta(hours=8) / step), step)

    slots = calc.normalize_price_slots(raw)

    assert all(duration > timedelta(0) for duration in _durations(slots))
    starts = [slot.start for slot in slots]
    assert len(starts) == len(set(starts))
    assert all(duration == step for duration in _durations(slots))


@pytest.mark.parametrize("step", [timedelta(hours=1), timedelta(minutes=15)])
def test_spring_forward_slot_is_one_step_long(step):
    # 2026-03-29: 02:00 CET -> 03:00 CEST. 22:00 UTC .. 06:00 UTC.
    start = datetime(2026, 3, 28, 22, 0, tzinfo=timezone.utc)
    raw = _nordpool(start, int(timedelta(hours=8) / step), step)
    assert max(_durations(raw)) > step  # the parser's long slot

    slots = calc.normalize_price_slots(raw)

    assert all(duration == step for duration in _durations(slots))
    assert len(slots) == len(raw)


def test_ordinary_day_and_mixed_resolution_are_unchanged():
    base = datetime(2026, 11, 9)
    slots = [
        PriceSlot(base, base + timedelta(hours=1), 0.2),
        PriceSlot(base + timedelta(hours=1), base + timedelta(hours=1, minutes=15), 0.3),
    ]

    assert calc.normalize_price_slots(slots) is slots


def test_select_cheapest_hours_survives_the_fall_back_night():
    """Reproduces the reviewer's ZeroDivisionError at 01:30 on 2026-10-25."""
    start = datetime(2026, 10, 24, 21, 0, tzinfo=timezone.utc)
    raw = _nordpool(
        start, 8, timedelta(hours=1), price=lambda i: 0.5 if i < 3 else 0.1
    )
    now = datetime(2026, 10, 25, 1, 30)

    # Unnormalized input must not crash either (defence in depth).
    calc.select_cheapest_hours(raw, 2.0, 0.3, now=now)
    selected = calc.select_cheapest_hours(
        calc.normalize_price_slots(raw), 2.0, 0.3, now=now
    )

    assert selected
    assert all(slot.end > slot.start for slot in selected)


def test_select_cheapest_blocks_guards_a_zero_duration():
    base = datetime(2026, 11, 9)
    slots = [PriceSlot(base + timedelta(minutes=15 * i), base + timedelta(minutes=15 * (i + 1)), 0.1) for i in range(8)]

    assert calc.select_cheapest_blocks(slots, 1.0, 0.0)
