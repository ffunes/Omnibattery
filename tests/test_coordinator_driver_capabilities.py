"""Regression tests for driver-specific coordinator capability handling."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

from custom_components.omnibattery import _device_owns_initial_config
from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
)
from custom_components.omnibattery.drivers.marstek import MarstekModbusDriver


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


def test_venus_e_v2_v3_keep_a_software_power_cap_during_polling():
    for version in ("v2", "v3"):
        coordinator = SimpleNamespace(brand="marstek", battery_version=version)
        assert (
            MarstekVenusDataUpdateCoordinator.needs_software_power_cap.fget(
                coordinator
            )
            is True
        )

    for version in ("vA", "vD"):
        coordinator = SimpleNamespace(brand="marstek", battery_version=version)
        assert (
            MarstekVenusDataUpdateCoordinator.needs_software_power_cap.fget(
                coordinator
            )
            is False
        )


def test_power_limits_expose_normalized_layers_and_legacy_aliases():
    coordinator = object.__new__(MarstekVenusDataUpdateCoordinator)
    coordinator._device_max_charge_power = 2400
    coordinator._device_max_discharge_power = 2200
    coordinator._configured_max_charge_power = 600
    coordinator._configured_max_discharge_power = 700
    coordinator._effective_max_charge_power = 600
    coordinator._effective_max_discharge_power = 700

    assert coordinator.device_max_charge_power == 2400
    assert coordinator.device_max_discharge_power == 2200
    assert coordinator.configured_max_charge_power == 600
    assert coordinator.configured_max_discharge_power == 700
    assert coordinator.effective_max_charge_power == 600
    assert coordinator.effective_max_discharge_power == 700
    assert coordinator.max_charge_power == coordinator.effective_max_charge_power
    assert coordinator.max_discharge_power == coordinator.effective_max_discharge_power
    assert coordinator.user_max_charge_power == coordinator.configured_max_charge_power
    assert coordinator.user_max_discharge_power == coordinator.configured_max_discharge_power

    coordinator.configured_max_charge_power = 2500
    coordinator.device_max_discharge_power = 500

    assert coordinator.configured_max_charge_power == 2500
    assert coordinator.effective_max_charge_power == 2400

    assert coordinator.effective_max_discharge_power == 500

    coordinator.max_charge_power = 3000

    assert coordinator.configured_max_charge_power == 3000
    assert coordinator.effective_max_charge_power == 2400


def test_venus_d_runtime_firmware_sync_clamps_legacy_config():
    coordinator = object.__new__(MarstekVenusDataUpdateCoordinator)
    coordinator.brand = "marstek"
    coordinator.battery_version = "vD"
    coordinator.name = "Venus D"
    coordinator.ems_version = 149
    coordinator.driver = MarstekModbusDriver(
        "1.2.3.4", 502, "vD", client=AsyncMock(), ems_version=149
    )
    coordinator.data = {"ems_version": 147}
    coordinator._device_max_charge_power = 2500
    coordinator._device_max_discharge_power = 2500
    coordinator._configured_max_charge_power = 2500
    coordinator._configured_max_discharge_power = 2500
    coordinator._effective_max_charge_power = 2500
    coordinator._effective_max_discharge_power = 2500
    coordinator.persist_battery_config = Mock()

    coordinator._sync_marstek_ems_power_ceiling()

    assert coordinator.device_max_charge_power == 2200
    assert coordinator.device_max_discharge_power == 2200
    assert coordinator.configured_max_charge_power == 2200
    assert coordinator.configured_max_discharge_power == 2200
    assert coordinator.effective_max_charge_power == 2200
    assert coordinator.effective_max_discharge_power == 2200
    assert call("ems_version", 147) in coordinator.persist_battery_config.call_args_list
    assert call("max_charge_power", 2200) in coordinator.persist_battery_config.call_args_list

def test_zendure_model_promotion_updates_device_cap_and_persists_model():
    coordinator = object.__new__(MarstekVenusDataUpdateCoordinator)
    coordinator.brand = "zendure"
    coordinator.zendure_model = "2400ac_plus"
    coordinator.name = "SolarFlow"
    coordinator._device_max_charge_power = 2400
    coordinator._device_max_discharge_power = 2400
    coordinator._configured_max_charge_power = 1200
    coordinator._configured_max_discharge_power = 1200
    coordinator._effective_max_charge_power = 1200
    coordinator._effective_max_discharge_power = 1200
    coordinator.driver = SimpleNamespace(
        model_key="4000mix_ac_plus",
        capabilities=SimpleNamespace(
            max_charge_power_w=4000,
            max_discharge_power_w=4000,
        ),
    )
    coordinator.persist_battery_config = Mock()

    coordinator._sync_detected_zendure_model()

    assert coordinator.zendure_model == "4000mix_ac_plus"
    assert coordinator.device_max_charge_power == 4000
    assert coordinator.device_max_discharge_power == 4000
    assert coordinator.effective_max_charge_power == 1200
    assert coordinator.effective_max_discharge_power == 1200
    assert coordinator.persist_battery_config.call_args_list == [
        call("zendure_model", "4000mix_ac_plus"),
        call("device_max_charge_power", 4000),
        call("device_max_discharge_power", 4000),
    ]


def test_zendure_model_promotion_repairs_stale_saved_cap_after_restart():
    coordinator = object.__new__(MarstekVenusDataUpdateCoordinator)
    coordinator.brand = "zendure"
    coordinator.zendure_model = "4000mix_ac_plus"
    coordinator.name = "SolarFlow"
    coordinator._device_max_charge_power = 2400
    coordinator._device_max_discharge_power = 2400
    coordinator._configured_max_charge_power = 4000
    coordinator._configured_max_discharge_power = 4000
    coordinator._effective_max_charge_power = 2400
    coordinator._effective_max_discharge_power = 2400
    coordinator.driver = SimpleNamespace(
        model_key="4000mix_ac_plus",
        capabilities=SimpleNamespace(
            max_charge_power_w=4000,
            max_discharge_power_w=4000,
        ),
    )
    coordinator.persist_battery_config = Mock()

    coordinator._sync_detected_zendure_model()

    assert coordinator.device_max_charge_power == 4000
    assert coordinator.device_max_discharge_power == 4000
    assert coordinator.effective_max_charge_power == 4000
    assert coordinator.effective_max_discharge_power == 4000
    assert coordinator.persist_battery_config.call_args_list == [
        call("device_max_charge_power", 4000),
        call("device_max_discharge_power", 4000),
    ]


def test_zendure_reported_inverse_max_power_repairs_stale_configured_cap():
    coordinator = object.__new__(MarstekVenusDataUpdateCoordinator)
    coordinator.brand = "zendure"
    coordinator.name = "SolarFlow"
    coordinator._device_max_charge_power = 4000
    coordinator._device_max_discharge_power = 4000
    coordinator._configured_max_charge_power = 4000
    coordinator._configured_max_discharge_power = 2400
    coordinator._effective_max_charge_power = 4000
    coordinator._effective_max_discharge_power = 2400
    coordinator.data = {"inverse_max_power": 4000}
    coordinator.persist_battery_config = Mock()

    coordinator._sync_zendure_inverse_max_power()

    assert coordinator.configured_max_discharge_power == 4000
    assert coordinator.effective_max_discharge_power == 4000
    coordinator.persist_battery_config.assert_called_once_with(
        "max_discharge_power", 4000
    )


def test_zendure_reported_inverse_max_power_does_not_repersist_matching_cap():
    coordinator = object.__new__(MarstekVenusDataUpdateCoordinator)
    coordinator.brand = "zendure"
    coordinator.name = "SolarFlow"
    coordinator._device_max_charge_power = 4000
    coordinator._device_max_discharge_power = 4000
    coordinator._configured_max_charge_power = 4000
    coordinator._configured_max_discharge_power = 4000
    coordinator._effective_max_charge_power = 4000
    coordinator._effective_max_discharge_power = 4000
    coordinator.data = {"inverse_max_power": 4000}
    coordinator.persist_battery_config = Mock()

    coordinator._sync_zendure_inverse_max_power()

    assert coordinator.effective_max_discharge_power == 4000
    coordinator.persist_battery_config.assert_not_called()


def test_zendure_reported_inverse_max_power_ignores_invalid_cap():
    coordinator = object.__new__(MarstekVenusDataUpdateCoordinator)
    coordinator.brand = "zendure"
    coordinator.name = "SolarFlow"
    coordinator._device_max_charge_power = 4000
    coordinator._device_max_discharge_power = 4000
    coordinator._configured_max_charge_power = 4000
    coordinator._configured_max_discharge_power = 2400
    coordinator._effective_max_charge_power = 4000
    coordinator._effective_max_discharge_power = 2400
    coordinator.data = {"inverse_max_power": "unknown"}
    coordinator.persist_battery_config = Mock()

    coordinator._sync_zendure_inverse_max_power()

    assert coordinator.effective_max_discharge_power == 2400
    coordinator.persist_battery_config.assert_not_called()


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
