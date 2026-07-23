"""Manual Mode idle must not reassert 0 W (Anker Third-Party release)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.select import MarstekManualForceModeSelect
from custom_components.omnibattery.switch import ManualModeSwitch


@pytest.mark.asyncio
async def test_software_manual_idle_skips_zero_watt_reassert():
    """Idle force mode must not call _set_battery_power — that would force
    Anker Third-Party Control every control cycle while Manual Mode is on."""
    controller = ChargeDischargeController.__new__(ChargeDischargeController)
    coord = SimpleNamespace(
        needs_software_manual_control=True,
        manual_force_mode=None,
        manual_set_charge_power=0,
        manual_set_discharge_power=0,
    )
    controller.coordinators = [coord]
    controller._set_battery_power = AsyncMock()

    await ChargeDischargeController._apply_software_manual_setpoints(controller)

    controller._set_battery_power.assert_not_awaited()


@pytest.mark.asyncio
async def test_software_manual_charge_still_asserts_setpoint():
    controller = ChargeDischargeController.__new__(ChargeDischargeController)
    coord = SimpleNamespace(
        needs_software_manual_control=True,
        manual_force_mode="Charge",
        manual_set_charge_power=1200,
        manual_set_discharge_power=0,
    )
    controller.coordinators = [coord]
    controller._set_battery_power = AsyncMock()

    await ChargeDischargeController._apply_software_manual_setpoints(controller)

    controller._set_battery_power.assert_awaited_once_with(
        coord, 1200, 0, bypass_blockers=True
    )


@pytest.mark.asyncio
async def test_software_manual_discharge_still_asserts_setpoint():
    controller = ChargeDischargeController.__new__(ChargeDischargeController)
    coord = SimpleNamespace(
        needs_software_manual_control=True,
        manual_force_mode="Discharge",
        manual_set_charge_power=0,
        manual_set_discharge_power=800,
    )
    controller.coordinators = [coord]
    controller._set_battery_power = AsyncMock()

    await ChargeDischargeController._apply_software_manual_setpoints(controller)

    controller._set_battery_power.assert_awaited_once_with(
        coord, 0, 800, bypass_blockers=True
    )


@pytest.mark.asyncio
async def test_selecting_idle_stops_power_once_then_leaves_device_alone():
    """Charge/Discharge -> None sends one 0 W command, but idle control cycles
    do not keep forcing Anker back into Third-Party Control."""
    coord = SimpleNamespace(
        name="Anker",
        needs_software_manual_control=True,
        manual_force_mode="Charge",
        manual_set_charge_power=1200,
        manual_set_discharge_power=0,
        commanded_charge_power=1200,
        commanded_discharge_power=0,
        apply_power=AsyncMock(),
        async_request_refresh=AsyncMock(),
        persist_battery_config=Mock(),
    )
    select = MarstekManualForceModeSelect.__new__(MarstekManualForceModeSelect)
    select.coordinator = coord
    select.async_write_ha_state = Mock()

    await select.async_select_option("None")

    coord.apply_power.assert_awaited_once_with(0, read_back=False)
    coord.async_request_refresh.assert_awaited_once()
    assert coord.manual_force_mode == "None"
    assert coord.commanded_charge_power == 0
    assert coord.commanded_discharge_power == 0
    coord.persist_battery_config.assert_called_once_with("manual_force_mode", "None")

    controller = ChargeDischargeController.__new__(ChargeDischargeController)
    controller.coordinators = [coord]
    controller._set_battery_power = AsyncMock()

    await ChargeDischargeController._apply_software_manual_setpoints(controller)

    controller._set_battery_power.assert_not_awaited()
    coord.apply_power.assert_awaited_once()


@pytest.mark.asyncio
async def test_enabling_manual_mode_clears_persisted_force_mode():
    """A saved manual command must not restart on the first paused cycle."""
    coord = SimpleNamespace(
        name="Anker",
        manual_force_mode="Discharge",
        commanded_charge_power=0,
        commanded_discharge_power=800,
        persist_battery_config=Mock(),
        apply_power=AsyncMock(),
        async_request_refresh=AsyncMock(),
    )
    controller = SimpleNamespace(
        manual_mode_enabled=False,
        coordinators=[coord],
    )
    entry = SimpleNamespace(data={})
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=Mock()),
        services=SimpleNamespace(async_call=AsyncMock()),
    )
    switch = ManualModeSwitch.__new__(ManualModeSwitch)
    switch.hass = hass
    switch.entry = entry
    switch.controller = controller
    switch.async_write_ha_state = Mock()

    await switch.async_turn_on()

    assert controller.manual_mode_enabled is True
    assert coord.manual_force_mode == "None"
    assert coord.commanded_charge_power == 0
    assert coord.commanded_discharge_power == 0
    coord.persist_battery_config.assert_called_once_with("manual_force_mode", "None")
    coord.apply_power.assert_awaited_once_with(0, read_back=False)
    coord.async_request_refresh.assert_awaited_once()
