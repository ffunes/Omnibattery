"""Calculated sensors for the Omnibattery integration."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from ..const import DOMAIN, EFFICIENCY_SENSOR_DEFINITIONS, STORED_ENERGY_SENSOR_DEFINITIONS, CYCLE_SENSOR_DEFINITIONS
from ..infra.coordinator import MarstekVenusDataUpdateCoordinator
from ..infra.entity_naming import english_entity_id

# Skip integration across gaps larger than this (stalled coordinator / sensor
# offline) so a resumed update can't dump one giant energy block.
_MAX_INTEGRATION_GAP_S = 600.0

# Only sample the dual-plane efficiency (vA/vD) while PV is not feeding the
# cells: above this MPPT total the AC port no longer equals the battery's own
# conversion leg, so the AC/DC comparison would be contaminated.
_MPPT_ZERO_W = 10.0

# Ignore samples where either plane is near idle: standby self-consumption and
# zero-crossings carry no useful conversion information and would just add noise.
_MIN_POWER_W = 20.0


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
        self.entity_id = english_entity_id("sensor", coordinator.name, definition["key"])
        self._attr_device_class = definition.get("device_class")
        self._attr_state_class = definition.get("state_class")
        self._attr_native_unit_of_measurement = definition.get("unit")
        self._attr_icon = definition.get("icon")
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_should_poll = False
        self._dependency_keys = definition["dependency_keys"]
        # On Venus D/A the AC-side hardware energy counters can't see DC-coupled
        # PV charging the cells, so their round-trip ratio runs >100%. For those
        # units measure the real inverter loss directly while PV is idle (MPPT=0):
        # the AC port (ac_power) and the DC battery terminal (battery_power) are
        # two independent planes whose difference is the conversion loss. Each leg
        # is the ratio of two simultaneous power readings, so unlike a cumulative
        # charge/discharge ratio it has no SoC-endpoint dependence and can't blow
        # up on partial cycles. AC-only models keep the accurate hardware counters.
        self._integrate_mode = coordinator.capabilities.has_mppt_pv
        self._mppt_keys = ["mppt1_power", "mppt2_power", "mppt3_power", "mppt4_power"]
        # Energy on each plane, split by direction (kWh), MPPT=0 windows only.
        self._charge_ac_kwh = 0.0      # AC drawn while charging the cells
        self._charge_dc_kwh = 0.0      # DC stored while charging the cells
        self._discharge_ac_kwh = 0.0   # AC delivered while discharging
        self._discharge_dc_kwh = 0.0   # DC extracted while discharging
        self._last_mono: float | None = None

    def _leg_efficiencies(self):
        """Return (charge_eff, discharge_eff) or (None, None) if not yet sampled."""
        charge_eff = (
            self._charge_dc_kwh / self._charge_ac_kwh
            if self._charge_ac_kwh > 0 else None
        )
        discharge_eff = (
            self._discharge_ac_kwh / self._discharge_dc_kwh
            if self._discharge_dc_kwh > 0 else None
        )
        return charge_eff, discharge_eff

    @property
    def native_value(self):
        """Return round-trip efficiency (%)."""
        if self._integrate_mode:
            charge_eff, discharge_eff = self._leg_efficiencies()
            # A DC-coupled-PV unit (Venus A/D) charges its cells through the
            # MPPT, not the AC port, so the charge leg only samples during the
            # rare AC grid-charge windows — most installs never measure it and
            # round-trip would sit at "unknown" forever. The inverter's AC<->DC
            # conversion is near-symmetric, so when only one leg has been seen
            # estimate the round trip from it; the real product takes over as
            # soon as both legs exist. Per-leg attributes flag which is which.
            if charge_eff is None and discharge_eff is None:
                return None
            charge_eff = charge_eff if charge_eff is not None else discharge_eff
            discharge_eff = discharge_eff if discharge_eff is not None else charge_eff
            return round(min(charge_eff * discharge_eff * 100, 100.0), 2)

        if self.coordinator.data is None:
            return None

        charge_energy = self.coordinator.data.get(self._dependency_keys["charge"], 0)
        discharge_energy = self.coordinator.data.get(self._dependency_keys["discharge"], 0)

        # Anker and Venus E v3 only expose lifetime energy registers. Their
        # derived daily counters share a common midnight baseline, unlike the
        # independent lifetime counters that can retain different historical
        # baselines. Prefer that like-for-like pair when it is available.
        if not self.coordinator.capabilities.has_daily_energy_counters:
            daily_charge = self.coordinator.data.get("total_daily_charging_energy")
            daily_discharge = self.coordinator.data.get("total_daily_discharging_energy")
            if daily_charge is not None and daily_discharge is not None:
                charge_energy = daily_charge
                discharge_energy = daily_discharge

        if charge_energy <= 0:
            return None

        # Hardware lifetime counters are normally a reliable round-trip
        # measurement. Some devices, however, can expose charge and discharge
        # counters with different historical baselines. Never publish a
        # physically impossible efficiency when that happens.
        return round(min((discharge_energy / charge_energy) * 100, 100.0), 2)

    @property
    def extra_state_attributes(self):
        """Expose per-leg efficiency and integrated energy (vA/vD only).

        The energy buckets survive restarts; the leg efficiencies give partial
        visibility before a full round trip (e.g. a unit that only discharges at
        night surfaces its discharge efficiency while round-trip stays None).
        """
        if not self._integrate_mode:
            return None
        charge_eff, discharge_eff = self._leg_efficiencies()
        return {
            "charge_efficiency": round(charge_eff * 100, 2) if charge_eff is not None else None,
            "discharge_efficiency": round(discharge_eff * 100, 2) if discharge_eff is not None else None,
            "charge_ac_kwh": round(self._charge_ac_kwh, 4),
            "charge_dc_kwh": round(self._charge_dc_kwh, 4),
            "discharge_ac_kwh": round(self._discharge_ac_kwh, 4),
            "discharge_dc_kwh": round(self._discharge_dc_kwh, 4),
        }

    async def async_added_to_hass(self) -> None:
        """Restore integrated energy counters on startup."""
        await super().async_added_to_hass()
        if not self._integrate_mode:
            return
        last = await self.async_get_last_state()
        if last is not None:
            try:
                self._charge_ac_kwh = float(last.attributes.get("charge_ac_kwh") or 0.0)
                self._charge_dc_kwh = float(last.attributes.get("charge_dc_kwh") or 0.0)
                self._discharge_ac_kwh = float(last.attributes.get("discharge_ac_kwh") or 0.0)
                self._discharge_dc_kwh = float(last.attributes.get("discharge_dc_kwh") or 0.0)
            except (TypeError, ValueError):
                pass

    @callback
    def _handle_coordinator_update(self) -> None:
        """Integrate terminal power on each coordinator update, then write state."""
        self._accumulate()
        super()._handle_coordinator_update()

    def _accumulate(self) -> None:
        """Integrate AC- and DC-plane energy by direction, while PV is idle."""
        if not self._integrate_mode:
            return
        data = self.coordinator.data
        if not data:
            return
        battery = data.get("battery_power")  # DC terminal, + charge / - discharge
        ac = data.get("ac_power")            # AC port, opposite sign to battery
        if battery is None or ac is None:
            return
        solar = sum(v for k in self._mppt_keys if (v := data.get(k)) is not None)

        now = time.monotonic()
        last = self._last_mono
        self._last_mono = now
        # First sample (fresh start or post-restart): seed the timer, accumulate
        # nothing — monotonic resets across restarts, so this also skips downtime.
        if last is None:
            return
        # PV feeding the cells, or either plane near idle: skip but keep the timer
        # current so the next valid sample doesn't integrate the skipped span.
        if solar > _MPPT_ZERO_W or abs(battery) < _MIN_POWER_W or abs(ac) < _MIN_POWER_W:
            return
        dt = now - last
        if dt <= 0 or dt > _MAX_INTEGRATION_GAP_S:
            return
        hours = dt / 3600.0
        ac_kwh = abs(ac) * hours / 1000.0
        dc_kwh = abs(battery) * hours / 1000.0
        if battery > 0:  # charging the cells: AC drawn in, DC stored
            self._charge_ac_kwh += ac_kwh
            self._charge_dc_kwh += dc_kwh
        else:            # discharging the cells: DC extracted, AC delivered
            self._discharge_ac_kwh += ac_kwh
            self._discharge_dc_kwh += dc_kwh

    @property
    def device_info(self):
        """Return device information."""
        return self.coordinator.battery_device_info


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
        self.entity_id = english_entity_id("sensor", coordinator.name, definition["key"])
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
        return self.coordinator.battery_device_info


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
        self.entity_id = english_entity_id("sensor", coordinator.name, definition["key"])
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
        return self.coordinator.battery_device_info


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
        self.entity_id = english_entity_id("sensor", coordinator.name, definition["key"])
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
        return self.coordinator.battery_device_info


# Synthesised charge/discharge energy for drivers without hardware counters
# (Zendure). Reuses the Marstek register keys so the existing translations and
# dashboard cards apply unchanged.
SYNTHETIC_ENERGY_SENSOR_DEFINITIONS: list[dict] = [
    {"key": "total_charging_energy",         "direction": "charge",    "period": "total",
     "unit": "kWh", "device_class": "energy", "state_class": "total_increasing",
     "precision": 2, "icon": "mdi:battery-plus-variant"},
    {"key": "total_discharging_energy",      "direction": "discharge", "period": "total",
     "unit": "kWh", "device_class": "energy", "state_class": "total_increasing",
     "precision": 2, "icon": "mdi:battery-minus-variant"},
    {"key": "total_daily_charging_energy",   "direction": "charge",    "period": "daily",
     "unit": "kWh", "device_class": "energy", "state_class": "total_increasing",
     "precision": 2, "icon": "mdi:battery-plus-variant"},
    {"key": "total_daily_discharging_energy","direction": "discharge", "period": "daily",
     "unit": "kWh", "device_class": "energy", "state_class": "total_increasing",
     "precision": 2, "icon": "mdi:battery-minus-variant"},
]

# Daily energy derived from lifetime hardware counters. Anker reports accurate
# cumulative charge/discharge energy but has no registers that reset at midnight.
# Keep this separate from SyntheticEnergySensor: using hardware counter deltas is
# more accurate than integrating instantaneous power at poll cadence.
CUMULATIVE_DAILY_ENERGY_SENSOR_DEFINITIONS: list[dict] = [
    {"key": "total_daily_charging_energy", "source_key": "total_charging_energy",
     "unit": "kWh", "device_class": "energy", "state_class": "total_increasing",
     "precision": 2, "icon": "mdi:battery-plus-variant"},
    {"key": "total_daily_discharging_energy", "source_key": "total_discharging_energy",
     "unit": "kWh", "device_class": "energy", "state_class": "total_increasing",
     "precision": 2, "icon": "mdi:battery-minus-variant"},
]


@dataclass
class _CumulativeDailyEnergyData(ExtraStoredData):
    """Persisted daily accumulator backed by a lifetime hardware counter."""

    kwh: float
    last_total: float | None
    reset_date: str

    def as_dict(self) -> dict:
        """Serialize for Home Assistant's restore-state store."""
        return {
            "kwh": self.kwh,
            "last_total": self.last_total,
            "reset_date": self.reset_date,
        }

    @classmethod
    def from_dict(cls, restored: dict) -> "_CumulativeDailyEnergyData | None":
        """Rebuild persisted state, rejecting incomplete or malformed data."""
        try:
            last_total = restored.get("last_total")
            return cls(
                kwh=float(restored["kwh"]),
                last_total=float(last_total) if last_total is not None else None,
                reset_date=str(restored["reset_date"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def update(self, total: float, today: str) -> None:
        """Accumulate a lifetime-counter sample into the current local day."""
        if today != self.reset_date:
            # The first sample after midnight becomes the new baseline. During
            # normal operation this loses at most one cumulative-counter poll
            # interval (30 s); after a long offline period no exact split is possible.
            self.kwh = 0.0
            self.last_total = total
            self.reset_date = today
            return

        if self.last_total is None:
            self.last_total = total
            return

        delta = total - self.last_total
        self.last_total = total
        if delta >= 0:
            self.kwh += delta
        # A negative delta means the lifetime counter reset or wrapped. Preserve
        # today's accumulated value and use the new reading as the next baseline.


def _legacy_daily_energy_value(last_state: State | None, today: str) -> float | None:
    """Return today's value from the daily-register entity this replaces.

    The v3 migration keeps the entity ID but changes its implementation from a
    Modbus register sensor to a derived sensor.  The old sensor has no extra
    restore data, so retain its last numeric state if Home Assistant recorded it
    today.  A state from an earlier local day must not leak into a new day's
    counter.
    """
    if last_state is None:
        return None
    if dt_util.as_local(last_state.last_updated).date().isoformat() != today:
        return None
    try:
        value = float(last_state.state)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _highest_daily_energy_value(states: list[State]) -> float | None:
    """Return the highest valid daily-counter value from recorder states."""
    values = []
    for state in states:
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            values.append(value)
    return max(values, default=None)


class CumulativeDailyEnergySensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    """Daily charge/discharge energy derived from a lifetime hardware counter."""

    def __init__(
        self, coordinator: MarstekVenusDataUpdateCoordinator, definition: dict
    ) -> None:
        """Initialize the derived daily energy sensor."""
        super().__init__(coordinator)
        self.definition = definition
        self._attr_has_entity_name = True
        self._attr_translation_key = definition["key"]
        self._attr_unique_id = f"{coordinator.device_key}_{definition['key']}"
        self.entity_id = english_entity_id("sensor", coordinator.name, definition["key"])
        self._attr_device_class = definition.get("device_class")
        self._attr_state_class = definition.get("state_class")
        self._attr_native_unit_of_measurement = definition.get("unit")
        self._attr_icon = definition.get("icon")
        self._attr_suggested_display_precision = definition.get("precision")
        self._attr_should_poll = False

        self._key = definition["key"]
        self._source_key = definition["source_key"]
        self._precision = definition.get("precision", 2)
        self._energy_data = _CumulativeDailyEnergyData(
            kwh=0.0,
            last_total=None,
            reset_date=dt_util.now().date().isoformat(),
        )

    async def async_added_to_hass(self) -> None:
        """Restore today's accumulator and cumulative-counter baseline."""
        await super().async_added_to_hass()
        stored = await self.async_get_last_extra_data()
        restored = _CumulativeDailyEnergyData.from_dict(stored.as_dict()) if stored else None
        today = dt_util.now().date().isoformat()
        if restored is not None and restored.reset_date == today:
            self._energy_data = restored
        # On migration from v3's register sensor there is no extra restore
        # payload: that sensor did not inherit RestoreEntity. A previous release
        # may also have persisted a small post-migration value before this
        # recovery runs. Read today's recorder history in either case and keep
        # the greater value: daily counters are monotonic within a day, so this
        # preserves the old reading plus any value already accumulated by the new
        # implementation while ignoring unavailable states and later zeroes.
        recovered_value = await self._recover_daily_value_from_recorder(today)
        if recovered_value is None:
            recovered_value = _legacy_daily_energy_value(
                await self.async_get_last_state(), today
            )
        if recovered_value is not None and recovered_value > self._energy_data.kwh:
            self._energy_data = _CumulativeDailyEnergyData(
                recovered_value, self._energy_data.last_total, today
            )
        self._publish_daily()

    async def _recover_daily_value_from_recorder(self, today: str) -> float | None:
        """Recover this entity's current-day state from Home Assistant Recorder."""
        try:
            from homeassistant.components.recorder import get_instance, history

            local_tz = dt_util.get_time_zone(self.hass.config.time_zone) or dt_util.UTC
            start = datetime.combine(
                date.fromisoformat(today), datetime.min.time(), tzinfo=local_tz
            )
            query = partial(
                history.state_changes_during_period,
                self.hass,
                start,
                entity_id=self.entity_id,
                include_start_time_state=False,
            )
            states_map = await get_instance(self.hass).async_add_executor_job(query)
        except Exception as err:  # Recorder is optional and may not be ready at boot.
            _LOGGER.debug("Could not recover %s from recorder: %s", self.entity_id, err)
            return None

        return _highest_daily_energy_value(states_map.get(self.entity_id, []))

    @callback
    def _handle_coordinator_update(self) -> None:
        """Consume the newest hardware total and publish the daily delta."""
        self._accumulate()
        self._publish_daily()
        super()._handle_coordinator_update()

    def _accumulate(self) -> None:
        data = self.coordinator.data
        if not data:
            return
        raw_total = data.get(self._source_key)
        try:
            total = float(raw_total)
        except (TypeError, ValueError):
            return
        if total < 0:
            return
        self._energy_data.update(total, dt_util.now().date().isoformat())

    def _publish_daily(self) -> None:
        """Make the derived key available to system aggregates and the panel."""
        if self.coordinator.data is not None:
            self.coordinator.data[self._key] = self._energy_data.kwh

    @property
    def native_value(self) -> float:
        """Return energy accumulated since local midnight."""
        return round(self._energy_data.kwh, self._precision)

    @property
    def extra_restore_state_data(self) -> _CumulativeDailyEnergyData:
        """Persist raw precision and the last observed lifetime total."""
        return self._energy_data

    @property
    def extra_state_attributes(self):
        """Expose reset metadata useful when diagnosing daily totals."""
        return {
            "reset_date": self._energy_data.reset_date,
            "source": self._source_key,
        }

    @property
    def device_info(self):
        """Return device information."""
        return self.coordinator.battery_device_info


@dataclass
class _SyntheticEnergyData(ExtraStoredData):
    """Raw accumulator persisted independently of the displayed state.

    The displayed state reads ``unavailable`` whenever the battery connection
    drops (frequent for Zendure's single connection). If a restart's last
    persisted *state* is non-numeric it can't be parsed back, which previously
    zeroed the lifetime total. This extra data is taken from the entity object
    at dump time, not the state string, so it survives an unavailable-at-
    shutdown and the total is never lost.
    """

    kwh: float
    reset_date: str | None

    def as_dict(self) -> dict:
        """Serialize for the restore-state store."""
        return {"kwh": self.kwh, "reset_date": self.reset_date}

    @classmethod
    def from_dict(cls, restored: dict) -> "_SyntheticEnergyData | None":
        """Rebuild from stored data, or None if it is malformed."""
        try:
            return cls(float(restored["kwh"]), restored.get("reset_date"))
        except (KeyError, TypeError, ValueError):
            return None


class SyntheticEnergySensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    """Charge/discharge energy (kWh) integrated from battery_power.

    For drivers without hardware energy counters (Zendure): the device reports no
    kWh, so the value is a Riemann sum of battery_power over time, signed by
    direction. One entity per (direction, period) — daily entities reset at local
    midnight, total entities accumulate for the device's lifetime. Persisted across
    restarts via RestoreEntity (state = kWh; a `reset_date` attribute drives the
    daily reset across a restart that straddles midnight). The integration is an
    estimate at poll cadence, not a metered value.
    """

    def __init__(
        self, coordinator: MarstekVenusDataUpdateCoordinator, definition: dict
    ) -> None:
        """Initialize the synthetic energy sensor."""
        super().__init__(coordinator)
        self.definition = definition
        self._attr_has_entity_name = True
        self._attr_translation_key = definition["key"]
        self._attr_unique_id = f"{coordinator.device_key}_{definition['key']}"
        self.entity_id = english_entity_id("sensor", coordinator.name, definition["key"])
        self._attr_device_class = definition.get("device_class")
        self._attr_state_class = definition.get("state_class")
        self._attr_native_unit_of_measurement = definition.get("unit")
        self._attr_icon = definition.get("icon")
        self._attr_suggested_display_precision = definition.get("precision")
        self._attr_should_poll = False

        self._key = definition["key"]
        self._direction = definition["direction"]       # "charge" / "discharge"
        self._daily = definition["period"] == "daily"
        self._precision = definition.get("precision", 2)
        self._kwh = 0.0
        self._last_mono: float | None = None
        self._reset_date = dt_util.now().date() if self._daily else None
        # Per-serial backup so a delete + re-add reclaims the lifetime total.
        self._backup = None
        self._restored_from_entity = False
        self._backup_restore_done = False

    async def async_added_to_hass(self) -> None:
        """Restore the accumulated energy on startup.

        Prefer the typed extra data (immune to a non-numeric last state); fall
        back to the recorded state string for installs that predate it.
        """
        await super().async_added_to_hass()

        from ..synthetic_energy_backup import async_get_backup
        self._backup = await async_get_backup(self.hass)

        stored = await self.async_get_last_extra_data()
        data = _SyntheticEnergyData.from_dict(stored.as_dict()) if stored else None
        self._restored_from_entity = data is not None
        if data is not None:
            self._kwh = data.kwh
            stored_reset_date = data.reset_date
        else:
            # Legacy fallback. A non-numeric state (unavailable/unknown) leaves
            # the accumulator untouched rather than wiping a real lifetime total.
            last = await self.async_get_last_state()
            stored_reset_date = last.attributes.get("reset_date") if last else None
            if last is not None:
                try:
                    self._kwh = float(last.state)
                except (TypeError, ValueError):
                    pass

        if self._daily:
            today = dt_util.now().date()
            # A restart that straddled local midnight starts a fresh day.
            if stored_reset_date != today.isoformat():
                self._kwh = 0.0
            self._reset_date = today

        # No per-entity restore (deleted and re-added): reclaim the lifetime total
        # from the per-serial backup. The serial is normally already known here.
        self._maybe_restore_from_backup()

        # Seed the running total into coordinator.data immediately so the cycle /
        # efficiency sensors can read it before the first poll re-publishes it.
        self._publish_total()

    @callback
    def _maybe_restore_from_backup(self) -> None:
        """Reclaim kWh from the per-serial backup once the serial is known.

        Skipped when a per-entity restore already supplied the total. The serial
        is set by the driver before this entity is added, so this normally runs
        in async_added_to_hass; a slow first poll can delay it, so
        _handle_coordinator_update retries. *Adds* to the live accumulator (rather
        than overwriting) so the few samples taken before the serial appeared are
        not lost. Runs at most once.
        """
        if self._backup_restore_done or self._restored_from_entity or self._backup is None:
            return
        serial = self.coordinator.driver.serial
        if not serial:
            return
        self._backup_restore_done = True
        saved = self._backup.get(serial, self._key)
        if saved is None:
            return
        # A daily counter only carries forward within the same local day.
        if self._daily and saved.get("reset_date") != self._reset_date.isoformat():
            return
        self._kwh += saved.get("kwh", 0.0)

    def _save_to_backup(self) -> None:
        """Mirror the running total into the per-serial backup (debounced)."""
        if self._backup is None:
            return
        serial = self.coordinator.driver.serial
        if not serial:
            return
        self._backup.set(
            serial,
            self._key,
            self._kwh,
            self._reset_date.isoformat() if self._reset_date else None,
        )

    def _publish_total(self) -> None:
        """Expose the running total in coordinator.data.

        The cycle and round-trip-efficiency sensors read coordinator.data, not
        this entity, so without this the synthesised totals stay invisible to
        them (cycles pinned at 0, efficiency at unknown). The coordinator mutates
        its data dict in place each poll, so the key persists between refreshes.
        """
        if self.coordinator.data is not None:
            self.coordinator.data[self._key] = self._kwh

    @callback
    def _handle_coordinator_update(self) -> None:
        """Integrate battery_power on each coordinator update, then write state."""
        self._maybe_restore_from_backup()
        self._accumulate()
        self._publish_total()
        self._save_to_backup()
        super()._handle_coordinator_update()

    def _accumulate(self) -> None:
        """Add the energy moved since the last sample in this entity's direction."""
        data = self.coordinator.data
        now = time.monotonic()
        last = self._last_mono
        self._last_mono = now

        # Reset daily counters at local midnight regardless of battery activity.
        if self._daily:
            today = dt_util.now().date()
            if today != self._reset_date:
                self._kwh = 0.0
                self._reset_date = today

        if not data:
            return
        battery = data.get("battery_power")  # signed: + charge / - discharge
        if battery is None or last is None:
            return
        dt = now - last
        if dt <= 0 or dt > _MAX_INTEGRATION_GAP_S:
            return
        kwh = abs(battery) * (dt / 3600.0) / 1000.0
        if self._direction == "charge" and battery > 0:
            self._kwh += kwh
        elif self._direction == "discharge" and battery < 0:
            self._kwh += kwh

    @property
    def native_value(self) -> float:
        """Return the accumulated energy (kWh)."""
        return round(self._kwh, self._precision)

    @property
    def extra_restore_state_data(self) -> _SyntheticEnergyData:
        """Persist the raw accumulator so it survives an unavailable shutdown."""
        return _SyntheticEnergyData(
            self._kwh,
            self._reset_date.isoformat() if self._reset_date else None,
        )

    @property
    def extra_state_attributes(self):
        """Expose the daily reset date so a restart can detect a day rollover."""
        if not self._daily:
            return None
        return {"reset_date": self._reset_date.isoformat()}

    @property
    def device_info(self):
        """Return device information."""
        return self.coordinator.battery_device_info


class SyntheticCapacitySensor(CoordinatorEntity, SensorEntity):
    """Nominal battery capacity (kWh) for drivers without energy counters.

    Marstek exposes battery_total_energy as a register sensor; Zendure has no such
    register, so the coordinator injects the user-set capacity into data instead.
    This entity surfaces that value under the same translation_key so the panel,
    more-info and aggregate sensors see it like a register-backed battery.
    """

    def __init__(self, coordinator: MarstekVenusDataUpdateCoordinator) -> None:
        """Initialize the capacity sensor."""
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_translation_key = "battery_total_energy"
        self._attr_unique_id = f"{coordinator.device_key}_battery_total_energy"
        self.entity_id = english_entity_id("sensor", coordinator.name, "battery_total_energy")
        self._attr_device_class = "energy"
        self._attr_state_class = "total"
        self._attr_native_unit_of_measurement = "kWh"
        self._attr_should_poll = False

    @property
    def native_value(self):
        """Return the user-set capacity injected by the coordinator (None if unset)."""
        if self.coordinator.data is None:
            return None
        capacity = self.coordinator.data.get("battery_total_energy")
        return capacity if capacity and capacity > 0 else None

    @property
    def device_info(self):
        """Return device information."""
        return self.coordinator.battery_device_info


class ZendurePackSensor(CoordinatorEntity, SensorEntity):
    """Per-pack telemetry sensor (one per pack × field).

    The driver pre-scales packData[] into pack{N}_{suffix} keys; this entity reads
    one of them. Uses a plain name (not a translation key) so per-pack entities
    need no translations. The SoC field also exposes the pack's serial / model /
    state as attributes so heterogeneous packs can be told apart.
    """

    def __init__(
        self, coordinator: MarstekVenusDataUpdateCoordinator, pack_index: int, spec: dict
    ) -> None:
        """Initialize. pack_index is 1-based; spec is a PACK_FIELD_SPECS entry."""
        super().__init__(coordinator)
        self._pack_index = pack_index
        self._spec = spec
        self._key = f"pack{pack_index}_{spec['suffix']}"
        self._attr_has_entity_name = True
        self._attr_name = f"Pack {pack_index} {spec['name']}"
        self._attr_unique_id = f"{coordinator.device_key}_{self._key}"
        self.entity_id = english_entity_id("sensor", coordinator.name, self._key)
        self._attr_native_unit_of_measurement = spec.get("unit")
        self._attr_device_class = spec.get("device_class")
        self._attr_state_class = "measurement"
        self._attr_icon = spec.get("icon")
        self._attr_suggested_display_precision = spec.get("precision")
        self._attr_should_poll = False

    @property
    def native_value(self):
        """Return this pack field's current value."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._key)

    @property
    def extra_state_attributes(self):
        """On the SoC sensor, surface the pack's serial / model / state."""
        if self._spec["suffix"] != "soc" or self.coordinator.data is None:
            return None
        attrs = {}
        for label, suffix in (("serial_number", "sn"), ("model", "model"), ("state", "state")):
            value = self.coordinator.data.get(f"pack{self._pack_index}_{suffix}")
            if value is not None:
                attrs[label] = value
        return attrs or None

    @property
    def device_info(self):
        """Return device information (same device as the battery)."""
        return self.coordinator.battery_device_info


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
        self.entity_id = english_entity_id("sensor", coordinator.name, definition["key"])
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
        return self.coordinator.battery_device_info
