"""Sensor platform for the Omnibattery integration."""
from __future__ import annotations

import inspect
import logging
import math
import time
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .infra.entity_naming import english_entity_id, system_entity_id, SYSTEM_UNIQUE_ID_PREFIX
from .const import (
    DOMAIN,
    EFFICIENCY_SENSOR_DEFINITIONS,
    STORED_ENERGY_SENSOR_DEFINITIONS,
    CYCLE_SENSOR_DEFINITIONS,
    SOLAR_POWER_SENSOR_DEFINITIONS,
    BATTERY_CELL_POWER_SENSOR_DEFINITIONS,
    CONF_ENABLE_CHARGE_DELAY,
    CONF_ENABLE_WEEKLY_FULL_CHARGE_DELAY,
    SLOT_BATTERY_SCOPE_ALL,
    DEFAULT_SLOT_MODE,
    DEFAULT_SLOT_ALLOW_CHARGE,
    DEFAULT_SLOT_ALLOW_DISCHARGE,
)
from .infra.coordinator import MarstekVenusDataUpdateCoordinator
from .drivers.base import has_connected_mppt_pv
from .energy import (
    BACKUP_DISCHARGING_ENERGY_KEY,
    effective_total_discharging_energy,
)
from .tracking.consumption_profile import INTERVAL_COUNT, INTERVAL_MINUTES
from .sensors.aggregate_sensors import AGGREGATE_SENSOR_DEFINITIONS, SYSTEM_BATTERY_CELL_POWER_DEFINITION, MarstekVenusAggregateSensor, DailyGridAtMinSocSensor, SystemAlarmSensor, PdControlQualitySensor
from .sensors.calculated_sensors import (
    MarstekVenusEfficiencySensor,
    MarstekVenusStoredEnergySensor,
    MarstekVenusCycleSensor,
    MarstekVenusSolarPowerSensor,
    MarstekVenusBatteryCellPowerSensor,
    SyntheticEnergySensor,
    CumulativeDailyEnergySensor,
    SyntheticCapacitySensor,
    ZendurePackSensor,
    SYNTHETIC_ENERGY_SENSOR_DEFINITIONS,
    CUMULATIVE_DAILY_ENERGY_SENSOR_DEFINITIONS,
)

_LOGGER = logging.getLogger(__name__)


