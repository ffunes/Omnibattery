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
    )

    result = await MarstekVenusDataUpdateCoordinator.async_reconnect_fresh(
        coordinator
    )

    assert result is True
    driver.connect.assert_awaited_once()
