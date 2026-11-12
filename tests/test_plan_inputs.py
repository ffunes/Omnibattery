"""Direct tests of the pure chronological-planner inputs (S4)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.pricing.plan_inputs import (
    battery_idle_intervals,
    consumption_shape,
    fleet_plan_state,
    guaranteed_floor,
    solar_timeline,
    time_slot_window_hold,
    weekly_reserve,
)
from custom_components.omnibattery.pricing.chronological import (
    EnergyDeadline,
    EnergyInterval,
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


# ---------------------------------------------------------------------------
# time_slot_window_hold
# ---------------------------------------------------------------------------

def test_window_hold_without_windows_changes_nothing():
    bounds = _boundaries(MIDNIGHT, 2)
    intervals = _idle(bounds, [1.0, 1.0], [0.0, 0.0])
    result, load = time_slot_window_hold(intervals, [])
    assert result == intervals
    assert load is None


def test_window_hold_idles_intervals_starting_inside_a_window():
    bounds = _boundaries(MIDNIGHT, 4)
    intervals = _idle(bounds, [1.0, 2.0, 3.0, 4.0], [0.25, 0.0, 5.0, 0.0])
    window = SimpleNamespace(start=bounds[1][0], end=bounds[3][0])
    result, load = time_slot_window_hold(intervals, [window])
    # Intervals 1 and 2 start inside (end exclusive): idled, grid carries net load.
    assert [i.consumption_kwh for i in result] == [1.0, 0.0, 0.0, 4.0]
    assert load == pytest.approx(2.0)


def test_window_hold_load_is_zero_when_the_sun_covers_it():
    bounds = _boundaries(MIDNIGHT, 2)
    intervals = _idle(bounds, [1.0, 1.0], [3.0, 3.0])
    window = SimpleNamespace(start=bounds[0][0], end=bounds[1][1])
    _result, load = time_slot_window_hold(intervals, [window])
    assert load == 0.0


# ---------------------------------------------------------------------------
# fleet_plan_state
# ---------------------------------------------------------------------------

def test_fleet_state_sums_capacity_usable_and_room_in_order():
    state = fleet_plan_state([
        (10.0, 50.0, 10.0, 100.0),
        (5.0, 20.0, 10.0, 80.0),
    ])
    assert state.total_capacity_kwh == 15.0
    assert state.weighted_min_soc == pytest.approx(10.0)
    assert state.usable_kwh == pytest.approx(4.0 + 0.5)
    assert state.room_kwh == pytest.approx((5.0, 3.0))
    assert state.headroom_kwh == pytest.approx(8.0)


def test_fleet_state_clamps_below_floor_and_above_ceiling_to_zero():
    state = fleet_plan_state([(10.0, 5.0, 10.0, 90.0), (10.0, 95.0, 10.0, 90.0)])
    assert state.usable_kwh == pytest.approx(8.5)
    assert state.room_kwh[0] == pytest.approx(8.5)
    assert state.room_kwh[1] == 0.0


def test_fleet_state_with_no_capacity_has_zero_weighted_floor():
    state = fleet_plan_state([(0.0, 50.0, 10.0, 100.0)])
    assert state.weighted_min_soc == 0.0
    assert fleet_plan_state([]).headroom_kwh == 0.0


def _night(consumption_kwh):
    start = datetime(2026, 1, 10, 22, 0, tzinfo=UTC)
    return [
        EnergyInterval(
            start + timedelta(hours=i),
            start + timedelta(hours=i + 1),
            consumption_kwh,
            0.0,
        )
        for i in range(8)
    ]


def test_weekly_reserve_overnight_runs_from_now_to_window_end():
    now = datetime(2026, 1, 10, 22, 0, tzinfo=UTC)
    window_end = datetime(2026, 1, 11, 2, 0, tzinfo=UTC)
    values = weekly_reserve(
        _night(1.0),
        usable=6.0,
        headroom=4.0,
        now=now,
        weekly_from_now=True,
        weekly_window_end=window_end,
        solar_end=None,
    )
    assert values["weekly_reserve_not_before"] == now.isoformat()
    assert values["weekly_reserve_not_after"] == window_end.isoformat()
    # 4 kWh drain before the window end: usable falls to 2 of 10 -> 8 reserved.
    assert values["weekly_reserve_kwh"] == pytest.approx(8.0)


def test_weekly_reserve_after_sun_starts_at_solar_end_without_window():
    now = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    solar_end = datetime(2026, 1, 10, 24 - 1, 0, tzinfo=UTC)
    values = weekly_reserve(
        _night(1.0),
        usable=6.0,
        headroom=4.0,
        now=now,
        weekly_from_now=False,
        weekly_window_end=None,
        solar_end=solar_end,
    )
    assert values["weekly_reserve_not_before"] == solar_end.isoformat()
    assert "weekly_reserve_not_after" not in values
    # Only the first interval (22:00-23:00) ends by solar_end.
    assert values["weekly_reserve_kwh"] == pytest.approx(5.0)


def _floor_call(intervals, deadlines, *, soc, floor, solar_start, **kw):
    return guaranteed_floor(
        intervals,
        deadlines,
        usable=max(0.0, (soc - 10.0) / 100.0 * 10.0),
        headroom=5.0,
        required=0.0,
        premium_kwh=0.0,
        floor=floor,
        batteries=[(10.0, soc, 10.0)],
        solar_start=solar_start,
        energy_deficit_kwh=0.0,
        **kw,
    )


def test_guaranteed_floor_gap_adds_deadline_at_solar_start():
    solar_start = datetime(2026, 1, 11, 6, 0, tzinfo=UTC)
    result = _floor_call(_night(0.0), [], soc=15.0, floor=30.0, solar_start=solar_start)
    assert result is not None
    assert result.deadlines[-1].deadline == solar_start
    assert result.deadlines[-1].required_cumulative_kwh == pytest.approx(1.5)
    assert result.required == pytest.approx(1.5)
    assert result.energy_deficit_kwh == pytest.approx(1.5)
    assert result.planned_grid_charge_kwh == pytest.approx(1.5)
    assert result.guaranteed_floor_deadline == solar_start.isoformat()
    assert result.premium_kwh == pytest.approx(1.5)


def test_guaranteed_floor_returns_none_below_hysteresis():
    solar_start = datetime(2026, 1, 11, 6, 0, tzinfo=UTC)
    assert (
        _floor_call(_night(0.0), [], soc=50.0, floor=10.0, solar_start=solar_start)
        is None
    )


def test_guaranteed_floor_keeps_later_larger_ordinary_deadlines():
    solar_start = datetime(2026, 1, 11, 6, 0, tzinfo=UTC)
    later = EnergyDeadline(solar_start + timedelta(hours=2), 5.0)
    result = _floor_call(
        _night(0.0), [later], soc=15.0, floor=30.0, solar_start=solar_start
    )
    assert result is not None
    assert result.deadlines[-1] == later
    assert result.deadlines[-2].deadline == solar_start