def _remove_obsolete_solar_entities(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    coordinators: list[MarstekVenusDataUpdateCoordinator],
) -> None:
    """Remove PV entities that no longer belong to the configured topology.

    Entity setup is additive from Home Assistant's point of view: omitting a
    definition on reload does not remove an old registry entry. Clean model-
    incompatible Anker solar entities and Venus A/D calculated PV entities when
    the user declares that no panels are connected.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for coordinator in coordinators:
        if (
            getattr(coordinator, "brand", None) == "marstek"
            and getattr(coordinator.capabilities, "has_mppt_pv", False)
            and not has_connected_mppt_pv(coordinator)
        ):
            for suffix in ("solar_power", "battery_cell_power"):
                unique_id = f"{coordinator.device_key}_{suffix}"
                entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
                registered = registry.async_get(entity_id) if entity_id else None
                if registered and registered.config_entry_id == config_entry.entry_id:
                    registry.async_remove(entity_id)
        if getattr(coordinator, "brand", None) != "anker":
            continue
        if getattr(getattr(coordinator, "driver", None), "has_independent_pv", False):
            continue
        for unique_id in (
            f"{coordinator.device_key}_solar_power",
            f"{coordinator.host}_solar_power",
            f"{coordinator.name}_solar_power",
        ):
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            registered = registry.async_get(entity_id) if entity_id else None
            if registered and registered.config_entry_id == config_entry.entry_id:
                registry.async_remove(entity_id)

    # The system aggregate is only meaningful when at least one connected
    # battery contributes independent PV. Existing installations may still
    # have this registry entry after upgrading from 1.4.0b4; remove it so the
    # configured external sensor remains the sole source for AC-only Anker.
    has_independent_solar = any(
        bool(
            has_connected_mppt_pv(coordinator)
            or getattr(
                getattr(coordinator, "capabilities", None),
                "has_solar_telemetry",
                False,
            )
        )
        for coordinator in coordinators
    )
    if not has_independent_solar:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{SYSTEM_UNIQUE_ID_PREFIX}solar_power"
        )
        registered = registry.async_get(entity_id) if entity_id else None
        if registered and registered.config_entry_id == config_entry.entry_id:
            registry.async_remove(entity_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinators: list[MarstekVenusDataUpdateCoordinator] = hass.data[DOMAIN][entry.entry_id]["coordinators"]

    _remove_obsolete_solar_entities(hass, entry, coordinators)

    entities = []

    # Add individual battery sensors. The driver owns the per-platform split, so
    # use its sensor_definitions directly (same pattern as number.py). The old
    # _all_definitions filter required a "register" field, which silently dropped
    # property-based drivers (Zendure) whose sensor defs carry no register.
    for coordinator in coordinators:
        for definition in coordinator.sensor_definitions:
            entities.append(MarstekVenusSensor(coordinator, definition))

    # Add aggregate sensors. Created even for a single-battery system so the
    # "Marstek Venus System" device never exposes `unavailable` entities — with
    # one battery each aggregate simply mirrors that battery's value.
    for definition in AGGREGATE_SENSOR_DEFINITIONS:
        entities.append(MarstekVenusAggregateSensor(coordinators, definition, entry, hass))

    # System alarm sensor — only for batteries that expose alarm/fault registers (v2)
    alarm_coordinators = [c for c in coordinators if c.capabilities.has_alarm_registers]
    if alarm_coordinators:
        entities.append(SystemAlarmSensor(alarm_coordinators))

    # Add calculated sensors (efficiency, stored energy, cycle count) per battery
    for coordinator in coordinators:
        for definition in EFFICIENCY_SENSOR_DEFINITIONS:
            entities.append(MarstekVenusEfficiencySensor(coordinator, definition))
        for definition in STORED_ENERGY_SENSOR_DEFINITIONS:
            entities.append(MarstekVenusStoredEnergySensor(coordinator, definition))
        for definition in CYCLE_SENSOR_DEFINITIONS:
            entities.append(MarstekVenusCycleSensor(coordinator, definition))
        # Drivers without hardware energy counters (Zendure): synthesise the
        # charge/discharge energy totals by integrating power, and expose per-pack
        # telemetry sized to the live pack count (the first refresh already ran).
        if not coordinator.capabilities.has_energy_counters:
            for definition in SYNTHETIC_ENERGY_SENSOR_DEFINITIONS:
                entities.append(SyntheticEnergySensor(coordinator, definition))
            entities.append(SyntheticCapacitySensor(coordinator))
        elif not coordinator.capabilities.has_daily_energy_counters:
            for definition in CUMULATIVE_DAILY_ENERGY_SENSOR_DEFINITIONS:
                entities.append(CumulativeDailyEnergySensor(coordinator, definition))
        pack_specs = getattr(coordinator.driver, "pack_field_specs", None)
        if pack_specs:
            data = coordinator.data or {}
            pack_count = sum(1 for i in range(1, 33) if f"pack{i}_soc" in data)
            for pack_index in range(1, pack_count + 1):
                for spec in pack_specs:
                    entities.append(ZendurePackSensor(coordinator, pack_index, spec))
        # DC-coupled PV total + solar-corrected battery power exist only on
        # units that expose independent DC-coupled PV telemetry. Venus D/A
        # exposes individual MPPT inputs. Verified Anker E5000 entities expose
        # their aggregate ``solar_power`` through the driver definitions; Max/XE
        # do not create that per-battery entity.
        if has_connected_mppt_pv(coordinator):
            for definition in SOLAR_POWER_SENSOR_DEFINITIONS:
                entities.append(MarstekVenusSolarPowerSensor(coordinator, definition))
            for definition in BATTERY_CELL_POWER_SENSOR_DEFINITIONS:
                entities.append(MarstekVenusBatteryCellPowerSensor(coordinator, definition))

    # Add discharge window diagnostic sensor (always, even without slots)
    entities.append(DischargeWindowSensor(hass, entry))

    # Add active batteries diagnostic sensor. The controller updates its
    # load-sharing tracking even for a single battery (see
    # _select_batteries_for_operation), so this reflects charging/discharging/idle
    # instead of staying unavailable.
    controller = hass.data[DOMAIN][entry.entry_id].get("controller")
    # The timeline is deliberately registered whenever the controller exists.
    # Its manager is optional during startup and the entity reads it lazily, so
    # adding this sensor cannot make older controllers fail setup.
    if controller is not None:
        entities.append(DailyOperationTimelineSensor(controller))

    if controller:
        entities.append(ActiveBatteriesSensor(hass, entry, controller, coordinators))

    # Add the three-phase protection status and per-phase diagnostic sensor.
    if controller:
        entities.append(ThreePhaseProtectionSensor(hass, entry, controller))

    # Add weekly full charge status sensor (when weekly charge is enabled)
    if controller and controller.weekly_full_charge_enabled:
        entities.append(WeeklyFullChargeSensor(hass, entry, controller))

    # Add charge delay sensor (when charge delay is configured, regardless of enabled state)
    has_charge_delay_config = (
        CONF_ENABLE_CHARGE_DELAY in entry.data
        or CONF_ENABLE_WEEKLY_FULL_CHARGE_DELAY in entry.data
    )
    if controller and has_charge_delay_config:
        entities.append(ChargeDelaySensor(hass, entry, controller))

    # Add integration status sensor (always, when controller is present)
    if controller:
        entities.append(IntegrationStatusSensor(hass, entry, controller))

    # Add non-responsive batteries sensor (always, when controller is present)
    if controller:
        entities.append(NonResponsiveBatteriesSensor(hass, entry, controller, coordinators))

    # Add daily grid-at-min-soc energy sensor (feeds into consumption estimation)
    if controller:
        entities.append(DailyGridAtMinSocSensor(controller))

    # Add PD control-quality diagnostic sensor (feeds the tuning-profile feedback)
    if controller:
        entities.append(PdControlQualitySensor(controller))

    # Exact daily energy totals from the real power sensors (panel "Energía hoy").
    # Each is added only when its source sensor is configured.
    # Daily solar = external solar sensor + independent battery-reported
    # DC-coupled PV (individual MPPT channels or verified Anker aggregate).
    has_solar_telemetry = any(
        bool(
            has_connected_mppt_pv(c)
            or getattr(
                getattr(c, "capabilities", None), "has_solar_telemetry", False
            )
        )
        for c in coordinators
    )
    if controller and (
        getattr(controller, "solar_production_sensor", None) or has_solar_telemetry
    ):
        entities.append(DailySolarEnergySensor(controller))
    # Live total solar power (external sensor + battery-reported DC PV). Only
    # useful when a battery actually has DC-coupled PV; without it the sensor
    # would just mirror the external sensor.
    if controller and has_solar_telemetry:
        entities.append(SystemSolarPowerSensor(controller))
    # Signed system battery power (+charge / -discharge). Always present so the
    # flow-diagram battery node and SOC card blocks can link to a single signed
    # aggregate instead of the unsigned system_charge_power. MPPT is included when
    # available; non-MPPT systems fall back to battery_power per coordinator.
    entities.append(MarstekVenusAggregateSensor(coordinators, SYSTEM_BATTERY_CELL_POWER_DEFINITION, entry, hass))
    # The daily home total is derived from the (always-present) net grid meter:
    # grid + battery AC + solar, matching the power-flow Home Consumption sensor.
    if controller and getattr(controller, "consumption_sensor", None):
        entities.append(DailyHomeEnergySensor(controller))
    # Grid import/export are sign-split from the net consumption meter, which is
    # always configured, so these are always added.
    if controller and getattr(controller, "consumption_sensor", None):
        entities.append(DailyGridImportEnergySensor(controller))
        entities.append(DailyGridExportEnergySensor(controller))

    # Quarter-hour household profile.  It is diagnostic-only and remains
    # available even while it is learning; control consumers explicitly inspect
    # the maturity/source metadata before using it.
    if controller and getattr(controller, "_consumption_tracker", None) is not None:
        entities.append(ConsumptionProfileCaptureSensor(controller))
        entities.append(ConsumptionProfileSensor(controller))



    async_add_entities(entities)

    # Balance monitor sensors (registered separately so they get their own setup call)
    from .sensors import balance_sensors as _balance_sensors
    await _balance_sensors.async_setup_entry(hass, entry, async_add_entities)

    # Hourly balance sensors
    from .sensors import hourly_balance_sensors as _hourly_balance_sensors
    await _hourly_balance_sensors.async_setup_entry(hass, entry, async_add_entities)


class MarstekVenusSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Marstek Venus sensor."""

    def __init__(
        self, coordinator: MarstekVenusDataUpdateCoordinator, definition: dict
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.definition = definition
        
        # Set entity attributes
        self._attr_has_entity_name = True
        self._attr_translation_key = definition["key"]
        self._attr_unique_id = f"{coordinator.device_key}_{definition['key']}"
        self.entity_id = english_entity_id("sensor", coordinator.name, definition["key"])
        self._attr_device_class = definition.get("device_class")
        self._attr_state_class = definition.get("state_class")
        self._attr_native_unit_of_measurement = definition.get("unit")
        self._attr_icon = definition.get("icon")
        self._attr_entity_registry_enabled_default = definition.get("enabled_by_default", True)
        if definition.get("category") == "diagnostic":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if "precision" in definition and (definition.get("unit") or definition.get("state_class")):
            self._attr_suggested_display_precision = definition["precision"]
        self._attr_should_poll = False

    @property
    def native_value(self):
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None
        key = self.definition["key"]
        value = self.coordinator.data.get(key)
        if key == "total_discharging_energy":
            corrected = effective_total_discharging_energy(self.coordinator.data)
            if corrected is not None:
                value = round(corrected, self.definition.get("precision", 2))
        
        if value is None:
            return None
        
        # Map numeric values to state names if available
        if "states" in self.definition:
            states = self.definition["states"]
            if value in states:
                return states[value]
            # Coordinators may store ints as floats after scale/round; try int key.
            try:
                ivalue = int(value)
            except (TypeError, ValueError):
                return value
            return states.get(ivalue, value)
        
        # For bit-described values, show which bits are active
        if "bit_descriptions" in self.definition:
            active_bits = []
            bit_descriptions = self.definition["bit_descriptions"]
            
            # Check bits based on data type
            max_bits = 64 if self.definition.get("data_type") == "uint64" else 32
            for bit_pos in range(max_bits):
                if value & (1 << bit_pos):
                    if bit_pos in bit_descriptions:
                        active_bits.append(bit_descriptions[bit_pos])
            
            if active_bits:
                return ", ".join(active_bits)
            else:
                return "No active alarms/faults"
        
        return value

    @property
    def extra_state_attributes(self):
        """Surface the driver's model label on the SOC sensor for the panel chip.

        The device-registry model is hardcoded "Venus", so the per-battery model
        (Marstek version / Zendure product) rides along here on the always-present
        battery_soc entity the panel already reads.
        """
        if self.definition["key"] == "total_discharging_energy":
            backup = self.coordinator.data.get(BACKUP_DISCHARGING_ENERGY_KEY)
            if backup is None:
                return None
            return {
                "hardware_discharging_energy": self.coordinator.data.get(
                    "total_discharging_energy"
                ),
                "backup_discharging_energy": backup,
            }
        if self.definition["key"] != "battery_soc":
            return None
        model = getattr(self.coordinator.driver, "model_label", None)
        attributes = {"model": model} if model else {}
        if getattr(self.coordinator.capabilities, "has_mppt_pv", False):
            attributes["dc_pv_connected"] = has_connected_mppt_pv(self.coordinator)
        return attributes or None

    @property
    def device_info(self):
        """Return device information."""
        return self.coordinator.battery_device_info


class DischargeWindowSensor(SensorEntity):
    """Diagnostic sensor showing whether we are currently inside an allowed discharge window."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the discharge window sensor."""
        self.hass = hass
        self.entry = entry

        self._attr_has_entity_name = True
        self._attr_translation_key = "discharge_window"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}discharge_window"
        self.entity_id = system_entity_id("sensor", "discharge_window")
        self._attr_icon = "mdi:clock-check-outline"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_should_poll = True

    @property
    def native_value(self) -> str:
        """Return the current discharge window status."""
        from datetime import datetime, time as dt_time

        all_slots = self.entry.data.get("no_discharge_time_slots", [])
        # Only slots that govern discharge define a discharge window. Charge-only
        # slots (allow_discharge=False) leave discharge unrestricted.
        enabled_slots = [
            s for s in all_slots
            if s.get("enabled", True) and s.get("allow_discharge", DEFAULT_SLOT_ALLOW_DISCHARGE)
        ]

        if not enabled_slots:
            return "no_slots"

        now = datetime.now()
        current_time = now.time()
        current_day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][now.weekday()]

        for i, slot in enumerate(enabled_slots):
            if current_day not in slot.get("days", []):
                continue
            try:
                start_time = dt_time.fromisoformat(slot["start_time"])
                end_time = dt_time.fromisoformat(slot["end_time"])
            except Exception:
                continue
            if start_time <= current_time <= end_time:
                return "active"

        return "inactive"

    @property
    def extra_state_attributes(self) -> dict:
        """Return configuration details of all time slots."""
        all_slots = self.entry.data.get("no_discharge_time_slots", [])
        enabled_slots = [
            s for s in all_slots
            if s.get("enabled", True) and s.get("allow_discharge", DEFAULT_SLOT_ALLOW_DISCHARGE)
        ]
        attrs = {
            "slots_configured": len(enabled_slots),
        }

        # Find active slot number
        from datetime import datetime, time as dt_time
        now = datetime.now()
        current_time = now.time()
        current_day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][now.weekday()]
        active_slot = None

        # Index over the full slot list so the number matches the panel's
        # "Franja N" (switch time_slot_{index}); skip slots that don't govern
        # discharge inline rather than enumerating the filtered list.
        for i, slot in enumerate(all_slots):
            if not slot.get("enabled", True) or not slot.get("allow_discharge", DEFAULT_SLOT_ALLOW_DISCHARGE):
                continue
            if current_day not in slot.get("days", []):
                continue
            try:
                start_time = dt_time.fromisoformat(slot["start_time"])
                end_time = dt_time.fromisoformat(slot["end_time"])
            except Exception:
                continue
            if start_time <= current_time <= end_time:
                active_slot = i + 1
                break

        attrs["active_slot"] = active_slot

        # Add details for each configured slot (all slots, not just enabled)
        for i, slot in enumerate(all_slots):
            n = i + 1
            days = slot.get("days", [])
            days_str = ", ".join(d.capitalize() for d in days) if days else "None"
            attrs[f"slot_{n}_schedule"] = f"{slot.get('start_time', '??')}-{slot.get('end_time', '??')}"
            attrs[f"slot_{n}_days"] = days_str
            attrs[f"slot_{n}_enabled"] = slot.get("enabled", True)
            attrs[f"slot_{n}_mode"] = slot.get("mode", DEFAULT_SLOT_MODE)
            attrs[f"slot_{n}_battery_scope"] = slot.get("battery_scope", SLOT_BATTERY_SCOPE_ALL)
            attrs[f"slot_{n}_allow_charge"] = slot.get("allow_charge", DEFAULT_SLOT_ALLOW_CHARGE)
            attrs[f"slot_{n}_allow_discharge"] = slot.get("allow_discharge", DEFAULT_SLOT_ALLOW_DISCHARGE)

        return attrs

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class ActiveBatteriesSensor(SensorEntity):
    """Diagnostic sensor showing which batteries are currently active in load sharing."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, controller, coordinators: list
    ) -> None:
        """Initialize the active batteries sensor."""
        self.hass = hass
        self.entry = entry
        self.controller = controller
        self._coordinators = coordinators

        self._attr_has_entity_name = True
        self._attr_translation_key = "active_batteries"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}active_batteries"
        self.entity_id = system_entity_id("sensor", "active_batteries")
        self._attr_icon = "mdi:battery-sync"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_should_poll = True

    @property
    def native_value(self) -> str:
        """Return a summary of active batteries."""
        discharge = self.controller._active_discharge_batteries
        charge = self.controller._active_charge_batteries

        if discharge:
            names = ", ".join(c.name for c in discharge)
            return f"Discharging: {names}"
        elif charge:
            names = ", ".join(c.name for c in charge)
            return f"Charging: {names}"
        return "Idle"

    @property
    def extra_state_attributes(self) -> dict:
        """Return detailed load sharing state."""
        discharge = self.controller._active_discharge_batteries
        charge = self.controller._active_charge_batteries
        total = len(self._coordinators)
        manual = [
            c.name for c in self._coordinators
            if getattr(c, "battery_manual_mode_enabled", False)
        ]
        automatic = [
            c.name for c in self._coordinators
            if not getattr(c, "battery_manual_mode_enabled", False)
        ]

        attrs = {
            "total_batteries": total,
            "manual_batteries": manual,
            "automatic_batteries": automatic,
            "discharge_active": len(discharge),
            "discharge_batteries": [c.name for c in discharge],
            "charge_active": len(charge),
            "charge_batteries": [c.name for c in charge],
        }

        # Add per-battery SOC and lifetime energy for context
        for c in self._coordinators:
            if c.data:
                soc = c.data.get("battery_soc", "N/A")
                discharge_kwh = effective_total_discharging_energy(c.data)
                if discharge_kwh is None:
                    discharge_kwh = "N/A"
                charge_kwh = c.data.get("total_charging_energy", "N/A")
                attrs[f"{c.name}_soc"] = f"{soc}%"
                attrs[f"{c.name}_total_discharged"] = f"{discharge_kwh} kWh"
                attrs[f"{c.name}_total_charged"] = f"{charge_kwh} kWh"

        return attrs

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class ThreePhaseProtectionSensor(SensorEntity):
    """Diagnostic sensor showing the live three-phase protection envelope."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        """Initialize the three-phase protection status sensor."""
        self.hass = hass
        self.entry = entry
        self._controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "three_phase_protection_status"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}three_phase_protection_status"
        self.entity_id = system_entity_id("sensor", "three_phase_protection_status")
        self._attr_icon = "mdi:shield-check-outline"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_should_poll = True

    def _diagnostics(self) -> dict:
        """Return a fresh snapshot so HA attributes reflect the latest readings."""
        limiter = getattr(self._controller, "_phase_power_limiter", None)
        if limiter is None:
            return {
                "state": "disabled",
                "enabled": False,
                "protection_enabled": False,
                "limited_batteries": [],
                "limited_battery_details": [],
                "unassigned_batteries": [],
                "degraded_phases": [],
                "phases": {},
            }
        return limiter.diagnostics()

    @property
    def native_value(self) -> str:
        """Return the translated protection status key."""
        return str(self._diagnostics().get("state", "disabled"))

    @property
    def extra_state_attributes(self) -> dict:
        """Return configuration, phase budgets and limited-battery details."""
        details = self._diagnostics()
        details.pop("state", None)
        return details

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class WeeklyFullChargeSensor(SensorEntity):
    """Diagnostic sensor showing weekly full charge status and delay calculations."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        """Initialize the weekly full charge sensor."""
        self.hass = hass
        self.entry = entry
        self._controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "weekly_full_charge"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}weekly_full_charge_status"
        self.entity_id = system_entity_id("sensor", "weekly_full_charge_status")
        self._attr_icon = "mdi:battery-clock"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_should_poll = True

    @property
    def native_value(self) -> str:
        """Return the current weekly charge status as a translation key."""
        state = self._controller._weekly_charge_status.get("state", "Idle")
        return {
            "Idle": "idle",
            "Disabled": "disabled",
            "Charging to 100%": "charging",
            "Complete": "complete",
        }.get(state, "idle")

    @property
    def extra_state_attributes(self) -> dict:
        """Return weekly charge details as attributes."""
        attrs = {
            "weekly_charge_day": self._controller.weekly_full_charge_day,
            "charge_delay_enabled": self._controller.charge_delay_enabled,
        }
        completion_reason = self._controller._weekly_charge_status.get("completion_reason")
        if completion_reason:
            attrs["completion_reason"] = completion_reason
        batteries = self._controller._weekly_charge_status.get("batteries")
        if batteries:
            attrs["batteries"] = batteries
        return attrs

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class ChargeDelaySensor(RestoreEntity, SensorEntity):
    """Sensor showing estimated charge start time for the unified charge delay.

    Shows the estimated unlock time as HH:MM or current delay status.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        """Initialize the charge delay sensor."""
        self.hass = hass
        self.entry = entry
        self._controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "charge_delay_status"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}charge_delay_status"
        self.entity_id = system_entity_id("sensor", "charge_delay_status")
        self._attr_icon = "mdi:clock-alert-outline"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_should_poll = True

    async def async_added_to_hass(self) -> None:
        """Restore same-day charge-delay latch state after integration reload."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is None:
            return

        same_day = (
            dt_util.as_local(last_state.last_updated).date()
            == dt_util.now().date()
        )
        if not same_day:
            return

        if (
            self._controller._delay_soc_setpoint_enabled
            and last_state.state in ("delayed", "waiting_for_solar", "charging_allowed")
        ):
            self._controller._delay_setpoint_reached = True
            _LOGGER.info("Charge Delay: restored SOC setpoint latch from previous state %s", last_state.state)

        if last_state.state == "charging_allowed":
            self._controller._charge_delay_unlocked = True
            _LOGGER.info("Charge Delay: restored same-day unlock state after reload")

    @property
    def native_value(self) -> str:
        """Return the charge delay state as a translation key."""
        status = self._controller._charge_delay_status
        state = status.get("state", "Idle")

        if state.startswith("Delayed"):
            return "delayed"

        if state.startswith("Waiting"):
            return "waiting_for_solar"

        if state.startswith("Unlocking") or state == "Charging allowed":
            return "charging_allowed"

        if state == "Skipped - Full Charge Day":
            return "skipped_full_charge_day"

        if state == "Charging to setpoint":
            return "charging_to_setpoint"

        return state.lower()  # "idle", "disabled"

    @property
    def extra_state_attributes(self) -> dict:
        """Return delay calculation details."""
        status = self._controller._charge_delay_status

        attrs = {
            "state": status.get("state", "Idle"),
            "target_soc": status.get("target_soc"),
            "soc_setpoint": status.get("soc_setpoint"),
            "safety_margin_min": status.get("safety_margin_min"),
        }

        for key in (
            "forecast_kwh", "solar_t_start", "solar_t_end",
            "energy_needed_kwh", "remaining_solar_kwh",
            "remaining_consumption_kwh", "net_solar_kwh",
            "consumption_forecast_source", "profile_coverage_ratio",
            "profile_days", "profile_fallback_reason", "solar_forecast_source",
            "solar_forecast_diagnostic_source",
            "solar_forecast_conversion", "charge_time_h", "estimated_unlock_time",
            "projected_unlock_time", "estimated_setpoint_time", "unlock_reason",
        ):
            value = status.get(key)
            if value is not None:
                attrs[key] = value

        return attrs

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class IntegrationStatusSensor(SensorEntity):
    """Primary status sensor showing what the integration is currently doing.

    Provides a single at-a-glance state representing the highest-priority
    active mode, from manual override down to normal PD control.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        """Initialize the integration status sensor."""
        self.hass = hass
        self.entry = entry
        self._controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "integration_status"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}integration_status"
        self.entity_id = system_entity_id("sensor", "integration_status")
        self._attr_icon = "mdi:home-battery"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_should_poll = True

    def _time_slot_blocked(self, direction: str) -> bool:
        """Return True when a time-slot whitelist blocks `direction` on every battery.

        Time-slot blockers are stored per-battery (`time_slot_charge` /
        `time_slot_discharge`), so the system-level status only reports the
        restriction when no available battery can act in that direction.
        """
        c = self._controller
        if direction == "discharge":
            getter, key = c.get_discharge_blockers, "time_slot_discharge"
        else:
            getter, key = c.get_charge_blockers, "time_slot_charge"
        coordinators = [
            coordinator
            for coordinator in c.coordinators
            if getattr(coordinator, "is_available", True)
            and not getattr(coordinator, "battery_manual_mode_enabled", False)
        ]
        if not coordinators:
            return False
        return all(key in getter(coordinator) for coordinator in coordinators)

    def _hourly_balance_state_key(self) -> str | None:
        """Return the integration-status key for hourly net balance activity."""
        c = self._controller
        mgr = getattr(c, "_hourly_balance_mgr", None)
        if mgr is None or not getattr(c, "hourly_balance_enabled", False):
            return None

        return {
            "compensating_import": "hourly_balance_import",
            "compensating_export": "hourly_balance_export",
            "capped": "hourly_balance_capped",
            "compensation_stopped": "hourly_balance_blocked",
        }.get(mgr.get_state_label())

    def _capacity_protection_state_key(self) -> str | None:
        """Return the integration-status key for peak-shaving activity."""
        c = self._controller
        if not getattr(c, "_capacity_protection_active", False):
            return None

        action = c._capacity_protection_status.get("action")
        return {
            "shaving": "peak_shaving",
            "shaving_excluded": "peak_shaving",
            "conserving": "capacity_conserving",
            "charging": "capacity_protection_charging",
        }.get(action, "capacity_protection")

    def _ev_charger_state_key(self) -> str | None:
        """Return the integration-status key for no-telemetry EV charger handling."""
        c = self._controller
        charge_blockers = c.get_charge_blockers()
        discharge_blockers = c.get_discharge_blockers()
        if "ev_pause" in charge_blockers or "ev_pause" in discharge_blockers:
            return "ev_charger_pause"
        if "ev_charging" in discharge_blockers:
            return "ev_discharge_blocked"
        return None

    def _balance_hold_batteries(self) -> list[str]:
        """Return batteries currently held by the cell-balance monitor."""
        return [
            coordinator.name
            for coordinator in self._controller.coordinators
            if getattr(coordinator, "balance_hold", False)
            and not getattr(coordinator, "battery_manual_mode_enabled", False)
        ]

    def _price_reserve_batteries(self) -> list[str]:
        """Return batteries holding energy back for a dearer hour still ahead."""
        controller = self._controller
        return [
            coordinator.name
            for coordinator in controller.coordinators
            if "price_reserve" in controller.get_discharge_blockers(coordinator)
        ]

    def _backup_cooldown_batteries(self) -> list[str]:
        """Return batteries temporarily excluded because backup/offgrid load was active."""
        from homeassistant.util import dt as dt_util

        now = dt_util.utcnow()
        return [
            coordinator.name
            for coordinator, cooldown_until in self._controller._backup_cooldown_until.items()
            if not getattr(coordinator, "battery_manual_mode_enabled", False)
            if cooldown_until and now < cooldown_until
        ]

    @property
    def native_value(self) -> str:
        """Return the current integration status as a translation key."""
        c = self._controller

        # Priority 1: Manual mode overrides everything
        if c.manual_mode_enabled:
            return "manual"

        # Priority 2: Predictive grid charging active
        if c.predictive_charging_enabled and c.grid_charging_active:
            return "grid_charging"

        # Priority 3: Weekly full charge in progress
        if c.weekly_full_charge_enabled:
            if c._weekly_charge_status.get("state") in ("Charging to 100%", "Active balancing"):
                return "weekly_full_charge"

        # Priority 4: Charge delay states
        if c.charge_delay_enabled:
            delay_state = c._charge_delay_status.get("state", "Idle")
            if delay_state.startswith("Delayed"):
                return "charge_delayed"
            if delay_state.startswith("Waiting"):
                return "waiting_for_solar"
            # Skip "charging_to_setpoint" if the controller is actively
            # discharging: _is_charge_delayed() is not called during discharge
            # so this state can be stale.
            if (
                delay_state == "Charging to setpoint"
                and c.previous_power >= 0
                and not getattr(c, "_capacity_protection_active", False)
            ):
                return "charging_to_setpoint"

        # Priority 4b: Surplus is deliberately exporting until a cheaper
        # feed-in window. Ranked after charge delay, which owns the same
        # blocker while it is active.
        if "surplus_price_hold" in c.get_charge_blockers():
            return "surplus_price_hold"

        # Priority 5: Operational restrictions and feature overrides
        ev_state = self._ev_charger_state_key()
        if ev_state:
            return ev_state

        if self._balance_hold_batteries():
            return "cell_balance_hold"

        capacity_state = self._capacity_protection_state_key()
        if capacity_state:
            return capacity_state

        discharge_blockers = c.get_discharge_blockers()
        if "price_discharge" in discharge_blockers:
            return "price_discharge_blocked"

        # Per-battery, so it is not in the global registry above.
        if self._price_reserve_batteries():
            return "price_reserve_hold"

        hourly_state = self._hourly_balance_state_key()
        if hourly_state:
            return hourly_state

        if self._backup_cooldown_batteries():
            return "backup_mode"

        # Priority 6: Manual time slot forcing batteries off the PD path
        if getattr(c, "_manual_slot_owned", None):
            return "time_slot_manual"

        # Priority 7: Outside all configured operating windows
        if self._time_slot_blocked("discharge"):
            return "no_discharge_slot"
        if self._time_slot_blocked("charge"):
            return "no_charge_slot"

        # Priority 8: PD control state from last command
        if c.first_execution:
            return "initializing"

        prev_power = c.previous_power
        if prev_power > 0:
            return "charging"
        elif prev_power < 0:
            return "discharging"
        return "balanced"

    @property
    def extra_state_attributes(self) -> dict:
        """Return current controller details for diagnostics."""
        c = self._controller
        attrs = {
            "setpoint_active": c.compute_active_target(),
            "previous_power_w": c.previous_power,
            "first_execution": c.first_execution,
            "manual_mode_enabled": c.manual_mode_enabled,
            "manual_batteries": [
                coordinator.name for coordinator in c.coordinators
                if getattr(coordinator, "battery_manual_mode_enabled", False)
            ],
            "automatic_batteries": [
                coordinator.name for coordinator in c.coordinators
                if not getattr(coordinator, "battery_manual_mode_enabled", False)
            ],
            "grid_charging_active": c.grid_charging_active,
            "price_based_discharge_blocked": c._price_based_discharge_blocked,
            "charge_blocked": c.is_charge_effectively_blocked(),
            "discharge_blocked": c.is_discharge_effectively_blocked(),
        }
        charge_blockers = c.get_charge_blockers()
        if charge_blockers:
            attrs["charge_blockers"] = charge_blockers
        discharge_blockers = c.get_discharge_blockers()
        if discharge_blockers:
            attrs["discharge_blockers"] = discharge_blockers
        battery_charge_blockers = c.get_battery_charge_blockers()
        if battery_charge_blockers:
            attrs["battery_charge_blockers"] = battery_charge_blockers
        battery_discharge_blockers = c.get_battery_discharge_blockers()
        if battery_discharge_blockers:
            attrs["battery_discharge_blockers"] = battery_discharge_blockers
        offsets = dict(c._setpoint_offsets)
        if offsets:
            attrs["setpoint_offsets"] = offsets
        overrides = {k: v[1] for k, v in c._setpoint_overrides.items()}
        if overrides:
            attrs["setpoint_overrides"] = overrides
        attrs["capacity_protection"] = dict(c._capacity_protection_status)

        external_loads = getattr(c, "_external_loads", None)
        if external_loads is not None:
            dynamic_status = dict(external_loads.dynamic_power_control_status)
            if dynamic_status.get("active"):
                attrs["dynamic_power_control"] = dynamic_status

        if c.predictive_charging_enabled:
            attrs["predictive_charging_mode"] = c.predictive_charging_mode
            attrs["predictive_charging_overridden"] = c.predictive_charging_overridden
            attrs["dynamic_price_slot_active"] = c._current_price_slot_active
            attrs["realtime_price_charging"] = c._realtime_price_charging
            attrs["price_data_status"] = c._price_data_status

        mgr = getattr(c, "_hourly_balance_mgr", None)
        if mgr is not None:
            status = mgr.get_status_dict()
            attrs["hourly_balance_status"] = mgr.get_state_label()
            attrs["hourly_balance_offset_w"] = status["offset_w"]
            attrs["hourly_balance_theoretical_offset_w"] = status["theoretical_offset_w"]
            attrs["hourly_balance_net_kwh"] = status["net_kwh"]
            attrs["hourly_balance_remaining_min"] = status["remaining_min"]
            if status["charge_block_reason"]:
                attrs["hourly_balance_charge_block_reason"] = status["charge_block_reason"]

        temp_mgr = getattr(c, "_temp_limit_mgr", None)
        if temp_mgr is not None and c.temp_charge_limit_enabled:
            attrs["temperature_charge_limit"] = temp_mgr.get_status()

        balance_hold_batteries = self._balance_hold_batteries()
        if balance_hold_batteries:
            attrs["balance_hold_batteries"] = balance_hold_batteries

        backup_cooldown_batteries = self._backup_cooldown_batteries()
        if backup_cooldown_batteries:
            attrs["backup_cooldown_batteries"] = backup_cooldown_batteries

        ev_chargers = [entity_id for entity_id, active in c._ev_charging_states.items() if active]
        if ev_chargers:
            attrs["ev_chargers_active"] = ev_chargers
        if c._ev_pause_until:
            attrs["ev_pause_until"] = {
                entity_id: pause_until.isoformat()
                for entity_id, pause_until in c._ev_pause_until.items()
                if pause_until is not None
            }

        normal_balance = c.get_max_soc_charge_status()
        if normal_balance:
            attrs["normal_balance_protection"] = normal_balance

        non_responsive = c.non_responsive_battery_names
        if non_responsive:
            attrs["non_responsive_batteries"] = non_responsive
        return attrs

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class NonResponsiveBatteriesSensor(SensorEntity):
    """Diagnostic sensor showing batteries that are unreachable or non-delivering."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, controller, coordinators: list
    ) -> None:
        """Initialize the non-responsive batteries sensor."""
        self.hass = hass
        self.entry = entry
        self._controller = controller
        self._coordinators = coordinators

        self._attr_has_entity_name = True
        self._attr_translation_key = "non_responsive_batteries"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}non_responsive_batteries"
        self.entity_id = system_entity_id("sensor", "non_responsive_batteries")
        self._attr_icon = "mdi:battery-alert"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_should_poll = True

    @property
    def native_value(self) -> str:
        """Return names of non-responsive batteries, or 'None' if all are healthy."""
        names = self._controller.non_responsive_battery_names
        return ", ".join(names) if names else "None"

    @property
    def extra_state_attributes(self) -> dict:
        """Return per-battery non-responsive state details."""
        from homeassistant.util import dt as dt_util
        now = dt_util.utcnow()
        attrs = {}
        for coordinator in self._coordinators:
            # This check also expires a completed cooldown before attributes are
            # rendered, avoiding state="None" with excluded=true/0 minutes.
            currently_excluded = self._controller._non_responsive.is_excluded(
                coordinator
            )
            info = self._controller._non_responsive_batteries.get(coordinator)
            unreachable = (
                not coordinator.is_available
                and not getattr(coordinator, "_is_shutting_down", False)
                and getattr(coordinator, "_consecutive_failures", 0) > 0
            )
            if currently_excluded and info:
                cooldown_min = self._controller._non_responsive.cooldown_min
                elapsed_min = (now - info["excluded_at"]).total_seconds() / 60
                remaining_min = max(0.0, cooldown_min - elapsed_min)
                attrs[coordinator.name] = {
                    "excluded": True,
                    "unreachable": unreachable,
                    "reason": info.get("reason") or "non_delivery",
                    "retry_attempted": info.get("retry_attempted", False),
                    "wake_attempted": info.get("wake_attempted", False),
                    "cooldown_minutes": cooldown_min,
                    "remaining_minutes": round(remaining_min, 1),
                    "consecutive_failures": getattr(coordinator, "_consecutive_failures", 0),
                }
            else:
                attrs[coordinator.name] = {
                    "excluded": unreachable,
                    "unreachable": unreachable,
                    "reason": "connection_unavailable" if unreachable else (info.get("reason") if info else None),
                    "retry_attempted": info.get("retry_attempted", False) if info else False,
                    "wake_attempted": info.get("wake_attempted", False) if info else False,
                    "fail_count": info["fail_count"] if info else 0,
                    "consecutive_failures": getattr(coordinator, "_consecutive_failures", 0),
                }
        return attrs

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


_DAILY_OPERATION_INTERVAL_COUNT = 96
_DAILY_OPERATION_INTERVAL_MINUTES = 15
_DAILY_OPERATION_EXTENDED_INTERVAL_COUNT = 48
_DAILY_OPERATION_EVENT_THROTTLE_S = 15.0
_TIMELINE_MISSING = object()


def _timeline_read(source: object, name: str, default: object = None) -> object:
    """Read a timeline field from either a mapping or a duck-typed object."""
    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(name, default)
    try:
        return getattr(source, name)
    except Exception:  # noqa: BLE001 - optional manager fields must be best-effort
        return default


def _daily_operation_timeline_source(controller: object) -> object | None:
    """Return the public or legacy timeline manager without requiring either."""
    for name in ("daily_operation_timeline", "_daily_operation_timeline"):
        source = _timeline_read(controller, name, _TIMELINE_MISSING)
        if source is not _TIMELINE_MISSING and source is not None:
            return source
    return None


def _daily_operation_timeline_snapshot(controller: object) -> object | None:
    """Return a synchronous snapshot from an optional timeline manager.

    The manager is intentionally not part of this adapter's hard dependency
    surface. During startup it can be absent, and older controllers may expose
    a mapping or a dataclass directly instead of a ``snapshot()`` method.
    """
    source = _daily_operation_timeline_source(controller)
    if source is None:
        return None

    if isinstance(source, Mapping):
        nested = source.get("snapshot", _TIMELINE_MISSING)
        if (
            isinstance(nested, Mapping)
            and not any(
                key in source
                for key in ("schema_version", "local_date", "series", "operations")
            )
        ):
            return nested
        return source

    for name in (
        "snapshot",
        "get_snapshot",
        "current_snapshot",
        "as_snapshot",
        "to_dict",
        "build_public_snapshot",
        "build_public_dto",
        "public_snapshot",
    ):
        candidate = _timeline_read(source, name, _TIMELINE_MISSING)
        if candidate is _TIMELINE_MISSING or candidate is None:
            continue
        if callable(candidate):
            try:
                candidate = candidate()
            except Exception:
                _LOGGER.debug("Optional timeline snapshot provider failed", exc_info=True)
                continue
        if inspect.isawaitable(candidate):
            # Entity properties are synchronous. Do not leak an un-awaited
            # coroutine if an implementation accidentally exposes async data.
            close = getattr(candidate, "close", None)
            if callable(close):
                close()
            continue
        if candidate is not None:
            return candidate

    data = _timeline_read(source, "data", _TIMELINE_MISSING)
    if isinstance(data, Mapping):
        return data
    return source


def _timeline_find(
    snapshot: object,
    names: tuple[str, ...],
    *sections: object,
) -> object:
    """Find a field in nested sections first and the snapshot second."""
    for section in (*sections, snapshot):
        if section is None:
            continue
        for name in names:
            value = _timeline_read(section, name, _TIMELINE_MISSING)
            if value is not _TIMELINE_MISSING and value is not None:
                return value
    return None


def _timeline_scalar(value: object, max_length: int = 160) -> object:
    """Return a bounded scalar that Home Assistant can serialise as JSON."""
    if value is None:
        return None
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:max_length]
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return str(isoformat())[:max_length]
        except (AttributeError, TypeError, ValueError):
            pass
    return str(value)[:max_length]


def _timeline_number(value: object, digits: int = 6) -> float | None:
    """Return a finite rounded number, never NaN or infinity."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, Enum):
        value = value.value
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _timeline_bool(value: object, default: bool | None = None) -> bool | None:
    """Read common boolean representations without exposing arbitrary values."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "on", "1", "fresh", "restored"}:
            return True
        if lowered in {"false", "no", "off", "0", "stale", "failed"}:
            return False
    return default


def _timeline_date_string(value: object) -> str | None:
    """Normalise a snapshot date to an ISO local-date string."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        candidate = value.strip()
        try:
            return date.fromisoformat(candidate[:10]).isoformat()
        except ValueError:
            return candidate[:32] or None
    return None


