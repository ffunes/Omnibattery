"""Direct tests of the pure chronological-planner inputs (S4)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.pricing.plan_inputs import (
    battery_idle_intervals,
    consumption_shape,
    solar_timeline,
)
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.solar_forecast import SolarForecastPeriod

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


# ---------------------------------------------------------------------------
# solar_timeline
# ---------------------------------------------------------------------------

def _solar_input(remaining=4.0, periods=None, temporal_shape=None):
    return SimpleNamespace(
        remaining_kwh=remaining, periods=periods, temporal_shape=temporal_shape
    )


def _solar(bounds, solar_input, **overrides):
    kwargs = dict(
        solar_start=None,
        solar_end=None,
        mode="active",
        safety_margin_kwh=0.0,
        extended_timezone=None,
        excluded_claim_kwh=0.0,
        today_end=MIDNIGHT,
    )
    kwargs.update(overrides)
    return solar_timeline(bounds, solar_input, None, **kwargs)


def test_solar_timeline_claim_only_reduces_todays_intervals():
    bounds = _boundaries(MIDNIGHT - timedelta(hours=1), 8)
    periods = (
        SolarForecastPeriod(MIDNIGHT - timedelta(hours=1), MIDNIGHT, 1.0),
        SolarForecastPeriod(MIDNIGHT, MIDNIGHT + timedelta(hours=1), 3.0),
    )
    base = _solar(bounds, _solar_input(4.0, periods), extended_timezone=UTC)
    claimed = _solar(
        bounds, _solar_input(4.0, periods), extended_timezone=UTC,
        excluded_claim_kwh=0.5,
    )
    assert claimed.excluded_claim_kwh == pytest.approx(0.5)
    assert sum(claimed.intervals_kwh[:4]) == pytest.approx(0.5)
    assert claimed.intervals_kwh[4:] == base.intervals_kwh[4:]
    assert claimed.source == claimed.timeline.source


def test_solar_timeline_maps_extended_periods_onto_the_boundaries():
    bounds = _boundaries(MIDNIGHT, 8)
    periods = (
        SolarForecastPeriod(MIDNIGHT, MIDNIGHT + timedelta(hours=1), 1.0),
        SolarForecastPeriod(MIDNIGHT + timedelta(hours=1), MIDNIGHT + timedelta(hours=2), 3.0),
    )
    extended = _solar(bounds, _solar_input(4.0, periods), extended_timezone=UTC)
    assert sum(extended.intervals_kwh[:4]) == pytest.approx(1.0)
    assert sum(extended.intervals_kwh[4:]) == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# battery_idle_intervals
# ---------------------------------------------------------------------------

def _idle(bounds, consumption, solar, **overrides):
    kwargs = dict(
        held_until=None,
        limit_kw=0.0,
        fleet_blocks=[],
        block_matches=lambda item, block: False,
        blocked_price_slots=None,
        tz=UTC,
    )
    kwargs.update(overrides)
    return battery_idle_intervals(bounds, consumption, solar, **kwargs)


def test_idle_intervals_without_masks_pass_the_shape_through():
    bounds = _boundaries(MIDNIGHT, 3)
    result = _idle(bounds, [1.0, 2.0, 3.0], [0.0, 0.5, 4.0])
    assert [i.consumption_kwh for i in result] == [1.0, 2.0, 3.0]
    assert [i.solar_kwh for i in result] == [0.0, 0.5, 4.0]


def test_peak_shaving_hold_keeps_only_the_load_above_the_limit_until_held_until():
    bounds = _boundaries(MIDNIGHT, 4)
    # 2 kW limit -> 0.5 kWh per 15 min.
    result = _idle(
        bounds, [0.2, 1.0, 1.0, 1.0], [0.0] * 4,
        held_until=bounds[1][1], limit_kw=2.0,
    )
    assert [i.consumption_kwh for i in result] == [0.0, 0.5, 1.0, 1.0]


def test_fleet_blocks_and_cheap_price_slots_idle_the_battery():
    bounds = _boundaries(MIDNIGHT, 4)
    cheap = [PriceSlot(bounds[2][0], bounds[3][1], 0.05)]
    result = _idle(
        bounds, [1.0] * 4, [0.25] * 4,
        fleet_blocks=["block"],
        block_matches=lambda item, block: item.start == bounds[0][0],
        blocked_price_slots=cheap,
    )
    assert [i.consumption_kwh for i in result] == [0.0, 1.0, 0.0, 0.0]
    # Idle intervals still let the sun charge: only its surplus remains.
    assert [i.solar_kwh for i in result] == [0.0, 0.25, 0.0, 0.0]
    assert _idle(bounds, [1.0] * 4, [0.0] * 4, blocked_price_slots=[])[2].consumption_kwh == 1.0
