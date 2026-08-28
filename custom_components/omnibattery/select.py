"""Select platform for the Omnibattery integration."""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    CONF_BATTERY_PHASE,
    CONF_THREE_PHASE_ENABLED,
    CONF_CHARGE_PRIORITY,
    DEFAULT_CHARGE_PRIORITY,
    CONF_PRIMARY_BATTERY,
    DEFAULT_PRIMARY_BATTERY,
    CONF_WEEKLY_FULL_CHARGE_DAY,
    DEFAULT_THREE_PHASE_ENABLED,
    PHASE_ASSIGNMENT_VALUES,
    PHASE_UNASSIGNED,
    CONF_PD_TUNING_PROFILE,
    PD_PROFILE_CUSTOM,
    PD_TUNING_PROFILES,
    PD_TUNING_PROFILE_OPTIONS,
    normalize_battery_phase,
    pd_profile_from_params,
)
from .infra.coordinator import MarstekVenusDataUpdateCoordinator
from .infra.entity_naming import english_entity_id, system_entity_id, SYSTEM_UNIQUE_ID_PREFIX
from .infra.manual_control import assert_manual_control

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the select platform."""
    coordinators: list[MarstekVenusDataUpdateCoordinator] = hass.data[DOMAIN][entry.entry_id]["coordinators"]
    entities = []

    # Add Modbus register selects (per battery)
    for coordinator in coordinators:
        # Physical phase is metadata for the safety limiter, not a battery
        # register. It remains available as a live selector whenever the global
        # protection switch is on.
        entities.append(BatteryPhaseSelect(hass, entry, coordinator))
        for definition in coordinator.select_definitions:
            entities.append(MarstekVenusSelect(coordinator, definition))
        # Drivers without a force_mode register (Zendure) get a software force
        # mode; the controller applies it via apply_setpoint while global manual
        # mode or this battery's individual manual ownership is active.
        if coordinator.needs_software_manual_control:
            entities.append(MarstekManualForceModeSelect(coordinator))

    # Add weekly full charge day select (system-level, always present: the enable
    # switch is always available, so the day must be pickable before enabling —
    # toggling the switch does not reload platforms).
    entities.append(WeeklyFullChargeDaySelect(hass, entry))

    # Add PD tuning profile select (system-level, always available)
    entities.append(PdTuningProfileSelect(hass, entry))

    # Which battery serves the house first. Only meaningful with more than one.
    if len(entry.data.get("batteries", [])) > 1:
        entities.append(PrimaryBatterySelect(hass, entry))
        entities.append(ChargePrioritySelect(hass, entry))

    async_add_entities(entities)


class BatteryPhaseSelect(CoordinatorEntity, SelectEntity):
    """Live physical-phase assignment for one battery."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: MarstekVenusDataUpdateCoordinator,
    ) -> None:
        """Initialize the phase selector."""
        super().__init__(coordinator)
        self.hass = hass
        self.entry = entry

        self._attr_has_entity_name = True
        self._attr_translation_key = "battery_phase"
        self._attr_unique_id = f"{coordinator.device_key}_battery_phase"
        self.entity_id = english_entity_id("select", coordinator.name, "battery_phase")
        self._attr_options = list(PHASE_ASSIGNMENT_VALUES)
        self._attr_icon = "mdi:transmission-tower"
        self._attr_should_poll = False

    @property
    def current_option(self) -> str:
        """Return the normalized live phase assignment."""
        return normalize_battery_phase(
            getattr(self.coordinator, "phase", PHASE_UNASSIGNED)
        )

    @property
    def available(self) -> bool:
        """Expose the selector only while three-phase protection is active."""
        coordinator_available = getattr(self.coordinator, "available", None)
        if coordinator_available is None:
            is_available = getattr(self.coordinator, "is_available", None)
            coordinator_available = (
                is_available() if callable(is_available) else True
            )
        return bool(
            coordinator_available
            and self.entry.data.get(
                CONF_THREE_PHASE_ENABLED, DEFAULT_THREE_PHASE_ENABLED
            )
        )

    async def async_select_option(self, option: str) -> None:
        """Update the phase metadata and persist it for the next restart."""
        if option not in self._attr_options:
            raise ValueError(f"Invalid battery phase: {option}")
        phase = normalize_battery_phase(option)
        self.coordinator.phase = phase
        self.coordinator.persist_battery_config(CONF_BATTERY_PHASE, phase)
        _LOGGER.info("%s: battery phase set to %s", self.coordinator.name, phase)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Refresh availability when the global protection switch changes."""
        self.async_on_remove(self.entry.add_update_listener(self._handle_entry_update))

    async def _handle_entry_update(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Re-render the selector after a config-entry update."""
        self.async_write_ha_state()

    @property
    def device_info(self):
        """Return device information for the battery."""
        return self.coordinator.battery_device_info


