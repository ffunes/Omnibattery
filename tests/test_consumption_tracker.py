"""Characterization tests for ConsumptionTracker.

These pin the *current* behavior so the planned module refactors can be proven
to change nothing. No Home Assistant entities, no Modbus, no battery: the pure
helpers are called directly, and the one instance test uses the in-process
``hass`` fixture plus a stand-in controller object.
"""
from __future__ import annotations

import asyncio
import math
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.const import (
    DEFAULT_BASE_CONSUMPTION_KWH,
)
from custom_components.omnibattery.tracking.consumption_tracker import (
    CONSUMPTION_HISTORY_SCOPE,
    ConsumptionTracker,
)
from tests.conftest import FakeCoordinator


# ----------------------------------------------------------------------
# Pure solar-energy model: get_solar_fraction_done (static, no HA needed)
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "now_h, t_start, t_end, expected",
    [
        (8.0, 8.0, 16.0, 0.0),    # at sunrise -> nothing produced yet
        (16.0, 8.0, 16.0, 1.0),   # at sunset  -> fully produced
        (12.0, 8.0, 16.0, 0.5),   # midpoint   -> half (sinusoid is symmetric)
        (7.0, 8.0, 16.0, 0.0),    # before window -> clamped to 0
        (17.0, 8.0, 16.0, 1.0),   # after window  -> clamped to 1
        (10.0, 8.0, 16.0, (1.0 - math.cos(math.pi * 0.25)) / 2.0),  # quarter way
    ],
)
def test_solar_fraction_curve(now_h, t_start, t_end, expected):
    result = ConsumptionTracker.get_solar_fraction_done(now_h, t_start, t_end)
    assert result == pytest.approx(expected)


def test_solar_fraction_invalid_window_returns_full():
    # t_end <= t_start is treated as "all produced" rather than dividing by zero.
    assert ConsumptionTracker.get_solar_fraction_done(10.0, 12.0, 12.0) == 1.0
    assert ConsumptionTracker.get_solar_fraction_done(10.0, 12.0, 8.0) == 1.0


# ----------------------------------------------------------------------
# Pure formatting helper: h_to_hhmm (static, no HA needed)
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "hours, expected",
    [
        (13.25, "13:15"),
        (7.5, "07:30"),
        (0.0, "00:00"),
        (9.0, "09:00"),
        (None, None),
    ],
)
def test_h_to_hhmm(hours, expected):
    assert ConsumptionTracker.h_to_hhmm(hours) == expected


# ----------------------------------------------------------------------
# Instance method with a mocked controller: get_avg_daily_consumption
# Proves the controller-by-reference pattern is testable without hardware.
# The tracker is built via __new__ so __init__ (which needs a real hass for
# its Store objects) is skipped: this method only reads one controller attr,
# so isolating it that way keeps the test free of the hass fixture.
# ----------------------------------------------------------------------

def _make_tracker(history):
    """Build a tracker wired to a stand-in controller holding `history`."""
    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._controller = SimpleNamespace(_daily_consumption_history=history)
    return tracker


def test_avg_daily_consumption_empty_uses_fallback():
    tracker = _make_tracker([])
    assert tracker.get_avg_daily_consumption() == DEFAULT_BASE_CONSUMPTION_KWH


def test_avg_daily_consumption_averages_history():
    history = [(date(2026, 6, 1), 4.0), (date(2026, 6, 2), 6.0)]
    tracker = _make_tracker(history)
    assert tracker.get_avg_daily_consumption() == pytest.approx(5.0)


def test_avg_daily_consumption_single_day():
    tracker = _make_tracker([(date(2026, 6, 1), 3.0)])
    assert tracker.get_avg_daily_consumption() == pytest.approx(3.0)


