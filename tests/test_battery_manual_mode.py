"""Unit coverage for per-battery manual ownership."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.control.weekly_full_charge import (
    WeeklyFullChargeManager,
)
from custom_components.omnibattery.control.power_distribution import PowerDistribution
from custom_components.omnibattery.switch import BatteryManualModeSwitch, ManualModeSwitch
from tests.conftest import FakeCoordinator


class HashableNamespace(SimpleNamespace):
    """Simple test double that can participate in coordinator-keyed dicts."""

    __hash__ = object.__hash__
    __eq__ = object.__eq__


def _controller_with_reset(**overrides):
    """Return a light controller double with the production reset helper bound."""
    controller = SimpleNamespace(
        _active_charge_batteries=[],
        _active_discharge_batteries=[],
        _manual_slot_owned=set(),
        _power_distribution=SimpleNamespace(
            _charge_selection_hold_until={},
            _discharge_selection_hold_until={},
        ),
        _last_commanded_net_sign={},
        _charge_engage_started={},
        _discharge_engage_started={},
        _idle_commanded_started={},
        _idle_runaway_handled={},
        _weekly_charge_mgr=SimpleNamespace(_bms_cutoff_counts={}),
    )
    controller.__dict__.update(overrides)
    controller._reset_battery_ownership_state = (
        ChargeDischargeController._reset_battery_ownership_state.__get__(controller)
    )
    return controller


@pytest.mark.asyncio
async def test_automatic_power_write_is_rejected_for_manual_battery():
    coordinator = SimpleNamespace(
        name="Manual A",
        battery_manual_mode_enabled=True,
    )

    result = await ChargeDischargeController._set_battery_power(
        SimpleNamespace(), coordinator, 500, 0
    )

    assert result is False


@pytest.mark.asyncio
async def test_individual_software_manual_setpoint_is_reasserted_with_owner():
    coordinator = SimpleNamespace(
        battery_manual_mode_enabled=True,
        needs_software_manual_control=True,
        manual_force_mode="Charge",
        manual_set_charge_power=700,
        manual_set_discharge_power=0,
    )
    controller = ChargeDischargeController.__new__(ChargeDischargeController)
    controller.coordinators = [coordinator]
    controller._set_battery_power = AsyncMock()

    await ChargeDischargeController._apply_software_manual_setpoints(
        controller, global_mode=False
    )

    controller._set_battery_power.assert_awaited_once_with(
        coordinator,
        700,
        0,
        bypass_blockers=True,
        owner="battery_manual",
    )


def test_effective_system_capacity_excludes_manual_battery():
    manual = SimpleNamespace(battery_manual_mode_enabled=True)
    automatic = SimpleNamespace(battery_manual_mode_enabled=False)
    controller = ChargeDischargeController.__new__(ChargeDischargeController)
    controller.enable_system_power_limits = False
    controller._battery_power_limit = lambda _coordinator, _charging: 1000

    assert (
        ChargeDischargeController._effective_system_capacity(
            controller, [manual, automatic], True
        )
        == 1000
    )


def test_reset_battery_ownership_state_clears_transient_automatic_state():
    coordinator = HashableNamespace(
        name="Manual A",
        battery_manual_mode_enabled=True,
    )
    controller = _controller_with_reset()
    controller._active_charge_batteries.append(coordinator)
    controller._active_discharge_batteries.append(coordinator)
    controller._manual_slot_owned.add(coordinator)
    controller._power_distribution._charge_selection_hold_until[coordinator] = 10
    controller._power_distribution._discharge_selection_hold_until[coordinator] = 10
    controller._last_commanded_net_sign[coordinator] = 1
    controller._weekly_charge_mgr._bms_cutoff_counts[coordinator.name] = 2

    controller._reset_battery_ownership_state(coordinator)

    assert coordinator not in controller._active_charge_batteries
    assert coordinator not in controller._active_discharge_batteries
    assert coordinator not in controller._manual_slot_owned
    assert coordinator not in controller._power_distribution._charge_selection_hold_until
    assert coordinator not in controller._power_distribution._discharge_selection_hold_until
    assert coordinator not in controller._last_commanded_net_sign
    assert coordinator.name not in controller._weekly_charge_mgr._bms_cutoff_counts


@pytest.mark.asyncio
async def test_manual_mode_persists_before_idle_handoff_and_clears_setpoints():
    coordinator = HashableNamespace(
        name="Battery A",
        battery_manual_mode_enabled=False,
        manual_force_mode="Discharge",
        manual_set_charge_power=0,
        manual_set_discharge_power=800,
        persist_battery_config=Mock(),
        async_request_refresh=AsyncMock(),
    )
    controller = _controller_with_reset(
        _control_lock=asyncio.Lock(),
        _set_battery_power=AsyncMock(return_value=True),
        schedule_control_cycle=Mock(),
        previous_power=1200,
        previous_error=75.0,
        error_integral=240.0,
        derivative_filtered=12.0,
        last_output_sign=1,
        _grid_filter_ema=900.0,
    )

    await ChargeDischargeController._set_battery_manual_mode(
        controller, coordinator, True
    )

    assert coordinator.battery_manual_mode_enabled is True
    assert coordinator.manual_force_mode == "None"
    assert coordinator.manual_set_discharge_power == 0
    assert coordinator.persist_battery_config.call_args_list[0].args == (
        "battery_manual_mode_enabled",
        True,
    )
    controller._set_battery_power.assert_awaited_once_with(
        coordinator,
        0,
        0,
        bypass_blockers=True,
        force_write=True,
        owner="battery_manual",
    )
    assert controller.previous_power == 1200
    assert controller.previous_error == 75.0
    assert controller.error_integral == 240.0
    assert controller.derivative_filtered == 12.0
    assert controller.last_output_sign == 1
    assert controller._grid_filter_ema == 900.0


@pytest.mark.asyncio
async def test_manual_mode_stays_enabled_when_deactivation_idle_fails():
    coordinator = HashableNamespace(
        name="Battery A",
        battery_manual_mode_enabled=True,
        manual_force_mode="Charge",
        manual_set_charge_power=500,
        manual_set_discharge_power=0,
        persist_battery_config=Mock(),
        async_request_refresh=AsyncMock(),
    )
    controller = _controller_with_reset(
        _control_lock=asyncio.Lock(),
        _set_battery_power=AsyncMock(return_value=False),
        schedule_control_cycle=Mock(),
    )

    with pytest.raises(HomeAssistantError):
        await ChargeDischargeController._set_battery_manual_mode(
            controller, coordinator, False
        )

    assert coordinator.battery_manual_mode_enabled is True
    assert not any(
        call.args == ("battery_manual_mode_enabled", False)
        for call in coordinator.persist_battery_config.call_args_list
    )


@pytest.mark.asyncio
async def test_manual_mode_deactivation_preserves_automatic_pd_command():
    coordinator = HashableNamespace(
        name="Battery A",
        battery_manual_mode_enabled=True,
        manual_force_mode="Charge",
        manual_set_charge_power=500,
        manual_set_discharge_power=0,
        persist_battery_config=Mock(),
        async_request_refresh=AsyncMock(),
    )
    controller = _controller_with_reset(
        _control_lock=asyncio.Lock(),
        _set_battery_power=AsyncMock(return_value=True),
        schedule_control_cycle=Mock(),
        previous_power=1200,
        previous_error=75.0,
        error_integral=240.0,
        derivative_filtered=12.0,
        last_output_sign=1,
        _grid_filter_ema=900.0,
    )

    await ChargeDischargeController._set_battery_manual_mode(
        controller, coordinator, False
    )

    assert coordinator.battery_manual_mode_enabled is False
    assert controller.previous_power == 1200
    assert controller._grid_filter_ema == 900.0
    assert controller.previous_error == 0.0
    assert controller.error_integral == 0.0
    assert controller.derivative_filtered == 0.0
    controller.schedule_control_cycle.assert_called_once_with()


def test_weekly_full_charge_does_not_classify_manual_battery_as_full():
    coordinator = SimpleNamespace(
        name="Battery A",
        battery_manual_mode_enabled=True,
        data={"battery_soc": 100},
    )
    manager = SimpleNamespace(_bms_cutoff_counts={coordinator.name: 3})

    assert WeeklyFullChargeManager.is_battery_full(manager, coordinator) is False


def test_existing_coordinator_defaults_individual_manual_mode_off():
    assert FakeCoordinator().battery_manual_mode_enabled is False


def test_battery_manual_switch_uses_stable_device_identity():
    coordinator = HashableNamespace(
        name="Battery A",
        device_key="battery_a_stable_key",
        battery_manual_mode_enabled=False,
    )
    switch = BatteryManualModeSwitch(
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), coordinator
    )

    assert switch.unique_id == "battery_a_stable_key_battery_manual_mode"
    assert switch.entity_id.endswith("_battery_manual_mode")
    assert switch.is_on is False


def _available_pool_controller(manual, automatic):
    """Build the small controller surface used by _get_available_batteries."""
    controller = ChargeDischargeController.__new__(ChargeDischargeController)
    controller.coordinators = [manual, automatic]
    controller._non_responsive = SimpleNamespace(is_excluded=lambda _c: False)
    controller._is_backup_function_active = lambda _c: False
    controller._is_manual_slot_owned = lambda _c: False
    controller.get_charge_blockers = lambda _c: {}
    controller.is_discharge_blocked = lambda _c: False
    controller.get_discharge_blockers = lambda _c: {}
    controller._weekly_full_charge_unlocked = lambda: False
    controller._weekly_charge_mgr = SimpleNamespace(
        is_battery_full=lambda _coordinator: False,
    )
    controller._effective_charge_max_soc = lambda _c, _weekly, **_kw: (100, "min_soc")
    controller._normal_balance_recal_override = {}
    return controller


def test_available_pool_excludes_manual_battery_even_without_blocker_checks():
    manual = HashableNamespace(
        name="Manual A",
        battery_manual_mode_enabled=True,
        data={"battery_soc": 50},
        is_available=True,
    )
    automatic = HashableNamespace(
        name="Automatic B",
        battery_manual_mode_enabled=False,
        data={"battery_soc": 50},
        is_available=True,
        _consecutive_failures=0,
        rs485_user_disabled=False,
        enable_charge_hysteresis=False,
        max_soc=100,
        min_soc=10,
        _discharge_min_soc_latched=False,
    )
    controller = _available_pool_controller(manual, automatic)

    assert controller._get_available_batteries(True, False) == [automatic]
    assert controller._get_available_batteries(False, False) == [automatic]


def test_power_distribution_drops_manual_battery_before_selection():
    manual = HashableNamespace(
        name="Manual A",
        battery_manual_mode_enabled=True,
        data={"battery_soc": 50},
    )
    automatic = HashableNamespace(
        name="Automatic B",
        battery_manual_mode_enabled=False,
        data={"battery_soc": 50},
    )
    controller = SimpleNamespace(
        _active_charge_batteries=[],
        _active_discharge_batteries=[],
        _is_battery_manual_owned=lambda coordinator: coordinator.battery_manual_mode_enabled,
        _battery_power_limit=lambda _coordinator, _charging: 1000,
        _clamp_to_system_capacity=lambda power, _batteries, _charging: power,
        _get_available_batteries=lambda _charging: [automatic],
        _phase_power_limiter=None,
    )
    distribution = PowerDistribution(SimpleNamespace(), SimpleNamespace(), controller)

    selected = distribution._select_batteries_for_operation(
        500, [manual, automatic], True
    )
    allocation = distribution._distribute_power_by_limits(
        500, [manual, automatic], True
    )

    assert selected == [automatic]
    assert allocation == {automatic: 500}


def test_manual_grid_feedback_uses_ac_power_not_dc_solar_power():
    manual = SimpleNamespace(
        battery_manual_mode_enabled=True,
        data={"ac_power": -700, "battery_power": 1000},
    )
    controller = SimpleNamespace(coordinators=[manual])

    assert (
        ChargeDischargeController._manual_battery_power_for_grid_feedback(
            controller
        )
        == 700
    )

    # On a DC-coupled battery, battery_power can include solar while ac_power
    # remains zero. Only the AC contribution belongs in the grid feedback.
    manual.data = {"ac_power": 0, "battery_power": 700}
    assert (
        ChargeDischargeController._manual_battery_power_for_grid_feedback(
            controller
        )
        == 0
    )


def test_manual_grid_feedback_falls_back_to_manual_setpoint_without_telemetry():
    manual = SimpleNamespace(
        battery_manual_mode_enabled=True,
        data={},
        manual_force_mode="Charge",
        manual_set_charge_power=800,
    )
    controller = SimpleNamespace(coordinators=[manual])

    assert (
        ChargeDischargeController._manual_battery_power_for_grid_feedback(
            controller
        )
        == 800
    )


def test_automatic_measured_power_excludes_manual_battery():
    manual = HashableNamespace(
        battery_manual_mode_enabled=True,
        data={"battery_power": 700},
    )
    automatic = HashableNamespace(
        battery_manual_mode_enabled=False,
        data={"battery_power": -400},
    )
    controller = SimpleNamespace(
        coordinators=[manual, automatic],
        _coordinator_delivered_power=ChargeDischargeController._coordinator_delivered_power,
    )

    assert ChargeDischargeController._measured_battery_power(controller) == -400


@pytest.mark.asyncio
async def test_global_manual_mode_preserves_individual_software_state():
    coordinator = SimpleNamespace(
        name="Battery A",
        battery_manual_mode_enabled=True,
        manual_force_mode="Charge",
        commanded_charge_power=700,
        commanded_discharge_power=0,
        persist_battery_config=Mock(),
        apply_power=AsyncMock(),
        async_request_refresh=AsyncMock(),
    )
    controller = SimpleNamespace(
        manual_mode_enabled=False,
        coordinators=[coordinator],
        _active_discharge_batteries=[],
        _active_charge_batteries=[],
        _control_lock=asyncio.Lock(),
        error_integral=0.0,
        previous_error=0.0,
        sign_changes=0,
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
    await switch.async_turn_off()

    assert coordinator.manual_force_mode == "Charge"
    coordinator.persist_battery_config.assert_not_called()
