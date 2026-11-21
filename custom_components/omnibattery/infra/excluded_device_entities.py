"""Entities of excluded devices that no longer have a device behind them.

The per-device entities are keyed by the device's position in the list. When a
device is removed, the ones after it move down one index and the entities of
the last index stay in the registry with nothing behind them; so do the two a
state-only EV charger does not have, when its index was a metered device
before. ``async_remove_orphaned_entities`` drops them at setup, before the
platforms add the current ones.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .entity_naming import SYSTEM_UNIQUE_ID_PREFIX

# unique_id key of each per-device entity -> whether a state-only EV charger
# (no power reading) has it too. Mirrors the platforms' setup.
_KEYS = {
    "excluded_device_enabled": True,
    "solar_surplus": True,
    "cover_home": True,
    "excluded_device_status": True,
    "dynamic_power_control": False,
    "exclusion_pct": False,
}
_UNIQUE_ID = re.compile(
    rf"^{re.escape(SYSTEM_UNIQUE_ID_PREFIX)}(?:{'|'.join(_KEYS)})_\d+$"
)


def expected_unique_ids(devices: Iterable[Mapping[str, Any]]) -> set[str]:
    """The unique_ids the platforms create for these excluded devices."""
    return {
        f"{SYSTEM_UNIQUE_ID_PREFIX}{key}_{index}"
        for index, device in enumerate(devices)
        for key, state_only in _KEYS.items()
        if state_only or not device.get("ev_charger_no_telemetry", False)
    }


@callback
def async_remove_orphaned_entities(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """Remove this entry's per-device entities that have no device any more."""
    expected = expected_unique_ids(entry.data.get("excluded_devices", []))
    registry = er.async_get(hass)
    removed = []
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if _UNIQUE_ID.match(entity.unique_id) and entity.unique_id not in expected:
            registry.async_remove(entity.entity_id)
            removed.append(entity.entity_id)
    return removed
