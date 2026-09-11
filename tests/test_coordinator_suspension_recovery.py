"""Regression tests for the failure counter across a suspension window.

A battery that stops answering fails five polls, gets suspended for two
minutes, and is then given one fresh reconnection attempt. When that attempt
also fails the battery must stay *visibly* unreachable.

``_consecutive_failures`` is the flag every consumer gates on
(``ChargeDischargeController.non_responsive_battery_names``, the
``non_responsive_batteries`` diagnostic sensor, and the diagnostics
``health`` block). Its value is the only thing that separates "unreachable"
from "idle": the entities of an unreachable battery keep their last read
value instead of going ``unavailable``, so a dead battery and a healthy one
sitting in standby look identical without it.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
)
from custom_components.omnibattery.tracking.non_responsive_tracker import (
    NonResponsiveTracker,
)
from tests.conftest import FakeCoordinator


def _expired_suspension(coordinator):
    """Put the coordinator back at the moment its suspension window elapses."""
    coordinator._suspension_reset_time = dt_util.utcnow() - timedelta(seconds=1)
    return coordinator


def _suspended_coordinator(*, reconnects: bool, consecutive_failures: int = 5):
    """A coordinator suspended after ``_max_failures_before_suspend`` failures.

    Only the attributes the suspension branch touches are needed: that branch
    returns before any telemetry read.
    """
    return _expired_suspension(
        SimpleNamespace(
            name="Battery",
            data={"battery_soc": 25},
            _is_shutting_down=False,
            _is_connected=False,
            _consecutive_failures=consecutive_failures,
            async_reconnect_fresh=AsyncMock(return_value=reconnects),
        )
    )


async def test_failed_reconnect_after_suspension_counts_as_a_failure():
    coordinator = _suspended_coordinator(reconnects=False)

    await MarstekVenusDataUpdateCoordinator._async_update_data(coordinator)

    coordinator.async_reconnect_fresh.assert_awaited_once()
    assert coordinator._consecutive_failures == 6
    assert coordinator._suspension_reset_time is not None


async def test_battery_stuck_in_suspension_never_looks_healthy():
    """Ten suspension cycles must not walk the counter back down to zero."""
    coordinator = _suspended_coordinator(reconnects=False)

    for _ in range(10):
        _expired_suspension(coordinator)
        await MarstekVenusDataUpdateCoordinator._async_update_data(coordinator)

    assert coordinator._consecutive_failures == 15


async def test_battery_stuck_in_suspension_is_reported_non_responsive():
    """The end-to-end contract: the sensor keeps naming an unreachable battery."""
    coordinator = FakeCoordinator(name="Battery", is_available=False)
    coordinator._is_shutting_down = False
    coordinator._consecutive_failures = 5
    coordinator.async_reconnect_fresh = AsyncMock(return_value=False)

    for _ in range(10):
        _expired_suspension(coordinator)
        await MarstekVenusDataUpdateCoordinator._async_update_data(coordinator)

    controller = SimpleNamespace(
        _non_responsive=NonResponsiveTracker(),
        coordinators=[coordinator],
    )

    names = ChargeDischargeController.non_responsive_battery_names.fget(controller)

    assert names == ["Battery"]


async def test_successful_fresh_reconnect_clears_the_counter():
    """Resetting the counter is ``async_reconnect_fresh``'s job, on success only."""
    coordinator = SimpleNamespace(
        name="Battery",
        host="192.0.2.10",
        port=502,
        lock=asyncio.Lock(),
        driver=SimpleNamespace(
            connect=AsyncMock(return_value=True),
            read_telemetry=AsyncMock(return_value={"battery_soc": 50}),
        ),
        capabilities=SimpleNamespace(has_rs485_control=False, push_telemetry=False),
        rs485_user_disabled=False,
        _consecutive_failures=7,
        _is_connected=False,
        _suspension_reset_time=dt_util.utcnow(),
        _last_rs485_reenable_success=None,
        _last_update_times={"battery_soc": 1.0},
        _critical_group_failures={("battery_soc",): 2},
    )

    assert await MarstekVenusDataUpdateCoordinator.async_reconnect_fresh(coordinator)

    assert coordinator._consecutive_failures == 0
    assert coordinator._is_connected is True
    assert coordinator._suspension_reset_time is None