def _timeline_array(
    value: object,
    converter,
    default: object,
) -> list[object]:
    """Convert an optional interval iterable to exactly 96 safe values."""
    result: list[object] = []
    if value is not None and not isinstance(value, (str, bytes, Mapping)):
        try:
            iterator = iter(value)
        except TypeError:
            iterator = iter(())
        for item in iterator:
            if len(result) >= _DAILY_OPERATION_INTERVAL_COUNT:
                break
            try:
                result.append(converter(item))
            except (AttributeError, TypeError, ValueError, OverflowError):
                result.append(default)
    result.extend([default] * (_DAILY_OPERATION_INTERVAL_COUNT - len(result)))
    return result


def _timeline_energy(value: object) -> float | None:
    return _timeline_number(value)


def _timeline_soc(value: object) -> float | None:
    number = _timeline_number(value, 3)
    return None if number is None else max(0.0, min(100.0, number))


def _timeline_mask(value: object) -> int:
    number = _timeline_number(value, 0)
    return max(0, int(number)) if number is not None else 0


def _timeline_power(value: object) -> float:
    number = _timeline_number(value)
    return number if number is not None else 0.0


def _timeline_text(value: object) -> str | None:
    safe = _timeline_scalar(value, 64)
    return None if safe is None else str(safe)