class MarstekVenusSelect(CoordinatorEntity, SelectEntity):
    """Representation of a Marstek Venus select."""

    def __init__(
        self, coordinator: MarstekVenusDataUpdateCoordinator, definition: dict
    ) -> None:
        """Initialize the select."""
        super().__init__(coordinator)
        self.definition = definition
        
        self._attr_has_entity_name = True
        self._attr_translation_key = definition["key"]
        self._attr_unique_id = f"{coordinator.device_key}_{definition['key']}"
        self.entity_id = english_entity_id("select", coordinator.name, definition["key"])
        self._attr_options = list(definition["options"].keys())
        self._attr_entity_registry_enabled_default = definition.get("enabled_by_default", True)
        self._attr_should_poll = False
        self._options_map = definition["options"]

    @property
    def current_option(self):
        """Return the current option."""
        if self.definition.get("use_shadow_state"):
            shadow = self.coordinator.get_shadow_select(self.definition["key"])
            if shadow is not None:
                for option, val in self._options_map.items():
                    if val == shadow:
                        return option
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self.definition["key"])
        for option, val in self._options_map.items():
            if val == value:
                return option
        return None

    async def async_select_option(self, option: str) -> None:
        """Select an option.

        Force Mode is re-asserted by the control loop every cycle, so writing it
        while the controller owns the battery is refused rather than silently
        reverted (see infra.manual_control).
        """
        assert_manual_control(self.hass, self.coordinator, self.definition["key"])
        value = self._options_map[option]
        await self.coordinator.write_control(self.definition["key"], value, do_refresh=True)
        if self.definition.get("use_shadow_state"):
            self.coordinator.set_shadow_select(self.definition["key"], value)

    @property
    def device_info(self):
        """Return device information."""
        return self.coordinator.battery_device_info


WEEKDAY_OPTIONS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
# Map full names to internal short codes used in config_entry.data and WEEKDAY_MAP
WEEKDAY_TO_CODE = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu",
    "friday": "fri", "saturday": "sat", "sunday": "sun",
}
CODE_TO_WEEKDAY = {v: k for k, v in WEEKDAY_TO_CODE.items()}


class WeeklyFullChargeDaySelect(SelectEntity):
    """Select entity to choose the day for weekly full charge."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the weekly full charge day select."""
        self.hass = hass
        self.entry = entry

        self._attr_has_entity_name = True
        self._attr_translation_key = "weekly_full_charge_day"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}weekly_full_charge_day"
        self.entity_id = system_entity_id("select", "weekly_full_charge_day")
        self._attr_icon = "mdi:calendar-week"
        self._attr_options = WEEKDAY_OPTIONS
        self._attr_should_poll = False

    @property
    def current_option(self) -> str:
        """Return the currently selected day as full name."""
        code = self.entry.data.get(CONF_WEEKLY_FULL_CHARGE_DAY, "sun")
        return CODE_TO_WEEKDAY.get(code, "sunday")

    async def async_select_option(self, option: str) -> None:
        """Update the selected day in config_entry.data."""
        code = WEEKDAY_TO_CODE.get(option, option)
        new_data = dict(self.entry.data)
        new_data[CONF_WEEKLY_FULL_CHARGE_DAY] = code
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("Weekly full charge day updated to %s (%s)", option, code)
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