async def test_reconnect_that_answers_nothing_is_not_a_success():
    """#445: V150 accepts the socket and then ignores every Modbus frame.

    Counting that as a recovery cleared the failure counter on every attempt, so
    the suspend threshold was never reached and the coordinator re-opened the
    socket every few polls forever instead of backing off for two minutes.
    """
    coordinator = SimpleNamespace(
        name="Battery",
        host="192.0.2.10",
        port=502,
        lock=asyncio.Lock(),
        driver=SimpleNamespace(
            connect=AsyncMock(return_value=True),
            read_telemetry=AsyncMock(return_value={}),
        ),
        capabilities=SimpleNamespace(has_rs485_control=True, push_telemetry=False),
        rs485_user_disabled=False,
        _consecutive_failures=7,
        _is_connected=False,
        _suspension_reset_time=None,
        _last_rs485_reenable_success=None,
        _last_update_times={"battery_soc": 1.0},
        _critical_group_failures={("battery_soc",): 2},
    )

    assert not await MarstekVenusDataUpdateCoordinator.async_reconnect_fresh(coordinator)

    assert coordinator._consecutive_failures == 7
    assert coordinator._is_connected is False


async def test_push_driver_reconnect_skips_the_probe():
    """A cached read proves nothing, so push drivers keep the old contract."""
    driver = SimpleNamespace(
        connect=AsyncMock(return_value=True),
        read_telemetry=AsyncMock(return_value={}),
    )
    coordinator = SimpleNamespace(
        name="Zendure",
        host="192.0.2.11",
        port=80,
        lock=asyncio.Lock(),
        driver=driver,
        capabilities=SimpleNamespace(
            has_rs485_control=False,
            push_telemetry=True,
            telemetry_liveness_checked=False,
        ),
        rs485_user_disabled=False,
        _consecutive_failures=7,
        _is_connected=False,
        _suspension_reset_time=None,
        _last_rs485_reenable_success=None,
        _last_update_times={},
        _critical_group_failures={},
    )

    assert await MarstekVenusDataUpdateCoordinator.async_reconnect_fresh(coordinator)

    assert coordinator._consecutive_failures == 0
    driver.read_telemetry.assert_not_awaited()


async def test_liveness_checked_push_driver_still_probes():
    """A push driver that dates its cache must not skip the probe (#452).

    The ESPHome bridge's connect() only re-resolves registry entries, so it
    succeeds against a wedged Modbus bus. Taking that at its word cleared the
    failure counter every third poll, so the suspend back-off never engaged and
    each reconnect re-asserted RS485 — adding writes to the jammed bus it was
    supposed to be recovering.
    """
    driver = SimpleNamespace(
        connect=AsyncMock(return_value=True),
        read_telemetry=AsyncMock(return_value={}),
    )
    coordinator = SimpleNamespace(
        name="Marstek Venus 3",
        host="devid123",
        port=0,
        lock=asyncio.Lock(),
        driver=driver,
        capabilities=SimpleNamespace(
            has_rs485_control=True,
            push_telemetry=True,
            telemetry_liveness_checked=True,
        ),
        rs485_user_disabled=False,
        _consecutive_failures=7,
        _is_connected=False,
        _suspension_reset_time=None,
        _last_rs485_reenable_success=None,
        _last_update_times={},
        _critical_group_failures={},
    )

    assert not await MarstekVenusDataUpdateCoordinator.async_reconnect_fresh(coordinator)

    driver.read_telemetry.assert_awaited()
    assert coordinator._consecutive_failures == 7
    assert coordinator._is_connected is False