def test_vacation_forecast_uses_median_of_last_three_valid_nights():
    tracker = _make_tracker([])
    tracker._controller.vacation_mode_enabled = True
    tracker._vacation_nights = [
        {"date": "2026-06-01", "energy_kwh": 2.0, "coverage_s": 10800.0},
        {"date": "2026-06-02", "energy_kwh": 4.0, "coverage_s": 14400.0},
        {"date": "2026-06-03", "energy_kwh": 6.0, "coverage_s": 10800.0},
    ]
    forecast = tracker.forecast_consumption_between(
        datetime(2026, 6, 4, 8, tzinfo=timezone.utc),
        datetime(2026, 6, 4, 10, tzinfo=timezone.utc),
    )
    # Rates are 2/3, 1 and 2 kW; 3 hours is a valid incomplete night.
    assert forecast.source == "vacation_baseline"
    assert forecast.energy_kwh == pytest.approx(2.0)


def test_vacation_period_marks_a_partial_legacy_day_as_excluded():
    tracker = _make_tracker([])
    tracker._vacation_periods = [{
        "start": "2026-06-04T12:00:00+00:00", "end": "2026-06-04T14:00:00+00:00",
    }]
    assert tracker._period_intersects(
        datetime(2026, 6, 4, tzinfo=timezone.utc),
        datetime(2026, 6, 5, tzinfo=timezone.utc),
    )


def test_vacation_baseline_ignores_nights_without_three_hours_coverage():
    tracker = _make_tracker([])
    tracker._vacation_nights = [
        {"date": "2026-06-01", "energy_kwh": 9.0, "coverage_s": 10799.0},
        {"date": "2026-06-02", "energy_kwh": 3.0, "coverage_s": 10800.0},
    ]
    baseline_kw, source = tracker._vacation_baseline_kw()
    assert baseline_kw == pytest.approx(1.0)
    assert source == "vacation_night_median"


def test_vacation_baseline_averages_two_valid_nights():
    tracker = _make_tracker([])
    tracker._vacation_nights = [
        {"date": "2026-06-01", "energy_kwh": 3.0, "coverage_s": 10800.0},
        {"date": "2026-06-02", "energy_kwh": 6.0, "coverage_s": 10800.0},
    ]
    assert tracker._vacation_baseline_kw()[0] == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_vacation_state_save_is_coalesced():
    tracker = _make_tracker([])
    tracker._vacation_save_task = None
    tracker._request_vacation_save()
    first = tracker._vacation_save_task
    tracker._request_vacation_save()
    assert tracker._vacation_save_task is first
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


@pytest.mark.asyncio
async def test_vacation_reconcile_starts_a_new_session_without_old_nights():
    tracker = _make_tracker([])
    tracker._controller.vacation_mode_enabled = True
    tracker._vacation_periods = [{
        "start": "2026-05-01T00:00:00+00:00", "end": "2026-05-02T00:00:00+00:00",
    }]
    tracker._vacation_nights = [{
        "date": "2026-05-01", "energy_kwh": 3.0, "coverage_s": 10800.0,
    }]
    tracker._consumption_profile = SimpleNamespace(set_excluded_periods=lambda _periods: None)
    tracker._vacation_store = _FakeConsumptionStore({})

    await tracker.async_reconcile_vacation_mode()

    assert tracker._vacation_nights == []
    assert tracker._vacation_periods[-1]["end"] is None


def test_vacation_night_uses_absolute_dst_duration():
    tracker = _make_tracker([])
    tracker._vacation_nights = []
    tracker._vacation_save_task = None
    madrid = ZoneInfo("Europe/Madrid")
    start = datetime(2026, 3, 29, 1, 59, 59, tzinfo=madrid)
    tracker._vacation_last_sample_time = start
    tracker._vacation_last_sample_mono = 0.0
    tracker._vacation_last_power_kw = 1.0
    tracker._request_vacation_save = lambda: None
    tracker._record_vacation_night_sample(
        1.0, datetime(2026, 3, 29, 3, 0, 1, tzinfo=madrid), 2.0
    )
    assert tracker._vacation_nights[0]["coverage_s"] == pytest.approx(2.0)
    assert tracker._vacation_nights[0]["energy_kwh"] == pytest.approx(2 / 3600)