def _timeline_projection_extension(value: object) -> list[dict[str, object]]:
    """Copy the optional 12-hour cross-midnight projection safely."""
    if isinstance(value, Mapping):
        value = (
            value.get("extended_intervals")
            or value.get("extended_projection")
            or value.get("intervals")
        )
    if not isinstance(value, (list, tuple)):
        return []

    numeric_fields = {
        "solar_kwh",
        "consumption_kwh",
        "solar_to_battery_kwh",
        "grid_to_battery_kwh",
        "battery_to_home_kwh",
        "grid_to_home_kwh",
        "solar_to_home_kwh",
        "charge_to_battery_kwh",
        "discharge_from_battery_kwh",
        "stored_energy_end_kwh",
        "soc_end_pct",
        "charge_power_w",
        "discharge_power_w",
    }
    mask_fields = {
        "action_mask",
        "planned_action_mask",
        "context_mask",
        "planned_context_mask",
        "coexistence_mask",
        "planned_coexistence_mask",
    }
    text_fields = {
        "start",
        "end",
        "delay_until",
        "source",
        "slot",
        "grid_charge_decision",
        "planned_grid_charge_decision",
    }
    boolean_fields = {"setpoint_active", "delay_active", "simultaneous"}
    result: list[dict[str, object]] = []
    for raw in value[:_DAILY_OPERATION_EXTENDED_INTERVAL_COUNT]:
        if not isinstance(raw, Mapping):
            continue
        item: dict[str, object] = {}
        for name in ("index", "extension_index"):
            if name in raw:
                number = _timeline_number(raw.get(name), 0)
                if number is not None:
                    item[name] = max(0, int(number))
        for name in numeric_fields:
            if name in raw:
                number = _timeline_number(raw.get(name))
                if number is not None and number >= 0.0:
                    item[name] = number
        for name in mask_fields:
            if name in raw:
                number = _timeline_number(raw.get(name), 0)
                if number is not None:
                    item[name] = max(0, int(number))
        for name in text_fields:
            if name in raw:
                text = _timeline_text(raw.get(name))
                if text is not None:
                    item[name] = text
        for name in boolean_fields:
            if name in raw:
                boolean = _timeline_bool(raw.get(name))
                if boolean is not None:
                    item[name] = boolean
        if item.get("start") is not None and item.get("end") is not None:
            result.append(item)
    return result


