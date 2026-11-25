"""Pure per-battery, per-phase PD planner; no Home Assistant or hardware writes.

Sign conventions match Omnibattery: positive grid watts = import;
negative battery command watts = discharge; positive = charge.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from time import monotonic


@dataclass(frozen=True)
class PhaseAssignment:
    battery_key: str
    grid_sensor: str
    target_w: float = 0.0
    deadband_w: float = 50.0
    min_command_interval_s: float = 5.0
    max_step_w: float = 800.0

    def __post_init__(self):
        if not self.battery_key or not self.grid_sensor.startswith("sensor."):
            raise ValueError("Battery key and grid sensor required")
        if not all(isfinite(v) for v in (
            self.target_w, self.deadband_w,
            self.min_command_interval_s, self.max_step_w,
        )):
            raise ValueError("Nonfinite control parameter")
        if min(self.deadband_w, self.min_command_interval_s, self.max_step_w) < 0:
            raise ValueError("Negative control limit")


@dataclass
class PhaseState:
    last_command_w: float = 0.0
    last_command_at: float | None = None


@dataclass
class PerPhasePlanner:
    assignments: tuple[PhaseAssignment, ...]
    states: dict[str, PhaseState] = field(default_factory=dict)

    def __post_init__(self):
        keys = [a.battery_key for a in self.assignments]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate battery assignment")
        for key in keys:
            self.states.setdefault(key, PhaseState())

    def plan(
        self, battery_key: str, grid_w: float | None, *,
        max_charge_w: float, max_discharge_w: float,
        now: float | None = None,
    ) -> float | None:
        assignment = next(
            (a for a in self.assignments if a.battery_key == battery_key), None
        )
        if assignment is None:
            raise KeyError(battery_key)
        if grid_w is None or not isfinite(grid_w):
            return None
        if not all(isfinite(v) and v >= 0 for v in (
            max_charge_w, max_discharge_w,
        )):
            raise ValueError("Invalid power limits")
        state = self.states[battery_key]
        timestamp = monotonic() if now is None else now
        if not isfinite(timestamp):
            raise ValueError("Invalid time")
        if (
            state.last_command_at is not None
            and timestamp - state.last_command_at
            < assignment.min_command_interval_s
        ):
            return None
        error = grid_w - assignment.target_w
        if abs(error) <= assignment.deadband_w:
            return None
        # Import -> discharge (negative command); export -> charge (positive).
        delta = max(
            -assignment.max_step_w,
            min(assignment.max_step_w, -error),
        )
        request = state.last_command_w + delta
        request = max(-max_discharge_w, min(max_charge_w, request))
        return request if request != state.last_command_w else None

    def acknowledge(
        self, battery_key: str, command_w: float, *,
        now: float | None = None,
    ) -> None:
        if battery_key not in self.states:
            raise KeyError(battery_key)
        timestamp = monotonic() if now is None else now
        if not isfinite(command_w) or not isfinite(timestamp):
            raise ValueError("Invalid acknowledgement")
        state = self.states[battery_key]
        state.last_command_w = command_w
        state.last_command_at = timestamp
