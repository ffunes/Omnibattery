"""Switch platform for the Omnibattery integration."""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_ACTIVE_BALANCE_MODE_ENABLED,
    CONF_CAPACITY_PROTECTION_ENABLED,
    CONF_DELAY_SOC_SETPOINT_ENABLED,
    CONF_ENABLE_CHARGE_DELAY,
    CONF_ENABLE_TEMP_CHARGE_LIMIT,
    CONF_TEMP_LIMIT_APPLY_DISCHARGE,
    CONF_ENABLE_HOURLY_BALANCE,
    CONF_ENABLE_SYSTEM_POWER_LIMITS,
    CONF_ENABLE_WEEKLY_FULL_CHARGE,
    CONF_ENABLE_WEEKLY_FULL_CHARGE_DELAY,
    CONF_WEEKLY_FULL_CHARGE_SKIP_DELAY,
    CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
    CONF_MANUAL_MODE_ENABLED,
    CONF_NO_PD_MODE_ENABLED,
    CONF_PREDICTIVE_CHARGING_OVERRIDDEN,
    CONF_PREDICTIVE_CHARGING_MODE,
    CONF_DP_PRICE_DISCHARGE_CONTROL,
    CONF_RT_PRICE_DISCHARGE_CONTROL,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_REALTIME_PRICE,
    CONF_SYSTEM_MAX_CHARGE_POWER,
    CONF_SYSTEM_MAX_DISCHARGE_POWER,
    CONF_ENABLE_MIN_SOC_FLOOR,
    CONF_ENABLE_PREDICTIVE_CHARGING,
    NOTIFICATION_ID_PREFIX,
)
from .infra.coordinator import MarstekVenusDataUpdateCoordinator
from .infra.entity_naming import english_entity_id, system_entity_id, SYSTEM_UNIQUE_ID_PREFIX

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the switch platform."""
    coordinators: list[MarstekVenusDataUpdateCoordinator] = hass.data[DOMAIN][entry.entry_id]["coordinators"]
    controller = hass.data[DOMAIN][entry.entry_id].get("controller")
    entities = []

    # Add regular battery switches
    for coordinator in coordinators:
        for definition in coordinator.switch_definitions:
            entities.append(MarstekVenusSwitch(coordinator, definition))
        if controller:
            entities.append(BatteryAllowChargeSwitch(hass, entry, controller, coordinator))
            entities.append(BatteryAllowDischargeSwitch(hass, entry, controller, coordinator))
            # Marstek-only cell maintenance: voltage taper + active balance need
            # per-cell voltages Anker/Zendure do not expose the same way.
            if coordinator.brand not in ("zendure", "anker"):
                entities.append(BatteryFullChargeVoltageTaperSwitch(hass, entry, controller, coordinator))
                entities.append(BatteryActiveBalanceModeSwitch(hass, entry, controller, coordinator))

    # Add manual mode switch (system-level, always present)
    if controller:
        entities.append(ManualModeSwitch(hass, entry, controller))
        entities.append(NoPdModeSwitch(hass, entry, controller))
        # Weekly full charge enable/disable (system-level, always present so it can
        # be turned on from the dashboard even if configured off at setup).
        entities.append(WeeklyFullChargeEnableSwitch(hass, entry, controller))

    # Add price-based discharge control switch, scoped to the active predictive
    # pricing mode (dynamic pricing or real-time price). The pricing engine reads
    # the controller flag live each cycle.
    if controller and entry.data.get(CONF_ENABLE_PREDICTIVE_CHARGING):
        mode = entry.data.get(CONF_PREDICTIVE_CHARGING_MODE)
        if mode == PREDICTIVE_MODE_DYNAMIC_PRICING:
            entities.append(PriceDischargeControlSwitch(hass, entry, controller, "dp"))
        elif mode == PREDICTIVE_MODE_REALTIME_PRICE:
            entities.append(PriceDischargeControlSwitch(hass, entry, controller, "rt"))

    # Add predictive charging switch (system-level, not per-battery). Shown
    # whenever predictive charging has been through config (the key is always
    # written, True or False), not only when currently enabled, so the enable
    # toggle behaves like the other feature blocks (charge delay, capacity
    # protection): always visible on the dashboard, hiding its sibling sliders
    # via the panel gate when OFF. Gating it on the enabled *value* previously
    # made the switch vanish while the sliders (keyed on presence) stayed,
    # leaving orphaned settings with no toggle (#68).
    if controller and CONF_ENABLE_PREDICTIVE_CHARGING in entry.data:
        entities.append(PredictiveChargingSwitch(hass, entry, controller))

    # Add capacity protection switch (system-level, when configured, regardless of enabled state)
    if controller and CONF_CAPACITY_PROTECTION_ENABLED in entry.data:
        entities.append(CapacityProtectionSwitch(hass, entry, controller))

    # Add charge delay switch (system-level, when charge delay is configured)
    has_charge_delay_config = (
        CONF_ENABLE_CHARGE_DELAY in entry.data
        or CONF_ENABLE_WEEKLY_FULL_CHARGE_DELAY in entry.data
    )
    if controller and has_charge_delay_config:
        entities.append(ChargeDelaySwitch(hass, entry, controller))
        entities.append(DelaySocSetpointEnabledSwitch(hass, entry, controller))

    # Add weekly-full-charge delay switch: lets the weekly charge wait for the
    # solar charge delay instead of charging immediately. Only meaningful when
    # the charge delay is configured; not gated on weekly-full-charge being
    # enabled since that switch can be flipped live without a platform reload.
    if controller and has_charge_delay_config:
        entities.append(WeeklyFullChargeDelaySwitch(hass, entry, controller))

    # Add temperature charge limit switch (system-level, when configured)
    if controller and CONF_ENABLE_TEMP_CHARGE_LIMIT in entry.data:
        entities.append(TempChargeLimitSwitch(hass, entry, controller))
        entities.append(TempChargeLimitDischargeSwitch(hass, entry, controller))

    # Add hourly balance switch (system-level, when hourly balance is configured)
    if controller and CONF_ENABLE_HOURLY_BALANCE in entry.data:
        entities.append(HourlyBalanceSwitch(hass, entry, controller))

    # Add system power limits switch when the feature is configured. Mirrors the
    # number-platform heuristic so the toggle appears exactly when its sliders do
    # (key present, or a legacy config with a non-zero limit predating the key).
    has_system_limits_config = (
        CONF_ENABLE_SYSTEM_POWER_LIMITS in entry.data
        or (entry.data.get(CONF_SYSTEM_MAX_CHARGE_POWER, 0) or 0) > 0
        or (entry.data.get(CONF_SYSTEM_MAX_DISCHARGE_POWER, 0) or 0) > 0
    )
    if controller and has_system_limits_config:
        entities.append(SystemPowerLimitsSwitch(hass, entry, controller))

    # Add guaranteed minimum SOC floor switch (predictive charging sub-feature)
    if controller and CONF_ENABLE_PREDICTIVE_CHARGING in entry.data:
        entities.append(MinSOCFloorSwitch(hass, entry, controller))

    # Add time slot enable/disable switches
    time_slots = entry.data.get("no_discharge_time_slots", [])
    for index in range(len(time_slots)):
        entities.append(TimeSlotSwitch(hass, entry, index))

    # Add per-device enable/disable and solar surplus switches for excluded devices
    excluded_devices = entry.data.get("excluded_devices", [])
    for index in range(len(excluded_devices)):
        entities.append(ExcludedDeviceEnabledSwitch(hass, entry, index))
        entities.append(ExcludedDeviceSolarSurplusSwitch(hass, entry, index))
        if not excluded_devices[index].get("ev_charger_no_telemetry", False):
            entities.append(ExcludedDeviceDynamicPowerControlSwitch(hass, entry, index))
        entities.append(ExcludedDeviceCoverHomeSwitch(hass, entry, index))

    async_add_entities(entities)


class MarstekVenusSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of a Marstek Venus switch."""

    def __init__(
        self, coordinator: MarstekVenusDataUpdateCoordinator, definition: dict
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self.definition = definition
        
        self._attr_has_entity_name = True
        self._attr_translation_key = definition["key"]
        self._attr_unique_id = f"{coordinator.device_key}_{definition['key']}"
        self.entity_id = english_entity_id("switch", coordinator.name, definition["key"])
        self._attr_icon = definition.get("icon")
        self._attr_entity_registry_enabled_default = definition.get("enabled_by_default", True)
        self._attr_should_poll = False
        self._command_on = definition["command_on"]
        self._command_off = definition["command_off"]

    @property
    def is_on(self):
        """Return the state of the switch."""
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self.definition["key"])
        if value is None:
            return None
        # Check if the value matches command_on (switch is on)
        return value == self._command_on

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the switch on."""
        if self.definition["key"] == "rs485_control_mode":
            was_off = self.is_on is False
            self.coordinator.set_rs485_user_disabled(False)

            if was_off:
                # Venus v3 can acknowledge 0x55AA on an existing TCP session yet
                # retain 0x55BB until that session is replaced (issue #92). A
                # reconnect is therefore part of the OFF -> ON transition, not a
                # recovery reserved for the automatic control path.
                _LOGGER.info(
                    "%s: RS485 control enabled by user; reconnecting with a fresh connection",
                    self.coordinator.name,
                )
                success = await self.coordinator.async_reconnect_fresh()
            else:
                success = await self.coordinator.set_rs485_control(True)

            if not success:
                raise HomeAssistantError(
                    f"Unable to enable RS485 control for {self.coordinator.name}"
                )

            # Do not optimistically report ON: the BMS may ACK the write while
            # leaving the control register disabled. This readback is deliberately
            # after the fresh connection, which is the sequence that makes v3
            # firmware apply the command reliably.
            if await self.coordinator.rs485_control_enabled() is not True:
                raise HomeAssistantError(
                    f"RS485 control could not be verified for {self.coordinator.name}"
                )

            await self.coordinator.async_request_refresh()
            return

        await self.coordinator.write_control(self.definition["key"], self._command_on, do_refresh=True)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the switch off."""
        if self.definition["key"] == "rs485_control_mode":
            self.coordinator.set_rs485_user_disabled(True)

            success = await self.coordinator.set_rs485_control(False)
            if not success:
                raise HomeAssistantError(
                    f"Unable to disable RS485 control for {self.coordinator.name}"
                )

            # Keep the preference even when confirmation fails: it prevents a
            # concurrent or later reconnect from re-enabling external control
            # against the user's request. Surface the failed verification to HA
            # rather than leaving the entity optimistically OFF.
            if await self.coordinator.rs485_control_enabled() is not False:
                raise HomeAssistantError(
                    f"RS485 control could not be verified as disabled for "
                    f"{self.coordinator.name}"
                )

            await self.coordinator.async_request_refresh()
            return

        await self.coordinator.write_control(self.definition["key"], self._command_off, do_refresh=True)

    @property
    def device_info(self):
        """Return device information."""
        return self.coordinator.battery_device_info


