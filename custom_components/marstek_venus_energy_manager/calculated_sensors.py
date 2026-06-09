"""Calculated sensors for the Marstek Venus Energy Manager integration."""
from __future__ import annotations

import time

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, EFFICIENCY_SENSOR_DEFINITIONS, STORED_ENERGY_SENSOR_DEFINITIONS, CYCLE_SENSOR_DEFINITIONS
from .coordinator import MarstekVenusDataUpdateCoordinator

# Skip integration across gaps larger than this (stalled coordinator / sensor
# offline) so a resumed update can't dump one giant energy block.
_MAX_INTEGRATION_GAP_S = 600.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the calculated sensor platform."""
    coordinators: list[MarstekVenusDataUpdateCoordinator] = hass.data[DOMAIN][entry.entry_id]["coordinators"]
    entities = []
    for coordinator in coordinators:
        for definition in EFFICIENCY_SENSOR_DEFINITIONS:
            entities.append(MarstekVenusEfficiencySensor(coordinator, definition))
        for definition in STORED_ENERGY_SENSOR_DEFINITIONS:
            entities.append(MarstekVenusStoredEnergySensor(coordinator, definition))
        for definition in CYCLE_SENSOR_DEFINITIONS:
            entities.append(MarstekVenusCycleSensor(coordinator, definition))
    async_add_entities(entities)


class MarstekVenusEfficiencySensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    """Representation of a Marstek Venus efficiency sensor."""

    def __init__(
        self, coordinator: MarstekVenusDataUpdateCoordinator, definition: dict
    ) -> None:
        """Initialize the efficiency sensor."""
        super().__init__(coordinator)
        self.definition = definition

        self._attr_has_entity_name = True
        self._attr_translation_key = definition["key"]
        self._attr_unique_id = f"{coordinator.device_key}_{definition['key']}"
        self._attr_device_class = definition.get("device_class")
        self._attr_state_class = definition.get("state_class")
        self._attr_native_unit_of_measurement = definition.get("unit")
        self._attr_icon = definition.get("icon")
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_should_poll = False
        self._dependency_keys = definition["dependency_keys"]
        # On Venus D/A the AC-side charge counter (reg 33000) can't see DC-coupled
        # PV charging the cells, while the discharge counter (reg 33002) sees
        # everything, so the hardware round-trip ratio runs >100%. For those units
        # integrate the true terminal power (battery_cell_power = battery_power +
        # MPPT) by sign instead. AC-only models have no MPPT and keep the accurate
        # hardware counters.
        self._integrate_mode = coordinator.battery_version in ("vA", "vD")
        self._mppt_keys = ["mppt1_power", "mppt2_power", "mppt3_power", "mppt4_power"]
        self._charge_energy_kwh = 0.0
        self._discharge_energy_kwh = 0.0
        self._last_mono: float | None = None

    @property
    def native_value(self):
        """Return round-trip efficiency (%)."""
        if self._integrate_mode:
            if self._charge_energy_kwh <= 0:
                return None
            return round(self._discharge_energy_kwh / self._charge_energy_kwh * 100, 2)

        if self.coordinator.data is None:
            return None

        charge_energy = self.coordinator.data.get(self._dependency_keys["charge"], 0)
        discharge_energy = self.coordinator.data.get(self._dependency_keys["discharge"], 0)

        if charge_energy <= 0:
            return None

        return round((discharge_energy / charge_energy) * 100, 2)

    @property
    def extra_state_attributes(self):
        """Expose integrated energy so it survives restarts (vA/vD only)."""
        if not self._integrate_mode:
            return None
        return {
            "charge_energy_kwh": round(self._charge_energy_kwh, 4),
            "discharge_energy_kwh": round(self._discharge_energy_kwh, 4),
        }

    async def async_added_to_hass(self) -> None:
        """Restore integrated energy counters on startup."""
        await super().async_added_to_hass()
        if not self._integrate_mode:
            return
        last = await self.async_get_last_state()
        if last is not None:
            try:
                self._charge_energy_kwh = float(last.attributes.get("charge_energy_kwh") or 0.0)
                self._discharge_energy_kwh = float(last.attributes.get("discharge_energy_kwh") or 0.0)
            except (TypeError, ValueError):
                pass

    @callback
    def _handle_coordinator_update(self) -> None:
        """Integrate terminal power on each coordinator update, then write state."""
        self._accumulate()
        super()._handle_coordinator_update()

    def _accumulate(self) -> None:
        """Add the energy moved since the last update to the charge/discharge totals."""
        if not self._integrate_mode:
            return
        data = self.coordinator.data
        if not data:
            return
        battery = data.get("battery_power")
        if battery is None:
            return
        solar = sum(v for k in self._mppt_keys if (v := data.get(k)) is not None)
        cell_power = battery + solar  # W, + charge / - discharge

        now = time.monotonic()
        last = self._last_mono
        self._last_mono = now
        # First sample (fresh start or post-restart): seed the timer, accumulate
        # nothing — monotonic resets across restarts, so this also skips downtime.
        if last is None:
            return
        dt = now - last
        if dt <= 0 or dt > _MAX_INTEGRATION_GAP_S:
            return
        energy_kwh = cell_power * (dt / 3600.0) / 1000.0
        if energy_kwh > 0:
            self._charge_energy_kwh += energy_kwh
        else:
            self._discharge_energy_kwh += -energy_kwh

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, f"{self.coordinator.device_key}")},
            "name": self.coordinator.name,
            "manufacturer": "Marstek",
            "model": "Venus",
        }


class MarstekVenusStoredEnergySensor(CoordinatorEntity, SensorEntity):
    """Representation of a Marstek Venus stored energy sensor."""

    def __init__(
        self, coordinator: MarstekVenusDataUpdateCoordinator, definition: dict
    ) -> None:
        """Initialize the stored energy sensor."""
        super().__init__(coordinator)
        self.definition = definition

        self._attr_has_entity_name = True
        self._attr_translation_key = definition["key"]
        self._attr_unique_id = f"{coordinator.device_key}_{definition['key']}"
        self._attr_device_class = definition.get("device_class")
        self._attr_state_class = definition.get("state_class")
        self._attr_native_unit_of_measurement = definition.get("unit")
        self._attr_icon = definition.get("icon")
        self._attr_should_poll = False
        self._dependency_keys = definition["dependency_keys"]

    @property
    def native_value(self):
        """Return the state of the stored energy sensor."""
        if self.coordinator.data is None:
            return None

        soc_key = self._dependency_keys["soc"]
        capacity_key = self._dependency_keys["capacity"]

        soc = self.coordinator.data.get(soc_key, 0)
        capacity = self.coordinator.data.get(capacity_key, 0)

        if capacity <= 0:
            return None

        stored_energy = (soc / 100) * capacity
        return round(stored_energy, 3)

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, f"{self.coordinator.device_key}")},
            "name": self.coordinator.name,
            "manufacturer": "Marstek",
            "model": "Venus",
        }


class MarstekVenusCycleSensor(CoordinatorEntity, SensorEntity):
    """Calculated battery cycle count: total_discharge / battery_capacity."""

    def __init__(
        self, coordinator: MarstekVenusDataUpdateCoordinator, definition: dict
    ) -> None:
        """Initialize the cycle count sensor."""
        super().__init__(coordinator)
        self.definition = definition

        self._attr_has_entity_name = True
        self._attr_translation_key = definition["key"]
        self._attr_unique_id = f"{coordinator.device_key}_{definition['key']}"
        self._attr_state_class = definition.get("state_class")
        self._attr_icon = definition.get("icon")
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_should_poll = False
        self._dependency_keys = definition["dependency_keys"]

    @property
    def native_value(self):
        """Return calculated cycle count: (discharge + charge) / 2 / capacity."""
        if self.coordinator.data is None:
            return None

        discharge = self.coordinator.data.get(self._dependency_keys["discharge"], 0)
        charge = self.coordinator.data.get(self._dependency_keys["charge"], 0)
        capacity = self.coordinator.data.get(self._dependency_keys["capacity"], 0)

        if not capacity or capacity <= 0:
            return None

        return round((discharge + charge) / 2 / capacity, 1)

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, f"{self.coordinator.device_key}")},
            "name": self.coordinator.name,
            "manufacturer": "Marstek",
            "model": "Venus",
        }


class MarstekVenusSolarPowerSensor(CoordinatorEntity, SensorEntity):
    """Total DC-coupled PV power for a Venus D/A unit: sum of its MPPT inputs."""

    def __init__(
        self, coordinator: MarstekVenusDataUpdateCoordinator, definition: dict
    ) -> None:
        """Initialize the solar power sensor."""
        super().__init__(coordinator)
        self.definition = definition

        self._attr_has_entity_name = True
        self._attr_translation_key = definition["key"]
        self._attr_unique_id = f"{coordinator.device_key}_{definition['key']}"
        self._attr_device_class = definition.get("device_class")
        self._attr_state_class = definition.get("state_class")
        self._attr_native_unit_of_measurement = definition.get("unit")
        self._attr_icon = definition.get("icon")
        self._attr_should_poll = False
        self._mppt_keys = definition["dependency_keys"]["mppt"]

    @property
    def native_value(self):
        """Return the sum of this unit's MPPT power inputs (W)."""
        if self.coordinator.data is None:
            return None

        total = 0
        for key in self._mppt_keys:
            value = self.coordinator.data.get(key)
            if value is not None:
                total += value
        return round(total)

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, f"{self.coordinator.device_key}")},
            "name": self.coordinator.name,
            "manufacturer": "Marstek",
            "model": "Venus",
        }