@pytest.mark.asyncio
async def test_load_vacation_state_keeps_partial_night_but_not_baseline():
    tracker = _make_tracker([])
    tracker._controller.vacation_mode_enabled = False
    tracker._vacation_periods = []
    tracker._vacation_store = _FakeConsumptionStore({
        "periods": [],
        "nights": [{"date": "2026-06-01", "energy_kwh": 2.0, "coverage_s": 7200.0}],
    })
    tracker._consumption_profile = SimpleNamespace(set_excluded_periods=lambda _periods: None)
    await tracker.load_vacation_state()
    assert tracker._vacation_nights[0]["coverage_s"] == 7200.0
    assert tracker._vacation_baseline_kw()[1] != "vacation_night_median"


# ----------------------------------------------------------------------
# Every calendar day belongs in consumption history. Predictive grid-charging
# windows schedule charging; they do not make the home or battery inactive.
# ----------------------------------------------------------------------

def _make_history_tracker(history, charging_time_slots):
    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._vacation_periods = []
    tracker._controller = SimpleNamespace(
        _daily_consumption_history=history,
        charging_time_slots=charging_time_slots,
        predictive_charging_enabled=True,
    )
    # __new__ skips __init__, so anything the methods under test read has to be
    # set here. History loading filters vacation periods out of the restored
    # days, and without this it fails on the attribute rather than the logic.
    tracker._vacation_periods = []
    return tracker


_MON_FRI = [{"days": ["mon", "tue", "wed", "thu", "fri"],
             "start_time": "00:00", "end_time": "08:00"}]


def test_recent_history_days_include_weekend_despite_weekday_charge_window():
    tracker = _make_history_tracker([], _MON_FRI)
    days = tracker._recent_history_days(7, before=date(2026, 7, 3))
    assert days == [date(2026, 7, 3) - timedelta(days=i) for i in range(1, 8)]
    assert date(2026, 6, 27) in days
    assert date(2026, 6, 28) in days


def test_recent_history_days_all_when_no_slots():
    tracker = _make_history_tracker([], [])
    days = tracker._recent_history_days(7, before=date(2026, 7, 3))
    assert days == [date(2026, 7, 3) - timedelta(days=i) for i in range(1, 8)]


def test_initialize_defaults_seeds_seven_calendar_days():
    tracker = _make_history_tracker([], _MON_FRI)
    import custom_components.omnibattery.tracking.consumption_tracker as ct

    class _FrozenDate(date):
        @classmethod
        def today(cls):
            return date(2026, 7, 3)  # Friday

    orig = ct.date
    ct.date = _FrozenDate
    try:
        tracker.initialize_history_with_defaults()
    finally:
        ct.date = orig

    seeded = {d for d, _ in tracker._controller._daily_consumption_history}
    assert len(seeded) == 7
    assert date(2026, 6, 27) in seeded
    assert date(2026, 6, 28) in seeded


def test_consumption_window_is_full_day_with_weekday_charge_slot():
    tracker = _make_history_tracker([], _MON_FRI)
    assert tracker.is_in_consumption_window() is True
    assert tracker.get_consumption_window_hours_per_day() == 24.0
    assert tracker.consumption_window_hours_in_range(0.0, 8.0) == 8.0


@pytest.mark.asyncio
async def test_accumulator_counts_power_during_predictive_charge_window(monkeypatch):
    """A predictive slot must not pause household consumption learning."""
    tracker = _make_history_tracker([], _MON_FRI)
    tracker._controller._household_energy_accumulator = 2.0
    tracker._household_last_accumulation_time = 100.0
    tracker._consumption_profile = SimpleNamespace(record_power_sample=lambda *a, **kw: None)
    monkeypatch.setattr(tracker, "get_adjusted_home_power_kw", lambda: 0.5)

    import custom_components.omnibattery.tracking.consumption_tracker as ct
    monkeypatch.setattr(ct, "monotonic", lambda: 3700.0)

    await tracker.accumulate_household_consumption()

    assert tracker._controller._household_energy_accumulator == pytest.approx(2.5)