def _timeline_horizon(value: object) -> dict[str, object]:
    """Copy the small metadata object describing the hidden extension."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for key in ("start", "end"):
        text = _timeline_text(value.get(key))
        if text is not None:
            result[key] = text
    for key in ("interval_minutes", "interval_count"):
        number = _timeline_number(value.get(key), 0)
        if number is not None:
            result[key] = max(0, int(number))
    for key, converter, default in (
        ("duration_s", _timeline_number, 0.0),
        ("dst_skipped", _timeline_bool, False),
        ("dst_repeated", _timeline_bool, False),
    ):
        raw = value.get(key)
        if not isinstance(raw, (list, tuple)):
            continue
        converted: list[object] = []
        for item in raw[:_DAILY_OPERATION_EXTENDED_INTERVAL_COUNT]:
            parsed = converter(item)
            converted.append(default if parsed is None else parsed)
        converted.extend(
            [default]
            * (_DAILY_OPERATION_EXTENDED_INTERVAL_COUNT - len(converted))
        )
        result[key] = converted
    return result


def _timeline_small_mapping(value: object, max_items: int = 16) -> dict[str, object]:
    """Copy only a small scalar mapping; never copy a manager/store object."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for index, (key, item) in enumerate(value.items()):
        if index >= max_items:
            break
        safe_key = str(key)[:64]
        if isinstance(item, (list, tuple, set, Mapping)):
            continue
        safe_value = _timeline_scalar(item, 128)
        if safe_value is not None:
            result[safe_key] = safe_value
    return result


def _timeline_duration_series(value: object) -> list[dict[str, float]]:
    """Copy per-interval action durations into a bounded 96-cell series."""
    result: list[dict[str, float]] = []

    def safe_mapping(raw: object) -> dict[str, float]:
        if not isinstance(raw, Mapping):
            return {}
        copied: dict[str, float] = {}
        for index, (key, item) in enumerate(raw.items()):
            if index >= 8:
                break
            seconds = _timeline_number(item, 3)
            if seconds is not None and seconds >= 0.0:
                copied[str(key)[:32]] = seconds
        return copied

    if isinstance(value, Mapping):
        # Accept both {action: [seconds × 96]} and {index: {action: seconds}}.
        list_values = {
            str(key): item
            for key, item in value.items()
            if isinstance(item, (list, tuple))
        }
        if list_values:
            result = [dict() for _ in range(_DAILY_OPERATION_INTERVAL_COUNT)]
            for key, values in list_values.items():
                for index, item in enumerate(values[:_DAILY_OPERATION_INTERVAL_COUNT]):
                    seconds = _timeline_number(item, 3)
                    if seconds is not None and seconds >= 0.0:
                        result[index][key[:32]] = seconds
        else:
            result = [
                safe_mapping(value.get(index, value.get(str(index))))
                for index in range(_DAILY_OPERATION_INTERVAL_COUNT)
            ]
    elif isinstance(value, (list, tuple)):
        result = [safe_mapping(item) for item in value[:_DAILY_OPERATION_INTERVAL_COUNT]]

    result.extend({} for _ in range(_DAILY_OPERATION_INTERVAL_COUNT - len(result)))
    return result[:_DAILY_OPERATION_INTERVAL_COUNT]