class BatteryAllowOperationSwitch(SwitchEntity):
    """Software switch that allows one battery to participate in one direction."""

    _config_key: str
    _block_source: str
    _direction: str

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller, coordinator) -> None:
        """Initialize the per-battery operation switch."""
        self.hass = hass
        self.entry = entry
        self.controller = controller
        self.coordinator = coordinator

        self._attr_has_entity_name = True
        self._attr_translation_key = self._translation_key
        self._attr_unique_id = f"{coordinator.device_key}_{self._translation_key}"
        self.entity_id = english_entity_id("switch", coordinator.name, self._translation_key)
        self._attr_icon = self._icon
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True if this battery is allowed to use this direction."""
        return bool(getattr(self.coordinator, self._config_key, True))

    def _persist_allowed(self, allowed: bool) -> None:
        """Persist the per-battery allow flag in config_entry.data."""
        setattr(self.coordinator, self._config_key, allowed)
        new_data = dict(self.entry.data)
        batteries = [dict(b) for b in new_data.get("batteries", [])]
        for battery in batteries:
            if (
                battery.get("host") == self.coordinator.host
                and battery.get("port") == self.coordinator.port
                and battery.get("slave_id", 1) == self.coordinator.slave_id
            ):
                battery[self._config_key] = allowed
                break
        new_data["batteries"] = batteries
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)

    async def _stop_if_active(self) -> None:
        """Stop this battery if it is currently active in the disabled direction."""
        active = (
            self.coordinator in self.controller._active_charge_batteries
            if self._direction == "charge"
            else self.coordinator in self.controller._active_discharge_batteries
        )
        if not active:
            return

        await self.controller._set_battery_power(self.coordinator, 0, 0)
        if self.coordinator in self.controller._active_charge_batteries:
            self.controller._active_charge_batteries.remove(self.coordinator)
        if self.coordinator in self.controller._active_discharge_batteries:
            self.controller._active_discharge_batteries.remove(self.coordinator)

    async def async_turn_on(self, **kwargs) -> None:
        """Allow this battery to participate in this direction."""
        self._persist_allowed(True)
        if self._direction == "charge":
            self.controller.remove_charge_block(self._block_source, coordinator=self.coordinator)
        else:
            self.controller.remove_discharge_block(self._block_source, coordinator=self.coordinator)
        _LOGGER.info("%s: %s allowed", self.coordinator.name, self._direction)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Block this battery from participating in this direction."""
        self._persist_allowed(False)
        if self._direction == "charge":
            self.controller.set_charge_block(
                self._block_source,
                "user_disabled",
                {"battery": self.coordinator.name},
                coordinator=self.coordinator,
            )
        else:
            self.controller.set_discharge_block(
                self._block_source,
                "user_disabled",
                {"battery": self.coordinator.name},
                coordinator=self.coordinator,
            )
        await self._stop_if_active()
        _LOGGER.info("%s: %s blocked by user switch", self.coordinator.name, self._direction)
        self.async_write_ha_state()

    @property
    def device_info(self):
        """Return device information."""
        return self.coordinator.battery_device_info