class _FakeConsumptionStore:
    def __init__(self, data):
        self._data = data

    async def async_load(self):
        return self._data

    async def async_save(self, data):
        self._data = data


def _make_daily_energy_controller(**overrides):
    """Build the controller fields used by the daily-energy Store."""
    values = {
        "_daily_solar_energy_kwh": 2.5,
        "_daily_solar_energy_date": date.today(),
        "_daily_home_energy_kwh": 7.25,
        "_daily_home_energy_date": date.today(),
        "_daily_grid_import_energy_kwh": 4.0,
        "_daily_grid_export_energy_kwh": 0.75,
        "_daily_grid_energy_date": date.today(),
        "_daily_solar_forecast_initial_kwh": None,
        "_daily_solar_forecast_initial_date": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_daily_solar_forecast_captures_first_value_only(monkeypatch):
    controller = _make_daily_energy_controller()
    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._controller = controller
    saved = []
    monkeypatch.setattr(tracker, "save_daily_energy", lambda: saved.append(True))

    assert tracker.capture_daily_solar_forecast(5.12345) is True
    assert controller._daily_solar_forecast_initial_kwh == pytest.approx(5.1235)
    assert controller._daily_solar_forecast_initial_date == date.today()

    # A later, live forecast must not replace the 00:05 reference.
    assert tracker.capture_daily_solar_forecast(9.0) is False
    assert controller._daily_solar_forecast_initial_kwh == pytest.approx(5.1235)
    assert saved == [True]

    for invalid in (None, -1.0, float("nan"), float("inf")):
        assert tracker.capture_daily_solar_forecast(invalid) is False


@pytest.mark.asyncio
async def test_daily_solar_forecast_is_restored_from_store():
    controller = _make_daily_energy_controller(
        _daily_solar_forecast_initial_kwh=6.75,
        _daily_solar_forecast_initial_date=date.today(),
    )
    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._controller = controller
    tracker._daily_energy_store = _FakeConsumptionStore(None)

    await tracker.async_save_daily_energy()
    saved = tracker._daily_energy_store._data
    assert saved["solar_forecast_initial_kwh"] == pytest.approx(6.75)
    assert saved["solar_forecast_initial_date"] == date.today().isoformat()

    restored_controller = _make_daily_energy_controller()
    restored = ConsumptionTracker.__new__(ConsumptionTracker)
    restored._controller = restored_controller
    restored._daily_energy_store = _FakeConsumptionStore(saved)

    await restored.load_daily_energy()

    assert restored_controller._daily_solar_forecast_initial_kwh == pytest.approx(6.75)
    assert restored_controller._daily_solar_forecast_initial_date == date.today()


@pytest.mark.asyncio
async def test_legacy_windowed_history_is_invalidated_for_recorder_rebuild():
    tracker = _make_history_tracker([(date(2026, 8, 15), 5.77)], _MON_FRI)
    tracker._controller._daily_grid_at_min_soc_kwh = 0.0
    tracker._consumption_store = _FakeConsumptionStore(
        {
            "history": [("2026-08-15", 5.77)],
            "grid_at_min_soc_kwh": 1.53,
        }
    )

    assert await tracker.load_consumption_history() is True
    assert tracker._controller._daily_consumption_history == []
    assert tracker._controller._daily_grid_at_min_soc_kwh == pytest.approx(1.53)


@pytest.mark.asyncio
async def test_full_day_history_is_restored_without_invalidation():
    tracker = _make_history_tracker([], _MON_FRI)
    tracker._controller._daily_grid_at_min_soc_kwh = 0.0
    tracker._consumption_store = _FakeConsumptionStore(
        {
            "consumption_scope": CONSUMPTION_HISTORY_SCOPE,
            "history": [("2026-08-15", 14.49)],
        }
    )

    assert await tracker.load_consumption_history() is True
    assert tracker._controller._daily_consumption_history == [
        (date(2026, 8, 15), 14.49)
    ]


@pytest.mark.asyncio
async def test_legacy_same_day_accumulator_rebuild_is_deferred_to_backfill():
    tracker = _make_history_tracker([], _MON_FRI)
    tracker._controller._household_energy_accumulator = 1.0
    tracker._controller._household_accumulator_date = None
    tracker._accumulator_store = _FakeConsumptionStore(
        {"date": date.today().isoformat(), "household_kwh": 1.0}
    )

    await tracker.load_accumulators()

    assert tracker._controller._household_energy_accumulator == pytest.approx(0.0)
    assert tracker._controller._household_accumulator_date == date.today()
    assert tracker._legacy_accumulator_rebuild_pending is True
    assert "consumption_scope" not in tracker._accumulator_store._data


# ----------------------------------------------------------------------
# Total solar power: external sensor + Venus DC-coupled PV (MPPT on vA/vD).
# Pins the #354 fix — daily solar must count the battery's own MPPT panels,
# not only the configured external sensor, and survive the external being gone.
# ----------------------------------------------------------------------

class _FakeStates:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, entity_id):
        return self._mapping.get(entity_id)


