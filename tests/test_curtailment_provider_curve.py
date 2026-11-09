"""The curtailment model follows the provider's solar curve, like the planner.

``_curtailment_forecast_model`` feeds the curtailment plan, the surplus price
hold, High-Price Sale and the discharge reserve. Its sine from sunrise to
sunset assumes a flat horizon, so on a morning where a hill or a building
keeps the panels dark until 09:00 it expected the sun hours early, while the
charge planner (which follows the provider's periods) did not.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.solar_forecast import SolarForecastPeriod

from tests.test_pricing_engine import _controller

BERLIN = ZoneInfo("Europe/Berlin")
DAY = datetime(2026, 3, 20, tzinfo=BERLIN)
NOW = DAY + timedelta(hours=6, minutes=30)

# The panels see nothing until 09:00, then 1, 2, 3, 2 and 1 kWh an hour.
MOUNTAIN = {9: 1.0, 10: 2.0, 11: 3.0, 12: 2.0, 13: 1.0}


def _sine(hour, start, end):
    """Stand-in for the tracker's sine: a straight ramp from sunrise to sunset."""
    return min(1.0, max(0.0, (hour - start) / (end - start)))


def _manager(periods):
    tracker = SimpleNamespace(
        calculate_sunrise=lambda for_date=None: 7.0,
        calculate_solar_noon=lambda: 12.5,
        get_solar_fraction_done=_sine,
        get_avg_daily_consumption=lambda: 12.0,
    )
    ctrl = _controller(
        solar_forecast_sensor="sensor.solar",
        _solar_t_start=None,
        _consumption_tracker=tracker,
    )
    # The forecast read refreshes the controller's periods from the sensor.
    attributes = {
        "periods": [
            {
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
                "energy_kwh": period.energy_kwh,
            }
            for period in periods or ()
        ]
    }
    state = SimpleNamespace(state="9.0", attributes=attributes)
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Berlin"),
        states=SimpleNamespace(get=lambda _entity_id: state),
    )
    return PricingManager(hass, ctrl)


def _periods(energy_by_hour, day=DAY):
    return tuple(
        SolarForecastPeriod(
            day + timedelta(hours=hour), day + timedelta(hours=hour + 1), energy
        )
        for hour in range(7, 18)
        for energy in [energy_by_hour.get(hour, 0.0)]
    )


def test_the_model_puts_the_sun_where_the_provider_expects_it():
    _forecast, fraction, _consumption = _manager(
        _periods(MOUNTAIN)
    )._curtailment_forecast_model(NOW)

    assert fraction(8.5) == pytest.approx(0.0)
    assert fraction(10.0) == pytest.approx(1.0 / 9.0)
    assert fraction(11.5) == pytest.approx(4.5 / 9.0)
    assert fraction(14.0) == pytest.approx(1.0)
    # The flat-horizon sine had a third of the day done by 09:00.
    assert _sine(9.0, 7.0, 18.0) > 0.18


def test_without_provider_periods_the_sine_stays():
    _forecast, fraction, _consumption = _manager(None)._curtailment_forecast_model(NOW)

    assert fraction(9.0) == pytest.approx(_sine(9.0, 7.0, 18.0))


def test_periods_that_leave_the_daylight_uncovered_keep_the_sine():
    # Periods only for the afternoon: a gap of hours inside the daylight.
    afternoon = tuple(
        SolarForecastPeriod(DAY + timedelta(hours=h), DAY + timedelta(hours=h + 1), 1.0)
        for h in range(13, 18)
    )
    _forecast, fraction, _consumption = _manager(afternoon)._curtailment_forecast_model(
        NOW
    )

    assert fraction(9.0) == pytest.approx(_sine(9.0, 7.0, 18.0))


def test_yesterday_s_periods_do_not_shape_today():
    _forecast, fraction, _consumption = _manager(
        _periods(MOUNTAIN, day=DAY - timedelta(days=1))
    )._curtailment_forecast_model(NOW)

    assert fraction(9.0) == pytest.approx(_sine(9.0, 7.0, 18.0))


def test_a_naive_clock_reads_the_periods_in_local_time():
    _forecast, fraction, _consumption = _manager(
        _periods(MOUNTAIN)
    )._curtailment_forecast_model(NOW.replace(tzinfo=None))

    assert fraction(10.0) == pytest.approx(1.0 / 9.0)
