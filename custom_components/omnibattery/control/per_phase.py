"""Independent per-phase PD planning primitives.

This module is deliberately command-free: it calculates battery power requests
but does not send them. A future integration must arbitrate ownership with the
existing fleet PD loop before applying any request to hardware.

Grid convention: positive = import; negative = export.
Battery command convention: positive = discharge; negative = charge.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from time import monotonic


@dataclass(frozen=True)
class PhaseAssignment:
    """Stable battery-to-meter mapping for one control channel."""

    battery_key: str
    grid_sensor: str
    target_w: float = 0.0

    def __post_init__(self) -> None:
        if not self.battery_key or not self.grid_sensor.startswith("sensor."):
            raise ValueError("Battery key and grid sensor entity are required")
        if not isfinite(self.target_w):
            raise ValueError("Target must be finite")


@dataclass
class PhaseState:
    """Controller state belonging to one battery, never shared with others."""

    last_command_w: float = 0.0
    last_command_at: float | None = None


@dataclass
class PerPhasePlanner:
    """Compute independent commands without touching device hardware."""

    assignments: tuple[PhaseAssignment, ...]
    deadband_w: float = 50.0
    min_command_interval_s: float = 5.0
    max_step_w: float = 800.0
    states: dict[str, PhaseState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        keys = [item.battery_key for item in self.assignments]
        if len(keys) != len(set(keys)):
            raise ValueError("A battery cannot be assigned to two phases")
        if not all(isfinite(v) and v >= 0 for v in (
            self.deadband_w, self.min_command_interval_s, self.max_step_w
        )):
            raise ValueError("Control limits must be finite and nonnegative")
        for key in keys:
            self.states.setdefault(key, PhaseState())

    def plan(
        self,
        battery_key: str,
        grid_w: float | None,
        *,
        max_charge_w: float,
        max_discharge_w: float,
        now: float | None = None,
    ) -> float | None:
        """Return next signed command, or None when no command is appropriate.

        Call :meth:`acknowledge` only after the command is accepted by the
        battery. A missing or nonfinite meter reading always fails closed.
        """
        assignment = next(
            (item for item in self.assignments if item.battery_key == battery_key),
            None,
        )
        if assignment is None:
            raise KeyError(battery_key)
        if grid_w is None or not isfinite(grid_w):
            return None
        if not all(isfinite(v) and v >= 0 for v in (max_charge_w, max_discharge_w)):
            raise ValueError("Power limits must be finite and nonnegative")

        state = self.states[battery_key]
        timestamp = monotonic() if now is None else now
        if state.last_command_at is not None:
            if timestamp - state.last_command_at < self.min_command_interval_s:
                return None

        error = grid_w - assignment.target_w
        if abs(error) <= self.deadband_w:
            return None

        # Grid import calls for more discharge; grid export for more charge.
        requested = state.last_command_w + error
        delta = max(-self.max_step_w, min(self.max_step_w, requested - state.last_command_w))
        requested = state.last_command_w + delta
        requested = max(-max_charge_w, min(max_discharge_w, requested))
        if requested == state.last_command_w:
            return None
        return requested

    def acknowledge(self, battery_key: str, command_w: float, *, now: float | None = None) -> None:
        """Record an accepted command; failed writes must not be acknowledged."""
        if battery_key not in self.states:
            raise KeyError(battery_key)
        if not isfinite(command_w):
            raise ValueError("Command must be finite")
        state = self.states[battery_key]
        state.last_command_w = command_w
        state.last_command_at = monotonic() if now is None else now
