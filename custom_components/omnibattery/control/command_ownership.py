"""Exclusive command ownership for the future per-phase PD integration.

Fail closed: a battery has at most one active automatic owner. Ownership is
separate from manual/backup/slot safety checks in the existing controller.
No hardware writes occur in this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field


FLEET = "fleet"
PHASE = "phase"


@dataclass
class BatteryCommandOwnership:
    """Track which automatic control loop may issue a battery command."""

    phase_owners: dict[str, str] = field(default_factory=dict)

    def assign_phase(self, battery_key: str, phase: str) -> None:
        if not battery_key or phase not in ("L1", "L2", "L3"):
            raise ValueError("Invalid phase assignment")
        if phase in self.phase_owners.values() and self.phase_owners.get(battery_key) != phase:
            raise ValueError("Phase already assigned to another battery")
        self.phase_owners[battery_key] = phase

    def release_phase(self, battery_key: str) -> None:
        self.phase_owners.pop(battery_key, None)

    def permits(self, battery_key: str, owner: str) -> bool:
        """Unknown owners are rejected, not treated as fleet control."""
        if owner == FLEET:
            return battery_key not in self.phase_owners
        if owner.startswith("phase:"):
            return self.phase_owners.get(battery_key) == owner[6:]
        return False

    def owner(self, battery_key: str) -> str:
        phase = self.phase_owners.get(battery_key)
        return f"phase:{phase}" if phase is not None else FLEET
