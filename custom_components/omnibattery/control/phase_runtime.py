"""Opt-in, single-entry per-phase runtime.

Phase control is disabled unless explicitly configured. All commands are sent
through ChargeDischargeController._set_battery_power under its existing lock,
so hardware blockers, SOC and power ceilings remain authoritative.
"""
from __future__ import annotations

import logging
from time import monotonic

from .phase_channels import PhaseChannel, PhaseChannelManager
from .phase_meter import read_phase_power

_LOGGER = logging.getLogger(__name__)
KEY = "phase_pd_channels"


def configured_channels(entry, coordinators):
    """Resolve user-configured battery names and HA power sensors strictly."""
    raw = entry.data.get(KEY, [])
    if not isinstance(raw, list):
        raise ValueError("phase_pd_channels must be a list")
    names = {c.name: c for c in coordinators}
    if len(names) != len(coordinators):
        raise ValueError("Battery names must be unique for phase control")
    channels = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Invalid phase channel")
        name = item["battery"]
        if name not in names:
            raise ValueError(f"Unknown battery: {name}")
        channels.append(PhaseChannel(
            phase=item["phase"],
            battery_key=name,
            grid_sensor=item["grid_sensor"],
            enabled=bool(item.get("enabled", False)),
            target_w=float(item.get("target_w", 0)),
            deadband_w=float(item.get("deadband_w", 50)),
            min_command_interval_s=float(item.get("min_command_interval_s", 5)),
            max_step_w=float(item.get("max_step_w", 800)),
        ))
    return tuple(channels)


class PhaseRuntime:
    """Runs only inside the controller's serialized control cycle."""

    def __init__(self, controller):
        self.controller = controller
        self.channels = configured_channels(
            controller.config_entry, controller.coordinators
        )
        self.manager = PhaseChannelManager(self.channels)
        self.by_name = {c.name: c for c in controller.coordinators}
        self.by_phase = {channel.phase: channel for channel in self.channels}
        self.active_batteries = frozenset(
            c.battery_key for c in self.channels if c.enabled
        )

    async def run(self):
        ctl = self.controller
        if not self.active_batteries or ctl.manual_mode_enabled:
            return
        for channel in self.channels:
            if not channel.enabled:
                continue
            battery = self.by_name[channel.battery_key]
            if (
                not battery.is_available
                or battery.data is None
                or ctl._is_battery_manual_owned(battery)
                or ctl._is_backup_function_active(battery)
                or battery.rs485_user_disabled
                or ctl._is_manual_slot_owned(battery)
            ):
                continue
            reading = read_phase_power(
                ctl.hass.states.get(channel.grid_sensor),
                max_age_s=15,
            )
            # Never continue an old nonzero command on stale telemetry.
            # This safety zero still passes through the normal write blockers.
            state = self.manager.planner.states[channel.battery_key]
            if reading.power_w is None:
                if state.last_command_w:
                    ok = await ctl._set_battery_power(
                        battery, 0, 0, owner=f"phase:{channel.phase}"
                    )
                    if ok:
                        self.manager.acknowledge(
                            channel.phase, 0, now=monotonic()
                        )
                continue
            max_charge = max(0, ctl._battery_power_limit(battery, True))
            max_discharge = max(0, ctl._battery_power_limit(battery, False))
            # Directional operation blockers are enforced before planning as
            # well as at the final _set_battery_power write boundary.
            if ctl.get_charge_blockers(battery):
                max_charge = 0
            if ctl.get_discharge_blockers(battery):
                max_discharge = 0
            now = monotonic()
            command = self.manager.plan(
                channel.phase, reading,
                max_charge_w=max_charge,
                max_discharge_w=max_discharge,
                now=now,
            )
            if command is None:
                continue
            ok = await ctl._set_battery_power(
                battery, max(0, command), max(0, -command),
                owner=f"phase:{channel.phase}",
            )
            if ok:
                self.manager.acknowledge(channel.phase, command, now=now)
            else:
                _LOGGER.debug("Phase %s command was not acknowledged", channel.phase)
