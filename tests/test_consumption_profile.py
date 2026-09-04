"""Pure tests for the quarter-hour consumption profile."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.tracking.consumption_profile import (
    FALLBACK_HOURLY_WEIGHTS,
    INTERVAL_COUNT,
    INTERVAL_SECONDS,
    MIN_INTERVAL_COVERAGE_S,
    ConsumptionForecast,
    ConsumptionProfileTracker,
    ProfileDay,
    _apply_external_load_to_day,
    _local_segments,
    _series_to_bins,
    adjust_remaining_fallback_energy,
    fallback_daily_intervals,
    rebin_days_to_timezone,
    split_sample_across_bins,
)


MADRID = ZoneInfo("Europe/Madrid")


def _profile(days=None, *, slots=None, fallback_daily=5.0):
    profile = ConsumptionProfileTracker.__new__(ConsumptionProfileTracker)
    profile._days = days or {}
    profile._controller = SimpleNamespace(
        charging_time_slots=slots or [],
        get_avg_daily_consumption=lambda: fallback_daily,
    )
    profile._fallback_daily_kwh = fallback_daily
    profile._last_error = None
    profile._hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Madrid")
    )
    profile._last_local_date = None
    profile._last_sample_time = None
    profile._last_sample_monotonic = None
    profile._last_power_kw = None
    profile._last_save_monotonic = 1.0
    profile._save_task = None
    profile._backfill_task = None
    profile._loaded = True
    profile._invalidated = False
    profile._active_fingerprint = "test"
    profile._excluded_periods = []
    return profile


def _day(local_date: date, value: float, *, coverage=INTERVAL_SECONDS):
    return ProfileDay(
        local_date,
        [value * coverage / INTERVAL_SECONDS] * INTERVAL_COUNT,
        [coverage] * INTERVAL_COUNT,
        complete=True,
    )


def _single_interval_day(local_date: date, value: float, interval=40):
    energy = [0.0] * INTERVAL_COUNT
    coverage = [0.0] * INTERVAL_COUNT
    energy[interval] = value
    coverage[interval] = INTERVAL_SECONDS
    return ProfileDay(local_date, energy, coverage, complete=True)


def test_split_sample_inside_one_bin():
    start = datetime(2026, 8, 10, 10, 0, tzinfo=MADRID)
    end = start + timedelta(minutes=10)
    result = split_sample_across_bins(start, end, 1.0, 1.0)

    assert len(result) == 1
    assert result[0].interval_index == 40
    assert result[0].coverage_s == pytest.approx(600)
    assert result[0].energy_kwh == pytest.approx(1 / 6)


def test_split_sample_crosses_quarter_boundary():
    start = datetime(2026, 8, 10, 10, 14, tzinfo=MADRID)
    end = datetime(2026, 8, 10, 10, 16, tzinfo=MADRID)
    result = split_sample_across_bins(start, end, 1.0, 1.0)

    assert [item.interval_index for item in result] == [40, 41]
    assert [item.coverage_s for item in result] == pytest.approx([60, 60])
    assert sum(item.energy_kwh for item in result) == pytest.approx(1 / 30)


def test_split_sample_crosses_midnight():
    start = datetime(2026, 8, 10, 23, 59, tzinfo=MADRID)
    end = datetime(2026, 8, 11, 0, 1, tzinfo=MADRID)
    result = split_sample_across_bins(start, end, 1.0, 1.0)

    assert [item.local_date for item in result] == [date(2026, 8, 10), date(2026, 8, 11)]
    assert [item.coverage_s for item in result] == pytest.approx([60, 60])


def test_split_sample_uses_trapezoidal_power():
    start = datetime(2026, 8, 10, 10, 0, tzinfo=MADRID)
    end = datetime(2026, 8, 10, 10, 15, tzinfo=MADRID)
    result = split_sample_across_bins(start, end, 0.0, 2.0)

    assert result[0].energy_kwh == pytest.approx(0.25)


def test_split_sample_rejects_invalid_power():
    start = datetime(2026, 8, 10, 10, 0, tzinfo=MADRID)
    end = start + timedelta(minutes=1)

    assert split_sample_across_bins(start, end, float("nan"), 1.0) == []
    assert split_sample_across_bins(start, end, 1.0, float("inf")) == []
    assert split_sample_across_bins(start, end, -1.0, 1.0) == []


def test_split_sample_handles_local_dst_transitions():
    autumn = split_sample_across_bins(
        datetime(2026, 10, 25, 2, 55, tzinfo=MADRID, fold=0),
        datetime(2026, 10, 25, 2, 15, tzinfo=MADRID, fold=1),
        1.0,
        1.0,
    )
    spring = split_sample_across_bins(
        datetime(2026, 3, 29, 1, 55, tzinfo=MADRID),
        datetime(2026, 3, 29, 3, 15, tzinfo=MADRID),
        1.0,
        1.0,
    )

    assert [item.coverage_s for item in autumn] == pytest.approx([300, 900])
    assert [item.coverage_s for item in spring] == pytest.approx([300, 900])


def test_recorder_utc_timestamps_are_binned_in_configured_local_timezone():
    states = [
        SimpleNamespace(
            state="1000",
            attributes={"unit_of_measurement": "W"},
            last_updated=datetime(2026, 8, 10, 22, 59, tzinfo=ZoneInfo("UTC")),
        ),
        SimpleNamespace(
            state="1000",
            attributes={"unit_of_measurement": "W"},
            last_updated=datetime(2026, 8, 10, 23, 1, tzinfo=ZoneInfo("UTC")),
        ),
    ]

    days = _series_to_bins(states, MADRID)

    assert list(days) == [date(2026, 8, 11)]
    assert days[date(2026, 8, 11)].coverage_s[3] == pytest.approx(60)
    assert days[date(2026, 8, 11)].coverage_s[4] == pytest.approx(60)


@pytest.mark.asyncio
async def test_recorder_binning_runs_through_home_assistant_executor():
    profile = _profile()
    executor_calls = []

    def async_add_executor_job(target, *args):
        executor_calls.append((target, args))
        return asyncio.get_running_loop().run_in_executor(None, target, *args)

    profile._hass.async_add_executor_job = async_add_executor_job
    states = [
        SimpleNamespace(
            state="1000",
            attributes={"unit_of_measurement": "W"},
            last_updated=datetime(2026, 8, 10, 10, minute, tzinfo=MADRID),
        )
        for minute in (0, 1)
    ]

    days = await profile._series_to_bins_in_executor(states, MADRID)

    assert executor_calls == [(_series_to_bins, (states, MADRID))]
    assert days[date(2026, 8, 10)].coverage_s[40] == pytest.approx(60)


@pytest.mark.parametrize(
    ("factor", "expected_kwh"),
    [
        (-1.0, 0.25),
        (-0.6, 0.35),
        (1.0, 0.75),
    ],
)
def test_recorder_external_load_adjustment_preserves_energy_units(
    factor,
    expected_kwh,
):
    """Backfill must subtract/add kWh, not treat kWh/s as kW."""
    local_date = date(2026, 8, 10)
    home = _single_interval_day(local_date, 0.5)
    device = ProfileDay(local_date)
    device.energy_kwh[40] = 0.125
    device.coverage_s[40] = INTERVAL_SECONDS / 2

    _apply_external_load_to_day(home, device, factor)

    assert home.energy_kwh[40] == pytest.approx(expected_kwh)


def test_capture_breaks_continuity_after_unknown_and_long_gap():
    profile = _profile()
    start = datetime(2026, 8, 10, 10, 0, tzinfo=MADRID)
    profile.record_power_sample(1.0, local_time=start, monotonic_time=0.0)
    profile.record_power_sample(
        1.0,
        local_time=start + timedelta(minutes=2),
        monotonic_time=120.0,
    )
    profile.record_power_sample(
        None,
        local_time=start + timedelta(minutes=3),
        monotonic_time=180.0,
    )
    profile.record_power_sample(
        1.0,
        local_time=start + timedelta(minutes=13),
        monotonic_time=780.0,
    )
    profile.record_power_sample(
        1.0,
        local_time=start + timedelta(minutes=19),
        monotonic_time=1140.0,
    )

    day = profile._days[start.date()]
    assert day.coverage_s[40] == pytest.approx(120.0)
    assert day.coverage_s[41] == pytest.approx(0.0)


def test_corrupt_profile_day_is_rejected_without_partial_data():
    assert ConsumptionProfileTracker._parse_day({"date": "2026-08-10"}) is None
    assert ConsumptionProfileTracker._parse_day(
        {
            "date": "2026-08-10",
            "energy_kwh": [0.0] * (INTERVAL_COUNT - 1),
            "coverage_s": [0.0] * INTERVAL_COUNT,
        }
    ) is None


def test_profile_day_requires_seventy_five_percent_coverage():
    day = ProfileDay(
        date(2026, 8, 10),
        [1.0] + [0.0] * (INTERVAL_COUNT - 1),
        [MIN_INTERVAL_COVERAGE_S - 1] + [0.0] * (INTERVAL_COUNT - 1),
        complete=True,
    )
    assert day.normalized_interval(0) is None

    day.coverage_s[0] = MIN_INTERVAL_COVERAGE_S
    assert day.normalized_interval(0) == pytest.approx(INTERVAL_SECONDS / MIN_INTERVAL_COVERAGE_S)


def test_daily_energy_does_not_reuse_training_only_partial_coverage():
    local_date = date(2026, 8, 10)
    day = ProfileDay(
        local_date,
        [0.25] * 72 + [0.0] * 24,
        [INTERVAL_SECONDS] * 72 + [0.0] * 24,
        complete=True,
    )
    profile = _profile({local_date: day})

    # The day remains useful for profile training but cannot be treated as a
    # complete legacy daily total (18 kWh instead of the physical 24 kWh).
    assert profile._day_needs_backfill(local_date) is False
    assert profile.daily_energy_for_date(local_date) is None


@pytest.mark.parametrize(
    ("local_date", "skipped", "repeated", "expected_kwh"),
    [
        (date(2026, 3, 29), range(8, 12), (), 23.0),
        (date(2026, 10, 25), (), range(8, 12), 25.0),
    ],
)
def test_daily_energy_accepts_complete_dst_days(
    local_date,
    skipped,
    repeated,
    expected_kwh,
):
    energy = [0.25] * INTERVAL_COUNT
    coverage = [INTERVAL_SECONDS] * INTERVAL_COUNT
    for index in skipped:
        energy[index] = 0.0
        coverage[index] = 0.0
    for index in repeated:
        energy[index] = 0.5
        coverage[index] = 2 * INTERVAL_SECONDS
    profile = _profile(
        {local_date: ProfileDay(local_date, energy, coverage, complete=True)}
    )

    assert profile.daily_energy_for_date(local_date) == pytest.approx(expected_kwh)


def test_profile_retention_keeps_current_and_previous_twenty_eight_days():
    profile = _profile()
    today = date.today()
    profile._days = {
        today - timedelta(days=offset): _day(today - timedelta(days=offset), 1.0)
        for offset in range(31)
    }
    profile._prune(today)

    assert len(profile._days) == 29
    assert min(profile._days) == today - timedelta(days=28)
    assert max(profile._days) == today


def test_partial_current_day_is_kept_but_not_used_as_training_sample():
    today = date.today()
    profile = _profile({today: _day(today, 2.0, coverage=INTERVAL_SECONDS)})
    profile._days[today].complete = False

    forecast = profile.forecast_for_date(today)

    assert forecast.total_days == 0
    assert forecast.mature is False
    assert forecast.source == "legacy_daily"


def test_current_day_capture_exposes_raw_energy_and_coverage():
    profile = _profile()
    today = date.today()
    start = datetime.combine(today, datetime.min.time()).replace(
        hour=10,
        tzinfo=MADRID,
    )
    profile.record_power_sample(1.0, local_time=start, monotonic_time=0.0)
    profile.record_power_sample(
        1.0,
        local_time=start + timedelta(minutes=5),
        monotonic_time=300.0,
    )
    profile.record_power_sample(
        1.0,
        local_time=start + timedelta(minutes=10),
        monotonic_time=600.0,
    )
    profile.record_power_sample(
        1.0,
        local_time=start + timedelta(minutes=15),
        monotonic_time=900.0,
    )

    capture = profile.current_day_capture(today)

    assert capture["date"] == today.isoformat()
    assert capture["complete"] is False
    assert capture["energy_kwh"] == pytest.approx(0.25)
    assert capture["hourly_energy_kwh"][10] == pytest.approx(0.25)
    assert capture["interval_energy_kwh"][40] == pytest.approx(0.25)
    assert capture["interval_coverage_s"][40] == pytest.approx(900.0)
    assert capture["valid_intervals"] == 1
    assert capture["coverage_ratio"] == pytest.approx(round(900 / 86400, 6))


def test_vacation_mask_keeps_raw_capture_but_excludes_profile_training():
    local_date = date(2026, 6, 4)
    profile = _profile({local_date: _single_interval_day(local_date, 0.25, 40)})
    profile.set_excluded_periods([{
        "start": "2026-06-04T10:00:00+02:00",
        "end": "2026-06-04T10:15:00+02:00",
    }])

    capture = profile.current_day_capture(local_date)
    forecast = profile.forecast_for_date(local_date)

    assert capture["interval_energy_kwh"][40] == pytest.approx(0.25)
    assert capture["interval_coverage_s"][40] == pytest.approx(INTERVAL_SECONDS)
    assert forecast.total_days == 0


def test_vacation_mask_covers_second_dst_fold():
    local_date = date(2026, 10, 25)
    profile = _profile()
    profile.set_excluded_periods([{
        "start": datetime(2026, 10, 25, 2, 5, tzinfo=MADRID, fold=1).isoformat(),
        "end": datetime(2026, 10, 25, 2, 10, tzinfo=MADRID, fold=1).isoformat(),
    }])
    assert profile._interval_is_excluded(local_date, 8)  # 02:00–02:15
    assert not profile._interval_is_excluded(local_date, 11)  # 02:45–03:00 fold=1


def test_current_day_capture_is_unavailable_without_covered_samples():
    profile = _profile()
    today = date.today()
    start = datetime.combine(today, datetime.min.time()).replace(
        hour=10,
        tzinfo=MADRID,
    )

    assert profile.current_day_capture(today) is None

    # The first valid sample establishes a baseline but cannot integrate
    # energy yet.  This is the transient state observed after a restart.
    profile.record_power_sample(1.0, local_time=start, monotonic_time=0.0)

    assert profile.current_day_capture(today) is None


def test_current_day_capture_keeps_zero_energy_when_coverage_is_valid():
    profile = _profile({date.today(): _day(date.today(), 0.0)})

    capture = profile.current_day_capture(date.today())

    assert capture is not None
    assert capture["energy_kwh"] == 0.0
    assert capture["valid_intervals"] == INTERVAL_COUNT


def test_four_weekday_samples_are_weighted_by_age_and_profile_becomes_mature():
    # Freeze the profile's notion of today so the age weights do not depend on
    # the calendar date on which the test suite happens to run.
    monday_samples = {
        date(2026, 8, 10): 1.0,
        date(2026, 8, 3): 2.0,
        date(2026, 7, 27): 3.0,
        date(2026, 7, 20): 4.0,
    }
    days = {local_date: _day(local_date, value) for local_date, value in monday_samples.items()}
    for local_date in (date(2026, 8, 9), date(2026, 8, 8), date(2026, 8, 7)):
        days[local_date] = _day(local_date, 2.0)

    profile = _profile(days)
    profile._today = lambda: date(2026, 8, 15)
    forecast = profile.forecast_for_date(date(2026, 8, 17))

    # Weighted weekday mean = (1 + .75*2 + .50*3 + .25*4) / 2.5 = 2.
    assert forecast.mature is True
    assert forecast.source == "profile"
    assert forecast.intervals_kwh[40] == pytest.approx(2.0)
    assert forecast.energy_kwh == pytest.approx(2.0 * INTERVAL_COUNT)
    assert forecast.weekday_samples == 4


def test_partial_query_maturity_uses_requested_intervals_only():
    days = {
        date(2026, 8, 10): _single_interval_day(date(2026, 8, 10), 1.0),
        date(2026, 8, 3): _single_interval_day(date(2026, 8, 3), 1.0),
        date(2026, 7, 27): _single_interval_day(date(2026, 7, 27), 1.0),
        date(2026, 7, 20): _single_interval_day(date(2026, 7, 20), 1.0),
        date(2026, 8, 9): _single_interval_day(date(2026, 8, 9), 1.0),
        date(2026, 8, 8): _single_interval_day(date(2026, 8, 8), 1.0),
        date(2026, 8, 7): _single_interval_day(date(2026, 8, 7), 1.0),
    }

    profile = _profile(days)
    profile._today = lambda: date(2026, 8, 15)
    forecast = profile.forecast_for_date(
        date(2026, 8, 17),
        interval_indices={40},
    )

    assert forecast.mature is True
    assert forecast.coverage_ratio == pytest.approx(1.0)
    assert forecast.intervals_kwh[40] == pytest.approx(1.0)


def test_immature_profile_uses_coherent_legacy_fallback():
    days = {date.today() - timedelta(days=1): _day(date.today() - timedelta(days=1), 2.0)}
    forecast = _profile(days).forecast_for_date(date.today())

    assert forecast.mature is False
    assert forecast.source == "legacy_daily"
    assert forecast.fallback_reason == "insufficient_days"
    assert forecast.energy_kwh == pytest.approx(5.0)


def test_legacy_fallback_uses_household_shape_without_changing_daily_total():
    intervals = fallback_daily_intervals(30.0)
    hourly = [
        sum(intervals[index:index + 4])
        for index in range(0, INTERVAL_COUNT, 4)
    ]

    assert len(FALLBACK_HOURLY_WEIGHTS) == 24
    assert sum(intervals) == pytest.approx(30.0)
    assert min(hourly[:6]) == pytest.approx(hourly[0])
    assert hourly[8] > hourly[9] > hourly[2]
    assert hourly[20] > hourly[12] > hourly[2]
    assert hourly[22] > hourly[2]


def test_live_fallback_adjustment_tracks_today_without_following_spikes():
    adjusted, correction = adjust_remaining_fallback_energy(10.0, 20.0, 15.0, 12.0)

    assert correction == pytest.approx(-3.0)
    assert adjusted == pytest.approx(7.0)

    adjusted, correction = adjust_remaining_fallback_energy(10.0, 20.0, 5.0, 12.0)

    assert correction == pytest.approx(3.0)
    assert adjusted == pytest.approx(13.0)


def test_live_fallback_reconciles_reported_notification_with_daily_budget():
    """A high consumed total must reduce, not inflate, the shaped remainder."""
    adjusted, correction = adjust_remaining_fallback_energy(
        10.381074740110245,
        31.435714285714287,
        25.35003983403174,
        17.8375,
    )

    assert correction == pytest.approx(-3.1143224220330747)
    assert adjusted == pytest.approx(7.266752318077171)


def test_live_fallback_adjustment_waits_for_enough_observation():
    adjusted, correction = adjust_remaining_fallback_energy(18.0, 20.0, 8.0, 3.0)

    assert correction == 0.0
    assert adjusted == pytest.approx(18.0)


def test_legacy_fallback_does_not_redistribute_masked_charging_window():
    monday = date(2026, 8, 17)
    profile = _profile(
        slots=[{
            "days": ["mon"],
            "start_time": "00:00",
            "end_time": "06:00",
        }],
        fallback_daily=30.0,
    )
    start = datetime.combine(monday, datetime.min.time()).replace(tzinfo=MADRID)

    result = profile.forecast_energy_between(
        start,
        start + timedelta(days=1),
        exclude_charging_windows=True,
        fallback="legacy_daily",
    )

    assert result.source == "legacy_daily"
    full_day = fallback_daily_intervals(30.0)
    assert result.energy_kwh == pytest.approx(sum(full_day[24:]))


@pytest.mark.parametrize(
    "local_date",
    [date(2026, 3, 29), date(2026, 10, 25)],
)
def test_legacy_fallback_preserves_daily_total_across_dst(local_date):
    profile = _profile(fallback_daily=30.0)
    start = datetime.combine(local_date, datetime.min.time()).replace(tzinfo=MADRID)
    end = datetime.combine(
        local_date + timedelta(days=1),
        datetime.min.time(),
    ).replace(tzinfo=MADRID)

    result = profile.forecast_energy_between(
        start,
        end,
        exclude_charging_windows=False,
        fallback="legacy_daily",
    )

    assert result.energy_kwh == pytest.approx(30.0)


@pytest.mark.parametrize(
    "local_date",
    [date(2026, 3, 29), date(2026, 10, 25)],
)
def test_range_forecast_keeps_dst_scaled_shape_by_date(local_date):
    profile = _profile(fallback_daily=30.0)
    start = datetime.combine(local_date, datetime.min.time()).replace(tzinfo=MADRID)
    end = datetime.combine(
        local_date + timedelta(days=1),
        datetime.min.time(),
    ).replace(tzinfo=MADRID)

    result = profile.forecast_energy_between(
        start,
        end,
        exclude_charging_windows=False,
        fallback="legacy_daily",
    )
    dated_shape = result.intervals_by_date[local_date]
    dated_energy = 0.0
    for segment_start, segment_end, midpoint in _local_segments(start, end):
        index = midpoint.hour * 4 + midpoint.minute // 15
        dated_energy += (
            dated_shape[index]
            * (segment_end - segment_start)
            / INTERVAL_SECONDS
        )

    assert dated_energy == pytest.approx(30.0)


def test_range_forecast_keeps_the_nominal_shape_for_each_date():
    today = date(2026, 8, 24)
    tomorrow = today + timedelta(days=1)
    profile = _profile()

    def forecast_for_date(target_date, **_kwargs):
        values = [0.0] * INTERVAL_COUNT
        values[40] = 1.0 if target_date == today else 9.0
        return ConsumptionForecast(
            sum(values), values, "profile", True,
        )

    profile.forecast_for_date = forecast_for_date
    start = datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    end = datetime(2026, 8, 25, 10, 15, tzinfo=MADRID)

    result = profile.forecast_energy_between(
        start,
        end,
        exclude_charging_windows=False,
        fallback="legacy_daily",
    )

    # The legacy interval vector remains an aggregate for range callers, but
    # a chronological consumer can retain the distinct date-specific shape.
    assert result.intervals_kwh[40] == pytest.approx(10.0)
    assert result.intervals_by_date[today][40] == pytest.approx(1.0)
    assert result.intervals_by_date[tomorrow][40] == pytest.approx(9.0)


def test_range_query_prorates_partial_interval_and_masks_slot_days():
    today = date.today()
    profile = _profile(
        {today - timedelta(days=offset): _day(today - timedelta(days=offset), 1.0)
         for offset in range(14)},
        slots=[{
            "days": ["mon"],
            "start_time": "10:00",
            "end_time": "11:00",
        }],
    )
    monday = today + timedelta(days=(0 - today.weekday()) % 7)
    start = datetime.combine(monday, datetime.min.time()).replace(tzinfo=MADRID)
    result = profile.forecast_energy_between(
        start + timedelta(hours=9, minutes=52),
        start + timedelta(hours=10, minutes=8),
        exclude_charging_windows=True,
        fallback="legacy_daily",
    )

    # The 16 minutes are all in one nominal 1 kWh/hour profile, but the middle
    # 8 minutes are a configured Monday charging window.
    assert result.energy_kwh == pytest.approx(8 / 15, abs=1e-6)


def test_a_source_change_keeps_the_learned_days():
    """No integration setting may erase learned capture (only a timezone move)."""
    today = date.today()
    profile = _profile({today: _day(today, 1.0)})
    profile._config_entry = SimpleNamespace(
        entry_id="e1", data={"consumption_sensor": "sensor.grid"}
    )
    profile._backfill_generation = 1
    profile._backfill_task = None
    profile._backfill_status = "complete"
    profile._active_fingerprint = profile.configuration_fingerprint()

    assert profile.invalidate_if_configuration_changed() is False

    profile._config_entry.data = {"consumption_sensor": "sensor.other_grid"}

    assert profile.invalidate_if_configuration_changed() is True
    assert list(profile._days) == [today]
    # Continuity is broken so the next sample is a baseline, not a trapezoid
    # spanning both derivations.
    assert profile._last_sample_time is None


def test_a_timezone_change_moves_the_bins_instead_of_dropping_them():
    """Madrid 20:00 is Lisbon 19:00: the same energy, one hour earlier."""
    day = ProfileDay(date(2026, 3, 3))
    day.energy_kwh[80] = 0.5  # 20:00-20:15 local
    day.coverage_s[80] = INTERVAL_SECONDS
    day.complete = True

    rebinned = rebin_days_to_timezone(
        {day.local_date: day}, MADRID, ZoneInfo("Europe/Lisbon")
    )

    moved = rebinned[date(2026, 3, 3)]
    assert moved.energy_kwh[76] == pytest.approx(0.5)  # 19:00-19:15 local
    assert moved.coverage_s[76] == pytest.approx(INTERVAL_SECONDS)
    assert moved.energy_kwh[80] == 0.0
    # A day rebuilt from partial hours stays incomplete so backfill re-fetches it.
    assert moved.complete is False


def test_rebinning_carries_energy_across_the_local_date_boundary():
    """Madrid 00:00 is the previous day 23:00 in Lisbon."""
    day = ProfileDay(date(2026, 3, 3))
    day.energy_kwh[0] = 0.25
    day.coverage_s[0] = INTERVAL_SECONDS

    rebinned = rebin_days_to_timezone(
        {day.local_date: day}, MADRID, ZoneInfo("Europe/Lisbon")
    )

    assert set(rebinned) == {date(2026, 3, 2)}
    assert rebinned[date(2026, 3, 2)].energy_kwh[92] == pytest.approx(0.25)


def test_one_week_of_day_type_samples_matures_without_a_weekday_pair():
    # Seven consecutive days ending Wednesday 2026-09-02, forecasting Thursday
    # 2026-09-03: only one Thursday (Aug 27) exists, so the same-weekday pair is
    # arithmetically impossible. The weekday-type samples must carry the day.
    days = {
        date(2026, 8, 27) + timedelta(days=offset): _day(
            date(2026, 8, 27) + timedelta(days=offset), 0.25
        )
        for offset in range(7)
    }

    profile = _profile(days)
    profile._today = lambda: date(2026, 9, 3)
    forecast = profile.forecast_for_date(date(2026, 9, 3))

    assert forecast.weekday_samples == 1  # the single Thursday, below the pair
    assert forecast.mature is True
    assert forecast.source == "profile"
    assert forecast.fallback_reason is None
    assert forecast.energy_kwh == pytest.approx(0.25 * INTERVAL_COUNT)