class BatteryAllowChargeSwitch(BatteryAllowOperationSwitch):
    """Switch allowing a battery to charge under automatic control."""

    _translation_key = "battery_allow_charge"
    _config_key = "allow_charge"
    _block_source = "user_battery_charge_disabled"
    _direction = "charge"
    _icon = "mdi:battery-arrow-up"


class BatteryAllowDischargeSwitch(BatteryAllowOperationSwitch):
    """Switch allowing a battery to discharge under automatic control."""

    _translation_key = "battery_allow_discharge"
    _config_key = "allow_discharge"
    _block_source = "user_battery_discharge_disabled"
    _direction = "discharge"
    _icon = "mdi:battery-arrow-down"


class BatteryFullChargeVoltageTaperSwitch(SwitchEntity):
    """Switch enabling 100% charge voltage tapering for one battery."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.controller = controller
        self.coordinator = coordinator

        self._attr_has_entity_name = True
        self._attr_translation_key = "full_charge_voltage_taper"
        self._attr_unique_id = f"{coordinator.device_key}_full_charge_voltage_taper"
        self.entity_id = english_entity_id("switch", coordinator.name, "full_charge_voltage_taper")
        self._attr_icon = "mdi:battery-clock"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        return bool(getattr(self.coordinator, CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED, True))

    def _persist(self, enabled: bool) -> None:
        setattr(self.coordinator, CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED, enabled)
        self.coordinator.persist_battery_config(CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED, enabled)

    def _clear_runtime_state(self) -> None:
        self.controller._normal_balance_charge_paused.pop(self.coordinator, None)
        self.controller._normal_balance_voltage_tapered.pop(self.coordinator, None)
        self.controller._normal_balance_pause_latch_soc.pop(self.coordinator, None)
        self.controller.remove_charge_block("normal_balance_pause", coordinator=self.coordinator)

    async def async_turn_on(self, **kwargs) -> None:
        self._persist(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._persist(False)
        self._clear_runtime_state()
        self.async_write_ha_state()

    @property
    def device_info(self):
        return self.coordinator.battery_device_info


class BatteryActiveBalanceModeSwitch(SwitchEntity):
    """Switch enabling active balancing for one battery."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.controller = controller
        self.coordinator = coordinator

        self._attr_has_entity_name = True
        self._attr_translation_key = "active_balance_mode"
        self._attr_unique_id = f"{coordinator.device_key}_active_balance_mode"
        self.entity_id = english_entity_id("switch", coordinator.name, "active_balance_mode")
        self._attr_icon = "mdi:battery-sync"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        return bool(getattr(self.coordinator, CONF_ACTIVE_BALANCE_MODE_ENABLED, False))

    def _persist(self, enabled: bool) -> None:
        setattr(self.coordinator, CONF_ACTIVE_BALANCE_MODE_ENABLED, enabled)
        self.coordinator.persist_battery_config(CONF_ACTIVE_BALANCE_MODE_ENABLED, enabled)

    async def async_turn_on(self, **kwargs) -> None:
        self._persist(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._persist(False)
        if self.controller._active_balance_mode_started(self.coordinator):
            await self.controller._complete_active_balance_mode(
                self.coordinator,
                "disabled",
                dt_util.now().date().isoformat(),
                mark_completed=False,
            )
        self.async_write_ha_state()

    @property
    def device_info(self):
        return self.coordinator.battery_device_info


class PredictiveChargingSwitch(SwitchEntity):
    """Switch to enable/disable predictive grid charging at runtime.

    Mirrors the other feature toggles (charge delay, capacity protection): always
    visible once predictive charging is configured, drives the ``enable`` flag,
    and the panel hides the sibling sliders when OFF.

    ON  = predictive charging enabled and running.
    OFF = predictive charging disabled/paused.

    The two persisted booleans (``enabled`` = configured on, ``overridden`` =
    runtime pause) are moved together so the switch stays consistent with every
    consumer, whichever flag it reads (the pricing engine checks ``overridden``,
    the time-slot path checks ``enabled``).
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        """Initialize the predictive charging switch."""
        self.hass = hass
        self.entry = entry
        self.controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "predictive_charging"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}predictive_charging"
        self.entity_id = system_entity_id("switch", "predictive_charging")
        self._attr_icon = "mdi:solar-power"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True when predictive charging is enabled and not paused."""
        return (
            self.controller.predictive_charging_enabled
            and not self.controller.predictive_charging_overridden
        )

    async def async_turn_on(self, **kwargs) -> None:
        """Enable predictive charging (set enabled, clear override)."""
        was_enabled = self.controller.predictive_charging_enabled
        self.controller.predictive_charging_enabled = True
        self.controller.predictive_charging_overridden = False
        new_data = dict(self.entry.data)
        new_data[CONF_ENABLE_PREDICTIVE_CHARGING] = True
        new_data[CONF_PREDICTIVE_CHARGING_OVERRIDDEN] = False
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        await self.hass.services.async_call(
            "persistent_notification",
            "dismiss",
            {"notification_id": f"{NOTIFICATION_ID_PREFIX}predictive_charging_override"},
        )
        _LOGGER.info("Predictive charging enabled")
        await self._apply_enabled_change(was_enabled, now_enabled=True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable predictive charging (clear enabled, set override)."""
        was_enabled = self.controller.predictive_charging_enabled
        self.controller.predictive_charging_enabled = False
        self.controller.predictive_charging_overridden = True
        new_data = dict(self.entry.data)
        new_data[CONF_ENABLE_PREDICTIVE_CHARGING] = False
        new_data[CONF_PREDICTIVE_CHARGING_OVERRIDDEN] = True
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        if self.controller.grid_charging_active:
            message = "Predictive grid charging has been paused. Turn the switch back on to resume."
        else:
            message = "Predictive charging is now disabled. It will not activate when the time slot becomes active."
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Predictive Charging Disabled",
                "message": message,
                "notification_id": f"{NOTIFICATION_ID_PREFIX}predictive_charging_override",
            },
        )
        _LOGGER.info("Predictive charging disabled (overridden)")
        await self._apply_enabled_change(was_enabled, now_enabled=False)

    async def _apply_enabled_change(self, was_enabled: bool, *, now_enabled: bool) -> None:
        """Finish a toggle. When the ``enabled`` value flips, reload the entry so
        the setup-time gating re-evaluates: the daily consumption-capture and
        dynamic-pricing schedules and the predictive status sensor are all armed
        (or torn down) only in ``async_setup_entry`` against this value, and the
        entry-update listener does not reload. This mirrors the options flow,
        which reloads on the same change. When only the runtime override moved
        (a legacy paused entry resuming with ``enabled`` already True), a plain
        state write suffices and avoids a needless reload."""
        if was_enabled != now_enabled:
            await self.hass.config_entries.async_reload(self.entry.entry_id)
        else:
            self.async_write_ha_state()

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class TimeSlotSwitch(SwitchEntity):
    """Switch to enable/disable an individual time slot."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int) -> None:
        """Initialize the time slot switch."""
        self.hass = hass
        self.entry = entry
        self._slot_index = index

        self._attr_has_entity_name = True
        self._attr_translation_key = "time_slot"
        self._attr_translation_placeholders = {"slot_number": str(index + 1)}
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}time_slot_{index}_enabled"
        self.entity_id = system_entity_id("switch", f"time_slot_{index}_enabled")
        self._attr_icon = "mdi:clock-outline"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True if the time slot is enabled."""
        slots = self.entry.data.get("no_discharge_time_slots", [])
        if self._slot_index < len(slots):
            return slots[self._slot_index].get("enabled", True)
        return False

    @property
    def extra_state_attributes(self) -> dict:
        """Return time slot details as attributes."""
        slots = self.entry.data.get("no_discharge_time_slots", [])
        if self._slot_index >= len(slots):
            return {}
        slot = slots[self._slot_index]
        days = slot.get("days", [])
        days_str = ", ".join(d.capitalize() for d in days) if days else "None"
        # Map "battery_N" config keys to user-facing battery names so consumers
        # (the panel tooltip) can show which battery a scope/limit refers to.
        batteries = self.entry.data.get("batteries", [])
        battery_names = {
            f"battery_{i + 1}": (b.get(CONF_NAME) or f"Battery {i + 1}")
            for i, b in enumerate(batteries)
        }
        scope = slot.get("battery_scope", "all")
        return {
            "schedule": f"{slot.get('start_time', '??')}-{slot.get('end_time', '??')}",
            "days": days_str,
            "battery_scope": scope,
            "battery_scope_name": "all" if scope == "all" else battery_names.get(scope, scope),
            "mode": slot.get("mode", "pd"),
            "allow_charge": slot.get("allow_charge", False),
            "allow_discharge": slot.get("allow_discharge", True),
            "soc_override_enabled": slot.get("soc_override_enabled", False),
            "power_override_enabled": slot.get("power_override_enabled", False),
            "battery_limits": slot.get("battery_limits", {}),
            "battery_names": battery_names,
        }

    async def _update_slot_enabled(self, enabled: bool) -> None:
        """Update the enabled state of this slot in config_entry.data."""
        new_data = dict(self.entry.data)
        slots = [dict(s) for s in new_data.get("no_discharge_time_slots", [])]
        if self._slot_index < len(slots):
            slots[self._slot_index]["enabled"] = enabled
            new_data["no_discharge_time_slots"] = slots
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)
            state = "enabled" if enabled else "disabled"
            _LOGGER.info("Time slot %d %s", self._slot_index + 1, state)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        """Enable the time slot."""
        await self._update_slot_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable the time slot."""
        await self._update_slot_enabled(False)

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class CapacityProtectionSwitch(SwitchEntity):
    """Switch to enable/disable capacity protection mode at runtime."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        """Initialize the capacity protection switch."""
        self.hass = hass
        self.entry = entry
        self.controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "capacity_protection"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}capacity_protection"
        self.entity_id = system_entity_id("switch", "capacity_protection")
        self._attr_icon = "mdi:battery-lock"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True if capacity protection is active."""
        return self.controller.capacity_protection_enabled

    async def async_turn_on(self, **kwargs) -> None:
        """Enable capacity protection mode."""
        self.controller.capacity_protection_enabled = True
        new_data = dict(self.entry.data)
        new_data[CONF_CAPACITY_PROTECTION_ENABLED] = True
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("Capacity Protection ENABLED (SOC threshold: %d%%, peak limit: %dW)",
                     self.controller.capacity_protection_soc_threshold,
                     self.controller.capacity_protection_limit)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable capacity protection mode."""
        self.controller.capacity_protection_enabled = False
        new_data = dict(self.entry.data)
        new_data[CONF_CAPACITY_PROTECTION_ENABLED] = False
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("Capacity Protection DISABLED")
        self.async_write_ha_state()

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class ChargeDelaySwitch(SwitchEntity):
    """Switch to enable/disable the charge delay feature at runtime."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        """Initialize the charge delay switch."""
        self.hass = hass
        self.entry = entry
        self.controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "charge_delay"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}charge_delay"
        self.entity_id = system_entity_id("switch", "charge_delay")
        self._attr_icon = "mdi:battery-clock"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True if charge delay is enabled."""
        return self.controller.charge_delay_enabled

    async def async_turn_on(self, **kwargs) -> None:
        """Enable charge delay."""
        self.controller.charge_delay_enabled = True
        self.controller._charge_delay_status["state"] = "Idle"
        # Re-enabling re-evaluates the delay from scratch: clear an unlock committed
        # earlier today (e.g. by a transient forecast blip) so it can be recovered on
        # demand, without waiting for the midnight reset.
        self.controller._charge_delay_unlocked = False
        self.controller._delay_setpoint_reached = False
        self.controller._forecast_unavailable_since = None
        self.controller._schedule_charge_delay_state_save()
        new_data = dict(self.entry.data)
        new_data[CONF_ENABLE_CHARGE_DELAY] = True
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("Charge Delay ENABLED")
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable charge delay."""
        self.controller.charge_delay_enabled = False
        self.controller._charge_delay_status["state"] = "Disabled"
        new_data = dict(self.entry.data)
        new_data[CONF_ENABLE_CHARGE_DELAY] = False
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("Charge Delay DISABLED")
        self.async_write_ha_state()

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class DelaySocSetpointEnabledSwitch(SwitchEntity):
    """Switch to enable the intermediate SOC setpoint during the charge delay."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        self.hass = hass
        self.entry = entry
        self.controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "delay_soc_setpoint_enabled"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}delay_soc_setpoint_enabled"
        self.entity_id = system_entity_id("switch", "delay_soc_setpoint_enabled")
        self._attr_icon = "mdi:battery-charging-50"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        return self.controller._delay_soc_setpoint_enabled

    def _set_enabled(self, enabled: bool) -> None:
        self.controller._delay_soc_setpoint_enabled = enabled
        new_data = dict(self.entry.data)
        new_data[CONF_DELAY_SOC_SETPOINT_ENABLED] = enabled
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("Charge delay SOC setpoint %s", "ENABLED" if enabled else "DISABLED")
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        self._set_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        self._set_enabled(False)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class TempChargeLimitSwitch(SwitchEntity):
    """Switch to enable/disable temperature-based charge power limiting."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        """Initialize the temperature charge limit switch."""
        self.hass = hass
        self.entry = entry
        self.controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "temp_charge_limit"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}temp_charge_limit"
        self.entity_id = system_entity_id("switch", "temp_charge_limit")
        self._attr_icon = "mdi:thermometer-alert"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True if temperature charge limiting is enabled."""
        return self.controller.temp_charge_limit_enabled

    async def async_turn_on(self, **kwargs) -> None:
        """Enable temperature charge limiting."""
        self.controller.temp_charge_limit_enabled = True
        new_data = dict(self.entry.data)
        new_data[CONF_ENABLE_TEMP_CHARGE_LIMIT] = True
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("Temperature Charge Limit ENABLED")
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable temperature charge limiting."""
        self.controller.temp_charge_limit_enabled = False
        new_data = dict(self.entry.data)
        new_data[CONF_ENABLE_TEMP_CHARGE_LIMIT] = False
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("Temperature Charge Limit DISABLED")
        self.async_write_ha_state()

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class TempChargeLimitDischargeSwitch(SwitchEntity):
    """Sub-toggle: also apply the thermal derate to discharge power."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        """Initialize the discharge sub-toggle."""
        self.hass = hass
        self.entry = entry
        self.controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "temp_charge_limit_discharge"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}temp_charge_limit_discharge"
        self.entity_id = system_entity_id("switch", "temp_charge_limit_discharge")
        self._attr_icon = "mdi:battery-arrow-down"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True if the derate also applies to discharge."""
        return self.controller.temp_limit_apply_discharge

    async def async_turn_on(self, **kwargs) -> None:
        """Apply the derate to discharge as well."""
        self.controller.temp_limit_apply_discharge = True
        new_data = dict(self.entry.data)
        new_data[CONF_TEMP_LIMIT_APPLY_DISCHARGE] = True
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("Temperature Charge Limit: discharge derate ENABLED")
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Limit the derate to charge only."""
        self.controller.temp_limit_apply_discharge = False
        new_data = dict(self.entry.data)
        new_data[CONF_TEMP_LIMIT_APPLY_DISCHARGE] = False
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("Temperature Charge Limit: discharge derate DISABLED")
        self.async_write_ha_state()

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class WeeklyFullChargeDelaySwitch(SwitchEntity):
    """Switch letting the weekly full charge be postponed by the solar charge delay.

    ON  = weekly full charge respects the charge delay (waits for solar).
    OFF = weekly full charge bypasses the delay and charges immediately (default).
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        """Initialize the weekly full charge delay switch."""
        self.hass = hass
        self.entry = entry
        self.controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "weekly_full_charge_delay"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}weekly_full_charge_delay"
        self.entity_id = system_entity_id("switch", "weekly_full_charge_delay")
        self._attr_icon = "mdi:timer-sand"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True when the weekly full charge respects the charge delay."""
        return not self.controller._weekly_full_charge_skip_delay

    async def _set_skip(self, skip: bool) -> None:
        """Persist the skip flag and update the controller."""
        self.controller._weekly_full_charge_skip_delay = skip
        new_data = dict(self.entry.data)
        new_data[CONF_WEEKLY_FULL_CHARGE_SKIP_DELAY] = skip
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info(
            "Weekly Full Charge delay %s",
            "DISABLED (bypassing delay)" if skip else "ENABLED (respecting delay)",
        )
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        """Let the weekly full charge wait for the solar charge delay."""
        await self._set_skip(False)

    async def async_turn_off(self, **kwargs) -> None:
        """Bypass the delay so the weekly full charge starts immediately."""
        await self._set_skip(True)

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


def _excluded_device_friendly_name(device: dict) -> str:
    """Derive a readable name from either excluded-device sensor field."""
    sensor_id = device.get("power_sensor") or device.get("activity_sensor") or "device"
    return sensor_id.split(".", 1)[-1].replace("_", " ").title()


class ExcludedDeviceEnabledSwitch(SwitchEntity):
    """Switch to enable/disable an individual excluded device at runtime.

    ON  = Device is active — its power affects battery charge/discharge calculations.
    OFF = Device is ignored — battery sees raw home sensor power for this device.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int) -> None:
        """Initialize the excluded device enabled switch."""
        self.hass = hass
        self.entry = entry
        self._device_index = index

        device = entry.data.get("excluded_devices", [])[index]
        friendly = _excluded_device_friendly_name(device)

        self._attr_has_entity_name = True
        self._attr_translation_key = "excluded_device_enabled"
        self._attr_translation_placeholders = {"device": friendly}
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}excluded_device_enabled_{index}"
        self.entity_id = system_entity_id("switch", f"excluded_device_enabled_{index}")
        self._attr_icon = "mdi:power-plug-off"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True if this excluded device is active."""
        devices = self.entry.data.get("excluded_devices", [])
        if self._device_index < len(devices):
            return devices[self._device_index].get("enabled", True)
        return False

    @property
    def extra_state_attributes(self) -> dict:
        """Return the power sensor entity as an attribute."""
        devices = self.entry.data.get("excluded_devices", [])
        if self._device_index >= len(devices):
            return {}
        device = devices[self._device_index]
        return {
            "power_sensor": device.get("power_sensor", ""),
            "included_in_consumption": device.get("included_in_consumption", True),
        }

    async def _update_enabled(self, enabled: bool) -> None:
        """Update enabled state for this device in config_entry.data."""
        new_data = dict(self.entry.data)
        devices = [dict(d) for d in new_data.get("excluded_devices", [])]
        if self._device_index < len(devices):
            devices[self._device_index]["enabled"] = enabled
            new_data["excluded_devices"] = devices
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)
            state = "enabled" if enabled else "disabled"
            _LOGGER.info(
                "Excluded device %d (%s) %s",
                self._device_index + 1,
                devices[self._device_index].get("power_sensor", ""),
                state,
            )
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        """Enable this excluded device."""
        await self._update_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable this excluded device."""
        await self._update_enabled(False)

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class ExcludedDeviceSolarSurplusSwitch(SwitchEntity):
    """Switch to toggle solar surplus priority for an excluded device at runtime.

    ON  = Battery does NOT charge with solar surplus while this device is consuming
          (solar goes to the device first — EV/priority mode).
    OFF = Battery charges normally with solar surplus regardless of this device.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int) -> None:
        """Initialize the solar surplus switch."""
        self.hass = hass
        self.entry = entry
        self._device_index = index

        device = entry.data.get("excluded_devices", [])[index]
        # Derive a friendly name from the configured sensor entity ID.
        friendly = _excluded_device_friendly_name(device)

        self._attr_has_entity_name = True
        self._attr_translation_key = "excluded_device_solar_surplus"
        self._attr_translation_placeholders = {"device": friendly}
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}solar_surplus_{index}"
        self.entity_id = system_entity_id("switch", f"solar_surplus_{index}")
        self._attr_icon = "mdi:solar-power-variant"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True if solar surplus priority is active for this device."""
        devices = self.entry.data.get("excluded_devices", [])
        if self._device_index < len(devices):
            return devices[self._device_index].get("allow_solar_surplus", False)
        return False

    @property
    def extra_state_attributes(self) -> dict:
        """Return the power sensor entity as an attribute."""
        devices = self.entry.data.get("excluded_devices", [])
        if self._device_index >= len(devices):
            return {}
        device = devices[self._device_index]
        return {
            "power_sensor": device.get("power_sensor", ""),
            "included_in_consumption": device.get("included_in_consumption", True),
        }

    async def _update_solar_surplus(self, enabled: bool) -> None:
        """Update allow_solar_surplus for this device in config_entry.data."""
        new_data = dict(self.entry.data)
        devices = [dict(d) for d in new_data.get("excluded_devices", [])]
        if self._device_index < len(devices):
            devices[self._device_index]["allow_solar_surplus"] = enabled
            new_data["excluded_devices"] = devices
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)
            state = "enabled" if enabled else "disabled"
            _LOGGER.info(
                "Solar surplus priority for device %d (%s) %s",
                self._device_index + 1,
                devices[self._device_index].get("power_sensor", ""),
                state,
            )
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        """Enable solar surplus priority (battery yields solar to this device)."""
        await self._update_solar_surplus(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable solar surplus priority (battery charges normally)."""
        await self._update_solar_surplus(False)

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class ExcludedDeviceDynamicPowerControlSwitch(SwitchEntity):
    """Give a telemetry excluded device first claim on changing solar surplus."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int) -> None:
        """Initialize the dynamic power-control switch."""
        self.hass = hass
        self.entry = entry
        self._device_index = index

        device = entry.data.get("excluded_devices", [])[index]
        friendly = _excluded_device_friendly_name(device)

        self._attr_has_entity_name = True
        self._attr_translation_key = "excluded_device_dynamic_power_control"
        self._attr_translation_placeholders = {"device": friendly}
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}dynamic_power_control_{index}"
        self.entity_id = system_entity_id("switch", f"dynamic_power_control_{index}")
        self._attr_icon = "mdi:ev-station"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True if dynamic power control is enabled for this device."""
        devices = self.entry.data.get("excluded_devices", [])
        if self._device_index < len(devices):
            return devices[self._device_index].get("dynamic_power_control", False)
        return False

    @property
    def extra_state_attributes(self) -> dict:
        """Return the feature prerequisites for troubleshooting."""
        devices = self.entry.data.get("excluded_devices", [])
        if self._device_index >= len(devices):
            return {}
        device = devices[self._device_index]
        return {
            "power_sensor": device.get("power_sensor", ""),
            "activity_sensor": device.get("activity_sensor", ""),
            "allow_solar_surplus": device.get("allow_solar_surplus", False),
            "included_in_consumption": device.get("included_in_consumption", True),
        }

    async def _update_dynamic_power_control(self, enabled: bool) -> None:
        """Persist dynamic_power_control in config_entry.data."""
        new_data = dict(self.entry.data)
        devices = [dict(d) for d in new_data.get("excluded_devices", [])]
        if self._device_index < len(devices):
            devices[self._device_index]["dynamic_power_control"] = enabled
            new_data["excluded_devices"] = devices
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)
            _LOGGER.info(
                "Dynamic power control for device %d (%s) %s",
                self._device_index + 1,
                devices[self._device_index].get("power_sensor", ""),
                "enabled" if enabled else "disabled",
            )
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        """Enable dynamic power control for this device."""
        await self._update_dynamic_power_control(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable dynamic power control for this device."""
        await self._update_dynamic_power_control(False)

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class ExcludedDeviceCoverHomeSwitch(SwitchEntity):
    """Toggle "battery covers home deficit while this device is active" (#42).

    Only meaningful together with Solar Surplus + a solar sensor. When ON, the
    device is offset by raw PV (pre-#415 rule) so only its real grid draw
    max(0, device − solar) is excluded, and the battery discharges to cover the
    remaining home base load instead of sitting idle. When OFF (default), the
    #421 home-first rule applies and the battery never discharges while the
    device is active.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int) -> None:
        """Initialize the cover-home switch."""
        self.hass = hass
        self.entry = entry
        self._device_index = index

        device = entry.data.get("excluded_devices", [])[index]
        friendly = _excluded_device_friendly_name(device)

        self._attr_has_entity_name = True
        self._attr_translation_key = "excluded_device_cover_home"
        self._attr_translation_placeholders = {"device": friendly}
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}cover_home_{index}"
        self.entity_id = system_entity_id("switch", f"cover_home_{index}")
        self._attr_icon = "mdi:home-lightning-bolt"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True if the battery covers the home deficit for this device."""
        devices = self.entry.data.get("excluded_devices", [])
        if self._device_index < len(devices):
            return devices[self._device_index].get("cover_home_when_active", False)
        return False

    @property
    def extra_state_attributes(self) -> dict:
        """Return the power sensor entity as an attribute."""
        devices = self.entry.data.get("excluded_devices", [])
        if self._device_index >= len(devices):
            return {}
        device = devices[self._device_index]
        return {
            "power_sensor": device.get("power_sensor", ""),
            "allow_solar_surplus": device.get("allow_solar_surplus", False),
        }

    async def _update_cover_home(self, enabled: bool) -> None:
        """Update cover_home_when_active for this device in config_entry.data."""
        new_data = dict(self.entry.data)
        devices = [dict(d) for d in new_data.get("excluded_devices", [])]
        if self._device_index < len(devices):
            devices[self._device_index]["cover_home_when_active"] = enabled
            new_data["excluded_devices"] = devices
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)
            _LOGGER.info(
                "Cover-home for device %d (%s) %s",
                self._device_index + 1,
                devices[self._device_index].get("power_sensor", ""),
                "enabled" if enabled else "disabled",
            )
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        """Enable cover-home (battery covers the home deficit for this device)."""
        await self._update_cover_home(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable cover-home (#421 home-first: battery idle while device active)."""
        await self._update_cover_home(False)

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class ManualModeSwitch(SwitchEntity):
    """Switch to enable manual control mode and pause automatic charge/discharge control."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        """Initialize the manual mode switch."""
        self.hass = hass
        self.entry = entry
        self.controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "manual_mode"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}manual_mode"
        self.entity_id = system_entity_id("switch", "manual_mode")
        self._attr_icon = "mdi:hand-back-right"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True if manual mode is active."""
        return self.controller.manual_mode_enabled

    async def async_turn_on(self, **kwargs) -> None:
        """Enable manual mode to pause automatic control."""
        self.controller.manual_mode_enabled = True
        new_data = dict(self.entry.data)
        new_data[CONF_MANUAL_MODE_ENABLED] = True
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("Manual Mode ENABLED - automatic control paused")

        # Set all batteries to 0W (idle state) when entering manual mode
        for coordinator in self.controller.coordinators:
            try:
                await coordinator.apply_power(0, read_back=False)
                await coordinator.async_request_refresh()
                _LOGGER.info("Set %s to 0W (idle) for manual mode", coordinator.name)
            except Exception as e:
                _LOGGER.error("Failed to set %s to 0W: %s", coordinator.name, e)

        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Manual Mode Active",
                "message": (
                    "Automatic charge/discharge control is paused. "
                    "All batteries have been set to idle (0W). "
                    "You can now manually control each battery using the "
                    "'Set Forcible Charge/Discharge Power' controls.\n\n"
                    "Turn off Manual Mode to resume automatic control."
                ),
                "notification_id": f"{NOTIFICATION_ID_PREFIX}manual_mode_active",
            },
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable manual mode to resume automatic control."""
        new_data = dict(self.entry.data)
        new_data[CONF_MANUAL_MODE_ENABLED] = False
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)

        # Reset PD controller state under the control lock so a running control
        # cycle never observes a partially-reset state.
        async with self.controller._control_lock:
            self.controller.manual_mode_enabled = False
            self.controller.error_integral = 0.0
            self.controller.previous_error = 0.0
            self.controller.sign_changes = 0
            self.controller._active_discharge_batteries = []
            self.controller._active_charge_batteries = []

        _LOGGER.info("Manual Mode DISABLED - resuming automatic control")

        await self.hass.services.async_call(
            "persistent_notification",
            "dismiss",
            {"notification_id": f"{NOTIFICATION_ID_PREFIX}manual_mode_active"},
        )
        self.async_write_ha_state()

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class NoPdModeSwitch(SwitchEntity):
    """Switch to enable no-PD direct-tracking mode.

    When on, the controller tracks the consumption sensor 1:1 (proportional gain 1,
    no integral, derivative, smoothing, rate limiter or directional hysteresis). It
    reuses the deadband, min charge/discharge power, relay min-ON dwell and grid
    setpoint sliders, plus the No-PD Command Delay slider. Off restores normal PD.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        """Initialize the no-PD mode switch."""
        self.hass = hass
        self.entry = entry
        self.controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "no_pd_mode"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}no_pd_mode"
        self.entity_id = system_entity_id("switch", "no_pd_mode")
        self._attr_icon = "mdi:sine-wave"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True if no-PD direct-tracking mode is active."""
        return self.controller.no_pd_mode_enabled

    async def _set_enabled(self, enabled: bool) -> None:
        """Persist the flag and hot-reload the controller parameters."""
        new_data = dict(self.entry.data)
        new_data[CONF_NO_PD_MODE_ENABLED] = enabled
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)

        # Apply under the control lock so a running cycle never sees a half-applied
        # parameter swap. update_pd_parameters re-reads config and (re)applies the
        # no-PD overrides; reset the PD transient state so neither law inherits a
        # stale derivative/integral across the switch.
        async with self.controller._control_lock:
            self.controller._cancel_no_pd_debounced_run()
            self.controller.no_pd_mode_enabled = enabled
            self.controller.update_pd_parameters()
            self.controller.error_integral = 0.0
            self.controller.previous_error = 0.0
            self.controller.derivative_filtered = 0.0
            self.controller.sign_changes = 0
        _LOGGER.info("No-PD direct-tracking mode %s", "ENABLED" if enabled else "DISABLED")
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        """Enable no-PD direct-tracking mode."""
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable no-PD mode and restore normal PD control."""
        await self._set_enabled(False)

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class SystemPowerLimitsSwitch(SwitchEntity):
    """Switch to enable/disable the system-wide combined power limits at runtime."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        """Initialize the system power limits switch."""
        self.hass = hass
        self.entry = entry
        self.controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "system_power_limits"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}system_power_limits"
        self.entity_id = system_entity_id("switch", "system_power_limits")
        self._attr_icon = "mdi:speedometer"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True if the system power limits are enforced."""
        return self.controller.enable_system_power_limits

    async def async_turn_on(self, **kwargs) -> None:
        """Enable the system-wide combined charge/discharge power limits."""
        self.controller.enable_system_power_limits = True
        new_data = dict(self.entry.data)
        new_data[CONF_ENABLE_SYSTEM_POWER_LIMITS] = True
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("System power limits ENABLED")
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable the system-wide combined charge/discharge power limits."""
        self.controller.enable_system_power_limits = False
        new_data = dict(self.entry.data)
        new_data[CONF_ENABLE_SYSTEM_POWER_LIMITS] = False
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("System power limits DISABLED")
        self.async_write_ha_state()

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class HourlyBalanceSwitch(SwitchEntity):
    """Switch to enable/disable the hourly balance feature at runtime."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        """Initialize the hourly balance switch."""
        self.hass = hass
        self.entry = entry
        self.controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "hourly_balance"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}hourly_balance"
        self.entity_id = system_entity_id("switch", "hourly_balance")
        self._attr_icon = "mdi:scale-balance"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True if hourly balance is enabled."""
        return self.controller.hourly_balance_enabled

    async def async_turn_on(self, **kwargs) -> None:
        """Enable hourly balance."""
        self.controller.hourly_balance_enabled = True
        new_data = dict(self.entry.data)
        new_data[CONF_ENABLE_HOURLY_BALANCE] = True
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("Hourly Balance ENABLED")
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable hourly balance."""
        self.controller.hourly_balance_enabled = False
        if self.controller._hourly_balance_mgr is not None:
            self.controller._hourly_balance_mgr.clear_offset()
        else:
            self.controller.remove_setpoint_offset("hourly_balance")
        new_data = dict(self.entry.data)
        new_data[CONF_ENABLE_HOURLY_BALANCE] = False
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("Hourly Balance DISABLED")
        self.async_write_ha_state()

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class MinSOCFloorSwitch(SwitchEntity):
    """Switch to enable/disable the guaranteed minimum SOC floor feature."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        self.hass = hass
        self.entry = entry
        self.controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "min_soc_floor_enabled"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}min_soc_floor_enabled"
        self.entity_id = system_entity_id("switch", "min_soc_floor_enabled")
        self._attr_icon = "mdi:battery-arrow-up"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        return self.controller._predictive_min_soc_floor_enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.controller._predictive_min_soc_floor_enabled = True
        new_data = dict(self.entry.data)
        new_data[CONF_ENABLE_MIN_SOC_FLOOR] = True
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("Guaranteed Minimum SOC floor ENABLED (floor: %.0f%%)",
                     self.controller._predictive_min_soc_floor)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.controller._predictive_min_soc_floor_enabled = False
        new_data = dict(self.entry.data)
        new_data[CONF_ENABLE_MIN_SOC_FLOOR] = False
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("Guaranteed Minimum SOC floor DISABLED")
        self.async_write_ha_state()

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class WeeklyFullChargeEnableSwitch(SwitchEntity):
    """Switch to enable/disable the weekly full (100%) charge for cell balancing."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        """Initialize the weekly full charge enable switch."""
        self.hass = hass
        self.entry = entry
        self.controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "weekly_full_charge_enabled"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}weekly_full_charge_enabled"
        self.entity_id = system_entity_id("switch", "weekly_full_charge_enabled")
        self._attr_icon = "mdi:calendar-check"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True if the weekly full charge is enabled."""
        return self.controller.weekly_full_charge_enabled

    def _set_enabled(self, enabled: bool) -> None:
        """Persist the flag; update_pd_parameters syncs the controller + reset state."""
        new_data = dict(self.entry.data)
        new_data[CONF_ENABLE_WEEKLY_FULL_CHARGE] = enabled
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        # Re-reads CONF_ENABLE_WEEKLY_FULL_CHARGE and handles the disable->reset
        # (mid-charge hardware restore) transition.
        self.controller.update_pd_parameters()
        _LOGGER.info("Weekly Full Charge %s", "ENABLED" if enabled else "DISABLED")
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        """Enable the weekly full charge."""
        self._set_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable the weekly full charge."""
        self._set_enabled(False)

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class PriceDischargeControlSwitch(SwitchEntity):
    """Switch gating battery discharge on the electricity price being above threshold.

    Two variants share this class: ``dp`` (dynamic pricing) and ``rt`` (real-time
    price). The pricing engine reads the matching controller flag
    (``dp_price_discharge_control`` / ``rt_price_discharge_control``) live each cycle.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller, kind: str) -> None:
        """Initialize. kind must be 'dp' or 'rt'."""
        self.hass = hass
        self.entry = entry
        self.controller = controller
        self._kind = kind
        if kind == "dp":
            self._attr_translation_key = "dp_price_discharge_control"
            self._conf_key = CONF_DP_PRICE_DISCHARGE_CONTROL
            self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}dp_price_discharge_control"
            self.entity_id = system_entity_id("switch", "dp_price_discharge_control")
        else:
            self._attr_translation_key = "rt_price_discharge_control"
            self._conf_key = CONF_RT_PRICE_DISCHARGE_CONTROL
            self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}rt_price_discharge_control"
            self.entity_id = system_entity_id("switch", "rt_price_discharge_control")

        self._attr_has_entity_name = True
        self._attr_icon = "mdi:cash-clock"
        self._attr_should_poll = False

    @property
    def is_on(self) -> bool:
        """Return True if price-based discharge control is active."""
        if self._kind == "dp":
            return self.controller.dp_price_discharge_control
        return self.controller.rt_price_discharge_control

    def _set_enabled(self, enabled: bool) -> None:
        """Set the controller flag and persist it."""
        if self._kind == "dp":
            self.controller.dp_price_discharge_control = enabled
        else:
            self.controller.rt_price_discharge_control = enabled
        new_data = dict(self.entry.data)
        new_data[self._conf_key] = enabled
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("Price-based discharge control (%s) %s",
                     self._kind, "ENABLED" if enabled else "DISABLED")
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        """Enable price-based discharge control."""
        self._set_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable price-based discharge control."""
        self._set_enabled(False)

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


