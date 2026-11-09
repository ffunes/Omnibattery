"""The solar period sums stay exact while skipping repeated work.

A re-plan spreads the solar forecast onto every interval; converting each
period's start and end with ``datetime.timestamp()`` on every pass, and walking
all 96 progress bins for every quarter of every stored day, made it take
seconds on a small host. These tests pin the shortcuts to the original maths.
"""

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.omnibattery.solar_forecast import (
    SolarForecastPeriod,
    solar_forecast_period_energy_between,
)
from custom_components.omnibattery.pricing import solar_timeline as st
from custom_components.omnibattery.tracking import consumption_profile as cp
from custom_components.omnibattery.tracking import solar_profile as sp

TZ = ZoneInfo("Europe/Berlin")


def _curve(index):
    hour = index / 4
    if not 7.5 <= hour <= 16.5:
        return 0.0
    return max(0.0, math.sin((hour - 7.5) / 9 * math.pi)) * 0.4


def _periods(day):
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    return tuple(
        SolarForecastPeriod(
            start + timedelta(minutes=15 * i),
            start + timedelta(minutes=15 * (i + 1)),
            _curve(i),
        )
        for i in range(96)
    )


def _reference_energy_between(periods, start, end):
    total = 0.0
    for period in periods:
        p_start, p_end = period.start.timestamp(), period.end.timestamp()
        overlap = max(0.0, min(end.timestamp(), p_end) - max(start.timestamp(), p_start))
        if overlap > 0.0:
            total += period.energy_kwh * overlap / (p_end - p_start)
    return total


def test_a_period_carries_its_timestamps_without_changing_equality():
    start = datetime(2026, 11, 9, 12, 0, tzinfo=TZ)
    end = start + timedelta(minutes=15)
    period = SolarForecastPeriod(start, end, 0.2)
    assert period.start_ts == start.timestamp()
    assert period.end_ts == end.timestamp()
    assert period == SolarForecastPeriod(start, end, 0.2)
    assert "start_ts" not in repr(period)


def test_period_sums_match_the_original_maths_exactly():
    # two arrays over today and tomorrow, as on a two-orientation install
    periods = _periods(date(2026, 11, 9)) * 2 + _periods(date(2026, 11, 10))
    cursor = datetime(2026, 11, 9, 22, 58, tzinfo=TZ)
    for _ in range(100):
        end = cursor + timedelta(minutes=15)
        expected = _reference_energy_between(periods, cursor, end)
        assert solar_forecast_period_energy_between(periods, cursor, end) == max(0.0, expected)
        assert st.provider_energy_between(periods, cursor, end) == expected
        cursor = end


def _reference_progress_day(day):
    """The remap before the change: every segment against all 96 bins."""
    bins = st.PROGRESS_BIN_COUNT
    start_ts, end_ts = day.solar_start.timestamp(), day.solar_end.timestamp()
    duration = end_ts - start_ts
    energy, coverage, flags = [0.0] * bins, [0.0] * bins, [0] * bins
    local_start = day.solar_start.replace(hour=0, minute=0, second=0, microsecond=0)
    segments = cp._local_segments(local_start, local_start + timedelta(days=1))
    for seg_start, seg_end, midpoint in segments:
        index = sp._interval_index(midpoint.timetz().replace(tzinfo=None))
        bin_coverage = day.coverage_s[index]
        if bin_coverage <= 0.0:
            continue
        segment_energy = day.energy_kwh[index] * ((seg_end - seg_start) / bin_coverage)
        seg_start, seg_end = max(seg_start, start_ts), min(seg_end, end_ts)
        if seg_end <= seg_start:
            continue
        p0, p1 = (seg_start - start_ts) / duration, (seg_end - start_ts) / duration
        for k in range(bins):
            overlap = max(0.0, min(p1, (k + 1) / bins) - max(p0, k / bins))
            if overlap <= 0.0:
                continue
            span = max(1e-12, p1 - p0)
            energy[k] += segment_energy * overlap / span
            coverage[k] += (seg_end - seg_start) * overlap / span
            flags[k] |= day.quality_flags[index]
    return energy, coverage, flags


def test_progress_remap_only_visits_the_touched_bins_and_stays_exact():
    # an ordinary autumn day, the 25-hour DST day and a long summer day
    for day, start, end in (
        (date(2026, 11, 9), (7, 41), (16, 37)),
        (date(2026, 10, 25), (7, 3), (17, 58)),
        (date(2026, 6, 21), (5, 7), (21, 41)),
    ):
        energy = [_curve(i) * 1.3 for i in range(96)]
        coverage = [900.0 if energy[i] > 0 else 0.0 for i in range(96)]
        flags = [i % 3 for i in range(96)]
        record = sp.SolarProfileDay(
            day,
            energy,
            coverage,
            flags,
            datetime(day.year, day.month, day.day, *start, tzinfo=TZ),
            datetime(day.year, day.month, day.day, *end, tzinfo=TZ),
            None,
            True,
        )
        assert sp.remap_day_to_progress(record) == _reference_progress_day(record)


def test_local_segments_are_cached_but_handed_out_as_fresh_lists():
    start = datetime(2026, 10, 25, tzinfo=TZ)
    end = start + timedelta(days=1)
    first = cp._local_segments(start, end)
    first.clear()
    second = cp._local_segments(start, end)
    assert len(second) == 100  # the repeated hour adds four quarters
    assert second == list(
        cp._local_segments_between(start.timestamp(), end.timestamp(), TZ)
    )


def test_indexed_overlap_sum_equals_a_full_scan_for_a_long_mixed_forecast():
    # eight days of two arrays plus hourly periods, deliberately unsorted:
    # the sorted index must find every overlapping period and keep the order
    from custom_components.omnibattery.solar_forecast import period_overlap_energy

    day0 = date(2026, 11, 9)
    quarters = tuple(
        period
        for offset in range(8)
        for period in _periods(day0 + timedelta(days=offset))
    )
    start = datetime(2026, 11, 9, tzinfo=TZ)
    hourly = tuple(
        SolarForecastPeriod(
            start + timedelta(hours=h), start + timedelta(hours=h + 1), _curve(4 * (h % 24))
        )
        for h in range(8 * 24)
    )
    forecast = tuple(reversed(quarters)) + hourly + quarters
    assert len(forecast) >= 64
    cursor = start + timedelta(hours=5, minutes=7)
    for _ in range(300):
        end = cursor + timedelta(minutes=23)
        assert period_overlap_energy(forecast, cursor.timestamp(), end.timestamp()) == (
            _reference_energy_between(forecast, cursor, end)
        )
        cursor = end
