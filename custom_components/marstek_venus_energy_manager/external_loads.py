"""Excluded-device and EV-charger load management for Marstek Venus.

Owns:
- Net kW correction to the home sensor for excluded-device accounting
- Power adjustment for excluded devices (applied before PD setpoint decisions)
- EV charger no-telemetry state detection and 5-minute battery pause logic

Reads/writes the controller's existing attributes by reference for backward
compatibility with the rest of the control loop:
    previous_power, _excluded_included_adjustment,
    _ev_charging_states, _ev_pause_until.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Substrings that indicate an EV is actively charging, across supported languages.
# Case-insensitive match against the sensor state string.
# Add new entries here when a new language reports a different charging keyword.
_CHARGING_SUBSTRINGS: frozenset[str] = frozenset({
    "charg",     # EN/FR: charging, charge, chargement
    "cargand",   # ES: cargando
    "carreg",    # CA/PT: carregant, carregando
    "laden",     # NL/DE: laden, ladend
    "caricand",  # IT: caricando
    "carica",    # IT: in carica
    "ladd",      # SV: laddar, laddning
    "lading",    # NO/DA: lading, oplading
})


class ExternalLoads:
    """Manages excluded-device and EV-charger load adjustments."""

    def __init__(
        self,
        hass: "HomeAssistant",
        config_entry: "ConfigEntry",
        controller: Any,
    ) -> None:
        self._hass = hass
        self._config_entry = config_entry
        self._controller = controller

    def consumption_delta_kw(self) -> float:
        """Net kW correction to apply to the home sensor for excluded-device accounting.

        Returns a value to ADD to the raw home sensor reading so the accumulator
        reflects only the load the battery is expected to cover:
          - included_in_consumption=True  → device IS in home sensor but battery skips it → subtract
          - included_in_consumption=False → device NOT in home sensor but battery covers it → add
        ev_charger_no_telemetry devices are skipped (no numeric power sensor).
        Unavailable sensors are silently ignored.
        """
        excluded_devices = self._config_entry.data.get("excluded_devices", [])
        if not excluded_devices:
            return 0.0

        delta = 0.0
        for device in excluded_devices:
            if not device.get("enabled", True):
                continue
            if device.get("ev_charger_no_telemetry", False):
                continue
            power_sensor = device.get("power_sensor")
            if not power_sensor:
                continue
            state = self._hass.states.get(power_sensor)
            if state is None or state.state in ("unknown", "unavailable"):
                continue
            try:
                power_w = float(state.state)
            except (ValueError, TypeError):
                continue
            unit = state.attributes.get("unit_of_measurement", "W")
            device_kw = power_w / 1000.0 if unit == "W" else power_w
            if device.get("included_in_consumption", True):
                delta -= device_kw
            else:
                delta += device_kw

        return delta

    def calculate_adjustment(self) -> float:
        """Calculate power adjustment for excluded devices.

        Logic:
        - included_in_consumption=True, allow_solar_surplus=False:
          → SUBTRACT device power (battery must not power this device)
        - included_in_consumption=True, allow_solar_surplus=True, solar sensor configured:
          → SUBTRACT max(0, device_power - solar_power_w): only the portion of device
             demand that exceeds solar production. Battery covers home deficit normally
             and charges from true solar surplus; it never discharges for the device.
             Stable for all ratios of device_power to solar_power (no sign-dependent
             discontinuity, no oscillation).
        - included_in_consumption=True, allow_solar_surplus=True, no solar sensor:
          → NO adjustment + sets _solar_surplus_discharge_blocked so the PD section
             clamps new_power >= 0 while the device is active (>10 W). Fallback when
             solar production is not available.
        - included_in_consumption=False:
          → ADD device power (battery should cover load the home sensor misses)

        Returns the total adjustment to apply to sensor_actual.
        Positive = reduce battery discharge
        Negative = increase battery discharge
        """
        excluded_devices = self._config_entry.data.get("excluded_devices", [])
        if not excluded_devices:
            self._controller._excluded_included_adjustment = 0.0
            return 0.0

        solar_surplus_blocks_discharge = False
        solar_sensor_id = getattr(self._controller, "solar_production_sensor", None)

        total_adjustment = 0.0
        included_adjustment = 0.0  # Track included_in_consumption portion separately
        for device in excluded_devices:
            if not device.get("enabled", True):
                continue
            # EV chargers in no-telemetry mode expose a state sensor, not a numeric
            # power sensor – their behaviour is handled by _check_ev_charger_state().
            if device.get("ev_charger_no_telemetry", False):
                continue

            power_sensor = device.get("power_sensor")
            if not power_sensor:
                continue

            state = self._hass.states.get(power_sensor)
            if state is None or state.state in ("unknown", "unavailable"):
                _LOGGER.debug("Excluded device sensor %s not available", power_sensor)
                continue

            try:
                device_power_raw = float(state.state)
                unit = state.attributes.get("unit_of_measurement", "W")
                device_power = device_power_raw if unit == "W" else device_power_raw * 1000.0
                included_in_consumption = device.get("included_in_consumption", True)
                allow_solar_surplus = device.get("allow_solar_surplus", False)

                if included_in_consumption:
                    if allow_solar_surplus:
                        if solar_sensor_id:
                            # Exclude only the portion of device demand that exceeds solar.
                            # sensor_actual = grid − adjustment = home ± battery_net, so the
                            # PD drives battery to cover home deficit and charge solar surplus
                            # without ever discharging for the device.
                            solar_power_w = self._read_sensor_w(solar_sensor_id)
                            grid_portion = max(0.0, device_power - solar_power_w)
                            total_adjustment += grid_portion
                            included_adjustment += grid_portion
                            _LOGGER.debug(
                                "Excluded device %s consuming %.1fW, solar=%.1fW → excluding %.1fW (device-over-solar portion)",
                                power_sensor, device_power, solar_power_w, grid_portion,
                            )
                        else:
                            # No solar sensor: block discharge instead (battery idle while device active)
                            if device_power > 10:
                                solar_surplus_blocks_discharge = True
                            _LOGGER.debug(
                                "Excluded device %s consuming %.1fW (solar surplus, no solar sensor → discharge_blocked=%s)",
                                power_sensor, device_power, device_power > 10,
                            )
                    else:
                        total_adjustment += device_power
                        included_adjustment += device_power
                        _LOGGER.debug("Excluded device %s consuming %.1fW (included in consumption, SUBTRACTING)",
                                    power_sensor, device_power)
                else:
                    # Device is NOT in home sensor → ADD (power from battery)
                    total_adjustment -= device_power
                    _LOGGER.debug("Additional device %s consuming %.1fW (NOT in consumption, ADDING)",
                                    power_sensor, device_power)
            except (ValueError, TypeError):
                _LOGGER.warning("Could not parse device sensor %s: %s", power_sensor, state.state)

        # Store the included-in-consumption portion for capacity protection
        self._controller._excluded_included_adjustment = included_adjustment
        self._controller._solar_surplus_discharge_blocked = solar_surplus_blocks_discharge
        return total_adjustment

    def _read_sensor_w(self, entity_id: str) -> float:
        """Read a power sensor and return its value in watts. Returns 0.0 if unavailable."""
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return 0.0
        try:
            raw = float(state.state)
            unit = state.attributes.get("unit_of_measurement", "W")
            return raw if unit == "W" else raw * 1000.0
        except (ValueError, TypeError):
            return 0.0

    def check_ev_charger_state(self) -> tuple[bool, bool]:
        """Check state of EV chargers configured with no-telemetry mode.

        Detects a charging state by matching against _CHARGING_SUBSTRINGS
        (case-insensitive). Covers EN, FR, ES, NL, DE, CA, PT, IT, SV, NO/DA.

        On the first cycle a charging state is detected, a 5-minute pause is
        started so the EV can grab as much current from the grid as it needs
        before the battery interferes.  After the pause the battery is allowed
        to charge from solar surplus but must never discharge.

        Returns:
            (pause_active, ev_charging_active):
            - pause_active: True if the 5-min post-detection pause is still running
            - ev_charging_active: True if EV is charging and pause has expired
        """
        excluded_devices = self._config_entry.data.get("excluded_devices", [])
        now = dt_util.utcnow()
        pause_active = False
        ev_charging_active = False

        for device in excluded_devices:
            if not device.get("enabled", True):
                continue
            if not device.get("ev_charger_no_telemetry", False):
                continue

            sensor_id = device.get("power_sensor")
            if not sensor_id:
                continue

            state = self._hass.states.get(sensor_id)
            if state is None or state.state in ("unknown", "unavailable"):
                continue

            state_lower = state.state.lower().strip()
            is_charging = any(sub in state_lower for sub in _CHARGING_SUBSTRINGS)

            prev_charging = self._controller._ev_charging_states.get(sensor_id, False)

            if is_charging and not prev_charging:
                # EV just started charging – start 5-minute battery pause
                self._controller._ev_pause_until[sensor_id] = now + timedelta(minutes=5)
                _LOGGER.info(
                    "EV charger %s: charging detected – 5-minute battery pause started",
                    sensor_id,
                )
            elif not is_charging and prev_charging:
                # EV stopped charging – cancel any remaining pause
                self._controller._ev_pause_until.pop(sensor_id, None)
                _LOGGER.info(
                    "EV charger %s: charging stopped – normal battery operation resumed",
                    sensor_id,
                )

            self._controller._ev_charging_states[sensor_id] = is_charging

            pause_until = self._controller._ev_pause_until.get(sensor_id)
            if pause_until is not None:
                if now < pause_until:
                    pause_active = True
                    _LOGGER.debug(
                        "EV charger %s: pause active, %ds remaining",
                        sensor_id,
                        (pause_until - now).total_seconds(),
                    )
                else:
                    # Pause has expired; remove entry and switch to discharge-block mode
                    self._controller._ev_pause_until.pop(sensor_id, None)
                    if is_charging:
                        ev_charging_active = True
            elif is_charging:
                ev_charging_active = True

        return pause_active, ev_charging_active
