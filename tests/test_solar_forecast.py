"""Regression coverage for the dual solar-forecast migration."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.const import CONF_SOLAR_FORECAST_REMAINING_SENSOR
from custom_components.omnibattery.solar_forecast import (
    SolarForecastInput,
    get_configured_solar_forecast_sensor,
    normalize_solar_forecast_config,
    read_remaining_solar_kwh,
    read_solar_forecast_kwh,
)


MADRID = ZoneInfo("Europe/Madrid")


def _state(value, unit="kWh", **attributes):
    return SimpleNamespace(
        state=str(value),
        attributes={"unit_of_measurement": unit, **attributes},
    )


def test_remaining_forecast_wins_and_is_normalized_from_wh():
    states = {
        "sensor.today": _state(20.52),
        "sensor.remaining": _state(1810, "Wh"),
    }
    controller = SimpleNamespace(
        solar_forecast_sensor="sensor.today",
        solar_forecast_remaining_sensor="sensor.remaining",
    )
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))

    forecast = read_solar_forecast_kwh(hass, controller)

    assert forecast is not None
    assert forecast.kwh == pytest.approx(1.81)
    assert forecast.source == "remaining"
    assert controller.solar_forecast_source == "remaining"


def test_remaining_forecast_is_not_reduced_by_production():
    """20.52 today - 12.34 produced is legacy-only; remaining stays 1.81."""
    states = {
        "sensor.today": _state(20.52),
        "sensor.remaining": _state(1.81),
    }
    controller = SimpleNamespace(
        solar_forecast_sensor="sensor.today",
        solar_forecast_remaining_sensor="sensor.remaining",
        _daily_solar_energy_kwh=12.34,
        _solar_t_start=8.0,
        _consumption_tracker=SimpleNamespace(),
    )
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))

    assert PricingManager(hass, controller)._remaining_solar_today_kwh(14.0) == pytest.approx(1.81)


def test_legacy_today_forecast_uses_dated_future_periods_not_actual_production():
    """A missed morning must not be moved into the final solar intervals."""
    now = datetime(2026, 8, 25, 18, 0, tzinfo=MADRID)
    periods = [
        {
            "start": now.replace(hour=8),
            "end": now,
            "energy_kwh": 18.71,
        },
        {
            "start": now,
            "end": now.replace(hour=20),
            "energy_kwh": 1.81,
        },
    ]
    state = _state(20.52, solar_forecast_periods=periods)
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Madrid"),
        states=SimpleNamespace(get=lambda _entity_id: state),
    )
    controller = SimpleNamespace(
        solar_forecast_sensor="sensor.today",
        solar_forecast_remaining_sensor=None,
        _daily_solar_energy_kwh=12.34,
    )

    result = read_remaining_solar_kwh(hass, controller, now=now)

    assert result.remaining_kwh == pytest.approx(1.81)
    assert result.conversion == "dated_periods"


def test_legacy_today_forecast_uses_remaining_curve_not_actual_production():
    """A scalar daily forecast must not turn an optimistic past into sunset PV."""
    now = datetime(2026, 8, 25, 18, 0, tzinfo=MADRID)
    tracker = SimpleNamespace(
        estimate_t_end=lambda: 20.0,
        get_solar_fraction_done=lambda *_args: 0.9,
    )
    controller = SimpleNamespace(
        solar_forecast_sensor="sensor.today",
        solar_forecast_remaining_sensor=None,
        _daily_solar_energy_kwh=12.34,
        _solar_t_start=8.0,
        _consumption_tracker=tracker,
    )
    hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda _entity_id: _state(20.52))
    )

    result = read_remaining_solar_kwh(hass, controller, now=now)

    assert result.remaining_kwh == pytest.approx(2.052)
    assert result.conversion == "temporal_fraction"


def test_read_only_remaining_forecast_does_not_change_controller_diagnostics():
    now = datetime(2026, 8, 25, 18, 0, tzinfo=MADRID)
    controller = SimpleNamespace(
        solar_forecast_sensor=None,
        solar_forecast_remaining_sensor="sensor.remaining",
        solar_forecast_source="previous",
        solar_forecast_diagnostic_source="previous_source",
        solar_forecast_periods=("previous_periods",),
        solar_forecast_conversion="previous_conversion",
    )
    before = dict(vars(controller))
    hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda _entity_id: _state(1.81))
    )

    result = read_remaining_solar_kwh(
        hass,
        controller,
        now=now,
        update_controller=False,
    )

    assert result.remaining_kwh == pytest.approx(1.81)
    assert vars(controller) == before


def test_midnight_zero_scalar_uses_positive_dated_periods_for_new_day():
    now = datetime(2026, 8, 25, 0, 0, tzinfo=MADRID)
    periods = [
        {
            "start": (now + timedelta(hours=8)).isoformat(),
            "end": (now + timedelta(hours=20)).isoformat(),
            "energy_kwh": 12.0,
        }
    ]
    state = _state(0.0, solar_forecast_periods=periods)
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Madrid"),
        states=SimpleNamespace(get=lambda _entity_id: state),
    )
    controller = SimpleNamespace(
        solar_forecast_remaining_sensor="sensor.remaining",
        solar_forecast_sensor=None,
    )

    result = read_remaining_solar_kwh(hass, controller, now=now)

    assert result.remaining_kwh == pytest.approx(12.0)
    assert result.conversion == "dated_periods_zero_scalar"
    assert sum(period.energy_kwh for period in result.periods or ()) == pytest.approx(12.0)


def test_late_night_periods_for_tomorrow_do_not_leak_into_today_control():
    now = datetime(2026, 8, 24, 23, 45, tzinfo=MADRID)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )
    periods = [
        {
            "start": (tomorrow + timedelta(hours=8)).isoformat(),
            "end": (tomorrow + timedelta(hours=20)).isoformat(),
            "energy_kwh": 12.0,
        }
    ]
    state = _state(0.0, solar_forecast_periods=periods)
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Madrid"),
        states=SimpleNamespace(get=lambda _entity_id: state),
    )
    controller = SimpleNamespace(
        solar_forecast_remaining_sensor="sensor.remaining",
        solar_forecast_sensor=None,
    )

    result = read_remaining_solar_kwh(hass, controller, now=now)

    assert result.remaining_kwh == 0.0
    assert result.conversion == "none"


def test_forecast_reader_uses_persisted_remaining_sensor_when_runtime_cache_is_empty():
    controller = SimpleNamespace(
        config_entry=SimpleNamespace(
            data={CONF_SOLAR_FORECAST_REMAINING_SENSOR: "sensor.remaining"},
            options={},
        ),
        solar_forecast_remaining_sensor=None,
        solar_forecast_sensor=None,
    )
    hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda entity_id: _state(41.94) if entity_id == "sensor.remaining" else None
        )
    )

    assert get_configured_solar_forecast_sensor(controller, "remaining") == "sensor.remaining"
    forecast = read_solar_forecast_kwh(hass, controller)

    assert forecast is not None
    assert forecast.source == "remaining"
    assert forecast.kwh == pytest.approx(41.94)


def test_invalid_remaining_sensor_falls_back_to_legacy_today():
    states = {
        "sensor.today": _state(20520, "Wh"),
        "sensor.remaining": _state("nan"),
    }
    controller = SimpleNamespace(
        solar_forecast_sensor="sensor.today",
        solar_forecast_remaining_sensor="sensor.remaining",
    )
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))

    forecast = read_solar_forecast_kwh(hass, controller)

    assert forecast is not None
    assert forecast.kwh == pytest.approx(20.52)
    assert forecast.source == "today"


def test_config_normalization_keeps_remaining_and_preserves_legacy_only_entries():
    assert normalize_solar_forecast_config(
        {
            "solar_forecast_sensor": "sensor.today",
            "solar_forecast_remaining_sensor": "sensor.remaining",
        }
    ) == {"solar_forecast_remaining_sensor": "sensor.remaining"}
    assert normalize_solar_forecast_config(
        {"solar_forecast_sensor": "sensor.today"}
    ) == {"solar_forecast_sensor": "sensor.today"}


def test_remaining_solar_adapter_normalizes_temporal_shape_to_remaining_energy():
    normalized = SolarForecastInput(
        remaining_kwh=4.0,
        source="remaining_sensor",
        temporal_shape=(1.0, 2.0, 1.0),
    ).normalized_shape()

    assert normalized == pytest.approx([1.0, 2.0, 1.0])
    assert sum(normalized) == pytest.approx(4.0)


def test_remaining_solar_adapter_exposes_safe_fallback():
    controller = SimpleNamespace(
        solar_forecast_sensor=None,
        solar_forecast_remaining_sensor=None,
    )
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: None))

    result = read_remaining_solar_kwh(hass, controller)

    assert result.remaining_kwh == 0.0
    assert result.source == "fallback"
    assert controller.solar_forecast_source == "fallback"
