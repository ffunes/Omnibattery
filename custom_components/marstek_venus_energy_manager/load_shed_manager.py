"""Load shedding manager for peak shaving via smart plugs.

When sustained grid draw exceeds the configured threshold for longer than
the trigger delay, nominated smart plugs are switched off in order until
the excess is covered. They are restored automatically when grid draw drops
back below the threshold.
"""
from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_LOAD_SHEDDING_THRESHOLD,
    CONF_LOAD_SHEDDING_DURATION_MIN,
    CONF_LOAD_SHEDDING_MIN_PLUG_POWER,
    CONF_LOAD_SHEDDING_PLUGS,
    CONF_LOAD_SHEDDING_NOTIFY_ENABLED,
    CONF_LOAD_SHEDDING_NOTIFY_TARGET,
    DEFAULT_LOAD_SHEDDING_THRESHOLD,
    DEFAULT_LOAD_SHEDDING_DURATION_MIN,
    DEFAULT_LOAD_SHEDDING_MIN_PLUG_POWER,
    DEFAULT_LOAD_SHEDDING_NOTIFY_ENABLED,
)

_LOGGER = logging.getLogger(__name__)


class LoadSheddingManager:
    """Switches off nominated plugs when grid draw stays above threshold too long."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        self._hass = hass
        self._consumption_sensor: str = config_entry.data["consumption_sensor"]
        self._threshold: float = config_entry.data.get(CONF_LOAD_SHEDDING_THRESHOLD, DEFAULT_LOAD_SHEDDING_THRESHOLD)
        self._duration_s: float = config_entry.data.get(CONF_LOAD_SHEDDING_DURATION_MIN, DEFAULT_LOAD_SHEDDING_DURATION_MIN) * 60
        self._min_plug_power: float = config_entry.data.get(CONF_LOAD_SHEDDING_MIN_PLUG_POWER, DEFAULT_LOAD_SHEDDING_MIN_PLUG_POWER)
        self._plugs: list[dict] = config_entry.data.get(CONF_LOAD_SHEDDING_PLUGS, [])
        self._notify_enabled: bool = config_entry.data.get(CONF_LOAD_SHEDDING_NOTIFY_ENABLED, DEFAULT_LOAD_SHEDDING_NOTIFY_ENABLED)
        self._notify_target: str = config_entry.data.get(CONF_LOAD_SHEDDING_NOTIFY_TARGET, "") or ""

        self._above_threshold_since: datetime | None = None
        self._shedding_active: bool = False
        self._shed_plugs: list[str] = []

    async def check(self) -> None:
        """Read grid power and trigger or restore shedding as needed. Called every 2 s."""
        state = self._hass.states.get(self._consumption_sensor)
        if state is None or state.state in ("unknown", "unavailable"):
            return

        try:
            grid_power = float(state.state)
        except ValueError:
            return

        if grid_power > self._threshold:
            if self._above_threshold_since is None:
                self._above_threshold_since = dt_util.utcnow()
                return

            elapsed = (dt_util.utcnow() - self._above_threshold_since).total_seconds()
            if elapsed >= self._duration_s and not self._shedding_active:
                await self._trigger_shedding(grid_power)
        else:
            if self._shedding_active:
                await self._restore_plugs()
            self._above_threshold_since = None

    async def _trigger_shedding(self, grid_power: float) -> None:
        """Switch off plugs in order until grid excess is covered."""
        excess = grid_power - self._threshold
        shed: list[str] = []

        for plug in self._plugs:
            if excess <= 0:
                break

            switch_entity = plug.get("switch_entity")
            power_sensor = plug.get("power_sensor")
            if not switch_entity:
                continue

            plug_power = 0.0
            if power_sensor:
                ps = self._hass.states.get(power_sensor)
                if ps is not None and ps.state not in ("unknown", "unavailable"):
                    try:
                        plug_power = float(ps.state)
                    except ValueError:
                        pass

            if plug_power < self._min_plug_power:
                _LOGGER.debug("Load shedding: skipping %s (%.0fW < %.0fW min)", switch_entity, plug_power, self._min_plug_power)
                continue

            await self._hass.services.async_call(
                "switch", "turn_off", {"entity_id": switch_entity}
            )
            shed.append(switch_entity)
            excess -= plug_power
            _LOGGER.info("Load shedding: switched off %s (%.0fW), remaining excess %.0fW", switch_entity, plug_power, max(0, excess))

        if shed:
            self._shedding_active = True
            self._shed_plugs = shed
            await self._send_notification(
                "shed",
                f"Grid draw {grid_power:.0f}W exceeded {self._threshold:.0f}W threshold "
                f"for >{self._duration_s / 60:.0f} min.\nSwitched off: {', '.join(shed)}",
            )
        else:
            _LOGGER.warning("Load shedding triggered but no eligible plugs found (all below %.0fW threshold)", self._min_plug_power)

    async def _restore_plugs(self) -> None:
        """Restore all shed plugs and notify."""
        for switch_entity in self._shed_plugs:
            await self._hass.services.async_call(
                "switch", "turn_on", {"entity_id": switch_entity}
            )
            _LOGGER.info("Load shedding: restored %s", switch_entity)

        await self._send_notification(
            "restore",
            f"Grid draw returned below {self._threshold:.0f}W threshold.\nRestored: {', '.join(self._shed_plugs)}",
        )
        self._shedding_active = False
        self._shed_plugs = []

    async def _send_notification(self, action: str, message: str) -> None:
        """Send persistent HA notification and optionally a push notification."""
        if not self._notify_enabled:
            return

        title = "⚡ Peak load shedding active" if action == "shed" else "✅ Peak load shedding resolved"

        await self._hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": title,
                "message": message,
                "notification_id": "marstek_load_shedding",
            },
        )

        if self._notify_target:
            parts = self._notify_target.split(".", 1)
            if len(parts) == 2:
                try:
                    await self._hass.services.async_call(
                        parts[0], parts[1], {"title": title, "message": message}
                    )
                except Exception as e:
                    _LOGGER.warning("Load shedding: failed to send notification to %s: %s", self._notify_target, e)
