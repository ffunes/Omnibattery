"""Regression tests for Marstek Venus number entity state."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.omnibattery.number import (
    MarstekManualSetPowerNumber,
    MarstekVenusNumber,
    SoftSocLimitNumber,
)
from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
)


def _number_entity(key: str, *, venus_e: bool):
    entity = object.__new__(MarstekVenusNumber)
    entity.definition = {"key": key, "max": 2500}
    entity.coordinator = SimpleNamespace(
        data={key: 2500},
        needs_software_power_cap=venus_e,
        user_max_charge_power=500,
        user_max_discharge_power=1200,
        max_charge_power=1500,
        max_discharge_power=1500,
    )
    return entity


def test_venus_e_number_shows_user_charge_cap_after_polling():
    entity = _number_entity("max_charge_power", venus_e=True)

    assert entity.native_value == 500.0


def test_venus_e_number_shows_user_discharge_cap_after_polling():
    entity = _number_entity("max_discharge_power", venus_e=True)

    assert entity.native_value == 1200.0


def test_non_venus_e_number_keeps_polled_register_value():
    entity = _number_entity("max_charge_power", venus_e=False)

    assert entity.native_value == 2500


@pytest.mark.parametrize(
    ("key", "limit"),
    [
        ("set_charge_power", "max_charge_power"),
        ("set_discharge_power", "max_discharge_power"),
    ],
)
def test_manual_register_slider_uses_configured_power_limit(key, limit):
    entity = _number_entity(key, venus_e=False)
    setattr(entity.coordinator, limit, 1500)

    assert entity.native_max_value == 1500
    assert entity.native_value == 1500.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "limit"),
    [
        ("set_charge_power", "max_charge_power"),
        ("set_discharge_power", "max_discharge_power"),
    ],
)
async def test_manual_register_write_is_capped_to_configured_power_limit(key, limit):
    entity = _number_entity(key, venus_e=False)
    setattr(entity.coordinator, limit, 1500)
    entity._scale = 1.0
    entity.coordinator.write_control = AsyncMock()
    entity.coordinator.persist_battery_config = Mock()

    await entity.async_set_native_value(2500)

    entity.coordinator.write_control.assert_awaited_once_with(
        key, 1500, do_refresh=True
    )
    # The capped value is also the manual intent re-asserted for #477.
    assert getattr(entity.coordinator, f"manual_{key}") == 1500
    entity.coordinator.persist_battery_config.assert_called_once_with(
        f"manual_{key}", 1500
    )


@pytest.mark.asyncio
async def test_zendure_inverse_max_power_updates_effective_discharge_limit():
    """The Zendure hardware discharge number must also update coordinator state."""
    coordinator = object.__new__(MarstekVenusDataUpdateCoordinator)
    coordinator.brand = "zendure"
    coordinator.battery_version = "v3"
    coordinator.name = "SolarFlow 4000"
    coordinator._config_entry = None
    coordinator._device_max_charge_power = 4000
    coordinator._device_max_discharge_power = 4000
    coordinator._configured_max_charge_power = 4000
    coordinator._configured_max_discharge_power = 2400
    coordinator._effective_max_charge_power = 4000
    coordinator._effective_max_discharge_power = 2400
    coordinator.data = {"inverse_max_power": 2400}
    coordinator.write_control = AsyncMock()
    coordinator.persist_battery_config = Mock()

    entity = object.__new__(MarstekVenusNumber)
    entity.definition = {"key": "inverse_max_power", "max": 4000}
    entity.coordinator = coordinator
    entity.hass = SimpleNamespace(data={})
    entity._scale = 1.0

    await entity.async_set_native_value(4000)

    coordinator.write_control.assert_awaited_once_with(
        "inverse_max_power", 4000, do_refresh=True
    )
    assert coordinator.configured_max_discharge_power == 4000
    assert coordinator.effective_max_discharge_power == 4000
    coordinator.persist_battery_config.assert_called_once_with(
        "max_discharge_power", 4000
    )


@pytest.mark.parametrize(
    ("kind", "limit"),
    [
        ("charge", "max_charge_power"),
        ("discharge", "max_discharge_power"),
    ],
)
def test_software_manual_slider_uses_configured_power_limit(kind, limit):
    entity = object.__new__(MarstekManualSetPowerNumber)
    entity._kind = kind
    entity._hardware_max = 2500
    entity.coordinator = SimpleNamespace(
        max_charge_power=1500,
        max_discharge_power=1500,
        capabilities=SimpleNamespace(
            max_charge_power_w=2500,
            max_discharge_power_w=2500,
        ),
        commanded_charge_power=2500,
        commanded_discharge_power=2500,
    )
    setattr(entity.coordinator, limit, 1500)

    assert entity.native_max_value == 1500
    assert entity.native_value == 1500.0


# ----------------------------------------------------------------------
# Cutoff bounds come from the driver, not from constants (#495)
#
# The software SOC-limit entity hard-coded the Venus D's hardware floors, and
# every brand inherited them. On a LUNA2000 that put a 12 % floor on a battery
# whose own minimum is 5 %, and offered a discharge cutoff up to 50 % that the
# Huawei write path rejects outright (its register takes 0-20 %).
# ----------------------------------------------------------------------


def _soc_limit_entity(kind, *, charge_range=None, discharge_range=None):
    from custom_components.omnibattery.drivers import DriverCapabilities

    fields = {}
    if charge_range is not None:
        fields["charge_cutoff_range"] = charge_range
    if discharge_range is not None:
        fields["discharge_cutoff_range"] = discharge_range
    capabilities = DriverCapabilities(
        hardware_soc_cutoff=False,
        has_force_mode=True,
        push_telemetry=False,
        max_charge_power_w=2500,
        max_discharge_power_w=2500,
        has_mppt_pv=False,
        has_alarm_registers=False,
        has_rs485_control=False,
        **fields,
    )
    entity = object.__new__(SoftSocLimitNumber)
    coordinator = SimpleNamespace(
        capabilities=capabilities,
        device_key="dev-key",
        name="Battery",
        max_soc=100,
        min_soc=5,
    )
    SoftSocLimitNumber.__init__(entity, coordinator, kind)
    return entity


def test_a_driver_without_its_own_window_keeps_the_marstek_bounds():
    """Every existing driver must be untouched by this change."""
    assert (_soc_limit_entity("min").native_min_value,
            _soc_limit_entity("min").native_max_value) == (12.0, 50.0)
    assert (_soc_limit_entity("max").native_min_value,
            _soc_limit_entity("max").native_max_value) == (50.0, 100.0)


def test_a_driver_that_declares_a_window_gets_it():
    entity = _soc_limit_entity("min", discharge_range=(0.0, 20.0))
    assert entity.native_min_value == 0.0
    assert entity.native_max_value == 20.0


def test_the_charge_side_follows_the_driver_too():
    entity = _soc_limit_entity("max", charge_range=(90.0, 100.0))
    assert entity.native_min_value == 90.0
    assert entity.native_max_value == 100.0


def test_a_huawei_floor_of_five_percent_is_now_reachable():
    """The reported symptom: 5 % could not be set because 12 % was the minimum."""
    entity = _soc_limit_entity("min", discharge_range=(0.0, 20.0))
    assert entity.native_min_value <= 5.0


def test_the_huawei_driver_declares_the_windows_its_registers_accept():
    """The declared window must be the one _write_cutoff will actually take."""
    from unittest.mock import MagicMock

    from custom_components.omnibattery.drivers.huawei import (
        _CHARGE_CUTOFF_RANGE,
        _DISCHARGE_CUTOFF_RANGE,
        HuaweiSolarDriver,
    )

    driver = HuaweiSolarDriver(
        MagicMock(),
        "1.2.3.4",
        port=502,
        slave_id=4,
        battery_device_id="dev",
        client=MagicMock(),
    )
    assert driver.capabilities.charge_cutoff_range == _CHARGE_CUTOFF_RANGE
    assert driver.capabilities.discharge_cutoff_range == _DISCHARGE_CUTOFF_RANGE