class PrimaryBatterySelect(SelectEntity):
    """Which battery serves the house first.

    Discharge normally goes to the fullest battery. Nominating a primary puts
    that one ahead of the ladder, and it is the battery the house-load
    feedforward addresses when that switch is on.
    """

    _AUTOMATIC = "automatic"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the primary battery select."""
        self.hass = hass
        self.entry = entry

        self._attr_has_entity_name = True
        self._attr_translation_key = "primary_battery"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}primary_battery"
        self.entity_id = system_entity_id("select", "primary_battery")
        self._attr_icon = "mdi:numeric-1-box-outline"
        self._attr_should_poll = False

    @property
    def options(self) -> list[str]:
        """Automatic, plus every configured battery by name."""
        names = [
            battery.get("name", "")
            for battery in self.entry.data.get("batteries", [])
            if battery.get("name")
        ]
        return [self._AUTOMATIC, *names]

    @property
    def current_option(self) -> str:
        """Return the nominated battery, or automatic when none is set."""
        name = self.entry.data.get(CONF_PRIMARY_BATTERY, DEFAULT_PRIMARY_BATTERY)
        return name if name in self.options else self._AUTOMATIC

    async def async_select_option(self, option: str) -> None:
        """Persist the choice and hand it to the running controller."""
        name = "" if option == self._AUTOMATIC else option
        new_data = dict(self.entry.data)
        new_data[CONF_PRIMARY_BATTERY] = name
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)

        # The running controller reads this per cycle; updating it here avoids
        # waiting for a reload to take effect.
        controller = (
            self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {}).get("controller")
        )
        if controller is not None:
            controller.primary_battery = name
        _LOGGER.info("Primary battery set to %s", name or "automatic (highest SOC)")
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


class ChargePrioritySelect(SelectEntity):
    """Which battery is filled first.

    Left automatic, the order follows the day: with sun enough for everything
    the battery needing the most hours goes first, and on a thin day the one
    that loses the least to conversion.
    """

    _AUTOMATIC = "automatic"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the charge priority select."""
        self.hass = hass
        self.entry = entry

        self._attr_has_entity_name = True
        self._attr_translation_key = "charge_priority"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}charge_priority"
        self.entity_id = system_entity_id("select", "charge_priority")
        self._attr_icon = "mdi:battery-arrow-up"
        # The order and the hours-to-full it reports are recomputed every cycle;
        # unpolled they would freeze at the moment the selection last changed.
        self._attr_should_poll = True

    @property
    def options(self) -> list[str]:
        """Automatic, plus every configured battery by name."""
        names = [
            battery.get("name", "")
            for battery in self.entry.data.get("batteries", [])
            if battery.get("name")
        ]
        return [self._AUTOMATIC, *names]

    @property
    def current_option(self) -> str:
        """Return the nominated battery, or automatic when none is set."""
        name = self.entry.data.get(CONF_CHARGE_PRIORITY, DEFAULT_CHARGE_PRIORITY)
        return name if name in self.options else self._AUTOMATIC

    async def async_select_option(self, option: str) -> None:
        """Persist the choice and hand it to the running controller."""
        name = "" if option == self._AUTOMATIC else option
        new_data = dict(self.entry.data)
        new_data[CONF_CHARGE_PRIORITY] = name
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)

        controller = (
            self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {}).get("controller")
        )
        if controller is not None:
            controller.charge_priority = name
        _LOGGER.info("Charge priority set to %s", name or "automatic")
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        """Show the order in force and what decided it."""
        from . import _charge_order, _scarce_solar_day, _time_to_full_h

        controller = (
            self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {}).get("controller")
        )
        if controller is None:
            return {}
        batteries = list(getattr(controller, "coordinators", []))
        return {
            "order": [c.name for c in _charge_order(controller, batteries)],
            "hours_to_full": {
                c.name: round(_time_to_full_h(controller, c), 1) for c in batteries
            },
            "thin_solar_day": _scarce_solar_day(controller),
        }

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class PdTuningProfileSelect(SelectEntity):
    """One-click PD tuning presets (system-level).

    Selecting a preset writes its PD gain parameters (Kp, Kd, max power change)
    into config_entry.data; the integration's existing config-entry update listener
    then hot-reloads them. Deadband is intentionally left to the user. The "custom"
    option leaves the sliders for manual fine-tuning. The displayed option is derived
    from the live parameters, so moving a profiled slider by hand falls back to
    "custom" automatically.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the PD tuning profile select."""
        self.hass = hass
        self.entry = entry

        self._attr_has_entity_name = True
        self._attr_translation_key = "pd_tuning_profile"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}pd_tuning_profile"
        self.entity_id = system_entity_id("select", "pd_tuning_profile")
        self._attr_icon = "mdi:tune-variant"
        self._attr_options = list(PD_TUNING_PROFILE_OPTIONS)
        self._attr_should_poll = False

    @property
    def current_option(self) -> str:
        """Return the active profile.

        An explicit "custom" selection sticks; otherwise the option is detected
        from the live parameters so a manual slider change reflects as "custom".
        """
        if self.entry.data.get(CONF_PD_TUNING_PROFILE) == PD_PROFILE_CUSTOM:
            return PD_PROFILE_CUSTOM
        return pd_profile_from_params(self.entry.data)

    async def async_select_option(self, option: str) -> None:
        """Apply a profile (writes its gain params) or switch to manual mode."""
        new_data = dict(self.entry.data)
        new_data[CONF_PD_TUNING_PROFILE] = option
        if option != PD_PROFILE_CUSTOM:
            new_data.update(PD_TUNING_PROFILES[option])
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        # The entry's update listener hot-reloads the controller's PD params.
        _LOGGER.info("PD tuning profile set to %s", option)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Refresh displayed option whenever the config entry changes.

        Manual PD slider moves update config_entry.data via the number entities;
        this keeps the profile select in sync (falling back to "custom").
        """
        self.async_on_remove(self.entry.add_update_listener(self._handle_entry_update))

    async def _handle_entry_update(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Re-render the current option after a config entry update."""
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


