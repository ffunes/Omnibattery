"""A short write glitch must not raise "battery not delivering" (#945).

A ``modbus_write_failed`` exclusion opens the non-delivery spell the Repair is
timed from. Only a real delivery used to end it, so a battery that was idle or
waiting its turn in the rotation kept the spell open while every later write
went through, and the Repair fired 30 minutes later on a healthy battery.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.drivers import SetpointResult
from custom_components.omnibattery.tracking.non_responsive_tracker import (
    NON_DELIVERY_REPAIR_AFTER_S,
    NonResponsiveTracker,
)
from tests.test_set_battery_power_skip import _Coord, _controller, _ok

IDLE = {"force_mode": 0, "set_charge_power": 0, "set_discharge_power": 0}


def _comm_excluded(coord):
    tracker = NonResponsiveTracker(fail_threshold=3)
    for _ in range(3):
        tracker.record_comm_failure(coord, "modbus_write_failed")
    assert tracker.is_excluded(coord) is True
    info = tracker.batteries[coord]
    info["degraded_since"] = dt_util.utcnow() - timedelta(
        seconds=NON_DELIVERY_REPAIR_AFTER_S + 60
    )
    return tracker


def _controller_with(tracker):
    ctrl = _controller()
    ctrl._non_responsive = tracker
    return ctrl


async def test_a_successful_idle_write_ends_a_comm_glitch_spell():
    coord = _Coord({"force_mode": 2, "set_charge_power": 0, "set_discharge_power": 800})
    coord.apply_power = AsyncMock(return_value=_ok(0))
    tracker = _comm_excluded(coord)
    ctrl = _controller_with(tracker)

    assert await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 0)

    coord.apply_power.assert_called()
    assert tracker.batteries[coord]["degraded_since"] is None


async def test_an_idle_battery_already_at_its_set_points_ends_the_spell():
    coord = _Coord(dict(IDLE))
    tracker = _comm_excluded(coord)
    ctrl = _controller_with(tracker)

    assert await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 0)

    coord.apply_power.assert_not_called()
    assert tracker.batteries[coord]["degraded_since"] is None


async def test_a_failing_write_keeps_the_spell():
    coord = _Coord({"force_mode": 2, "set_charge_power": 0, "set_discharge_power": 800})
    coord.apply_power = AsyncMock(
        return_value=SetpointResult(
            ok=False, net_power_w=0, confirmed=False,
            failure_reason="modbus_write_failed",
        )
    )
    coord._is_shutting_down = False
    tracker = _comm_excluded(coord)
    ctrl = _controller_with(tracker)

    assert not await ChargeDischargeController._set_battery_power(ctrl, coord, 0, 0)

    assert tracker.batteries[coord]["degraded_since"] is not None


def test_a_working_link_does_not_end_a_non_delivery_spell():
    """A write that goes through says nothing about power flowing (#452)."""
    coord = _Coord(dict(IDLE))
    tracker = NonResponsiveTracker(fail_threshold=3)
    for _ in range(6):
        tracker.record_non_delivery(coord, 1625, 5)
    assert tracker.is_excluded(coord) is True

    tracker.record_write_ok(coord)

    assert tracker.batteries[coord]["degraded_since"] is not None


def test_an_isolated_earlier_non_delivery_does_not_pin_a_comm_spell():
    coord = _Coord(dict(IDLE))
    tracker = NonResponsiveTracker(fail_threshold=3)
    tracker.record_non_delivery(coord, 1625, 5)
    for _ in range(2):
        tracker.record_comm_failure(coord, "modbus_write_failed")
    assert tracker.is_excluded(coord) is True

    tracker.record_write_ok(coord)

    assert tracker.batteries[coord]["degraded_since"] is None


def test_a_comm_failure_inside_a_non_delivery_spell_keeps_it():
    coord = _Coord(dict(IDLE))
    tracker = NonResponsiveTracker(fail_threshold=3)
    for _ in range(6):
        tracker.record_non_delivery(coord, 1625, 5)
    tracker.record_comm_failure(coord, "modbus_write_failed")

    tracker.record_write_ok(coord)

    assert tracker.batteries[coord]["degraded_since"] is not None
