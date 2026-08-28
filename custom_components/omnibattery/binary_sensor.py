"""Binary sensor platform for the Omnibattery integration."""
from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    CONF_CAPACITY_PROTECTION_ENABLED,
    CONF_ENABLE_PREDICTIVE_CHARGING,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
)
from .infra.coordinator import MarstekVenusDataUpdateCoordinator
from .infra.entity_naming import english_entity_id, system_entity_id, SYSTEM_UNIQUE_ID_PREFIX
from .solar_forecast import read_solar_forecast_kwh

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary sensor platform."""
    coordinators: list[MarstekVenusDataUpdateCoordinator] = hass.data[DOMAIN][entry.entry_id]["coordinators"]
    controller = hass.data[DOMAIN][entry.entry_id].get("controller")
    entities = []

    # Add regular battery binary sensors (version-specific)
    for coordinator in coordinators:
        for definition in coordinator.binary_sensor_definitions:
            entities.append(MarstekVenusBinarySensor(coordinator, definition))

        # Add charge hysteresis sensor for batteries with hysteresis enabled
        if coordinator.enable_charge_hysteresis:
            entities.append(ChargeHysteresisActiveSensor(coordinator))

    # Keep predictive diagnostics registered while the master switch is off so
    # enabling it live never requires a platform reload.
    if controller and CONF_ENABLE_PREDICTIVE_CHARGING in entry.data:
        entities.append(PredictiveChargingStatusSensor(hass, entry, controller))
        if controller.predictive_charging_mode == PREDICTIVE_MODE_DYNAMIC_PRICING:
            entities.append(CurtailmentStatusSensor(hass, entry, controller))

    # Add capacity protection status sensor (system-level, when configured, regardless of enabled state)
    if controller and CONF_CAPACITY_PROTECTION_ENABLED in entry.data:
        entities.append(CapacityProtectionStatusSensor(hass, entry, controller))

    async_add_entities(entities)


class MarstekVenusBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Representation of a Marstek Venus binary sensor."""

    def __init__(
        self, coordinator: MarstekVenusDataUpdateCoordinator, definition: dict
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self.definition = definition
        
        self._attr_has_entity_name = True
        self._attr_translation_key = definition["key"]
        self._attr_unique_id = f"{coordinator.device_key}_{definition['key']}"
        self.entity_id = english_entity_id("binary_sensor", coordinator.name, definition["key"])
        self._attr_device_class = definition.get("device_class")
        self._attr_icon = definition.get("icon")
        self._attr_entity_registry_enabled_default = definition.get("enabled_by_default", True)
        if definition.get("category") == "diagnostic":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_should_poll = False

    @property
    def is_on(self):
        """Return the state of the binary sensor."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self.definition["key"])

    @property
    def device_info(self):
        """Return device information."""
        return self.coordinator.battery_device_info


class ChargeHysteresisActiveSensor(RestoreEntity, BinarySensorEntity):
    """Binary sensor indicating if charge hysteresis is active for a battery.

    This sensor persists its state across reboots using RestoreEntity.
    When hysteresis is active, the battery won't charge until SOC drops
    below (max_soc - hysteresis_percent).
    """

    # current_soc changes every poll, so recording it rewrites the whole row
    # each cycle for no history value. Restore reads only .state, never attrs.
    _unrecorded_attributes = frozenset({"current_soc"})

    def __init__(self, coordinator: MarstekVenusDataUpdateCoordinator) -> None:
        """Initialize the hysteresis sensor."""
        self.coordinator = coordinator

        self._attr_has_entity_name = True
        self._attr_translation_key = "charge_hysteresis"
        self._attr_unique_id = f"{coordinator.device_key}_charge_hysteresis_active"
        self.entity_id = english_entity_id("binary_sensor", coordinator.name, "charge_hysteresis_active")
        self._attr_icon = "mdi:battery-lock"
        self._attr_should_poll = True
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_added_to_hass(self) -> None:
        """Restore hysteresis state when entity is added to hass."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()

        if last_state is None:
            _LOGGER.debug(
                "[%s] No previous hysteresis state found - starting with hysteresis inactive",
                self.coordinator.name
            )
            return

        # Restore the hysteresis state to the coordinator
        was_active = last_state.state == "on"
        self.coordinator._hysteresis_active = was_active

        _LOGGER.info(
            "[%s] Restored charge hysteresis state: %s",
            self.coordinator.name,
            "ACTIVE" if was_active else "inactive"
        )

    @property
    def is_on(self):
        """Return true if charge hysteresis is active."""
        return self.coordinator._hysteresis_active

    @property
    def extra_state_attributes(self):
        """Return additional state attributes."""
        current_soc = None
        if self.coordinator.data:
            current_soc = self.coordinator.data.get("battery_soc")

        # Use the latched base SOC (the ceiling actually hit) as the threshold
        # base, matching the control logic in _refresh_battery_charge_limit_blocks;
        # fall back to max_soc when not latched. Reporting max_soc here was
        # misleading whenever the latch captured a different SOC.
        base = (
            self.coordinator._hysteresis_base_soc
            if self.coordinator._hysteresis_base_soc is not None
            else self.coordinator.max_soc
        )
        charge_threshold = base - self.coordinator.charge_hysteresis_percent

        return {
            "max_soc": self.coordinator.max_soc,
            "hysteresis_percent": self.coordinator.charge_hysteresis_percent,
            "base_soc": base,
            "charge_resume_threshold": charge_threshold,
            "current_soc": current_soc,
        }

    @property
    def device_info(self):
        """Return device information."""
        return self.coordinator.battery_device_info


class CapacityProtectionStatusSensor(BinarySensorEntity):
    """Binary sensor indicating if capacity protection is currently intervening."""

    # While protection is active these numerics change every poll; keep the
    # on/off state + action/threshold in history, drop the per-cycle churn.
    _unrecorded_attributes = frozenset({
        "avg_soc", "peak_limit_w", "estimated_house_load_w",
        "original_target_w", "adjusted_target_w",
    })

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        """Initialize the status sensor."""
        self.hass = hass
        self.entry = entry
        self.controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "capacity_protection_active"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}capacity_protection_active"
        self.entity_id = system_entity_id("binary_sensor", "capacity_protection_active")
        self._attr_device_class = "running"
        self._attr_icon = "mdi:shield-alert"
        self._attr_should_poll = True
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self):
        """Return true if capacity protection is actively intervening."""
        return self.controller._capacity_protection_active

    @property
    def extra_state_attributes(self):
        """Return diagnostic attributes about the protection state."""
        status = self.controller._capacity_protection_status
        return {
            "enabled": self.controller.capacity_protection_enabled,
            "excluded_devices_enabled": (
                self.controller.capacity_protection_excluded_devices
            ),
            "avg_soc": status.get("avg_soc"),
            "soc_threshold": status.get("soc_threshold"),
            "peak_limit_w": status.get("peak_limit"),
            "estimated_house_load_w": status.get("estimated_house_load"),
            "action": status.get("action"),
            "original_target_w": status.get("original_target"),
            "adjusted_target_w": status.get("adjusted_target"),
            "excluded_peak_excess_w": status.get("excluded_peak_excess", 0),
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


class CurtailmentStatusSensor(BinarySensorEntity):
    """Diagnostic state for the dynamic-pricing smart pre-discharge planner."""

    _unrecorded_attributes = frozenset({
        "risk_slots",
        "selected_discharge_slots",
        "target_soc_by_battery",
        "charge_limit_reasons",
    })

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        self.hass = hass
        self.entry = entry
        self.controller = controller
        self._attr_has_entity_name = True
        self._attr_translation_key = "curtailment_status"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}curtailment_status"
        self.entity_id = system_entity_id("binary_sensor", "curtailment_status")
        self._attr_device_class = "running"
        self._attr_icon = "mdi:solar-power-variant"
        self._attr_should_poll = True
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        return getattr(self.controller, "_curtailment_runtime_status", "disabled") in {
            "predischarging",
            "protected_window",
        }

    @property
    def extra_state_attributes(self) -> dict:
        plan = getattr(self.controller, "_curtailment_plan", None)
        runtime_status = getattr(
            self.controller, "_curtailment_runtime_status", "disabled"
        )
        attrs = {
            "enabled": bool(getattr(self.controller, "smart_predischarge_enabled", False)),
            "status": runtime_status,
            "reason": getattr(self.controller, "_curtailment_runtime_reason", "disabled"),
            "protected_window_active": runtime_status == "protected_window",
            "active_export_target_w": getattr(
                self.controller, "_curtailment_active_export_target_w", 0.0
            ),
            "solar_reserve_remaining_kwh": getattr(
                self.controller, "_curtailment_solar_reserve_remaining_kwh", 0.0
            ),
            "opportunistic_space_available_kwh": getattr(
                self.controller, "_curtailment_opportunistic_space_kwh", 0.0
            ),
            "opportunistic_charge_limit_w": getattr(
                self.controller, "_curtailment_opportunistic_charge_limit_w", 0.0
            ),
            "charge_limit_reason": getattr(
                self.controller, "_curtailment_opportunistic_charge_reason", "not_calculated"
            ),
            "export_mode": getattr(
                self.controller, "predischarge_export_mode", None
            ),
            "export_limit_w": getattr(
                self.controller, "predischarge_export_limit_w", 0.0
            ),
            "negative_injection_threshold": getattr(
                self.controller, "negative_injection_threshold", 0.0
            ),
            # ``None`` means that no safe external inverter decision is
            # available.  This is intentional: an automation must not treat a
            # missing plan or fail-safe state as permission to restore PV power.
            "inverter_curtailment_required": None,
        }
        if plan is None:
            return attrs
        now = datetime.now()
        next_window = next(
            (slot.start.isoformat() for slot in plan.risk_slots if slot.end > now),
            None,
        )
        required_headroom = max(0.0, float(plan.required_headroom_kwh))
        current_headroom = max(0.0, float(plan.current_headroom_kwh))
        headroom_deficit = max(0.0, required_headroom - current_headroom)
        plan_is_fail_safe = getattr(plan, "status", "") == "fail_safe"
        attrs.update({
            "next_window": next_window,
            "risk_slots": [
                {"start": slot.start.isoformat(), "end": slot.end.isoformat(), "price": slot.price}
                for slot in plan.risk_slots
            ],
            "required_headroom_kwh": round(plan.required_headroom_kwh, 3),
            "current_headroom_kwh": round(plan.current_headroom_kwh, 3),
            "solar_reserve_remaining_kwh": round(
                getattr(
                    plan,
                    "solar_reserve_remaining_kwh",
                    getattr(plan, "required_headroom_kwh", 0.0),
                ),
                3,
            ),
            "opportunistic_space_available_kwh": round(
                getattr(plan, "opportunistic_space_kwh", 0.0), 3
            ),
            "opportunistic_charge_limit_w": round(
                getattr(plan, "opportunistic_charge_limit_w", 0.0)
            ),
            "charge_limit_reason": getattr(
                plan, "opportunistic_charge_reason", "not_calculated"
            ),
            "headroom_deficit_kwh": round(headroom_deficit, 3),
            "planned_discharge_kwh": round(plan.planned_discharge_kwh, 3),
            "shortfall_kwh": round(plan.shortfall_kwh, 3),
            "target_soc_by_battery": {
                name: round(value, 1)
                for name, value in plan.target_soc_by_battery.items()
            },
            "selected_discharge_slots": [
                {
                    "start": slot.start.isoformat(),
                    "end": slot.end.isoformat(),
                    "price": slot.price,
                    "planned_energy_kwh": round(slot.planned_energy_kwh, 3),
                    "power_w": round(slot.power_w),
                }
                for slot in plan.selected_discharge_slots
            ],
            "plan_status": plan.status,
            "plan_reason": plan.reason,
            "evaluation_time": (
                plan.evaluation_time.isoformat() if plan.evaluation_time else None
            ),
        })
        if not plan_is_fail_safe and runtime_status != "fail_safe":
            attrs["inverter_curtailment_required"] = (
                runtime_status == "protected_window" and headroom_deficit > 1e-6
            )
        return attrs

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }


