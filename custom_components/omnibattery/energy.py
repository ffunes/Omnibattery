"""Shared battery-energy calculations."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .const import DEFAULT_ROUND_TRIP_EFFICIENCY


BACKUP_DISCHARGING_ENERGY_KEY = "backup_discharging_energy"
BACKUP_DAILY_DISCHARGING_ENERGY_KEY = "backup_daily_discharging_energy"
EFFECTIVE_TOTAL_DISCHARGING_ENERGY_KEY = "effective_total_discharging_energy"
_MAX_BACKUP_INTEGRATION_GAP_S = 600.0


def effective_total_discharging_energy(data: dict | None) -> float | None:
    """Return hardware discharge plus software-integrated backup output."""
    if not data:
        return None
    effective = data.get(EFFECTIVE_TOTAL_DISCHARGING_ENERGY_KEY)
    if effective is not None:
        try:
            value = float(effective)
        except (TypeError, ValueError):
            pass
        else:
            return value if math.isfinite(value) and value >= 0 else None

    raw = data.get("total_discharging_energy")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


@dataclass
class BackupDischargeAccumulator:
    """Integrate driver-normalized discharge omitted by a hardware counter."""

    kwh: float = 0.0
    daily_kwh: float = 0.0
    reset_date: str | None = None
    last_sample_monotonic: float | None = None
    last_sample_active: bool = False

    def observe(
        self,
        *,
        now_monotonic: float,
        power_w: object,
        local_date: str,
    ) -> bool:
        """Consume one driver-normalized power sample and report whether kWh changed."""
        if self.reset_date != local_date:
            self.daily_kwh = 0.0
            self.reset_date = local_date
        last = self.last_sample_monotonic
        self.last_sample_monotonic = now_monotonic
        try:
            power = float(power_w)
        except (TypeError, ValueError, OverflowError):
            self.last_sample_active = False
            return False
        current_sample_active = math.isfinite(power) and power > 0
        previous_sample_active = self.last_sample_active
        self.last_sample_active = current_sample_active
        if last is None:
            return False

        elapsed_s = now_monotonic - last
        if elapsed_s <= 0 or elapsed_s > _MAX_BACKUP_INTEGRATION_GAP_S:
            return False
        # Require both endpoints to be active. This deliberately loses at most
        # one poll interval at each transition rather than attributing energy
        # from an inactive interval. The driver owns the definition of active.
        if not previous_sample_active or not current_sample_active:
            return False

        energy_kwh = power * elapsed_s / 3_600_000.0
        self.kwh += energy_kwh
        self.daily_kwh += energy_kwh
        return True

    def publish(self, data: dict) -> None:
        """Publish raw backup and corrected discharge totals to coordinator data."""
        data[BACKUP_DISCHARGING_ENERGY_KEY] = self.kwh
        data[BACKUP_DAILY_DISCHARGING_ENERGY_KEY] = self.daily_kwh
        raw = data.get("total_discharging_energy")
        try:
            raw_value = float(raw)
        except (TypeError, ValueError):
            data.pop(EFFECTIVE_TOTAL_DISCHARGING_ENERGY_KEY, None)
            return
        if not math.isfinite(raw_value) or raw_value < 0:
            data.pop(EFFECTIVE_TOTAL_DISCHARGING_ENERGY_KEY, None)
            return
        data[EFFECTIVE_TOTAL_DISCHARGING_ENERGY_KEY] = raw_value + self.kwh


# Measured round-trip efficiency feeding the arbitrage gate / High-Price Sale
# when the user has not set their own value.
MEASURED_EFFICIENCY_MIN = 0.75
MEASURED_EFFICIENCY_MAX = 0.95
MEASURED_EFFICIENCY_MIN_CYCLES = 50.0


def measured_battery_efficiency(coordinator) -> float | None:
    """Return one battery's measured round trip (0-1), or None if not trustworthy."""
    sensor = getattr(coordinator, "efficiency_sensor", None)
    capabilities = getattr(coordinator, "capabilities", None)
    # Same test as the sensor's integrate mode: a DC-coupled unit's lifetime AC
    # counters are not a round trip, so it only measures through its sensor.
    dual_plane = bool(
        (
            getattr(capabilities, "has_mppt_pv", False)
            or getattr(capabilities, "has_solar_telemetry", False)
        )
        and getattr(capabilities, "independent_power_planes", True)
    )
    if dual_plane:
        if sensor is None:
            return None
        # vA/vD: both integrated legs must exist; one leg is only an estimate.
        charge_eff, discharge_eff = sensor._leg_efficiencies()
        if charge_eff is None or discharge_eff is None:
            return None
        value = charge_eff * discharge_eff
    else:
        data = getattr(coordinator, "data", None) or {}
        try:
            charge = float(data.get("total_charging_energy"))
            capacity = float(data.get("battery_total_energy"))
        except (TypeError, ValueError):
            return None
        discharge = effective_total_discharging_energy(data)
        if discharge is None or capacity <= 0 or charge / capacity < MEASURED_EFFICIENCY_MIN_CYCLES:
            return None
        value = discharge / charge
    if MEASURED_EFFICIENCY_MIN <= value <= MEASURED_EFFICIENCY_MAX:
        return value
    return None


def effective_round_trip_efficiency(controller) -> tuple[float, str]:
    """Return (efficiency, source): configured, measured fleet average, or default."""
    configured = getattr(controller, "round_trip_efficiency", DEFAULT_ROUND_TRIP_EFFICIENCY)
    if configured != DEFAULT_ROUND_TRIP_EFFICIENCY:
        return configured, "configured"
    weighted = total = 0.0
    for coordinator in getattr(controller, "coordinators", None) or []:
        value = measured_battery_efficiency(coordinator)
        if value is None:
            continue
        capacity = float((getattr(coordinator, "data", None) or {}).get("battery_total_energy") or 0)
        weighted += value * capacity
        total += capacity
    if total > 0:
        return weighted / total, "measured"
    return DEFAULT_ROUND_TRIP_EFFICIENCY, "default"
