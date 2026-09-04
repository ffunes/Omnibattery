"""Coverage for refusing writes to controller-owned battery registers."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.omnibattery.const import DOMAIN
from custom_components.omnibattery.infra.manual_control import (
    assert_manual_control,
    controller_owns_battery,
)
from custom_components.omnibattery.number import MarstekVenusNumber
from custom_components.omnibattery.select import MarstekVenusSelect

FORCE_MODE_DEFINITION = {
    "key": "force_mode",
    "name": "Force Mode",
    "options": {"None": 0, "Charge": 1, "Discharge": 2},
}

SET_CHARGE_POWER_DEFINITION = {
    "key": "set_charge_power",
    "name": "Set Charge Power",
    "min": 0,
    "max": 2500,
    "step": 50,
}

MAX_SOC_DEFINITION = {
    "key": "charging_cutoff_capacity",
    "name": "Charging Cutoff Capacity",
    "min": 12,
    "max": 100,
    "step": 1,
}


def _coordinator(*, battery_manual: bool = False):
    """Return a coordinator double wired to a config entry."""
    return SimpleNamespace(
        name="Attic",
        device_key="attic",
        data={"force_mode": 0, "set_charge_power": 0, "charging_cutoff_capacity": 95},
        battery_manual_mode_enabled=battery_manual,
        enable_charge_hysteresis=False,
        needs_software_power_cap=False,
        max_soc=95,
        min_soc=12,
        write_control=AsyncMock(return_value=True),
        persist_battery_config=lambda *args, **kwargs: None,
        get_shadow_select=lambda key: None,
        set_shadow_select=lambda key, value: None,
        _config_entry=SimpleNamespace(entry_id="entry-1"),
    )


def _hass(*, manual_mode: bool = False, controller: bool = True):
    """Return a hass double exposing the controller the way the integration does."""
    entry_data = {}
    if controller:
        entry_data["controller"] = SimpleNamespace(manual_mode_enabled=manual_mode)
    return SimpleNamespace(data={DOMAIN: {"entry-1": entry_data}})


def _bind(entity, hass):
    """Attach the hass double without going through the entity platform."""
    entity.hass = hass
    return entity


def test_controller_owns_battery_only_while_both_manual_switches_are_off():
    coordinator = _coordinator()
    assert controller_owns_battery(_hass(), coordinator) is True
    assert controller_owns_battery(_hass(manual_mode=True), coordinator) is False
    assert (
        controller_owns_battery(_hass(), _coordinator(battery_manual=True)) is False
    )


def test_no_controller_yet_leaves_direct_writes_alone():
    """During setup/teardown nothing re-asserts the registers."""
    assert controller_owns_battery(_hass(controller=False), _coordinator()) is False


def test_configuration_registers_are_not_guarded():
    """Only the registers the control loop rewrites are refused."""
    assert_manual_control(_hass(), _coordinator(), "charging_cutoff_capacity")
    assert_manual_control(_hass(), _coordinator(), "max_charge_power")


@pytest.mark.asyncio
async def test_force_mode_write_is_refused_under_automatic_control():
    coordinator = _coordinator()
    entity = _bind(MarstekVenusSelect(coordinator, FORCE_MODE_DEFINITION), _hass())

    with pytest.raises(HomeAssistantError) as err:
        await entity.async_select_option("Charge")

    assert err.value.translation_key == "manual_control_required"
    coordinator.write_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_force_mode_write_passes_in_manual_mode():
    coordinator = _coordinator()
    entity = _bind(
        MarstekVenusSelect(coordinator, FORCE_MODE_DEFINITION),
        _hass(manual_mode=True),
    )

    await entity.async_select_option("Charge")

    coordinator.write_control.assert_awaited_once_with("force_mode", 1, do_refresh=True)


@pytest.mark.asyncio
async def test_setpoint_write_is_refused_under_automatic_control():
    coordinator = _coordinator()
    entity = _bind(
        MarstekVenusNumber(coordinator, SET_CHARGE_POWER_DEFINITION), _hass()
    )

    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(800)

    coordinator.write_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_setpoint_write_passes_for_an_individually_manual_battery():
    coordinator = _coordinator(battery_manual=True)
    entity = _bind(
        MarstekVenusNumber(coordinator, SET_CHARGE_POWER_DEFINITION), _hass()
    )

    await entity.async_set_native_value(800)

    coordinator.write_control.assert_awaited_once_with(
        "set_charge_power", 800, do_refresh=True
    )


@pytest.mark.asyncio
async def test_config_number_still_writes_under_automatic_control():
    coordinator = _coordinator()
    entity = _bind(MarstekVenusNumber(coordinator, MAX_SOC_DEFINITION), _hass())

    await entity.async_set_native_value(90)

    coordinator.write_control.assert_awaited_once_with(
        "charging_cutoff_capacity", 90, do_refresh=True
    )