def _w(value):
    """A power state in watts."""
    return SimpleNamespace(state=str(value), attributes={"unit_of_measurement": "W"})


def _make_solar_tracker(states, solar_sensor, coordinators):
    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._hass = SimpleNamespace(states=_FakeStates(states))
    tracker._controller = SimpleNamespace(
        solar_production_sensor=solar_sensor,
        coordinators=coordinators,
    )
    return tracker


def _vunit(version, mppt_total, available=True):
    return FakeCoordinator(
        battery_version=version,
        data={"mppt1_power": mppt_total},
        is_available=available,
    )


def _aggregate_pv_unit(solar_power, available=True):
    return SimpleNamespace(
        capabilities=SimpleNamespace(
            has_mppt_pv=False,
            has_solar_telemetry=True,
        ),
        data={"solar_power": solar_power},
        is_available=available,
    )


def _max_ac_unit(solar_power=800, available=True):
    """Solarbank Max AC telemetry must not be treated as independent PV."""
    return SimpleNamespace(
        capabilities=SimpleNamespace(
            has_mppt_pv=False,
            has_solar_telemetry=False,
        ),
        data={"solar_power": solar_power},
        is_available=available,
    )


def test_total_solar_external_only():
    tracker = _make_solar_tracker({"sensor.aps": _w(1500)}, "sensor.aps", [])
    assert tracker._read_total_solar_power_kw() == pytest.approx(1.5)


def test_total_solar_ignores_circular_omnibattery_external_sensor():
    tracker = _make_solar_tracker(
        {"sensor.omnibattery_solar_power": _w(999999)},
        "sensor.omnibattery_solar_power",
        [_aggregate_pv_unit(1500)],
    )

    assert tracker._read_total_solar_power_kw() == pytest.approx(1.5)
    assert tracker._solar_self_reference_warned is True


def test_total_solar_mppt_only_no_external():
    # No external sensor configured, panels on the Venus MPPT inputs.
    tracker = _make_solar_tracker({}, None, [_vunit("vA", 800)])
    assert tracker._read_total_solar_power_kw() == pytest.approx(0.8)


def test_total_solar_aggregate_pv_only_no_external():
    # Anker Solarbank 4 reports aggregate PV rather than individual MPPT keys.
    tracker = _make_solar_tracker({}, None, [_aggregate_pv_unit(1500)])
    assert tracker._read_total_solar_power_kw() == pytest.approx(1.5)


def test_total_solar_external_plus_aggregate_pv():
    tracker = _make_solar_tracker(
        {"sensor.aps": _w(1500)}, "sensor.aps", [_aggregate_pv_unit(800)]
    )
    assert tracker._read_total_solar_power_kw() == pytest.approx(2.3)


def test_total_solar_max_ac_with_external_uses_only_external_sensor():
    tracker = _make_solar_tracker(
        {"sensor.aps": _w(1500)}, "sensor.aps", [_max_ac_unit(800)]
    )
    assert tracker._read_total_solar_power_kw() == pytest.approx(1.5)


