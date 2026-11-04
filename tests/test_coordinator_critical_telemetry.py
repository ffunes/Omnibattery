"""Regression tests for partial critical-telemetry failures (issue #26)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
        _key_update_times={},
        _critical_group_failures={},
        boost_fast_poll_until=0.0,
        lock=asyncio.Lock(),
        _consecutive_failures=0,
        _max_failures_before_reconnect=reconnect_threshold,
        _max_failures_before_suspend=100,
        _is_connected=True,
        data={},
        async_reconnect_fresh=AsyncMock(return_value=True),
        capabilities=SimpleNamespace(
            has_energy_counters=True, has_daily_energy_counters=True
        ),
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


async def test_configured_capacity_is_injected_for_drivers_without_nominal_capacity():
    coordinator, _ = _coordinator(critical_succeeds=True)
    coordinator.capabilities = SimpleNamespace(
        has_energy_counters=False,
        has_nominal_capacity=False,
        has_daily_energy_counters=False,
    )
    coordinator.battery_capacity_kwh = 5.28

    await MarstekVenusDataUpdateCoordinator._async_update_data(coordinator)

    assert coordinator.data["battery_total_energy"] == 5.28


@pytest.mark.parametrize("configured,expected", [(0, 1.92), (5.28, 5.28)])
async def test_detected_capacity_and_manual_override(configured, expected):
    coordinator, _ = _coordinator(critical_succeeds=True)
    coordinator.capabilities.has_nominal_capacity = False
    coordinator.battery_capacity_kwh = configured
    coordinator.driver.read_telemetry = AsyncMock(return_value={
        "battery_soc": 50, "battery_total_energy": 1.92,
    })
    await MarstekVenusDataUpdateCoordinator._async_update_data(coordinator)
    assert coordinator.data["battery_total_energy"] == expected


async def test_unknown_detected_capacity_clears_previous_total():
    coordinator, _ = _coordinator(critical_succeeds=True)
    coordinator.capabilities.has_nominal_capacity = False
    coordinator.data["battery_total_energy"] = 1.92
    coordinator.driver.read_telemetry = AsyncMock(return_value={
        "battery_soc": 50, "battery_total_energy": None,
    })
    await MarstekVenusDataUpdateCoordinator._async_update_data(coordinator)
    assert coordinator.data["battery_total_energy"] is None


def _power_limit_coordinator(
    *, version: str, device_charge: int, device_discharge: int,
    user_charge: int, user_discharge: int,
):
    group = ReadGroup("high", ("max_charge_power", "max_discharge_power"))

    async def read_telemetry(keys):
        return {
            "max_charge_power": device_charge,
            "max_discharge_power": device_discharge,
        }

    driver = SimpleNamespace(
        read_groups=[group],
        read_telemetry=read_telemetry,
        control_dependency_keys=set(),
    )
    registry = SimpleNamespace(
        async_get_entity_id=lambda *args: None,
        entities={},
    )
    return SimpleNamespace(
        name="Battery",
        host="192.0.2.10",
        device_key="192.0.2.10_1",
        brand="marstek",
        battery_version=version,
        driver=driver,
        _def_by_key={
            "max_charge_power": {"key": "max_charge_power"},
            "max_discharge_power": {"key": "max_discharge_power"},
        },
        _get_entity_type=lambda definition, fallback_key=None: "number",
        _entity_registry=registry,
        _is_shutting_down=False,
        _suspension_reset_time=None,
        _last_update_times={},
        _key_update_times={},
        _critical_group_failures={},
        boost_fast_poll_until=0.0,
        lock=asyncio.Lock(),
        _consecutive_failures=0,
        _max_failures_before_reconnect=99,
        _max_failures_before_suspend=100,
        _is_connected=True,
        data={},
        async_reconnect_fresh=AsyncMock(return_value=True),
        capabilities=SimpleNamespace(
            has_energy_counters=True,
            has_daily_energy_counters=True,
            has_nominal_capacity=True,
        ),
        battery_capacity_kwh=0,
        _alarm_notifier=SimpleNamespace(check=AsyncMock()),
        number_definitions=[
            {"key": "max_charge_power"},
            {"key": "max_discharge_power"},
        ],
        user_max_charge_power=user_charge,
        user_max_discharge_power=user_discharge,
        needs_software_max_charge=False,
        needs_software_max_discharge=False,
        needs_software_power_cap=version in ("v2", "v3"),
        max_charge_power=2500,
        max_discharge_power=2500,
    )


@pytest.mark.parametrize("version", ["v2", "v3"])
async def test_venus_e_polling_preserves_user_power_caps(version):
    coordinator = _power_limit_coordinator(
        version=version,
        device_charge=800,
        device_discharge=2500,
        user_charge=500,
        user_discharge=1200,
    )

    await MarstekVenusDataUpdateCoordinator._async_update_data(coordinator)

    assert coordinator.data["max_charge_power"] == 800
    assert coordinator.data["max_discharge_power"] == 2500
    assert coordinator.max_charge_power == 500
    assert coordinator.max_discharge_power == 1200


async def test_non_venus_e_polling_keeps_register_value_as_effective_cap():
    coordinator = _power_limit_coordinator(
        version="vA",
        device_charge=800,
        device_discharge=2500,
        user_charge=500,
        user_discharge=1200,
    )

    await MarstekVenusDataUpdateCoordinator._async_update_data(coordinator)

    assert coordinator.max_charge_power == 800
    assert coordinator.max_discharge_power == 2500


async def test_lifetime_energy_totals_are_dependencies_for_derived_daily_energy():
    """Daily v3/Anker sensors must work when lifetime entities are disabled."""
    total_group = ReadGroup("low", ("total_charging_energy",))
    driver = SimpleNamespace(
        read_groups=[total_group],
        read_telemetry=AsyncMock(return_value={"total_charging_energy": 49100}),
        control_dependency_keys=set(),
    )
    disabled_entry = SimpleNamespace(disabled=True, disabled_by="user")
    registry = SimpleNamespace(
        async_get_entity_id=lambda *args: "sensor.battery_total_charging_energy",
        entities={"sensor.battery_total_charging_energy": disabled_entry},
    )
    coordinator = SimpleNamespace(
        name="Battery",
        host="192.0.2.10",
        device_key="192.0.2.10_1",
        driver=driver,
        _def_by_key={
            "total_charging_energy": {
                "key": "total_charging_energy", "scale": 0.01,
                "precision": 2, "state_class": "total_increasing",
            },
        },
        _get_entity_type=lambda definition, fallback_key=None: "sensor",
        _entity_registry=registry,
        _is_shutting_down=False,
        _suspension_reset_time=None,
        _last_update_times={},
        _key_update_times={},
        _critical_group_failures={},
        boost_fast_poll_until=0.0,
        lock=asyncio.Lock(),
        _consecutive_failures=0,
        _max_failures_before_reconnect=99,
        _max_failures_before_suspend=100,
        _is_connected=True,
        data={},
        async_reconnect_fresh=AsyncMock(return_value=True),
        capabilities=SimpleNamespace(
            has_energy_counters=True, has_daily_energy_counters=False
        ),
        battery_capacity_kwh=0,
        _alarm_notifier=SimpleNamespace(check=AsyncMock()),
    )

    await MarstekVenusDataUpdateCoordinator._async_update_data(coordinator)

    driver.read_telemetry.assert_awaited_once_with(["total_charging_energy"])
    assert coordinator.data["total_charging_energy"] == 491.0
