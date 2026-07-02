"""Diagnostics for Omnibattery config entries.

Home Assistant calls :func:`async_get_config_entry_diagnostics` when the user
presses *Download diagnostics* on the integration. It returns a JSON-serialisable
dump of connection health, driver traits and non-responsive-tracker state
(everything that otherwise lives only in transient logs), with host/serial and
sensor entity ids redacted.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# Identifiers and user sensor references that could deanonymise the dump.
# async_redact_data recurses into nested dicts/lists, so per-battery "host"
# entries inside a batteries list are covered too.
TO_REDACT = {
    "host",
    "serial",
    "serial_port",
    "ip_address",
    "mac",
    "consumption_sensor",
    "grid_sensor",
    "solar_forecast_sensor",
    "average_price_sensor",
}


def _driver_info(coordinator) -> dict[str, Any]:
    """Static driver traits (no host/serial, which are identifiers)."""
    driver = coordinator.driver
    caps = coordinator.capabilities
    return {
        "connected": driver.connected,
        "model_label": driver.model_label,
        "capabilities": asdict(caps) if is_dataclass(caps) else str(caps),
    }


def _tracker_info(controller, coordinator) -> dict[str, Any]:
    """Non-responsive exclusion state for one battery (side-effect free)."""
    tracker = getattr(controller, "_non_responsive", None)
    if tracker is None:
        return {}
    info = tracker.batteries.get(coordinator, {})
    return {
        # excluded_names() reads without mutating; is_excluded() would reset the
        # fail counter on cooldown expiry, which a read-only dump must not do.
        "excluded": coordinator.name in tracker.excluded_names(),
        "fail_count": info.get("fail_count", 0),
        "reason": info.get("reason"),
        "retry_attempted": info.get("retry_attempted", False),
        "wake_used": info.get("wake_used", False),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return a redacted health/driver/tracker dump for one config entry."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinators = data.get("coordinators") or []
    controller = data.get("controller")

    batteries = [
        {
            "health": coord.health_snapshot(),
            "driver": _driver_info(coord),
            "tracker": _tracker_info(controller, coord),
        }
        for coord in coordinators
    ]

    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "batteries": batteries,
    }
