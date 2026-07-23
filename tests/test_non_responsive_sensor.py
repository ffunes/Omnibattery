"""Tests for non-responsive diagnostic sensor consistency."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from custom_components.omnibattery.sensor import NonResponsiveBatteriesSensor
from custom_components.omnibattery.tracking.non_responsive_tracker import (
    NonResponsiveTracker,
)
from tests.conftest import FakeCoordinator


def test_expired_cooldown_is_not_reported_as_excluded():
    coordinator = FakeCoordinator(
        name="BAT1",
        is_available=True,
    )
    coordinator._is_shutting_down = False
    coordinator._consecutive_failures = 0
    tracker = NonResponsiveTracker(fail_threshold=3, cooldown_min=5)
    tracker.batteries[coordinator] = {
        "fail_count": 3,
        "excluded_at": dt_util.utcnow() - timedelta(minutes=6),
        "wake_used": True,
        "reason": "standby_no_delivery",
        "retry_attempted": False,
        "wake_attempted": True,
    }
    controller = SimpleNamespace(
        _non_responsive=tracker,
        _non_responsive_batteries=tracker.batteries,
        non_responsive_battery_names=[],
    )
    sensor = NonResponsiveBatteriesSensor(
        hass=None,
        entry=None,
        controller=controller,
        coordinators=[coordinator],
    )

    attributes = sensor.extra_state_attributes["BAT1"]

    assert attributes["excluded"] is False
    assert attributes["fail_count"] == 0
    assert "remaining_minutes" not in attributes