def _timeline_sources(snapshot: object, metadata: object) -> dict[str, object]:
    """Return the bounded source and maturity metadata used by the panel."""
    source_section = _timeline_find(snapshot, ("sources", "source"))
    fields = {
        "solar_actual": (
            "solar_actual",
            "solar_actual_source",
            "solar_source",
        ),
        "solar_forecast": (
            "solar_forecast",
            "solar_forecast_source",
            "solar_timeline_source",
        ),
        "solar_fallback_reason": (
            "solar_fallback_reason",
            "solar_forecast_fallback_reason",
        ),
        "consumption_actual": (
            "consumption_actual",
            "consumption_actual_source",
        ),
        "consumption_forecast": (
            "consumption_forecast",
            "consumption_forecast_source",
            "consumption_profile_source",
        ),
        "consumption_fallback_reason": (
            "consumption_fallback_reason",
            "consumption_forecast_fallback_reason",
        ),
        "operation_plan": (
            "operation_plan",
            "operation_plan_source",
            "plan_source",
        ),
        "solar_forecast_mature": (
            "solar_forecast_mature",
            "solar_mature",
            "solar_profile_mature",
        ),
        "consumption_forecast_mature": (
            "consumption_forecast_mature",
            "consumption_mature",
            "consumption_profile_mature",
        ),
        "solar_forecast_coverage": ("solar_forecast_coverage", "solar_coverage"),
        "consumption_forecast_coverage": (
            "consumption_forecast_coverage",
            "consumption_coverage",
        ),
    }
    result: dict[str, object] = {}
    for field, aliases in fields.items():
        value = _timeline_find(snapshot, aliases, source_section, metadata)
        if field.endswith("_mature"):
            result[field] = _timeline_bool(value)
        elif field.endswith("_coverage"):
            result[field] = _timeline_number(value)
        else:
            result[field] = _timeline_text(value)
    return result


def _timeline_status_info(
    snapshot: object,
    names: tuple[str, ...],
    aliases: dict[str, tuple[str, ...]],
) -> dict[str, object]:
    """Extract a small status object such as setpoint or charge delay."""
    section = _timeline_find(snapshot, names)
    result: dict[str, object] = {}
    for field, field_names in aliases.items():
        value = _timeline_find(snapshot, field_names, section)
        if field in {"target_soc", "target_soc_pct"}:
            value = _timeline_number(value, 3)
        elif field in {"reached", "enabled"}:
            value = _timeline_bool(value)
        else:
            value = _timeline_text(value)
        if value is not None:
            result[field] = value
    return result


def _daily_operation_timeline_attributes(controller: object) -> dict[str, object]:
    """Build the bounded, JSON-safe entity DTO for the daily timeline."""
    snapshot = _daily_operation_timeline_snapshot(controller)
    source = snapshot if snapshot is not None else {}
    metadata = _timeline_find(source, ("metadata", "meta"))

    local_date = _timeline_date_string(
        _timeline_find(source, ("local_date", "date", "snapshot_date"), metadata)
    )
    if local_date is None and snapshot is not None:
        local_date = dt_util.now().date().isoformat()

    timezone = _timeline_find(source, ("timezone", "tz"), metadata)
    timezone_value = _timeline_text(timezone)
    if timezone_value is None:
        try:
            local_zone = dt_util.get_time_zone()
            timezone_value = str(getattr(local_zone, "key", local_zone))
        except (AttributeError, TypeError, ValueError):
            timezone_value = "UTC"

    series = _timeline_find(source, ("series", "energy"))
    operations = _timeline_find(source, ("operations", "operation"))
    interval_grid = _timeline_find(source, ("interval_grid", "grid"))

    actual_action = _timeline_find(
        source, ("actual_action_mask", "actual_action_masks"), operations
    )
    planned_action = _timeline_find(
        source, ("planned_action_mask", "planned_action_masks"), operations
    )
    actual_coexistence = _timeline_find(
        source, ("actual_coexistence_mask",), operations
    )
    planned_coexistence = _timeline_find(
        source, ("planned_coexistence_mask",), operations
    )
    if actual_coexistence is None:
        actual_coexistence = actual_action
    if planned_coexistence is None:
        planned_coexistence = planned_action
    actual_context = _timeline_find(
        source, ("actual_context_mask", "actual_context_masks"), operations
    )
    planned_context = _timeline_find(
        source, ("planned_context_mask", "planned_context_masks"), operations
    )

    restored = _timeline_status_info(
        source,
        ("restoration", "restore", "restore_status"),
        {
            "status": ("status", "state"),
            "restored": ("restored", "loaded", "store_restored"),
            "date": ("date", "restored_date", "restore_date"),
            "error": ("error", "restore_error"),
            "at": ("at", "restored_at", "last_restored_at"),
        },
    )
    setpoint = _timeline_status_info(
        source,
        ("setpoint", "charge_to_setpoint", "setpoint_status"),
        {
            "state": ("state", "status"),
            "estimated_completion": (
                "estimated_completion",
                "estimated_completion_at",
                "estimated_setpoint_time",
                "setpoint_estimated_at",
                "setpoint_completion_at",
                "setpoint_eta",
            ),
            "target_soc": ("target_soc", "target_soc_pct", "setpoint_soc"),
            "reached": ("reached", "setpoint_reached"),
        },
    )
    delay = _timeline_status_info(
        source,
        ("delay", "charge_delay", "charge_delay_status"),
        {
            "enabled": ("enabled", "is_enabled"),
            "state": ("state", "status"),
            "estimated_unlock_time": (
                "estimated_unlock_time",
                "unlock_time",
                "delay_until",
                "estimated_delay_until",
            ),
            "reason": ("reason", "unlock_reason", "delay_reason"),
        },
    )

    raw_observed = _timeline_find(
        source, ("observed_seconds_by_action", "observed_duration_by_action"), operations
    )
    raw_observed_by_interval = _timeline_find(
        source,
        (
            "observed_seconds_by_action_by_interval",
            "observed_duration_by_action_by_interval",
        ),
        operations,
    )
    observed = {}
    if isinstance(raw_observed, Mapping):
        for index, (key, value) in enumerate(raw_observed.items()):
            if index >= 16:
                break
            seconds = _timeline_number(value, 3)
            if seconds is not None:
                observed[str(key)[:32]] = max(0.0, seconds)

    raw_grid_decision = _timeline_find(
        source, ("grid_charge_decision", "grid_charge_decisions"), operations
    )
    actual_grid_decision = _timeline_find(
        source, ("actual_grid_charge_decision", "actual_grid_charge_decisions"), operations
    )
    planned_grid_decision = _timeline_find(
        source,
        ("planned_grid_charge_decision", "planned_grid_charge_decisions"),
        operations,
    )
    if isinstance(raw_grid_decision, Mapping):
        if actual_grid_decision is None:
            actual_grid_decision = raw_grid_decision.get("actual")
        if planned_grid_decision is None:
            planned_grid_decision = raw_grid_decision.get("planned")
        raw_grid_decision = (
            raw_grid_decision.get("values")
            or raw_grid_decision.get("planned")
            or raw_grid_decision.get("actual")
        )

    stale_value = _timeline_bool(_timeline_find(source, ("stale",), metadata))
    last_error = _timeline_text(
        _timeline_find(source, ("last_error", "error", "timeline_error"), metadata)
    )
    if last_error is None:
        last_error = _timeline_text(restored.get("error"))
    freshness = _timeline_small_mapping(
        _timeline_find(source, ("freshness", "freshness_info"), metadata)
    )
    counts = _timeline_small_mapping(
        _timeline_find(source, ("counts", "diagnostic_counts"), metadata)
    )

    return {
        "timeline_available": snapshot is not None,
        "schema_version": int(
            _timeline_number(
                _timeline_find(source, ("schema_version", "version"), metadata), 0
            )
            or 1
        ),
        "local_date": local_date,
        "timezone": timezone_value,
        "interval_minutes": _DAILY_OPERATION_INTERVAL_MINUTES,
        "interval_count": _DAILY_OPERATION_INTERVAL_COUNT,
        "revision": int(
            _timeline_number(_timeline_find(source, ("revision",), metadata), 0)
            or 0
        ),
        "snapshot_build_count": int(
            _timeline_number(
                _timeline_find(source, ("snapshot_build_count",), metadata), 0
            )
            or 0
        ),
        "notification_count": int(
            _timeline_number(
                _timeline_find(source, ("notification_count",), metadata), 0
            )
            or 0
        ),
        "save_count": int(
            _timeline_number(_timeline_find(source, ("save_count",), metadata), 0)
            or 0
        ),
        "snapshot_age_s": _timeline_number(
            _timeline_find(source, ("snapshot_age_s",), metadata), 3
        ),
        "last_save_age_s": _timeline_number(
            _timeline_find(source, ("last_save_age_s",), metadata), 3
        ),
        "publications_last_minute": int(
            _timeline_number(
                _timeline_find(source, ("publications_last_minute",), metadata), 0
            )
            or 0
        ),
        "writes_last_minute": int(
            _timeline_number(
                _timeline_find(source, ("writes_last_minute",), metadata), 0
            )
            or 0
        ),
        "generated_at": _timeline_scalar(
            _timeline_find(source, ("generated_at", "updated_at", "last_updated"), metadata)
        ),
        "plan_evaluated_at": _timeline_scalar(
            _timeline_find(
                source,
                ("plan_evaluated_at", "plan_evaluated", "evaluated_at"),
                metadata,
            )
        ),
        "current_index": (
            max(
                0,
                min(
                    _DAILY_OPERATION_INTERVAL_COUNT - 1,
                    int(
                        _timeline_number(
                            _timeline_find(source, ("current_index", "index"), metadata),
                            0,
                        )
                        or 0
                    ),
                ),
            )
            if _timeline_find(source, ("current_index", "index"), metadata) is not None
            else None
        ),
        "current_progress": (
            max(
                0.0,
                min(
                    1.0,
                    _timeline_number(
                        _timeline_find(source, ("current_progress", "progress"), metadata),
                        6,
                    )
                    or 0.0,
                ),
            )
            if _timeline_find(source, ("current_progress", "progress"), metadata) is not None
            else None
        ),
        "mode": _timeline_text(
            _timeline_find(source, ("mode", "operation_mode", "charging_mode"), metadata)
        ),
        "stale": stale_value,
        "stale_reason": _timeline_text(
            _timeline_find(source, ("stale_reason", "freshness_reason"), metadata)
        ),
        "last_error": last_error,
        "freshness": freshness,
        "series": {
            "solar_actual_kwh": _timeline_array(
                _timeline_find(source, ("solar_actual_kwh", "solar_actual"), series),
                _timeline_energy,
                None,
            ),
            "solar_forecast_kwh": _timeline_array(
                _timeline_find(source, ("solar_forecast_kwh", "solar_forecast"), series),
                _timeline_energy,
                None,
            ),
            "consumption_actual_kwh": _timeline_array(
                _timeline_find(
                    source,
                    ("consumption_actual_kwh", "home_consumption_actual_kwh"),
                    series,
                ),
                _timeline_energy,
                None,
            ),
            "consumption_forecast_kwh": _timeline_array(
                _timeline_find(
                    source,
                    ("consumption_forecast_kwh", "home_consumption_forecast_kwh"),
                    series,
                ),
                _timeline_energy,
                None,
            ),
            "actual_coverage_s": _timeline_array(
                _timeline_find(source, ("actual_coverage_s", "coverage_s"), series),
                _timeline_energy,
                None,
            ),
            # Captures can have a different cadence for solar and consumption.
            # Keep both coverages so the panel only extrapolates the incomplete
            # series, rather than applying the longest shared interval to both.
            "solar_actual_coverage_s": _timeline_array(
                _timeline_find(source, ("solar_actual_coverage_s",), series),
                _timeline_energy,
                None,
            ),
            "consumption_actual_coverage_s": _timeline_array(
                _timeline_find(source, ("consumption_actual_coverage_s",), series),
                _timeline_energy,
                None,
            ),
        },
        "operations": {
            "actual_action_mask": _timeline_array(actual_action, _timeline_mask, 0),
            "planned_action_mask": _timeline_array(planned_action, _timeline_mask, 0),
            "actual_coexistence_mask": _timeline_array(
                actual_coexistence,
                _timeline_mask,
                0,
            ),
            "planned_coexistence_mask": _timeline_array(
                planned_coexistence,
                _timeline_mask,
                0,
            ),
            "actual_context_mask": _timeline_array(actual_context, _timeline_mask, 0),
            "planned_context_mask": _timeline_array(planned_context, _timeline_mask, 0),
            "grid_charge_decision": _timeline_array(
                raw_grid_decision,
                _timeline_text,
                None,
            ),
            "actual_grid_charge_decision": _timeline_array(
                actual_grid_decision, _timeline_text, None
            ),
            "planned_grid_charge_decision": _timeline_array(
                planned_grid_decision, _timeline_text, None
            ),
            "actual_source": _timeline_array(
                _timeline_find(source, ("actual_source", "actual_sources"), operations),
                _timeline_text,
                None,
            ),
            "planned_source": _timeline_array(
                _timeline_find(source, ("planned_source", "planned_sources"), operations),
                _timeline_text,
                None,
            ),
            "delay_until": _timeline_array(
                _timeline_find(source, ("delay_until", "delays_until"), operations),
                _timeline_text,
                None,
            ),
            "planned_delay_until": _timeline_array(
                _timeline_find(
                    source, ("planned_delay_until", "planned_delays_until"), operations
                ),
                _timeline_text,
                None,
            ),
            "charge_power_w": _timeline_array(
                _timeline_find(source, ("charge_power_w", "charge_power"), operations),
                _timeline_power,
                0.0,
            ),
            "discharge_power_w": _timeline_array(
                _timeline_find(
                    source, ("discharge_power_w", "discharge_power"), operations
                ),
                _timeline_power,
                0.0,
            ),
            "charge_to_battery_kwh": _timeline_array(
                _timeline_find(source, ("charge_to_battery_kwh",), operations),
                _timeline_energy,
                None,
            ),
            "actual_charge_to_battery_kwh": _timeline_array(
                _timeline_find(
                    source, ("actual_charge_to_battery_kwh",), operations
                ),
                _timeline_energy,
                None,
            ),
            "planned_charge_to_battery_kwh": _timeline_array(
                _timeline_find(
                    source, ("planned_charge_to_battery_kwh",), operations
                ),
                _timeline_energy,
                None,
            ),
            "discharge_from_battery_kwh": _timeline_array(
                _timeline_find(source, ("discharge_from_battery_kwh",), operations),
                _timeline_energy,
                None,
            ),
            "actual_discharge_from_battery_kwh": _timeline_array(
                _timeline_find(
                    source, ("actual_discharge_from_battery_kwh",), operations
                ),
                _timeline_energy,
                None,
            ),
            "planned_discharge_from_battery_kwh": _timeline_array(
                _timeline_find(
                    source, ("planned_discharge_from_battery_kwh",), operations
                ),
                _timeline_energy,
                None,
            ),
            "soc_pct": _timeline_array(
                _timeline_find(source, ("soc_pct",), operations),
                _timeline_soc,
                None,
            ),
            "actual_soc_pct": _timeline_array(
                _timeline_find(source, ("actual_soc_pct",), operations),
                _timeline_soc,
                None,
            ),
            "planned_soc_pct": _timeline_array(
                _timeline_find(
                    source, ("planned_soc_pct", "soc_end_pct"), operations
                ),
                _timeline_soc,
                None,
            ),
            "solar_to_battery_kwh": _timeline_array(
                _timeline_find(source, ("solar_to_battery_kwh",), operations),
                _timeline_energy,
                None,
            ),
            "grid_to_battery_kwh": _timeline_array(
                _timeline_find(source, ("grid_to_battery_kwh",), operations),
                _timeline_energy,
                None,
            ),
            "battery_to_home_kwh": _timeline_array(
                _timeline_find(source, ("battery_to_home_kwh",), operations),
                _timeline_energy,
                None,
            ),
            "grid_to_home_kwh": _timeline_array(
                _timeline_find(source, ("grid_to_home_kwh",), operations),
                _timeline_energy,
                None,
            ),
            "stored_energy_end_kwh": _timeline_array(
                _timeline_find(source, ("stored_energy_end_kwh",), operations),
                _timeline_energy,
                None,
            ),
            "soc_end_pct": _timeline_array(
                _timeline_find(
                    source, ("soc_end_pct", "stored_soc_end_pct"), operations
                ),
                _timeline_energy,
                None,
            ),
            "observed_seconds_by_action": observed,
            "observed_seconds_by_action_by_interval": _timeline_duration_series(
                raw_observed_by_interval
            ),
        },
        "dst_skipped": _timeline_array(
            _timeline_find(source, ("dst_skipped",), interval_grid, metadata),
            _timeline_bool,
            None,
        ),
        "dst_repeated": _timeline_array(
            _timeline_find(source, ("dst_repeated",), interval_grid, metadata),
            _timeline_bool,
            None,
        ),
        "dst_flags": _timeline_array(
            _timeline_find(
                source,
                ("dst_flags", "local_time_flags", "dst_status"),
                interval_grid,
                metadata,
            ),
            _timeline_text,
            None,
        ),
        "sources": _timeline_sources(source, metadata),
        "restoration": restored,
        "setpoint": setpoint,
        "delay": delay,
        "counts": counts,
        "extended_horizon": _timeline_horizon(
            _timeline_find(source, ("extended_horizon", "forecast_extension_horizon"))
        ),
        "extended_projection": _timeline_projection_extension(
            _timeline_find(
                source,
                ("extended_projection", "forecast_extension", "extended_intervals"),
            )
        ),
    }


