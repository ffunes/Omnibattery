"""System-health summary for Omnibattery.

Shown in Settings → System → Repairs (system health card). A compact
at-a-glance counterpart to the full diagnostics dump: how many batteries are
connected, how many are suspended by the failure ladder, and how many are
currently excluded as non-responsive. Reuses each coordinator's
``health_snapshot`` so it never diverges from the diagnostics view.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN


def _entry_datas(hass: HomeAssistant):
    """Yield the per-config-entry data dicts, skipping registration flags."""
    for value in hass.data.get(DOMAIN, {}).values():
        if isinstance(value, dict) and "coordinators" in value:
            yield value


@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    """Register the system-health info callback."""
    register.async_register_info(_async_system_health_info)


async def _async_system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Return the summary counters shown on the system-health card."""
    coordinators = [c for d in _entry_datas(hass) for c in (d.get("coordinators") or [])]

    total = len(coordinators)
    connected = sum(1 for c in coordinators if c.is_available)
    suspended = sum(1 for c in coordinators if c.health_snapshot()["suspended"])

    non_responsive: set[str] = set()
    for data in _entry_datas(hass):
        tracker = getattr(data.get("controller"), "_non_responsive", None)
        if tracker is not None:
            non_responsive.update(tracker.excluded_names())

    return {
        "batteries_connected": f"{connected}/{total}",
        "suspended": suspended,
        "non_responsive": len(non_responsive),
    }
