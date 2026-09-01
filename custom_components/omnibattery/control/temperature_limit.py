"""Temperature-based charge power derate (TemperatureChargeLimitManager).

Optional, charge-only thermal protection. When a battery's internal temperature
rises above a configured limit, the per-battery charge power is proportionally
reduced: full power at/below the limit, ramping linearly down to a floor (a
percentage of the normal charge ceiling) as the temperature climbs across the
band, and back up as it cools. The ramp is continuous, so no cooldown latch or
hysteresis is needed (it self-stabilises).

This mirrors ``MaxSocChargeManager.apply_charge_taper``'s cap-and-return
contract: it is called from ``_battery_power_limit`` with the current per-battery
limit and returns a limit that is never higher than the one passed in. When the
feature is disabled or the temperature is unavailable it returns the limit
unchanged (fail-safe: a sensor gap never throttles charging).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class TemperatureChargeLimitManager:
    """Proportional per-battery charge-power derate driven by internal temperature."""

    def __init__(self, hass: "HomeAssistant", controller: Any) -> None:
        self._hass = hass
        self._controller = controller

    def _derate_factor(self, temp: float) -> float:
        """Return the charge-power scale (0..1) for a given temperature.

        1.0 at/below the limit; ramps linearly to the floor across the band;
        pinned at the floor at/above ``limit + band``.
        """
        c = self._controller
        limit_c = c._temp_charge_limit_c
        band_c = c._temp_charge_limit_band_c
        floor = c._temp_charge_limit_floor_pct / 100.0

        if temp <= limit_c:
            return 1.0
        if band_c <= 0 or temp >= limit_c + band_c:
            return floor
        frac = (temp - limit_c) / band_c  # 0..1 across the band
        return 1.0 - frac * (1.0 - floor)

    @staticmethod
    def _min_power(coordinator, is_charging: bool) -> int:
        """The battery's minimum reliable operating power (0 when it has none).

        Read from the driver capabilities (0 for drivers with no such floor).
        """
        caps = getattr(coordinator, "capabilities", None)
        attr = "min_charge_power_w" if is_charging else "min_discharge_power_w"
        return int(getattr(caps, attr, 0) or 0)

    def _apply(self, coordinator, limit: int, is_charging: bool) -> int:
        """Cap ``limit`` by the thermal derate factor (fail-safe passthrough)."""
        temp = (coordinator.data or {}).get("internal_temperature")
        if temp is None:
            return limit
        try:
            temp_f = float(temp)
        except (TypeError, ValueError):
            return limit

        factor = self._derate_factor(temp_f)
        if factor >= 1.0:
            return limit
        derated = min(limit, int(round(limit * factor)))
        # Hard floor: never command a non-zero power below the battery's minimum
        # reliable operating power (0 when the driver declares none). Never raise
        # above a limit that was already below that floor.
        return max(derated, min(self._min_power(coordinator, is_charging), limit))

    def apply_temperature_limit(self, coordinator, limit: int) -> int:
        """Cap the per-battery charge limit by the thermal derate factor."""
        if not self._controller.temp_charge_limit_enabled:
            return limit
        return self._apply(coordinator, limit, True)

    def apply_discharge_limit(self, coordinator, limit: int) -> int:
        """Cap the per-battery discharge limit, when opted in.

        Uses the same (charge-tuned) curve; discharge tolerates heat better, so
        this is a compromise whose main value is avoiding the BMS hard cutoff.
        """
        c = self._controller
        if not (c.temp_charge_limit_enabled and c.temp_limit_apply_discharge):
            return limit
        return self._apply(coordinator, limit, False)

    def get_status(self) -> dict:
        """Return per-battery thermal-derate diagnostics for the status sensor."""
        c = self._controller
        status: dict = {}
        for coordinator in c.coordinators:
            data = coordinator.data or {}
            temp = data.get("internal_temperature")
            try:
                temp_f = float(temp) if temp is not None else None
            except (TypeError, ValueError):
                temp_f = None
            factor = self._derate_factor(temp_f) if temp_f is not None else 1.0
            status[coordinator.name] = {
                "enabled": c.temp_charge_limit_enabled,
                "apply_discharge": c.temp_limit_apply_discharge,
                "internal_temperature": temp,
                "limit_c": c._temp_charge_limit_c,
                "band_c": c._temp_charge_limit_band_c,
                "floor_pct": c._temp_charge_limit_floor_pct,
                "derate_factor": round(factor, 3),
                "derating": c.temp_charge_limit_enabled and temp_f is not None and factor < 1.0,
            }
        return status