class DailyOperationTimelineSensor(SensorEntity):
    """Diagnostic-only, renderable snapshot of the current local day."""

    _attr_has_entity_name = True
    _attr_translation_key = "daily_operation_timeline"
    _attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}daily_operation_timeline"
    _attr_device_class = SensorDeviceClass.DATE
    _attr_state_class = None
    _attr_icon = "mdi:chart-timeline-variant"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _unrecorded_attributes = frozenset(
        {
            "series",
            "operations",
            "dst_skipped",
            "dst_repeated",
            "dst_flags",
            "extended_horizon",
            "extended_projection",
        }
    )

    def __init__(self, controller) -> None:
        """Initialize the timeline sensor without requiring a manager yet."""
        self._controller = controller
        self.entity_id = system_entity_id("sensor", "daily_operation_timeline")
        self._timeline_listener_unsub = None
        self._last_event_update_at = 0.0
        self._timeline_publish_handle = None
        self._attributes_cache_revision = None
        self._attributes_cache = None

    def _cancel_timeline_publish(self) -> None:
        """Cancel a coalesced publication scheduled for the next throttle edge."""
        handle = self._timeline_publish_handle
        self._timeline_publish_handle = None
        if handle is not None:
            handle.cancel()

    @property
    def native_value(self) -> date | None:
        """Return the local date represented by the current snapshot."""
        source = _daily_operation_timeline_source(self._controller)
        value = _timeline_read(source, "local_date", _TIMELINE_MISSING)
        if value is _TIMELINE_MISSING:
            value = _daily_operation_timeline_attributes(self._controller).get(
                "local_date"
            )
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if not isinstance(value, str):
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return the bounded JSON-safe DTO consumed by the frontend."""
        manager = _daily_operation_timeline_source(self._controller)
        revision = _timeline_read(manager, "revision", _TIMELINE_MISSING)
        if (
            revision is not _TIMELINE_MISSING
            and self._attributes_cache is not None
            and self._attributes_cache_revision == revision
        ):
            return self._attributes_cache
        attributes = _daily_operation_timeline_attributes(self._controller)
        if revision is not _TIMELINE_MISSING:
            self._attributes_cache_revision = revision
            self._attributes_cache = attributes
        return attributes

    async def async_added_to_hass(self) -> None:
        """Subscribe to an optional manager/controller listener."""
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel_timeline_publish)
        await self._async_subscribe_to_timeline()

    async def _async_subscribe_to_timeline(self) -> None:
        """Register against whichever listener spelling the manager provides."""
        manager = _daily_operation_timeline_source(self._controller)
        owners = [manager] if manager is not None else []
        if self._controller is not manager:
            owners.append(self._controller)

        for owner in owners:
            if owner is None:
                continue
            for name in (
                "async_add_listener",
                "add_listener",
                "async_add_update_listener",
                "add_update_listener",
                "register_listener",
                "subscribe",
            ):
                listener = _timeline_read(owner, name, _TIMELINE_MISSING)
                if listener is _TIMELINE_MISSING or not callable(listener):
                    continue
                try:
                    unsubscribe = listener(self._handle_timeline_update)
                    if inspect.isawaitable(unsubscribe):
                        unsubscribe = await unsubscribe
                except Exception:
                    _LOGGER.debug(
                        "Daily operation timeline listener is not available on %s",
                        type(owner).__name__,
                        exc_info=True,
                    )
                    continue
                if callable(unsubscribe):
                    self._timeline_listener_unsub = unsubscribe
                    self.async_on_remove(unsubscribe)
                return

    def _publish_timeline_update(self) -> None:
        """Publish the latest cached snapshot, if the entity is still alive."""
        self._timeline_publish_handle = None
        if getattr(self, "hass", None) is None:
            return
        self._last_event_update_at = time.monotonic()
        try:
            # schedule_update_ha_state is safe when a manager callback happens
            # from a worker thread; HA reads the DTO later on its event loop.
            self.schedule_update_ha_state()
        except (AttributeError, RuntimeError, TypeError):
            # Lightweight test doubles and an entity being removed may not have
            # a complete HA runtime. Never let that break the controller event.
            try:
                self.async_write_ha_state()
            except (AttributeError, RuntimeError, TypeError):
                _LOGGER.debug("Unable to publish daily operation timeline update")

    def _handle_timeline_update(self, *_args: Any, **_kwargs: Any) -> None:
        """Coalesce continuous activity and publish structural events now."""
        if getattr(self, "hass", None) is None:
            return
        manager = _daily_operation_timeline_source(self._controller)
        immediate = bool(_timeline_read(manager, "last_notification_immediate", False))
        now = time.monotonic()
        if immediate:
            self._cancel_timeline_publish()
            self._publish_timeline_update()
            return

        remaining = _DAILY_OPERATION_EVENT_THROTTLE_S - (
            now - self._last_event_update_at
        )
        if remaining <= 0.0:
            self._cancel_timeline_publish()
            self._publish_timeline_update()
            return
        if self._timeline_publish_handle is None:
            loop = getattr(self.hass, "loop", None)
            if loop is not None:
                self._timeline_publish_handle = loop.call_later(
                    remaining, self._publish_timeline_update
                )

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class ConsumptionProfileSensor(SensorEntity):
    """Expected household consumption for the current local day.

    This is a forecast rather than an accumulated meter, so it deliberately
    has no state class.  The value may be recalculated as the profile changes.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "expected_home_consumption_profile"
    _attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}expected_home_consumption_profile"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = None
    _attr_native_unit_of_measurement = "kWh"
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:chart-bell-curve-cumulative"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True

    def __init__(self, controller) -> None:
        """Initialize the profile diagnostic sensor."""
        self._controller = controller
        self.entity_id = system_entity_id("sensor", "expected_home_consumption_profile")

    def _target_date(self):
        tracker = getattr(self._controller, "_consumption_tracker", None)
        profile = getattr(tracker, "consumption_profile", None)
        today = getattr(profile, "_today", None)
        if callable(today):
            try:
                return today()
            except Exception:  # noqa: BLE001
                pass
        return dt_util.now().date()

    def _forecast(self):
        tracker = getattr(self._controller, "_consumption_tracker", None)
        profile = getattr(tracker, "consumption_profile", None)
        forecast_for_date = getattr(tracker, "forecast_consumption_for_date", None)
        if profile is None or not callable(forecast_for_date):
            return None
        try:
            return forecast_for_date(self._target_date())
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Consumption profile sensor: forecast unavailable: %s", exc)
            return None

    @property
    def native_value(self) -> float | None:
        forecast = self._forecast()
        return round(forecast.energy_kwh, 3) if forecast is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        forecast = self._forecast()
        tracker = getattr(self._controller, "_consumption_tracker", None)
        profile = getattr(tracker, "consumption_profile", None)
        if forecast is None or profile is None:
            return {"state": "unavailable"}
        intervals = [round(value, 6) for value in forecast.intervals_kwh]
        hourly = [
            round(sum(intervals[index:index + 4]), 6)
            for index in range(0, INTERVAL_COUNT, 4)
        ]
        peak_index = max(range(len(hourly)), key=hourly.__getitem__) if hourly else 0
        return {
            "target_date": self._target_date().isoformat(),
            "interval_minutes": INTERVAL_MINUTES,
            "hourly_profile_kwh": hourly,
            "interval_profile_kwh": intervals,
            "expected_remaining_kwh": round(forecast.energy_kwh, 6),
            "source": forecast.source,
            "mature": forecast.mature,
            "coverage_ratio": round(forecast.coverage_ratio, 6),
            "weekday_samples": forecast.weekday_samples,
            "day_type_samples": forecast.day_type_samples,
            "total_profile_days": forecast.total_days,
            "newest_profile_date": (
                forecast.newest_profile_date.isoformat()
                if forecast.newest_profile_date is not None
                else None
            ),
            "fallback_reason": forecast.fallback_reason,
            "peak_hour": peak_index,
            "peak_hour_kwh": round(hourly[peak_index], 6) if hourly else 0.0,
        }

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class ConsumptionProfileCaptureSensor(SensorEntity):
    """Live raw energy captured for the current profile day.

    The value is a daily accumulated total and resets at the next local day.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "consumption_profile_capture"
    _attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}consumption_profile_capture"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"
    _attr_suggested_display_precision = 3
    _attr_icon = "mdi:chart-timeline-variant-shimmer"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True

    def __init__(self, controller) -> None:
        """Initialize the live profile-capture sensor."""
        self._controller = controller
        self.entity_id = system_entity_id("sensor", "consumption_profile_capture")

    def _capture(self):
        tracker = getattr(self._controller, "_consumption_tracker", None)
        profile = getattr(tracker, "consumption_profile", None)
        if profile is None:
            return None
        try:
            return profile.current_day_capture()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Consumption profile capture unavailable: %s", exc)
            return None

    @property
    def native_value(self) -> float | None:
        capture = self._capture()
        return round(capture["energy_kwh"], 3) if capture is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        capture = self._capture()
        if capture is None:
            return {"state": "unavailable"}
        return {
            "capture_date": capture["date"],
            "capture_complete": capture["complete"],
            "interval_minutes": INTERVAL_MINUTES,
            "capture_valid_intervals": capture["valid_intervals"],
            "capture_coverage_ratio": capture["coverage_ratio"],
            "hourly_capture_kwh": capture["hourly_energy_kwh"],
            "interval_capture_kwh": capture["interval_energy_kwh"],
            "interval_coverage_s": capture["interval_coverage_s"],
        }

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class DailySolarEnergySensor(SensorEntity):
    """Exact daily solar production (kWh), integrated from the real solar power.

    The controller integrates total solar — the configured solar_production_sensor
    plus independent battery PV (Venus MPPT inputs or verified Anker E5000
    aggregate) — at control-loop cadence and resets at local midnight (see
    ConsumptionTracker); this entity just surfaces that running total.
    total_increasing so HA handles the daily reset.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "system_daily_solar_energy"
    _attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}daily_solar_energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:solar-power"
    _attr_should_poll = True

    def __init__(self, controller) -> None:
        """Initialize the daily solar energy sensor."""
        self._controller = controller
        self.entity_id = system_entity_id("sensor", "daily_solar_energy")

    @property
    def native_value(self) -> float:
        """Return today's accumulated solar production in kWh."""
        return round(self._controller._daily_solar_energy_kwh, 2)

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class SystemSolarPowerSensor(SensorEntity):
    """Instantaneous total solar production from external and independent PV.

    Sums the configured solar_production_sensor and independent battery PV —
    Venus MPPT inputs or verified Anker E5000 aggregate telemetry — just as the
    ConsumptionTracker does for daily solar energy. AC-derived Anker Max/XE
    readings are excluded, and the entity is omitted when no battery contributes
    independent PV so it cannot duplicate the external sensor.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "system_solar_power"
    _attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}solar_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "W"
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:solar-power"
    _attr_should_poll = True

    def __init__(self, controller) -> None:
        """Initialize the system solar power sensor."""
        self._controller = controller
        self.entity_id = system_entity_id("sensor", "solar_power")

    @property
    def native_value(self) -> float | None:
        """Return total instantaneous solar production in W (None if no source readable)."""
        tracker = self._controller._consumption_tracker
        if tracker is None:
            return None
        power_kw = tracker._read_total_solar_power_kw()
        if power_kw is None:
            return None
        return round(power_kw * 1000.0)

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class DailyHomeEnergySensor(SensorEntity):
    """Exact daily home consumption (kWh), integrated from the home power.

    The value is derived from grid + battery AC + solar, matching the power-flow
    Home Consumption sensor and the predictive-charging daily accumulator. Both
    integrate the full 24 h; excluded/additional loads only adjust the predictive
    history contract.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "system_daily_home_energy"
    _attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}daily_home_energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:home-lightning-bolt"
    _attr_should_poll = True

    def __init__(self, controller) -> None:
        """Initialize the daily home energy sensor."""
        self._controller = controller
        self.entity_id = system_entity_id("sensor", "daily_home_energy")

    @property
    def native_value(self) -> float:
        """Return today's accumulated home consumption in kWh."""
        return round(self._controller._daily_home_energy_kwh, 2)

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class DailyGridImportEnergySensor(SensorEntity):
    """Exact daily grid import (kWh), integrated from the net consumption meter.

    The controller integrates the positive half of the consumption_sensor (power
    drawn FROM the grid) at control-loop cadence and resets at local midnight
    (see ConsumptionTracker); this entity surfaces that running total.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "system_daily_grid_import_energy"
    _attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}daily_grid_import_energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:transmission-tower-import"
    _attr_should_poll = True

    def __init__(self, controller) -> None:
        """Initialize the daily grid import energy sensor."""
        self._controller = controller
        self.entity_id = system_entity_id("sensor", "daily_grid_import_energy")

    @property
    def native_value(self) -> float:
        """Return today's accumulated grid import in kWh."""
        return round(self._controller._daily_grid_import_energy_kwh, 2)

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class DailyGridExportEnergySensor(SensorEntity):
    """Exact daily grid export (kWh), integrated from the net consumption meter.

    Mirrors DailyGridImportEnergySensor but for the negative half of the
    consumption_sensor (power fed TO the grid).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "system_daily_grid_export_energy"
    _attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}daily_grid_export_energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:transmission-tower-export"
    _attr_should_poll = True

    def __init__(self, controller) -> None:
        """Initialize the daily grid export energy sensor."""
        self._controller = controller
        self.entity_id = system_entity_id("sensor", "daily_grid_export_energy")

    @property
    def native_value(self) -> float:
        """Return today's accumulated grid export in kWh."""
        return round(self._controller._daily_grid_export_energy_kwh, 2)

    @property
    def device_info(self):
        """Return device information for the system."""
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }
