"""Shared battery-energy calculations."""
from __future__ import annotations

import math
from dataclasses import dataclass


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