def test_total_solar_external_plus_mppt():
    tracker = _make_solar_tracker(
        {"sensor.aps": _w(1500)}, "sensor.aps", [_vunit("vA", 800), _vunit("vD", 200)]
    )
    assert tracker._read_total_solar_power_kw() == pytest.approx(2.5)


def test_total_solar_ignores_non_pv_versions():
    # v2 has no MPPT registers; it must not contribute.
    tracker = _make_solar_tracker({}, None, [_vunit("v2", 999)])
    assert tracker._read_total_solar_power_kw() is None


def test_total_solar_none_when_no_source():
    tracker = _make_solar_tracker({}, None, [])
    assert tracker._read_total_solar_power_kw() is None


def test_total_solar_skips_disconnected_unit():
    # A disconnected unit keeps its last MPPT reading (coordinator.data is merged,
    # never expired). It must not be counted, or the daily solar total inflates.
    tracker = _make_solar_tracker(
        {}, None, [_vunit("vA", 800, available=False)]
    )
    assert tracker._read_total_solar_power_kw() is None


def test_total_solar_counts_only_connected_units():
    tracker = _make_solar_tracker(
        {}, None, [_vunit("vA", 800), _vunit("vD", 500, available=False)]
    )
    assert tracker._read_total_solar_power_kw() == pytest.approx(0.8)


# ----------------------------------------------------------------------
# Derived home power: home = grid + sum(ac_power) + external_solar.
# Pins the stale-battery fix — a unit that drops mid-discharge keeps a frozen
# ac_power in coordinator.data; counting it double-books the load the grid
# meter already shows, inflating home consumption and its daily integral.
# ----------------------------------------------------------------------

def _battunit(ac_w, available=True):
    return FakeCoordinator(data={"ac_power": ac_w}, is_available=available)


def _apply_meter_transform(meter_inverted, state):
    """Mirrors ChargeDischargeController._apply_meter_transform (__init__.py)."""
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    try:
        value = float(state.state)
    except (ValueError, TypeError):
        return None
    unit = state.attributes.get("unit_of_measurement", "W")
    if unit == "kW":
        value *= 1000.0
    if meter_inverted:
        value = -value
    return value


def _make_home_tracker(states, coordinators, grid_sensor="sensor.grid", solar_sensor=None, meter_inverted=False):
    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._hass = SimpleNamespace(states=_FakeStates(states))
    tracker._controller = SimpleNamespace(
        consumption_sensor=grid_sensor,
        solar_production_sensor=solar_sensor,
        coordinators=coordinators,
        meter_inverted=meter_inverted,
        _apply_meter_transform=lambda state: _apply_meter_transform(meter_inverted, state),
    )
    return tracker


def test_derive_home_counts_connected_discharge():
    # Grid imports 300 W, battery discharges 2500 W (positive ac_power): the
    # battery covers most of a 2.8 kW house load.
    tracker = _make_home_tracker(
        {"sensor.grid": _w(300)}, [_battunit(2500)]
    )
    assert tracker._derive_home_power_kw() == pytest.approx(2.8)


def test_derive_home_cancels_grid_energy_used_to_charge_battery():
    # Grid imports 2800 W while the battery charges at 2500 W (negative AC).
    # Only the remaining 300 W belongs to household consumption.
    tracker = _make_home_tracker(
        {"sensor.grid": _w(2800)}, [_battunit(-2500)]
    )
    assert tracker._derive_home_power_kw() == pytest.approx(0.3)


def test_adjusted_home_holds_last_valid_value_for_small_charge_balance():
    # A stale grid/battery pair can leave a small positive balance instead of
    # a negative one. It must not be accepted as real household consumption.
    tracker = _make_home_tracker(
        {"sensor.grid": _w(1000)}, [_battunit(-800)]
    )
    assert tracker.get_adjusted_home_power_kw() == pytest.approx(0.2)

    tracker._hass.states._mapping["sensor.grid"] = _w(801)
    assert tracker.get_adjusted_home_power_kw() == pytest.approx(0.2)


