"""Fail-closed readiness gate for future per-phase battery command dispatch.

This module cannot enable phase control by itself. Call it immediately before
any hardware write once exclusive command routing is integrated.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class PhaseDispatchReadiness:
    enabled: bool
    exclusive_owner: bool
    meter_fresh: bool
    battery_available: bool
    manual_mode: bool
    backup_active: bool
    manual_slot_active: bool
    rs485_disabled: bool
    soc_allows_charge: bool
    soc_allows_discharge: bool

    def allows(self, signed_command_w: float) -> bool:
        """Omnibattery convention: positive charge, negative discharge."""
        if not isfinite(signed_command_w):
            return False
        if not all((
            self.enabled,
            self.exclusive_owner,
            self.meter_fresh,
            self.battery_available,
        )):
            return False
        if any((
            self.manual_mode,
            self.backup_active,
            self.manual_slot_active,
            self.rs485_disabled,
        )):
            return False
        if signed_command_w > 0 and not self.soc_allows_charge:
            return False
        if signed_command_w < 0 and not self.soc_allows_discharge:
            return False
        return True
