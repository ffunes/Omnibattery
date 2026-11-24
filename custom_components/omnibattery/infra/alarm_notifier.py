"""Alarm and fault notifications for batteries.

Detects newly-active alarm/fault conditions (as decoded by the driver) and
emits Home Assistant persistent notifications. State (the previously active
conditions) is owned by this module so the coordinator no longer carries it.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

from homeassistant.core import HomeAssistant

from ..const import NOTIFICATION_ID_PREFIX
from ..drivers.base import DriverAlarm
from ..notifications import HEALTH, async_dismiss, async_notify, text

_LOGGER = logging.getLogger(__name__)


def _labels(alarms: Iterable[DriverAlarm], kind: str) -> list[str]:
    return [a.description for a in alarms if a.kind == kind and a.description]


class AlarmNotifier:
    """Tracks alarm/fault deltas and dispatches HA notifications."""

    def __init__(self, hass: HomeAssistant, device_name: str) -> None:
        self._hass = hass
        self._device_name = device_name
        self._previous: set[tuple[str, int]] = set()

    async def check(self, alarms: Iterable[DriverAlarm]) -> None:
        """Detect newly-active conditions and notify, or dismiss if all cleared."""
        alarms = list(alarms)
        current = {(a.kind, a.code) for a in alarms}
        new = [a for a in alarms if (a.kind, a.code) not in self._previous]

        if new:
            await self._send(alarms, new)
        elif self._previous and not current:
            await async_dismiss(
                self._hass,
                f"{NOTIFICATION_ID_PREFIX}battery_alarm_{self._device_name}",
            )

        self._previous = current

    async def _send(self, alarms: list[DriverAlarm], new: list[DriverAlarm]) -> None:
        is_fault = any(a.kind == "fault" for a in alarms)
        severity = "Fault" if is_fault else "Warning"
        sev = severity.lower()
        hass = self._hass

        sections: list[str] = []
        sections.append(text(hass, f"alarm.header.{sev}", device=self._device_name))
        sections.append("")

        if any(a.kind == "fault" for a in new):
            sections.append(text(hass, "alarm.new_faults"))
            for label in _labels(new, "fault"):
                sections.append(f"  🔴 {label}")

        if any(a.kind == "alarm" for a in new):
            sections.append(text(hass, "alarm.new_alarms"))
            for label in _labels(new, "alarm"):
                sections.append(f"  🟡 {label}")

        all_faults = _labels(alarms, "fault")
        all_alarms = _labels(alarms, "alarm")
        extra_faults = [l for l in all_faults if l not in _labels(new, "fault")]
        extra_alarms = [l for l in all_alarms if l not in _labels(new, "alarm")]

        if extra_faults or extra_alarms:
            sections.append("")
            sections.append(text(hass, "alarm.also_active"))
            for label in extra_faults:
                sections.append(f"  🔴 {label}")
            for label in extra_alarms:
                sections.append(f"  🟡 {label}")

        message = "\n".join(sections)
        title = text(hass, f"alarm.title.{sev}", device=self._device_name)

        await async_notify(
            self._hass,
            HEALTH,
            f"{NOTIFICATION_ID_PREFIX}battery_alarm_{self._device_name}",
            title,
            message,
        )
        log_conditions = ", ".join(all_faults + all_alarms)
        _LOGGER.warning("[%s] Battery %s — %s", self._device_name, severity.lower(), log_conditions)