def test_validated_physical_home_is_independent_of_external_load_adjustment():
    tracker = _make_home_tracker(
        {"sensor.grid": _w(300)}, [_battunit(2500)]
    )
    tracker._controller._external_loads = SimpleNamespace(
        consumption_delta_kw=lambda: -1.0
    )

    assert tracker.get_validated_home_power_kw() == pytest.approx(2.8)
    assert tracker.get_adjusted_home_power_kw() == pytest.approx(1.8)


@pytest.mark.asyncio
async def test_daily_home_energy_integrates_physical_not_adjusted_power(monkeypatch):
    tracker = _make_home_tracker(
        {"sensor.grid": _w(300)}, [_battunit(2500)]
    )
    tracker._controller._external_loads = SimpleNamespace(
        consumption_delta_kw=lambda: -1.0
    )
    tracker._controller._daily_home_energy_kwh = 0.0
    tracker._daily_home_last_time = 100.0
    tracker._daily_home_last_power_kw = 2.8

    import custom_components.omnibattery.tracking.consumption_tracker as ct
    monkeypatch.setattr(ct, "monotonic", lambda: 3700.0)

    await tracker.accumulate_daily_home_energy()

    assert tracker._controller._daily_home_energy_kwh == pytest.approx(2.8)


def test_validated_physical_home_rejects_expired_negative_transition(monkeypatch):
    tracker = _make_home_tracker(
        {"sensor.grid": _w(2800)}, [_battunit(-2500)]
    )
    clock = {"now": 100.0}
    import custom_components.omnibattery.tracking.consumption_tracker as ct
    monkeypatch.setattr(ct, "monotonic", lambda: clock["now"])

    assert tracker.get_validated_home_power_kw() == pytest.approx(0.3)
    tracker._hass.states._mapping["sensor.grid"] = _w(1000)
    assert tracker.get_validated_home_power_kw() == pytest.approx(0.3)

    clock["now"] += ct.HOME_CONSUMPTION_HOLD_S + 1.0
    assert tracker.get_validated_home_power_kw() is None


def test_derive_home_skips_disconnected_stale_discharge():
    # Same battery dropped mid-discharge: ac_power frozen at 2500, but its load
    # has shifted onto the grid meter (now 2800 W). Counting the stale 2500 would
    # report 5.3 kW; skipping it gives the true 2.8 kW.
    tracker = _make_home_tracker(
        {"sensor.grid": _w(2800)}, [_battunit(2500, available=False)]
    )
    assert tracker._derive_home_power_kw() == pytest.approx(2.8)


def test_derive_home_applies_inverted_meter_during_export():
    # Inverted meter: raw +1000 W means 1 kW EXPORT (not import). No battery
    # activity, house load is 0.5 kW covered entirely by solar surplus. The raw
    # (uncorrected) reading would wrongly add the export as if it were import,
    # reporting 1.5 kW instead of the true 0.5 kW.
    tracker = _make_home_tracker(
        {"sensor.grid": _w(1000)}, [], solar_sensor=None, meter_inverted=True,
    )
    assert tracker._derive_home_power_kw() == pytest.approx(0.0)  # -1.0 kW, clamped

    tracker = _make_home_tracker(
        {"sensor.grid": _w(-500)}, [], meter_inverted=True,
    )
    assert tracker._derive_home_power_kw() == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_daily_capture_runs_while_predictive_charging_is_off():
    """Learning must not be gated on the feature that consumes it.

    The home-energy accumulator is integrated on every control cycle whatever
    the switch says, so refusing to snapshot it left the seven-day history full
    of DEFAULT_BASE_CONSUMPTION_KWH sentinels for anyone who enabled predictive
    charging later.
    """
    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._controller = SimpleNamespace(
        predictive_charging_enabled=False,
        _household_energy_accumulator=6.42,
        _daily_consumption_history=[],
    )
    tracker._period_intersects = lambda *_args: False
    saved = []
    async def _save():
        saved.append(True)
    tracker.save_consumption_history = _save

    await tracker.capture_daily_consumption()

    assert tracker._controller._daily_consumption_history == [(date.today(), 6.42)]
    assert saved
