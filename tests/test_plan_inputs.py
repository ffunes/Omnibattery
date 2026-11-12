"""Direct tests of the pure chronological-planner inputs (S4)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.pricing.plan_inputs import consumption_shape

UTC = timezone.utc
T0 = datetime(2026, 1, 10, 23, 0, tzinfo=UTC)
MIDNIGHT = datetime(2026, 1, 11, 0, 0, tzinfo=UTC)


def _bins(per_bin: dict[int, float]) -> list[float]:
    return [per_bin.get(i, 0.0) for i in range(96)]


def _boundaries(start, count):
    return [
        (start + timedelta(minutes=15 * i), start + timedelta(minutes=15 * (i + 1)))
        for i in range(count)
    ]


def test_cross_midnight_uses_each_dates_own_shape():
    today = _bins({94: 1.0})
    tomorrow = _bins({0: 3.0})
    forecast = SimpleNamespace(
        intervals_kwh=_bins({}),
        intervals_by_date={T0.date(): today, MIDNIGHT.date(): tomorrow},
        energy_kwh=4.0,
    )
    bounds = _boundaries(datetime(2026, 1, 10, 23, 30, tzinfo=UTC), 4)
    result = consumption_shape(
        forecast,
        bounds,
        horizon_end=bounds[-1][1],
        daily_horizon_end=MIDNIGHT,
        scalar_total_kwh=4.0,
    )
    # 23:30 -> today bin 94 (1), 23:45 -> 0, 00:00 -> tomorrow bin 0 (3), 00:15 -> 0
    assert result == pytest.approx([1.0, 0.0, 3.0, 0.0])


def test_partial_first_interval_is_scaled_by_its_fraction():
    forecast = SimpleNamespace(
        intervals_kwh=_bins({40: 2.0, 41: 2.0}),
        intervals_by_date={},
        energy_kwh=99.0,
    )
    start = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)
    bounds = [
        (start + timedelta(minutes=5), start + timedelta(minutes=15)),
        (start + timedelta(minutes=15), start + timedelta(minutes=30)),
    ]
    result = consumption_shape(
        forecast,
        bounds,
        horizon_end=bounds[-1][1],
        daily_horizon_end=MIDNIGHT,
        scalar_total_kwh=3.0,
    )
    # raw = [2 * 10/15, 2 * 1] -> normalized to the 3 kWh total
    assert result == pytest.approx([3.0 * (4 / 3) / (10 / 3), 3.0 * 2 / (10 / 3)])
    assert sum(result) == pytest.approx(3.0)


@pytest.mark.parametrize(
    ("horizon_end", "expected_total"),
    [(MIDNIGHT, 5.0), (MIDNIGHT + timedelta(minutes=15), 7.0)],
)
def test_total_is_scalar_until_midnight_then_the_forecast(horizon_end, expected_total):
    forecast = SimpleNamespace(
        intervals_kwh=_bins({i: 1.0 for i in range(96)}),
        intervals_by_date={},
        energy_kwh=7.0,
    )
    bounds = _boundaries(MIDNIGHT - timedelta(minutes=15), 2)
    result = consumption_shape(
        forecast,
        bounds,
        horizon_end=horizon_end,
        daily_horizon_end=MIDNIGHT,
        scalar_total_kwh=5.0,
    )
    assert sum(result) == pytest.approx(expected_total)
