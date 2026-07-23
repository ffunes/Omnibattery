"""Regression tests for driver-specific coordinator capability handling."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.omnibattery import _device_owns_initial_config
from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
)


def test_only_zendure_owns_initial_config():
    assert _device_owns_initial_config("zendure") is True
    assert _device_owns_initial_config("anker") is False
    assert _device_owns_initial_config("marstek") is False


def test_inverse_max_power_is_a_hardware_discharge_limit():
    coordinator = SimpleNamespace(
        number_definitions=[{"key": "inverse_max_power"}],
        sensor_definitions=[],
    )

    assert (
        MarstekVenusDataUpdateCoordinator.needs_software_max_discharge.fget(
            coordinator
        )
        is False
    )


def test_anker_needs_software_max_despite_read_only_sensors():
    """Anker exposes 10036/10038 as sensors; soft-max numbers still gate PD."""
    from custom_components.omnibattery.drivers import anker as anker_mod

    coordinator = SimpleNamespace(
        number_definitions=list(anker_mod.NUMBER_DEFINITIONS),
        sensor_definitions=list(anker_mod.SENSOR_DEFINITIONS),
        select_definitions=list(anker_mod.SELECT_DEFINITIONS),
    )

    assert (
        MarstekVenusDataUpdateCoordinator.needs_software_max_charge.fget(coordinator)
        is True
    )
    assert (
        MarstekVenusDataUpdateCoordinator.needs_software_max_discharge.fget(
            coordinator
        )
        is True
    )
    assert (
        MarstekVenusDataUpdateCoordinator.needs_software_manual_control.fget(
            coordinator
        )
        is True
    )


def test_marstek_writable_max_charge_skips_software_max():
    coordinator = SimpleNamespace(
        number_definitions=[{"key": "max_charge_power"}, {"key": "max_discharge_power"}],
        sensor_definitions=[],
        select_definitions=[{"key": "force_mode"}],
    )

    assert (
        MarstekVenusDataUpdateCoordinator.needs_software_max_charge.fget(coordinator)
        is False
    )
    assert (
        MarstekVenusDataUpdateCoordinator.needs_software_max_discharge.fget(
            coordinator
        )
        is False
    )


async def test_reconnect_skips_rs485_for_driver_without_capability():
    driver = SimpleNamespace(connect=AsyncMock(return_value=True))
    coordinator = SimpleNamespace(
        name="Anker",
        host="192.0.2.1",
        port=502,
        _consecutive_failures=1,
        _is_connected=False,
        _suspension_reset_time=object(),
        lock=asyncio.Lock(),
        driver=driver,
        capabilities=SimpleNamespace(has_rs485_control=False),
        rs485_user_disabled=False,
        _last_update_times={("battery_soc",): object()},
        _critical_group_failures={("battery_soc",): 2},
    )

    result = await MarstekVenusDataUpdateCoordinator.async_reconnect_fresh(
        coordinator
    )

    assert result is True
    driver.connect.assert_awaited_once()
    assert coordinator._last_update_times == {}
    assert coordinator._critical_group_failures == {}
    assert coordinator._last_rs485_reenable_success is None


async def test_reconnect_records_failed_rs485_reenable():
    driver = SimpleNamespace(
        connect=AsyncMock(return_value=True),
        set_rs485_control=AsyncMock(return_value=False),
    )
    coordinator = SimpleNamespace(
        name="Marstek",
        host="192.0.2.2",
        port=502,
        _consecutive_failures=1,
        _is_connected=False,
        _suspension_reset_time=object(),
        lock=asyncio.Lock(),
        driver=driver,
        capabilities=SimpleNamespace(has_rs485_control=True),
        rs485_user_disabled=False,
        _last_update_times={},
        _critical_group_failures={},
    )

    result = await MarstekVenusDataUpdateCoordinator.async_reconnect_fresh(
        coordinator
    )

    assert result is True
    assert coordinator._last_rs485_reenable_success is False
