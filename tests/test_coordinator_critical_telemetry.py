"""Regression tests for partial critical-telemetry failures (issue #26)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.omnibattery.drivers.base import ReadGroup
from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
)


def _coordinator(
    *,
    critical_succeeds: bool,
    other_succeeds: bool = True,
    reconnect_threshold: int = 99,
):
    critical_group = ReadGroup("high", ("battery_soc",))
    other_group = ReadGroup("high", ("temperature",))

    async def read_telemetry(keys):
        if keys == ["battery_soc"]:
            return {"battery_soc": 80} if critical_succeeds else {}
        return {"temperature": 25} if other_succeeds else {}

    driver = SimpleNamespace(
        read_groups=[critical_group, other_group],
        read_telemetry=read_telemetry,
        control_dependency_keys=set(),
    )
    registry = SimpleNamespace(
        async_get_entity_id=lambda *args: None,
        entities={},
    )
    coordinator = SimpleNamespace(
        name="Battery",
        host="192.0.2.10",
        device_key="192.0.2.10_1",
        driver=driver,
        _def_by_key={
            "battery_soc": {"key": "battery_soc"},
            "temperature": {"key": "temperature"},
        },
        _get_entity_type=lambda definition, fallback_key=None: "sensor",
        _entity_registry=registry,
        _is_shutting_down=False,
        _suspension_reset_time=None,
        _last_update_times={},
        _critical_group_failures={},
        boost_fast_poll_until=0.0,
        lock=asyncio.Lock(),
        _consecutive_failures=0,
        _max_failures_before_reconnect=reconnect_threshold,
        _max_failures_before_suspend=100,
        _is_connected=True,
        data={},
        async_reconnect_fresh=AsyncMock(return_value=True),
        capabilities=SimpleNamespace(has_energy_counters=True),
        battery_capacity_kwh=0,
        _alarm_notifier=SimpleNamespace(check=AsyncMock()),
    )
    return coordinator, critical_group


async def test_successful_bms_group_does_not_hide_failed_critical_group():
    coordinator, _ = _coordinator(critical_succeeds=False)

    for _ in range(3):
        coordinator._last_update_times.clear()
        await MarstekVenusDataUpdateCoordinator._async_update_data(coordinator)

    assert coordinator._consecutive_failures == 0
    coordinator.async_reconnect_fresh.assert_awaited_once()


async def test_critical_group_success_clears_its_failure_streak():
    coordinator, critical_group = _coordinator(critical_succeeds=True)
    coordinator._critical_group_failures[critical_group.keys] = 2

    await MarstekVenusDataUpdateCoordinator._async_update_data(coordinator)

    assert critical_group.keys not in coordinator._critical_group_failures
    coordinator.async_reconnect_fresh.assert_not_awaited()


async def test_aggregate_and_critical_failures_trigger_only_one_reconnect():
    coordinator, _ = _coordinator(
        critical_succeeds=False,
        other_succeeds=False,
        reconnect_threshold=3,
    )

    for _ in range(3):
        await MarstekVenusDataUpdateCoordinator._async_update_data(coordinator)

    coordinator.async_reconnect_fresh.assert_awaited_once()