class PredictiveChargingStatusSensor(BinarySensorEntity):
    """Binary sensor indicating if predictive grid charging is currently active."""

    # This diagnostic sensor carries a large attribute payload that the recorder
    # would re-serialize on every poll. Exclude the heavy structures (lists/dicts)
    # and the per-cycle accumulators so only a small, stable row is recorded; the
    # live state keeps all attributes (panel + the hass.states.get() history-
    # restore fallback are unaffected — neither reads from recorder history).
    _unrecorded_attributes = frozenset({
        # heavy nested structures
        "active_slot_per_battery", "manual_slot_owned",
        "daily_consumption_history",
        "predictive_target_soc_pct", "selected_hours",
        # per-cycle accumulators
        "household_consumption_full_day_kwh",
        # last-decision diagnostic dump (changes on every evaluation)
        "stored_energy_kwh", "usable_energy_kwh",
        "cutoff_energy_kwh", "effective_min_soc", "avg_consumption_kwh",
        "total_available_kwh", "energy_deficit_kwh", "solar_forecast_kwh",
        "solar_surplus_kwh", "planned_grid_charge_kwh",
        "excluded_demand_claim_kwh", "solar_available_to_battery_kwh",
        "consumption_scope", "daily_avg_consumption_kwh", "consumed_today_kwh",
        "remaining_consumption_kwh", "remaining_solar_kwh",
        "consumption_rate_kwh_h", "consumption_accumulator_source",
        "energy_deadlines", "slot_energy_targets_kwh", "slot_deadlines",
        "decision_reason", "solar_forecast_periods",
    })

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        """Initialize the status sensor."""
        self.hass = hass
        self.entry = entry
        self.controller = controller

        self._attr_has_entity_name = True
        self._attr_translation_key = "predictive_charging_active"
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}predictive_charging_active"
        self.entity_id = system_entity_id("binary_sensor", "predictive_charging_active")
        self._attr_device_class = "running"
        self._attr_icon = "mdi:battery-charging-wireless"
        self._attr_should_poll = True  # Poll to update state
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self):
        """Return true if predictive charging is active."""
        return self.controller.grid_charging_active

    @property
    def extra_state_attributes(self):
        """Return additional state attributes."""
        attrs = {
            "in_charging_slot": self.controller._is_in_predictive_charging_slot(),
            "last_evaluation_soc": self.controller.last_evaluation_soc,
            "overridden": self.controller.predictive_charging_overridden,
        }

        if self.controller.charging_time_slots:
            attrs["time_slots"] = self.controller.charging_time_slots

        active_slot_per_battery = {}
        manual_slot_owned = []
        for coord in self.controller.coordinators:
            slot_d = self.controller._get_active_slot(coord, "discharge")
            slot_c = self.controller._get_active_slot(coord, "charge")
            slot = slot_d or slot_c
            if slot is not None:
                limits = self.controller._slot_battery_limits(slot, coord)
                active_slot_per_battery[coord.name] = {
                    "start_time": slot.get("start_time"),
                    "end_time": slot.get("end_time"),
                    "battery_scope": slot.get("battery_scope"),
                    "allow_charge": bool(slot.get("allow_charge")),
                    "allow_discharge": bool(slot.get("allow_discharge")),
                    "mode": slot.get("mode"),
                    "soc_override_enabled": bool(slot.get("soc_override_enabled")),
                    "power_override_enabled": bool(slot.get("power_override_enabled")),
                    "soc_min": limits.get("soc_min"),
                    "soc_max": limits.get("soc_max"),
                    "max_charge_power_w": limits.get("max_charge_power_w"),
                    "max_discharge_power_w": limits.get("max_discharge_power_w"),
                }
            if self.controller._is_manual_slot_owned(coord):
                manual_slot_owned.append(coord.name)
        if active_slot_per_battery:
            attrs["active_slot_per_battery"] = active_slot_per_battery
        if manual_slot_owned:
            attrs["manual_slot_owned"] = manual_slot_owned

        if self.controller.solar_forecast_sensor:
            attrs["solar_forecast_sensor"] = self.controller.solar_forecast_sensor
        if getattr(self.controller, "solar_forecast_remaining_sensor", None):
            attrs["solar_forecast_remaining_sensor"] = self.controller.solar_forecast_remaining_sensor
        if getattr(self.controller, "solar_forecast_source", None):
            attrs["solar_forecast_source"] = self.controller.solar_forecast_source

        attrs["max_contracted_power"] = self.controller.max_contracted_power

        # Home consumption diagnostics: home power is always derived
        # (grid + battery AC + solar); the household sensor was removed.
        attrs["consumption_source"] = "derived (grid + battery AC + solar)"
        full_day_consumption = round(self.controller._household_energy_accumulator, 2)
        attrs["household_consumption_full_day_kwh"] = full_day_consumption
        attrs["consumption_history_scope"] = "full_day_home"
        if self.controller._household_accumulator_date is not None:
            attrs["household_accumulator_date"] = self.controller._household_accumulator_date.isoformat()
        if self.controller._daily_solar_energy_date is not None:
            attrs["solar_accumulator_date"] = self.controller._daily_solar_energy_date.isoformat()

        initial_forecast = getattr(
            self.controller, "_daily_solar_forecast_initial_kwh", None
        )
        initial_date = getattr(
            self.controller, "_daily_solar_forecast_initial_date", None
        )
        if initial_forecast is not None:
            attrs["solar_forecast_initial_kwh"] = round(initial_forecast, 2)
        if initial_date is not None:
            attrs["solar_forecast_initial_date"] = initial_date.isoformat()

        # Persist daily consumption history for restoration after restarts
        if hasattr(self.controller, '_daily_consumption_history') and self.controller._daily_consumption_history:
            attrs["daily_consumption_history"] = [
                (d.isoformat(), c) for d, c in self.controller._daily_consumption_history
            ]

        # Add last decision data if available (for diagnostics).  Timeline
        # fields also have an independent snapshot because pre-slot/evening
        # balance checks replace _last_decision_data without rebuilding them.
        decision = getattr(self.controller, "_last_decision_data", None)
        if not isinstance(decision, dict):
            decision = {}
        chronological_diagnostics = getattr(
            self.controller, "_last_chronological_diagnostics", None
        )
        if not isinstance(chronological_diagnostics, dict):
            chronological_diagnostics = {}

        def _chronological_value(key, default=None):
            if key in decision:
                return decision[key]
            return chronological_diagnostics.get(key, default)

        if decision or chronological_diagnostics:
            if "chronological_planning_active" in decision:
                chronological_active = bool(
                    decision.get("chronological_planning_active")
                )
            else:
                chronological_active = bool(
                    getattr(
                        getattr(self.controller, "_dynamic_pricing_schedule", None),
                        "chronological_planning_active",
                        False,
                    )
                )
            attrs.update({
                "stored_energy_kwh": decision.get("stored_energy_kwh"),
                "usable_energy_kwh": decision.get("usable_energy_kwh"),
                "cutoff_energy_kwh": decision.get("cutoff_energy_kwh"),
                "effective_min_soc": decision.get("effective_min_soc"),
                "avg_consumption_kwh": decision.get("avg_consumption_kwh"),
                "consumption_scope": decision.get("consumption_scope"),
                "daily_avg_consumption_kwh": decision.get("daily_avg_consumption_kwh"),
                "consumed_today_kwh": decision.get("consumed_today_kwh"),
                "remaining_consumption_kwh": decision.get("remaining_consumption_kwh"),
                "remaining_solar_kwh": decision.get("remaining_solar_kwh"),
                "consumption_rate_kwh_h": decision.get("consumption_rate_kwh_h"),
                "consumption_accumulator_source": decision.get("consumption_accumulator_source"),
                "total_available_kwh": decision.get("total_available_kwh"),
                "energy_deficit_kwh": decision.get("energy_deficit_kwh"),
                "planned_grid_charge_kwh": decision.get("planned_grid_charge_kwh"),
                "solar_forecast_kwh": decision.get("solar_forecast_kwh"),
                "solar_forecast_original_source": _chronological_value(
                    "solar_forecast_original_source"
                ),
                "solar_forecast_conversion": _chronological_value(
                    "solar_forecast_conversion"
                ),
                "solar_remaining_raw_kwh": _chronological_value(
                    "solar_remaining_raw_kwh"
                ),
                "solar_safety_margin_kwh": _chronological_value(
                    "solar_safety_margin_kwh"
                ),
                "solar_remaining_effective_kwh": _chronological_value(
                    "solar_remaining_effective_kwh"
                ),
                "solar_surplus_kwh": decision.get("solar_surplus_kwh"),
                "excluded_demand_claim_kwh": _chronological_value(
                    "excluded_demand_claim_kwh"
                ),
                "solar_available_to_battery_kwh": _chronological_value(
                    "solar_available_to_battery_kwh"
                ),
                "decision_reason": decision.get("reason"),
                "chronological_planning_active": chronological_active,
                "chronological_source": _chronological_value("chronological_source"),
                "solar_timeline_source": _chronological_value(
                    "solar_timeline_source"
                ),
                "solar_timeline_effective_kwh": _chronological_value(
                    "solar_timeline_effective_kwh"
                ),
                "solar_timeline_fallback_reason": _chronological_value(
                    "solar_timeline_fallback_reason"
                ),
                "solar_timeline_energy_error_kwh": _chronological_value(
                    "solar_timeline_energy_error_kwh"
                ),
                "solar_profile_mature": _chronological_value("solar_profile_mature"),
                "solar_profile_days": _chronological_value("solar_profile_days"),
                "solar_profile_coverage_ratio": _chronological_value(
                    "solar_profile_coverage_ratio"
                ),
                "solar_profile_generation": _chronological_value(
                    "solar_profile_generation"
                ),
                "curtailment_timeline_mismatch": _chronological_value(
                    "curtailment_timeline_mismatch", False
                ),
                "earliest_projected_depletion": _chronological_value(
                    "earliest_projected_depletion"
                ),
                "minimum_projected_energy_kwh": _chronological_value(
                    "minimum_projected_energy_kwh"
                ),
                "minimum_projected_soc": _chronological_value(
                    "minimum_projected_soc"
                ),
                "deadline_required_kwh": _chronological_value(
                    "deadline_required_kwh", 0.0
                ),
                "flexible_required_kwh": _chronological_value(
                    "flexible_required_kwh", 0.0
                ),
                "deadline_shortfall_kwh": _chronological_value(
                    "deadline_shortfall_kwh", 0.0
                ),
                "total_shortfall_kwh": _chronological_value(
                    "total_shortfall_kwh", 0.0
                ),
                "energy_deadlines": _chronological_value("energy_deadlines", []),
                "chronological_plan_reason": _chronological_value(
                    "chronological_plan_reason"
                ),
                "guaranteed_floor_deadline": _chronological_value(
                    "guaranteed_floor_deadline"
                ),
            })
            # This was a rollout-only diagnostic. Keep exposing it for an
            # explicit legacy shadow evaluation, but do not publish an
            # ``unknown`` attribute in the normal automatic mode.
            shadow_source = _chronological_value("solar_shadow_selected_source")
            if shadow_source is not None:
                attrs["solar_shadow_selected_source"] = shadow_source

        schedule = getattr(self.controller, "_dynamic_pricing_schedule", None)
        if schedule is not None and getattr(schedule, "chronological_planning_active", False):
            attrs["slot_energy_targets_kwh"] = {
                f"{slot.start.isoformat()}/{slot.end.isoformat()}": round(float(value), 3)
                for slot, value in schedule.slot_energy_targets_kwh.items()
            }
            attrs["slot_deadlines"] = {
                f"{slot.start.isoformat()}/{slot.end.isoformat()}": (
                    deadline.isoformat() if deadline is not None else None
                )
                for slot, deadline in schedule.slot_deadlines.items()
            }
        elif getattr(self.controller, "_time_slot_chronological_plan", None) is not None:
            plan = self.controller._time_slot_chronological_plan
            targets = {}
            deadlines = {}
            for allocation in plan.allocations:
                key = (
                    f"{allocation.slot.start.isoformat()}/"
                    f"{allocation.slot.end.isoformat()}"
                )
                targets[key] = round(
                    targets.get(key, 0.0) + allocation.planned_battery_kwh,
                    3,
                )
                deadlines[key] = (
                    allocation.deadline.isoformat()
                    if allocation.deadline is not None
                    else None
                )
            attrs["slot_energy_targets_kwh"] = targets
            attrs["slot_deadlines"] = deadlines

        # Keep the live remainder current even between pricing reevaluations.
        # This also gives legacy whole-day forecast sensors the same dashboard
        # value as the control path, which maps the forecast's future periods
        # or remaining solar curve without moving missed past production later.
        if getattr(self.controller, "_pricing_mgr", None) is not None:
            try:
                forecast = read_solar_forecast_kwh(self.hass, self.controller)
                if forecast is not None:
                    now = datetime.now()
                    remaining = self.controller._pricing_mgr._remaining_solar_today_kwh(now)
                    attrs["remaining_solar_kwh"] = round(max(0.0, float(remaining)), 2)
            except (AttributeError, TypeError, ValueError):
                _LOGGER.debug("Predictive status: live solar remainder unavailable", exc_info=True)

        # Per-battery grid-only SOC targets (set at charge initialisation, None when not charging)
        if hasattr(self.controller, '_predictive_charge_target_soc') and self.controller._predictive_charge_target_soc:
            attrs["predictive_target_soc_pct"] = {
                c.name: round(v, 1)
                for c, v in self.controller._predictive_charge_target_soc.items()
            }

        # Dynamic pricing attributes
        attrs["pricing_mode"] = self.controller.predictive_charging_mode

        # Real-time price attributes
        if self.controller.predictive_charging_mode == "realtime_price":
            attrs["current_price"] = self.controller._pricing_mgr._get_current_price()
            threshold = None
            if self.controller.average_price_sensor:
                avg_state = self.controller.hass.states.get(self.controller.average_price_sensor)
                if avg_state is not None:
                    try:
                        threshold = float(avg_state.state)
                    except (ValueError, TypeError):
                        pass
            if threshold is None:
                threshold = self.controller.max_price_threshold
            attrs["price_threshold"] = threshold
            attrs["price_is_cheap"] = (
                attrs.get("current_price") is not None
                and threshold is not None
                and attrs["current_price"] <= threshold
            )
            attrs["realtime_charging_active"] = getattr(self.controller, "_realtime_price_charging", False)

        if self.controller.predictive_charging_mode == "dynamic_pricing":
            attrs["price_data_status"] = getattr(self.controller, "_price_data_status", "not_evaluated")
            attrs["max_price_threshold"] = self.controller.max_price_threshold
            attrs["negative_price_charging_enabled"] = getattr(
                self.controller, "negative_price_charging_enabled", False
            )
            attrs["active_slot_purpose"] = getattr(
                self.controller, "_active_dynamic_slot_purpose", None
            )

        if self.controller._dynamic_pricing_schedule:
            schedule = self.controller._dynamic_pricing_schedule
            attrs["charging_needed"] = schedule.charging_needed
            attrs["schedule_type"] = getattr(schedule, "schedule_type", "deficit")
            attrs["deficit_charging_needed"] = getattr(
                schedule, "deficit_charging_needed", schedule.charging_needed
            )
            attrs["negative_price_charging_needed"] = getattr(
                schedule, "negative_price_charging_needed", False
            )
            attrs["negative_price_energy_kwh"] = getattr(
                schedule, "negative_price_energy_kwh", 0.0
            )
            attrs["negative_price_hours_needed"] = getattr(
                schedule, "negative_price_hours_needed", 0.0
            )
            attrs["hours_needed"] = schedule.hours_needed
            attrs["selected_hours"] = [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "price": s.price,
                    "purpose": (
                        schedule.purpose_for(s)
                        if hasattr(schedule, "purpose_for")
                        else "deficit"
                    ),
                }
                for s in schedule.selected_slots
            ]
            attrs["average_price"] = schedule.average_price
            attrs["estimated_cost"] = schedule.estimated_cost
            attrs["in_cheap_slot"] = self.controller._is_in_dynamic_pricing_slot()
            attrs["max_price_threshold"] = self.controller.max_price_threshold
            attrs["evaluation_time"] = schedule.evaluation_time.isoformat()
            attrs["price_integration_type"] = self.controller.price_integration_type

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
