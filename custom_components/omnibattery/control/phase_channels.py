"""Read-only phase-channel arbitration for a single Omnibattery config entry.

This is a preparation layer. It does not take over fleet PD, write battery
setpoints, or register Home Assistant entities. Those steps must be wired
atomically with the existing controller's command ownership.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .per_phase import PhaseAssignment, PerPhasePlanner
from .phase_meter import PhaseMeterReading


@dataclass(frozen=True)
class PhaseChannel:
    phase: str
    battery_key: str
    grid_sensor: str
    enabled: bool = False
    target_w: float = 0.0
    deadband_w: float = 50.0
    min_command_interval_s: float = 5.0
    max_step_w: float = 800.0

    def __post_init__(self):
        if self.phase not in ("L1", "L2", "L3"):
            raise ValueError("Invalid phase")
        PhaseAssignment(
            self.battery_key, self.grid_sensor, self.target_w,
            self.deadband_w, self.min_command_interval_s, self.max_step_w,
        )


def validate_channels(channels: Iterable[PhaseChannel]) -> tuple[PhaseChannel, ...]:
    """Reject ambiguous mapping before any channel may control hardware."""
    result = tuple(channels)
    phases = [c.phase for c in result]
    batteries = [c.battery_key for c in result]
    sensors = [c.grid_sensor for c in result]
    if len(set(phases)) != len(phases):
        raise ValueError("Duplicate phase")
    if len(set(batteries)) != len(batteries):
        raise ValueError("Duplicate battery")
    if len(set(sensors)) != len(sensors):
        raise ValueError("Duplicate grid sensor")
    if len(result) > 3:
        raise ValueError("At most three phases are supported")
    return result


@dataclass
class PhaseChannelManager:
    """Command-free manager, retaining a separate planner state per battery."""

    channels: tuple[PhaseChannel, ...]

    def __post_init__(self):
        self.channels = validate_channels(self.channels)
        self.planner = PerPhasePlanner(tuple(
            PhaseAssignment(
                c.battery_key, c.grid_sensor, c.target_w, c.deadband_w,
                c.min_command_interval_s, c.max_step_w,
            )
            for c in self.channels
        ))

    def plan(
        self,
        phase: str,
        reading: PhaseMeterReading,
        *,
        max_charge_w: float,
        max_discharge_w: float,
        now: float | None = None,
    ) -> float | None:
        """Suggest a signed Omnibattery command; never dispatch it."""
        channel = next((c for c in self.channels if c.phase == phase), None)
        if channel is None:
            raise KeyError(phase)
        if not channel.enabled or reading.power_w is None:
            return None
        return self.planner.plan(
            channel.battery_key, reading.power_w,
            max_charge_w=max_charge_w, max_discharge_w=max_discharge_w,
            now=now,
        )

    def acknowledge(
        self, phase: str, command_w: float, *, now: float | None = None,
    ) -> None:
        """Only the future exclusive command dispatcher may acknowledge."""
        channel = next((c for c in self.channels if c.phase == phase), None)
        if channel is None:
            raise KeyError(phase)
        if not channel.enabled:
            raise ValueError("Disabled phase cannot acknowledge a command")
        self.planner.acknowledge(channel.battery_key, command_w, now=now)