class MarstekVenusBatteryCellPowerSensor(CoordinatorEntity, SensorEntity):
    """True battery cell power for a Venus D/A unit: battery_power plus DC PV (MPPT).

    The battery_power register mirrors the AC side with inverted sign and excludes
    the DC PV, which charges the cells without crossing the AC port. Adding the
    unit's MPPT recovers the battery's own power. Same sign as battery_power
    (+ charge / - discharge).
    """

    def __init__(
        self, coordinator: MarstekVenusDataUpdateCoordinator, definition: dict
    ) -> None:
        """Initialize the battery cell power sensor."""
        super().__init__(coordinator)
        self.definition = definition

        self._attr_has_entity_name = True
        self._attr_translation_key = definition["key"]
        self._attr_unique_id = f"{coordinator.device_key}_{definition['key']}"
        self._attr_device_class = definition.get("device_class")
        self._attr_state_class = definition.get("state_class")
        self._attr_native_unit_of_measurement = definition.get("unit")
        self._attr_icon = definition.get("icon")
        self._attr_should_poll = False
        self._battery_key = definition["dependency_keys"]["battery"]
        self._mppt_keys = definition["dependency_keys"]["mppt"]

    @property
    def native_value(self):
        """Return battery_power plus this unit's MPPT total (W)."""
        if self.coordinator.data is None:
            return None

        battery = self.coordinator.data.get(self._battery_key)
        if battery is None:
            return None
        solar = 0
        for key in self._mppt_keys:
            value = self.coordinator.data.get(key)
            if value is not None:
                solar += value
        return round(battery + solar)

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, f"{self.coordinator.device_key}")},
            "name": self.coordinator.name,
            "manufacturer": "Marstek",
            "model": "Venus",
        }
