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
import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import UnitOfEnergy
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# States that mean "the device is here and can draw". Kept wider than ``on`` so
# a device_tracker or a localised text status sensor can serve as the presence
# entity without a template helper in between. Matched whole, never as a
# substring: every negative phrasing contains its own positive ("disconnected"
# contains "connected", "ontladen" contains "laden"), so a substring pass would
# read every absent state as present. Anything unknown reads as absent, which is
# the safe direction.
_PRESENT_STATES: frozenset[str] = frozenset({
    "on",
    "true",
    "home",
    "present",
    "connected",   # EN
    "plugged",     # EN
    "plugged in",  # EN
    "verbunden",   # DE
    "aangesloten", # NL
    "aanwezig",    # NL
    "connesso",    # IT
    "connecté",    # FR
    "branché",     # FR
    "conectado",   # ES/PT
    "connectat",   # CA
    "charging",    # EN
    "cargando",    # ES
    "laden",       # NL/DE
})

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

# Dynamic power control always uses the excluded device's power sensor. An
# optional activity sensor can announce demand before numeric power appears,
# solving the cold-start deadlock without an external automation. A short yield
# then lets the external controller claim export before Omnibattery resumes on
# any residual. Brief zero-power pauses are latched for sensor-less setups.
DYNAMIC_CONTROL_ACTIVE_POWER_W = 100.0
DYNAMIC_CONTROL_HOLD = timedelta(minutes=5)
DYNAMIC_CONTROL_INITIAL_YIELD = timedelta(seconds=30)
DYNAMIC_CONTROL_REYIELD = timedelta(seconds=20)
DYNAMIC_CONTROL_SOLAR_STEP_W = 200.0
DYNAMIC_CONTROL_FALLBACK_PROBE = timedelta(minutes=5)


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
        self._dynamic_hold_until: dict[str, datetime] = {}
        self._dynamic_yield_until: dict[str, datetime] = {}
        self._dynamic_next_probe: dict[str, datetime] = {}
        self._dynamic_solar_reference_w: dict[str, float] = {}
        self._dynamic_device_reference_w: dict[str, float] = {}
        self._dynamic_was_drawing: dict[str, bool] = {}
        self.dynamic_power_control_status: dict[str, Any] = {
            "active": False,
            "charge_blocked": False,
            "discharge_blocked": False,
            "devices": [],
            "discharge_blocked_devices": [],
            "phases": {},
        }

    def _clear_dynamic_power_control_state(self, key: str) -> None:
        """Forget the runtime latch/probe state for one excluded device."""
        self._dynamic_hold_until.pop(key, None)
        self._dynamic_yield_until.pop(key, None)
        self._dynamic_next_probe.pop(key, None)
        self._dynamic_solar_reference_w.pop(key, None)
        self._dynamic_device_reference_w.pop(key, None)
        self._dynamic_was_drawing.pop(key, None)

    @staticmethod
    def _state_is_active(state: Any) -> bool:
        """Return whether a state/binary sensor reports EV charging or demand."""
        if state is None or state.state in ("unknown", "unavailable"):
            return False
        value = str(state.state).lower().strip()
        return value in {"on", "true", "1"} or any(
            substring in value for substring in _CHARGING_SUBSTRINGS
        )

    def _activity_sensor_is_active(self, sensor_id: str | None) -> bool:
        """Read one optional device-activity entity."""
        return bool(sensor_id) and self._state_is_active(self._hass.states.get(sensor_id))

    def refresh_dynamic_power_control(self) -> dict[str, Any]:
        """Refresh dynamic-load latches and return current operation blocks.

        Activity is inferred from the existing numeric power sensor. An optional
        activity sensor may request priority before numeric demand appears:

        * active state before demand: charging stays blocked until load appears;
        * first detection (>100 W): battery charging yields for 30 seconds;
        * while drawing: charging may resume for genuine residual export;
        * a >=200 W rise in available margin (solar power minus device power)
          starts a new 20-second yield;
        * without a solar sensor, a 20-second probe runs every five minutes;
        * when power drops: charging remains blocked for a short restart grace
          so the external controller can restart after clouds or a phase
          transition, then genuine residual export may charge the battery;
          discharge remains blocked for the full five-minute restart hold.
        * with Cover Home disabled: discharge remains blocked throughout every
          active phase so stale telemetry cannot hide grid import from the
          external controller.

        Only devices with both Allow Solar Surplus and Dynamic Power Control ON
        participate.  The returned mapping is also exposed in diagnostics.
        """
        now = dt_util.utcnow()
        solar_sensor_id = getattr(self._controller, "solar_production_sensor", None)
        solar_w = self._read_sensor_w_opt(solar_sensor_id) if solar_sensor_id else None

        eligible_keys: set[str] = set()
        priority_devices: list[str] = []
        blocked_devices: list[str] = []
        discharge_blocked_devices: list[str] = []
        phases: dict[str, str] = {}
        max_hold_remaining = 0
        max_yield_remaining = 0

        for index, device in enumerate(self._config_entry.data.get("excluded_devices", [])):
            sensor_id = device.get("power_sensor")
            activity_sensor_id = device.get("activity_sensor")
            key = f"{index}:{sensor_id or ''}"
            if activity_sensor_id:
                key = f"{key}:{activity_sensor_id}"
            eligible = (
                device.get("enabled", True)
                and not device.get("ev_charger_no_telemetry", False)
                and device.get("included_in_consumption", True)
                and device.get("allow_solar_surplus", False)
                and device.get("dynamic_power_control", False)
                and bool(sensor_id)
            )
            if not eligible:
                self._clear_dynamic_power_control_state(key)
                continue

            eligible_keys.add(key)
            device_power = self._read_sensor_w_opt(sensor_id)
            drawing = device_power is not None and device_power > DYNAMIC_CONTROL_ACTIVE_POWER_W
            activity_requested = self._activity_sensor_is_active(activity_sensor_id)
            was_drawing = self._dynamic_was_drawing.get(key, False)
            block_discharge = not device.get("cover_home_when_active", False)

            if drawing:
                priority_devices.append(sensor_id)
                if block_discharge:
                    discharge_blocked_devices.append(sensor_id)
                self._dynamic_hold_until[key] = now + DYNAMIC_CONTROL_HOLD

                if not was_drawing:
                    self._dynamic_yield_until[key] = now + DYNAMIC_CONTROL_INITIAL_YIELD
                    self._dynamic_next_probe[key] = now + DYNAMIC_CONTROL_FALLBACK_PROBE
                    if solar_w is not None:
                        self._dynamic_solar_reference_w[key] = solar_w
                        self._dynamic_device_reference_w[key] = device_power
                    _LOGGER.info(
                        "Dynamic power control for %s: %.0fW detected, yielding battery charge for %ds",
                        sensor_id,
                        device_power,
                        int(DYNAMIC_CONTROL_INITIAL_YIELD.total_seconds()),
                    )

                self._dynamic_was_drawing[key] = True
                yield_until = self._dynamic_yield_until.get(key, now)

                if solar_w is not None:
                    reference_solar_w = self._dynamic_solar_reference_w.get(key, solar_w)
                    reference_device_w = self._dynamic_device_reference_w.get(
                        key, device_power
                    )
                    reference_margin_w = reference_solar_w - reference_device_w
                    margin_w = solar_w - device_power
                    if margin_w < reference_margin_w:
                        # Follow reductions immediately so a later recovery can
                        # trigger a fresh yield after a cumulative 200 W rise.
                        self._dynamic_solar_reference_w[key] = solar_w
                        self._dynamic_device_reference_w[key] = device_power
                    elif now < yield_until:
                        # The external controller is already being given room;
                        # use the newest margin as the next comparison baseline.
                        self._dynamic_solar_reference_w[key] = solar_w
                        self._dynamic_device_reference_w[key] = device_power
                    elif margin_w - reference_margin_w >= DYNAMIC_CONTROL_SOLAR_STEP_W:
                        yield_until = now + DYNAMIC_CONTROL_REYIELD
                        self._dynamic_yield_until[key] = yield_until
                        self._dynamic_solar_reference_w[key] = solar_w
                        self._dynamic_device_reference_w[key] = device_power
                        _LOGGER.debug(
                            "Dynamic power control for %s: available margin rose %.0fW "
                            "(solar %.0fW, device %.0fW), re-yielding for %ds",
                            sensor_id,
                            margin_w - reference_margin_w,
                            solar_w,
                            device_power,
                            int(DYNAMIC_CONTROL_REYIELD.total_seconds()),
                        )
                else:
                    next_probe = self._dynamic_next_probe.get(key, now)
                    if now >= next_probe and now >= yield_until:
                        yield_until = now + DYNAMIC_CONTROL_REYIELD
                        self._dynamic_yield_until[key] = yield_until
                        self._dynamic_next_probe[key] = now + DYNAMIC_CONTROL_FALLBACK_PROBE

                if now < yield_until:
                    blocked_devices.append(sensor_id)
                    phases[sensor_id] = "yielding"
                    max_yield_remaining = max(
                        max_yield_remaining,
                        int((yield_until - now).total_seconds()),
                    )
                else:
                    phases[sensor_id] = "monitoring_residual"
            elif activity_requested:
                # The wallbox/controller says it wants power but its numeric
                # sensor is still idle. Keep the battery out of the way until
                # demand appears; the normal 30-second yield starts afterwards.
                self._dynamic_was_drawing[key] = False
                priority_devices.append(sensor_id)
                blocked_devices.append(sensor_id)
                if block_discharge:
                    discharge_blocked_devices.append(sensor_id)
                phases[sensor_id] = "waiting_for_load"
            else:
                self._dynamic_was_drawing[key] = False
                if activity_sensor_id:
                    # An explicit inactive state is authoritative: unlike the
                    # power-only fallback, there is no possible restart to hold.
                    self._clear_dynamic_power_control_state(key)
                    continue
                hold_until = self._dynamic_hold_until.get(key)
                if hold_until is not None and now < hold_until:
                    priority_devices.append(sensor_id)
                    if block_discharge:
                        discharge_blocked_devices.append(sensor_id)

                    # A power-only sensor cannot distinguish a completed
                    # charge from a short controller pause. Give the wallbox
                    # the same short opportunity used by the initial yield,
                    # but do not keep solar-residual charging blocked for the
                    # entire five-minute safety hold.
                    if was_drawing:
                        yield_until = now + DYNAMIC_CONTROL_INITIAL_YIELD
                        self._dynamic_yield_until[key] = yield_until
                        if solar_w is not None:
                            self._dynamic_solar_reference_w[key] = solar_w
                            if device_power is not None:
                                self._dynamic_device_reference_w[key] = device_power
                    else:
                        yield_until = self._dynamic_yield_until.get(key, now)

                    if solar_w is not None:
                        if device_power is None:
                            # A missing device reading cannot produce a margin;
                            # retain the existing solar-only fallback safely.
                            reference_w = self._dynamic_solar_reference_w.get(key, solar_w)
                            margin_rise_w = solar_w - reference_w
                        else:
                            reference_solar_w = self._dynamic_solar_reference_w.get(
                                key, solar_w
                            )
                            reference_device_w = self._dynamic_device_reference_w.get(
                                key, device_power
                            )
                            reference_margin_w = reference_solar_w - reference_device_w
                            margin_w = solar_w - device_power
                            margin_rise_w = margin_w - reference_margin_w

                        if margin_rise_w < 0:
                            self._dynamic_solar_reference_w[key] = solar_w
                            if device_power is not None:
                                self._dynamic_device_reference_w[key] = device_power
                        elif margin_rise_w >= DYNAMIC_CONTROL_SOLAR_STEP_W:
                            # Keep an existing grace intact if it is longer
                            # than the re-yield; otherwise start a fresh
                            # twenty-second opportunity for the wallbox.
                            re_yield_until = now + DYNAMIC_CONTROL_REYIELD
                            yield_until = max(yield_until, re_yield_until)
                            self._dynamic_yield_until[key] = yield_until
                            self._dynamic_solar_reference_w[key] = solar_w
                            if device_power is not None:
                                self._dynamic_device_reference_w[key] = device_power
                            _LOGGER.debug(
                                "Dynamic power control for %s: available margin rose %.0fW "
                                "during restart hold, re-yielding for %ds",
                                sensor_id,
                                margin_rise_w,
                                int(DYNAMIC_CONTROL_REYIELD.total_seconds()),
                            )

                    if now < yield_until:
                        blocked_devices.append(sensor_id)
                        max_yield_remaining = max(
                            max_yield_remaining,
                            int((yield_until - now).total_seconds()),
                        )
                    phases[sensor_id] = "restart_hold"
                    max_hold_remaining = max(
                        max_hold_remaining,
                        int((hold_until - now).total_seconds()),
                    )
                else:
                    self._clear_dynamic_power_control_state(key)

        # Remove runtime state for devices deleted or made ineligible since the
        # previous options hot-reload.
        known_keys = (
            set(self._dynamic_hold_until)
            | set(self._dynamic_yield_until)
            | set(self._dynamic_device_reference_w)
        )
        for stale_key in known_keys - eligible_keys:
            self._clear_dynamic_power_control_state(stale_key)

        status = {
            "active": bool(priority_devices),
            "charge_blocked": bool(blocked_devices),
            "discharge_blocked": bool(discharge_blocked_devices),
            "devices": priority_devices,
            "blocked_devices": blocked_devices,
            "discharge_blocked_devices": discharge_blocked_devices,
            "phases": phases,
            "hold_remaining_s": max_hold_remaining,
            "yield_remaining_s": max_yield_remaining,
        }
        self.dynamic_power_control_status = status
        return status

    @staticmethod
    def _exclusion_factor(device: dict) -> float:
        """Fraction of a device's demand to keep excluded from the battery.

        Set per-device at runtime via the excluded_device_exclusion_pct slider.
        100 (default) → 1.0 (fully excluded, battery covers 0%).
        60 → 0.6 (battery is allowed to cover the remaining 40%).
        Only meaningful for the included_in_consumption (exclusion) path.
        """
        try:
            pct = float(device.get("exclusion_pct", 100))
        except (ValueError, TypeError):
            return 1.0
        return max(0.0, min(100.0, pct)) / 100.0

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
                delta -= device_kw * self._exclusion_factor(device)
            else:
                delta += device_kw

        return delta

    def calculate_adjustment(self) -> float:
        """Calculate power adjustment for excluded devices.

        Logic:
        - included_in_consumption=True, allow_solar_surplus=False:
          → SUBTRACT device power (battery must not power this device)
        - included_in_consumption=True, allow_solar_surplus=True, solar sensor +
          Home Consumption available:
          → SUBTRACT max(0, device_power - surplus), where surplus = max(0, solar -
             home_base_load) and home_base_load = home_consumption - excluded devices.
             PV surplus offsets the device BEFORE the battery charges it (#421): the
             battery covers the home deficit and charges only the surplus the device
             cannot absorb, never discharging for the device and never exporting.
             Crediting only the real surplus (not raw PV) is what fixes #415.
             The surplus is a shared budget across solar-surplus devices.
        - included_in_consumption=True, allow_solar_surplus=True, solar sensor but
          Home Consumption unavailable:
          → full exclusion (battery never powers the device); conservative fallback.
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

        # PV-surplus priority (#421/#415): the solar that may offset an excluded
        # device is only what's left AFTER the genuine home load, not the raw PV.
        # base_load = home_consumption − Σ(in-home excluded devices); the surplus
        # is a shared budget so multiple surplus devices don't each claim it all.
        # None = Home Consumption unavailable or held after an invalid balance
        # → conservative full exclusion below. The display sensor may retain a
        # numeric last value, but control must not act on that stale estimate.
        # solar_remaining tracks the FULL PV left (before the home reservation) so a
        # device with cover_home_when_active can be offset by raw PV (pre-#415 rule),
        # letting the battery cover the home deficit instead of sitting idle (#42).
        surplus_remaining: float | None = None
        solar_remaining = self._read_sensor_w(solar_sensor_id) if solar_sensor_id else 0.0
        if solar_sensor_id:
            home_w = self._read_home_consumption_w_opt(
                getattr(self._controller, "home_consumption_sensor", None)
            )
            if home_w is not None:
                base_load = max(0.0, home_w - self._included_device_power_w())
                surplus_remaining = max(0.0, solar_remaining - base_load)

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
                # Per-device slider: fraction of demand kept excluded (100% = full).
                factor = self._exclusion_factor(device)

                if included_in_consumption:
                    if allow_solar_surplus:
                        if solar_sensor_id:
                            # PV-surplus priority: hand the real surplus to the device
                            # first, then exclude only the grid portion it must import.
                            # The battery covers the home deficit and charges any surplus
                            # the device can't absorb, never discharging for the device.
                            cover_home = device.get("cover_home_when_active", False)
                            if surplus_remaining is None and not cover_home:
                                # Home Consumption unavailable → exclude fully (battery
                                # never powers the device; conservative fallback).
                                grid_portion = device_power * factor
                            else:
                                # cover_home devices draw from raw PV (pre-#415): only
                                # their real grid draw max(0, device − solar) is excluded,
                                # so the battery covers the remaining home deficit (#42).
                                # Normal devices draw the home-reserved surplus (#421).
                                avail = solar_remaining if cover_home else surplus_remaining
                                offset = min(device_power, avail)
                                solar_remaining = max(0.0, solar_remaining - offset)
                                if surplus_remaining is not None:
                                    surplus_remaining = max(0.0, surplus_remaining - offset)
                                grid_portion = (device_power - offset) * factor
                            total_adjustment += grid_portion
                            included_adjustment += grid_portion
                            _LOGGER.debug(
                                "Excluded device %s consuming %.1fW → excluding %.1fW "
                                "(PV-surplus priority, cover_home=%s, surplus_left=%s)",
                                power_sensor, device_power, grid_portion, cover_home, surplus_remaining,
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
                        total_adjustment += device_power * factor
                        included_adjustment += device_power * factor
                        _LOGGER.debug("Excluded device %s consuming %.1fW (included in consumption, SUBTRACTING %.0f%%)",
                                    power_sensor, device_power, factor * 100)
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

    def _read_sensor_w_opt(self, entity_id: str | None) -> float | None:
        """Like _read_sensor_w but returns None (not 0.0) when the sensor is
        missing/unavailable, so a genuine 0 W reading is distinguishable from a
        missing sensor (needed to decide the PV-surplus fallback)."""
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            raw = float(state.state)
        except (ValueError, TypeError):
            return None
        unit = state.attributes.get("unit_of_measurement", "W")
        return raw if unit == "W" else raw * 1000.0

    def _read_sensor_kwh_opt(self, entity_id: str | None) -> float | None:
        """Energy sibling of _read_sensor_w_opt: read a sensor as kWh.

        Returns None when the sensor is missing, unavailable, unparsable or
        carries a unit that is not an energy unit, so the caller keeps its
        no-claim behaviour instead of reserving solar against a number whose
        magnitude is unknown. Conversion uses Home Assistant's own energy
        converter, so its unit strings are matched exactly: ``mWh`` (milli)
        and ``MWh`` (mega) differ only in case.
        Negative readings are clamped to 0.0 because some upstreams publish a
        transient -0.0 between sessions.
        """
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            raw = float(state.state)
        except (ValueError, TypeError):
            return None
        if not math.isfinite(raw):
            return None
        # A missing unit falls through to the converter too, so the "no claim"
        # decision is logged in one place. Template helpers often carry none.
        unit = state.attributes.get("unit_of_measurement")
        try:
            raw = EnergyConverter.convert(raw, str(unit), UnitOfEnergy.KILO_WATT_HOUR)
        except (HomeAssistantError, ValueError, TypeError):
            _LOGGER.debug(
                "Remaining-demand sensor %s reports unit %s, which is not an "
                "energy unit; ignoring its claim",
                entity_id,
                unit,
            )
            return None
        return max(0.0, raw)

    def claimable_solar_demand_kwh(self) -> float | None:
        """Σ energy (kWh) excluded devices still expect to take from the sun.

        Eligibility and weighting follow ``consumption_delta_kw`` exactly, so
        the same demand is never removed twice:
          - only devices the home sensor already includes may claim; a device
            it does not see is an additional load the battery is meant to
            cover, and its demand is not in the consumption forecast either;
          - ``exclusion_pct`` scales the claim the same way it scales the
            consumption correction. At 50 % the forecast keeps half the
            device's demand, so only the other half may be reserved from solar;
          - ``ev_charger_no_telemetry`` devices are skipped for the same reason
            they are skipped there: their demand stays inside the consumption
            forecast, which already covers it.

        A device may also declare a presence entity. Some upstreams keep
        publishing a remaining demand while the device cannot possibly draw --
        evcc reports the energy to the vehicle's SOC target whether or not a
        car is plugged in -- and reserving solar for that takes it away from the
        battery for nothing.

        Returns None when no device contributes a usable reading, so callers
        can keep today's behaviour untouched. A device whose presence entity
        says it is absent contributes 0.0 rather than nothing: it is a reading,
        and the difference decides whether the intraday re-plan runs.
        """
        total: float | None = None
        for device in self._config_entry.data.get("excluded_devices", []):
            sensor_id = device.get("remaining_demand_sensor")
            if not sensor_id:
                continue
            if not device.get("enabled", True):
                continue
            if device.get("ev_charger_no_telemetry", False):
                continue
            if not device.get("included_in_consumption", True):
                continue
            if not self._demand_is_present(device):
                # Zero, not "skip". Callers read None as "no usable reading" and
                # keep their last reference, which would leave the intraday
                # re-plan blind to a car that has just left.
                total = 0.0 if total is None else total
                continue
            value = self._read_sensor_kwh_opt(sensor_id)
            if value is None:
                continue
            value *= self._exclusion_factor(device)
            total = value if total is None else total + value
        return total

    def _demand_is_present(self, device: dict) -> bool:
        """Whether this device's declared demand can actually be drawn.

        True when no presence entity is configured, which is every existing
        installation. With one configured, the state must match one of
        ``_PRESENT_STATES`` whole. An unavailable, unknown, unrecognised or
        missing entity is treated as absent, so a broken sensor falls back to
        not reserving rather than to reserving for a device that may not be
        there.

        A refusal is logged, because a state nobody anticipated would otherwise
        zero a claim the user deliberately configured, in silence.
        """
        entity_id = device.get("remaining_demand_presence_sensor")
        if not entity_id:
            return True
        state = self._hass.states.get(entity_id)
        if state is None:
            _LOGGER.debug(
                "Presence entity %s does not exist; its device claims no solar",
                entity_id,
            )
            return False
        value = str(state.state).strip().lower()
        if value in _PRESENT_STATES:
            return True
        _LOGGER.debug(
            "Presence entity %s reports %s, which does not read as present; "
            "its device claims no solar",
            entity_id,
            state.state,
        )
        return False

    def _read_home_consumption_w_opt(self, entity_id: str | None) -> float | None:
        """Read Home Consumption only when its balance is currently coherent.

        The display entity intentionally holds its last valid value across a
        transient balance mismatch. Excluded-load control must not interpret
        that held value as live telemetry, so it falls back to its conservative
        unavailable path until ``balance_quality`` returns to ``calculated``.
        Entities without this diagnostic attribute remain supported.
        """
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        quality = state.attributes.get("balance_quality")
        if quality is not None and quality != "calculated":
            _LOGGER.debug(
                "Home Consumption %s is %s; ignoring held value for load control",
                entity_id,
                quality,
            )
            return None
        try:
            raw = float(state.state)
        except (ValueError, TypeError):
            return None
        unit = state.attributes.get("unit_of_measurement", "W")
        return raw if unit == "W" else raw * 1000.0

    def _included_device_power_w(self) -> float:
        """Σ power (W) of the enabled, telemetry, in-home-sensor excluded devices.

        These are the loads the battery does NOT serve; subtracting them from
        Home Consumption yields the home base load the battery targets, and thus
        the true PV surplus available to offset excluded devices.
        """
        total = 0.0
        for device in self._config_entry.data.get("excluded_devices", []):
            if not device.get("enabled", True):
                continue
            if device.get("ev_charger_no_telemetry", False):
                continue
            if not device.get("included_in_consumption", True):
                continue
            total += self._read_sensor_w(device.get("power_sensor"))
        return total

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

            # New entries have a dedicated activity sensor. Legacy no-telemetry
            # entries stored that same state entity in power_sensor, so retain
            # it as a transparent fallback indefinitely.
            sensor_id = device.get("activity_sensor") or device.get("power_sensor")
            if not sensor_id:
                continue

            state = self._hass.states.get(sensor_id)
            if state is None or state.state in ("unknown", "unavailable"):
                continue

            is_charging = self._state_is_active(state)

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
