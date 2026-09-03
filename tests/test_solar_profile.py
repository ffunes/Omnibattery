"""Pure tests for direct-PV capture and temporal profile learning."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import custom_components.omnibattery as omnibattery
from custom_components.omnibattery.tracking.consumption_tracker import ConsumptionTracker
from custom_components.omnibattery.tracking.solar_profile import (
    SOLAR_PROFILE_INTERVAL_COUNT,
    SolarProfileDay,
    SolarProfileTracker,
    SolarQualityFlag,
    remap_day_to_progress,
    weighted_median,
)


MADRID = ZoneInfo("Europe/Madrid")


def test_controller_module_imports_solar_profile_configuration():
    """The controller constructor must have the profile constants at runtime."""
    assert omnibattery.CONF_SOLAR_PROFILE_MODE == "solar_profile_mode"
    assert omnibattery.DEFAULT_SOLAR_PROFILE_MODE == "active"
    assert omnibattery.SOLAR_PROFILE_MODES == ("off", "shadow", "active")
    assert omnibattery.normalize_solar_profile_mode(None) == "active"
    assert omnibattery.normalize_solar_profile_mode("shadow") == "active"
    assert omnibattery.normalize_solar_profile_mode("active") == "active"
    assert omnibattery.normalize_solar_profile_mode("off") == "off"


def _profile() -> SolarProfileTracker:
    profile = SolarProfileTracker.__new__(SolarProfileTracker)
    profile._hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Madrid"))
    profile._config_entry = SimpleNamespace(data={"solar_production_sensor": "sensor.pv"})
    profile._controller = SimpleNamespace(
        solar_production_sensor="sensor.pv",
        solar_profile_mode="shadow",
        coordinators=(),
    )
    profile._days = {}
    profile._last_sample_time = None
    profile._last_sample_monotonic = None
    profile._last_power_kw = None
    profile._last_local_date = None
    profile._last_save_monotonic = 1.0
    profile._save_task = None
    profile._backfill_task = None
    profile._last_error = None
    profile._backfill_status = "not_started"
    profile._loaded = True
    profile._active_fingerprint = "test"
    profile._generation = 1
    profile._positive_candidate_start = None
    profile._positive_run_seconds = 0.0
    return profile


def _full_day(local_date: date) -> SolarProfileDay:
    coverage = [900.0] * SOLAR_PROFILE_INTERVAL_COUNT
    energy = [0.25] * SOLAR_PROFILE_INTERVAL_COUNT
    return SolarProfileDay(
        local_date,
        energy_kwh=energy,
        coverage_s=coverage,
        solar_start=datetime.combine(local_date, time(8), tzinfo=MADRID),
        solar_end=datetime.combine(local_date, time(20), tzinfo=MADRID),
        complete=True,
    )


def test_weighted_median_rejects_an_outlier_by_weight():
    assert weighted_median([(1.0, 5.0), (2.0, 1.0), (100.0, 1.0)]) == pytest.approx(1.0)


def test_progress_remap_preserves_energy_inside_observed_solar_window():
    day = _full_day(date(2026, 8, 10))

    energy, coverage, flags = remap_day_to_progress(day)

    assert sum(energy) == pytest.approx(12.0)
    assert sum(coverage) == pytest.approx(12 * 3600)
    assert len(energy) == len(coverage) == len(flags) == 96


def test_direct_capture_uses_trapezoid_and_requires_sustained_positive_power():
    profile = _profile()
    local_date = date.today()
    start = datetime.combine(local_date, time(10), tzinfo=MADRID)

    profile.record_power_sample(1.0, local_time=start, monotonic_time=0.0)
    profile.record_power_sample(
        1.0, local_time=start + timedelta(seconds=60), monotonic_time=60.0
    )
    profile.record_power_sample(
        2.0, local_time=start + timedelta(seconds=120), monotonic_time=120.0
    )

    day = profile._days[local_date]
    assert day.solar_start == start
    assert day.solar_end == start + timedelta(seconds=120)
    assert sum(day.energy_kwh) == pytest.approx((1.0 + 1.0) / 2 * 60 / 3600 + (1.0 + 2.0) / 2 * 60 / 3600)
    assert sum(day.coverage_s) == pytest.approx(120.0)


def test_mature_profile_requires_recent_complete_days():
    profile = _profile()
    today = date.today()
    profile._days = {
        today - timedelta(days=offset): _full_day(today - timedelta(days=offset))
        for offset in range(1, 8)
    }

    snapshot = profile.learn_shape(today)

    assert snapshot.mature is True
    assert snapshot.eligible_days == 7
    assert sum(snapshot.shape) == pytest.approx(1.0)
    assert all(count == 7 for count in snapshot.bin_contributions)


def test_empty_future_solar_range_preserves_mature_profile():
    profile = _profile()
    today = date.today()
    profile._days = {
        today - timedelta(days=offset): _full_day(today - timedelta(days=offset))
        for offset in range(1, 8)
    }

    snapshot = profile.get_snapshot(
        target_date=today,
        future_progress_start=1.0,
        future_progress_end=1.0,
    )

    assert snapshot.mature is True
    assert snapshot.future_coverage_ratio == pytest.approx(1.0)
    assert snapshot.fallback_reason is None


def test_legacy_shadow_mode_is_normalized_to_automatic_profile_mode():
    profile = _profile()

    profile.refresh_mode("shadow")

    assert profile.mode == "active"

    profile.refresh_mode("off")
    assert profile.mode == "off"


def test_curtailment_context_is_persisted_as_quality_flags():
    profile = _profile()
    local_date = date.today() - timedelta(days=1)
    profile._days[local_date] = _full_day(local_date)

    profile.mark_curtailment_intervals(
        local_date,
        [40, 41],
        battery_full_risk=True,
    )

    assert profile._days[local_date].quality_flags[40] & int(SolarQualityFlag.CURTAILMENT_SUSPECTED)
    assert profile._days[local_date].quality_flags[40] & int(SolarQualityFlag.BATTERY_FULL_RISK)


def test_direct_power_reader_sums_external_pv_and_mppt_only():
    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._controller = SimpleNamespace(
        solar_production_sensor="sensor.pv",
        coordinators=[
            SimpleNamespace(
                capabilities=SimpleNamespace(has_mppt_pv=True),
                is_available=True,
                data={"mppt1_power": 500, "ac_power": 900},
            )
        ],
    )
    tracker._hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda entity_id: SimpleNamespace(
                state="2000", attributes={"unit_of_measurement": "W"}
            )
            if entity_id == "sensor.pv"
            else None
        )
    )

    assert tracker._read_total_solar_power_kw() == pytest.approx(2.5)


def test_direct_power_reader_accepts_aggregate_battery_pv():
    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._controller = SimpleNamespace(
        solar_production_sensor=None,
        coordinators=[
            SimpleNamespace(
                capabilities=SimpleNamespace(
                    has_mppt_pv=False,
                    has_solar_telemetry=True,
                ),
                is_available=True,
                data={"solar_power": 900},
            )
        ],
    )
    tracker._hass = SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: None))

    assert tracker._read_total_solar_power_kw() == pytest.approx(0.9)


def test_solar_profile_excludes_anker_ac_derived_pv():
    profile = _profile()
    profile._controller.coordinators = [
        SimpleNamespace(
            capabilities=SimpleNamespace(
                has_mppt_pv=False,
                has_solar_telemetry=False,
            )
        )
    ]

    assert profile.telemetry_source() == "external"


def test_solar_profile_keeps_verified_anker_aggregate_pv():
    profile = _profile()
    profile._controller.coordinators = [
        SimpleNamespace(
            capabilities=SimpleNamespace(
                has_mppt_pv=False,
                has_solar_telemetry=True,
            )
        )
    ]

    assert profile.telemetry_source() == "external_plus_aggregate"


def test_sustained_peak_shift_starts_a_new_generation():
    profile = _profile()
    profile.request_save = lambda: None
    reference = date.today()
    profile._days = {}
    for offset in range(17, 0, -1):
        local_date = reference - timedelta(days=offset)
        profile._days[local_date] = _full_day(local_date)
    for offset in (3, 2, 1):
        day = profile._days[reference - timedelta(days=offset)]
        day.energy_kwh = [0.35] * SOLAR_PROFILE_INTERVAL_COUNT

    assert profile.detect_capacity_regime(reference) is True
    assert profile.generation == 2
    assert all(
        profile._days[reference - timedelta(days=offset)].generation == 2
        for offset in (3, 2, 1)
    )
    assert all(
        profile._days[reference - timedelta(days=offset)].generation == 1
        for offset in range(4, 18)
    )


def test_invalid_direct_power_does_not_become_a_zero_sample():
    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda _entity_id: SimpleNamespace(
                state="-1", attributes={"unit_of_measurement": "W"}
            )
        )
    )

    assert tracker._read_power_kw("sensor.pv") is None


def test_day_reports_name_the_reason_each_day_was_rejected():
    profile = _profile()
    today = date.today()
    valid_day = today - timedelta(days=1)
    windowless = today - timedelta(days=2)
    holed = today - timedelta(days=3)
    profile._days = {
        valid_day: _full_day(valid_day),
        windowless: _full_day(windowless),
        holed: _full_day(holed),
    }
    # A day whose solar window was never established still holds raw energy.
    profile._days[windowless].solar_start = None
    profile._days[windowless].solar_end = None
    # A day sampled with gaps keeps its window but loses coverage inside it.
    for index in range(32, 64):
        profile._days[holed].coverage_s[index] = 0.0
        profile._days[holed].energy_kwh[index] = 0.0

    reports = {report["date"]: report for report in profile._day_reports()}

    assert reports[valid_day.isoformat()]["valid"] is True
    assert reports[valid_day.isoformat()]["reason"] is None
    assert reports[windowless.isoformat()]["reason"] == "solar_window_invalid"
    assert reports[holed.isoformat()]["reason"] == "insufficient_coverage"
    assert reports[holed.isoformat()]["coverage_ratio"] < 0.90


def test_stale_scale_days_cannot_veto_the_current_scale():
    """A batch of days recorded at a wrong scale must age out of the reference.

    Regression for a profile that recorded ~60x inflated day totals until a
    fix landed: the inflated days kept the relative-energy median so high that
    every correct day after them was rejected, and the profile could never
    recover on its own.
    """
    profile = _profile()
    today = date.today()
    profile._days = {}
    for offset in range(8, 18):  # Old regime: same shape, 60x the energy.
        local_date = today - timedelta(days=offset)
        day = _full_day(local_date)
        day.energy_kwh = [value * 60.0 for value in day.energy_kwh]
        profile._days[local_date] = day
    for offset in range(1, 8):  # Current regime: correct energy.
        local_date = today - timedelta(days=offset)
        profile._days[local_date] = _full_day(local_date)

    reports = {report["date"]: report for report in profile._day_reports()}
    current = [
        reports[(today - timedelta(days=offset)).isoformat()] for offset in range(1, 8)
    ]

    assert all(report["valid"] for report in current), [
        (report["date"], report["reason"]) for report in current if not report["valid"]
    ]
    assert profile.learn_shape(today).mature is True


def test_regime_change_carries_the_whole_matching_run_and_stays_mature():
    """The new generation must keep the days that already match the new scale.

    Moving only the three confirming days -- and flagging all of them as a
    transition, which excludes their bins -- left the new generation with no
    shape at all, so a profile that was mature went back to the sinusoidal
    fallback for a week after every regime change.
    """
    profile = _profile()
    profile.request_save = lambda: None
    reference = date.today()
    profile._days = {}
    for offset in range(1, 25):
        local_date = reference - timedelta(days=offset)
        day = _full_day(local_date)
        if offset >= 8:  # Old regime: same shape, 60x the energy.
            day.energy_kwh = [value * 60.0 for value in day.energy_kwh]
        profile._days[local_date] = day
    assert profile.learn_shape(reference).mature is True

    assert profile.detect_capacity_regime(reference) is True

    assert profile.generation == 2
    carried = [reference - timedelta(days=offset) for offset in range(1, 8)]
    assert all(profile._days[local_date].generation == 2 for local_date in carried)
    assert all(
        profile._days[reference - timedelta(days=offset)].generation == 1
        for offset in range(8, 25)
    )
    # Only the oldest day of the run straddles the change.
    flagged = [
        local_date
        for local_date in carried
        if any(
            flags & int(SolarQualityFlag.CONFIGURATION_TRANSITION)
            for flags in profile._days[local_date].quality_flags
        )
    ]
    assert flagged == [reference - timedelta(days=7)]
    # The six unflagged days still describe a shape, so no maturity gap opens.
    snapshot = profile.learn_shape(reference)
    assert snapshot.mature is True
    assert snapshot.eligible_days == 7
    assert all(count == 6 for count in snapshot.bin_contributions)
