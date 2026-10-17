"""Fix flows for the Repairs issues a battery driver resolves itself.

An issue raised with ``is_fixable=True`` and ``data={"device_key": ...}`` gets a
confirm step. On Submit the flow asks the driver of the battery with that device
key to fix it (``BatteryDriver.async_fix_issue``); Home Assistant then removes
the issue. Issues without such a driver are not changed: the flow aborts.

Today only the Indevolt driver raises one: setting writes that stopped at its
yearly flash allowance. See docs/indevolt-core-changes.md.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant

from .const import DOMAIN


def _driver_for(hass: HomeAssistant, device_key: str) -> Any:
    """The driver of the loaded battery with ``device_key``, if any."""
    for entry_data in (hass.data.get(DOMAIN) or {}).values():
        if not isinstance(entry_data, dict):
            continue
        for coordinator in entry_data.get("coordinators") or ():
            if getattr(coordinator, "device_key", None) == device_key:
                return getattr(coordinator, "driver", None)
    return None


class DriverIssueRepairFlow(ConfirmRepairFlow):
    """Confirm, then let the battery's driver fix the issue."""

    def __init__(self, device_key: str) -> None:
        self._device_key = device_key

    async def async_step_confirm(self, user_input: dict[str, str] | None = None):
        if user_input is not None:
            fix = getattr(_driver_for(self.hass, self._device_key), "async_fix_issue", None)
            if fix is None or not await fix(self.issue_id):
                # The battery is not loaded (or no longer raises this issue):
                # keep the issue, so nothing is turned back on unseen.
                return self.async_abort(reason="battery_unavailable")
        return await super().async_step_confirm(user_input)


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    return DriverIssueRepairFlow(str((data or {}).get("device_key") or ""))