# Software force mode: same option strings as the Marstek force_mode register
# select so the existing translations / dashboard label apply unchanged.
MANUAL_FORCE_MODE_OPTIONS = ["None", "Charge", "Discharge"]


class MarstekManualForceModeSelect(CoordinatorEntity, SelectEntity):
    """Software force mode for drivers without a force_mode register.

    Stores the choice on the coordinator; while the global Manual Mode switch or
    this battery's individual manual switch is on, the controller drives the
    battery to the matching charge/discharge setpoint via apply_setpoint (see
    _apply_software_manual_setpoints). "None" leaves the battery idle.
    """

    def __init__(self, coordinator: MarstekVenusDataUpdateCoordinator) -> None:
        """Initialize the software force-mode select."""
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_translation_key = "force_mode"
        self._attr_unique_id = f"{coordinator.device_key}_force_mode"
        self.entity_id = english_entity_id("select", coordinator.name, "force_mode")
        self._attr_options = MANUAL_FORCE_MODE_OPTIONS
        self._attr_icon = "mdi:gesture-tap-button"
        self._attr_should_poll = False

    @property
    def current_option(self) -> str:
        """Return the live force mode derived from the commanded power, mirroring
        the Marstek force_mode register (which the controller overwrites)."""
        if self.coordinator.commanded_charge_power > 0:
            return "Charge"
        if self.coordinator.commanded_discharge_power > 0:
            return "Discharge"
        # A user may choose the direction before entering a non-zero power. Keep
        # that intent visible instead of immediately snapping back to None.
        stored = self.coordinator.manual_force_mode
        return stored if stored in MANUAL_FORCE_MODE_OPTIONS else "None"

    async def async_select_option(self, option: str) -> None:
        """Store the manual force mode and reflect it now.

        The optimistic commanded update (using the stored manual power for the
        chosen direction) keeps the select on the picked option until the next
        control cycle re-asserts it, instead of snapping back.
        """
        self.coordinator.manual_force_mode = option
        if option == "Charge":
            self.coordinator.commanded_charge_power = self.coordinator.manual_set_charge_power
            self.coordinator.commanded_discharge_power = 0
        elif option == "Discharge":
            self.coordinator.commanded_charge_power = 0
            self.coordinator.commanded_discharge_power = self.coordinator.manual_set_discharge_power
        else:
            # Stop the active manual command once. The controller deliberately
            # does not reassert 0 W on later idle cycles, so Anker users can then
            # select another Solix operating mode without OmniBattery reverting it.
            await self.coordinator.apply_power(0, read_back=False)
            await self.coordinator.async_request_refresh()
            self.coordinator.commanded_charge_power = 0
            self.coordinator.commanded_discharge_power = 0
        self.coordinator.persist_battery_config("manual_force_mode", option)
        _LOGGER.info("%s: manual_force_mode → %s", self.coordinator.name, option)
        self.async_write_ha_state()

    @property
    def device_info(self):
        """Return device information."""
        return self.coordinator.battery_device_info
