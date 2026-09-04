"""Ownership guard for the direct battery-command entities.

Register-backed drivers (Marstek, ESPHome) expose the raw force-mode and
setpoint registers as writable entities. The control loop re-asserts those
registers every cycle, so a write made while the controller owns the battery is
undone before it can do anything: the UI accepts the change, the select even
keeps showing the picked option for a moment, and the battery never moves.

Refusing the write turns that silent no-op into a message that names the
condition (Manual Mode off) instead of leaving the user to conclude the
integration is broken.
"""
from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError

from ..const import CONF_BATTERY_MANUAL_MODE_ENABLED, DOMAIN

# Register keys the control loop rewrites every cycle. Anything else on the
# battery device (SOC cutoffs, power caps, backup threshold) is configuration
# the controller reads rather than owns, so those stay freely writable.
CONTROLLER_OWNED_KEYS = frozenset(
    {"force_mode", "set_charge_power", "set_discharge_power"}
)


def controller_owns_battery(hass, coordinator) -> bool:
    """Return whether the control loop currently drives this battery.

    False when global Manual Mode is on, when this battery is individually
    manual-owned, or when there is no controller yet (setup/teardown) — in that
    last case nothing re-asserts the registers, so a direct write is honest.
    """
    entry = getattr(coordinator, "_config_entry", None)
    if entry is None:
        return False
    entry_data = (hass.data.get(DOMAIN) or {}).get(entry.entry_id) or {}
    controller = entry_data.get("controller")
    if controller is None:
        return False
    if getattr(controller, "manual_mode_enabled", False):
        return False
    return not bool(getattr(coordinator, CONF_BATTERY_MANUAL_MODE_ENABLED, False))


def assert_manual_control(hass, coordinator, key: str) -> None:
    """Raise when writing a controller-owned register would be a no-op."""
    if key not in CONTROLLER_OWNED_KEYS:
        return
    if not controller_owns_battery(hass, coordinator):
        return
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="manual_control_required",
        translation_placeholders={"battery": str(getattr(coordinator, "name", ""))},
    )
