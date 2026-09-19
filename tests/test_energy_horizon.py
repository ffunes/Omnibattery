"""Tests for the predictive-charging energy horizon."""
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.tracking.consumption_tracker import ConsumptionTracker


MADRID = ZoneInfo("Europe/Madrid")


def _tracker(latitude=40.4168, longitude=-3.7038):
    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._hass = SimpleNamespace(
        config=SimpleNamespace(
            latitude=latitude,
            longitude=longitude,
            time_zone="Europe/Madrid",
        )
    )
    tracker._solar_noon_cache = {}
    return tracker


def _manager(tracker):
    hass = SimpleNamespace(config=tracker._hass.config)
    controller = SimpleNamespace(_consumption_tracker=tracker)
    return PricingManager(hass, controller)


@pytest.mark.parametrize("hour, minute", [(0, 5), (3, 0), (23, 30)])
def test_energy_horizon_always_uses_tomorrows_sunrise(hour, minute):
    tracker = _tracker()
    manager = _manager(tracker)
    now = datetime(2026, 4, 10, hour, minute, tzinfo=MADRID)
    horizon_date = date(2026, 4, 11)
    midnight = datetime.combine(horizon_date, time.min, tzinfo=MADRID)

    result = manager.energy_horizon_end(now)

    assert result == midnight + timedelta(
        hours=tracker.calculate_sunrise(horizon_date)
    )
    assert result.date() == horizon_date


@pytest.mark.parametrize(
    "now, expected_offset",
    [
        (datetime(2026, 3, 28, 23, 30, tzinfo=MADRID), timedelta(hours=2)),
        (datetime(2026, 10, 24, 23, 30, tzinfo=MADRID), timedelta(hours=1)),
    ],
)
def test_energy_horizon_uses_target_dates_dst_offset(now, expected_offset):
    tracker = _tracker()

    result = _manager(tracker).energy_horizon_end(now)

    assert result.utcoffset() == expected_offset
    assert result.date() == now.date() + timedelta(days=1)
    assert now.date() + timedelta(days=1) in tracker._solar_noon_cache


def test_solar_noon_cache_is_keyed_by_date_across_dst_change():
    tracker = _tracker()

    before = tracker.calculate_solar_noon(date(2026, 3, 28))
    after = tracker.calculate_solar_noon(date(2026, 3, 29))

    assert after - before == pytest.approx(1.0)
    assert set(tracker._solar_noon_cache) == {
        date(2026, 3, 28),
        date(2026, 3, 29),
    }


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 6, 20, 12, 0, tzinfo=MADRID),
        datetime(2026, 12, 20, 12, 0, tzinfo=MADRID),
    ],
)
def test_energy_horizon_falls_back_at_polar_latitude(now):
    result = _manager(_tracker(latitude=78.0)).energy_horizon_end(now)
    expected = datetime.combine(
        now.date() + timedelta(days=1), time.min, tzinfo=MADRID
    )

    assert result == expected


def test_energy_horizon_falls_back_without_location():
    now = datetime(2026, 4, 10, 12, 0, tzinfo=MADRID)

    result = _manager(_tracker(latitude=None)).energy_horizon_end(now)

    assert result == datetime(2026, 4, 11, tzinfo=MADRID)


def test_energy_horizon_is_aware_in_ha_timezone():
    result = _manager(_tracker()).energy_horizon_end(
        datetime(2026, 4, 10, 12, 0, tzinfo=ZoneInfo("UTC"))
    )

    assert result.tzinfo is MADRID
    assert result.utcoffset() == timedelta(hours=2)


def test_energy_horizon_stays_naive_for_a_naive_caller():
    tracker = _tracker()

    result = _manager(tracker).energy_horizon_end(datetime(2026, 4, 10, 12, 0))

    assert result.tzinfo is None
    assert result == datetime(2026, 4, 11) + timedelta(
        hours=tracker.calculate_sunrise(date(2026, 4, 11))
    )


@pytest.mark.parametrize("sunrise, expected_hour", [(-1.0, 0), (15.0, 12)])
def test_energy_horizon_is_clamped_to_first_twelve_hours(sunrise, expected_hour):
    tracker = SimpleNamespace(calculate_sunrise=lambda for_date: sunrise)
    manager = PricingManager(
        SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Madrid")),
        SimpleNamespace(_consumption_tracker=tracker),
    )
    now = datetime(2026, 4, 10, 12, 0, tzinfo=MADRID)

    result = manager.energy_horizon_end(now)

    assert result == datetime(2026, 4, 11, expected_hour, tzinfo=MADRID)
