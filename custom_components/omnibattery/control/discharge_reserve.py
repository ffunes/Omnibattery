"""Guards for the Dynamic Pricing plan's holds (#888).

The 2.0.0 optimizer decides when the battery is held for a dearer hour; the
controller executes it through the ``price_reserve`` discharge blocker
(``_refresh_price_reserve_blocks``). This module keeps what decides whether a
hold applies at all: Dynamic Pricing running, and none of the paths that spend
the battery on purpose. The previous discharge reserve module, which sized a
reserve of its own, is gone.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..const import PREDICTIVE_MODE_DYNAMIC_PRICING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

GUARD_MANUAL = "manual_control"
GUARD_CURTAILMENT = "curtailment_active"
GUARD_CAPACITY_PROTECTION = "capacity_protection"

# Pre-discharge deliberately empties the battery before a curtailment window,
# and peak shaving deliberately spends it on a spike. Holding energy back
# against either would import at exactly the moment they exist to avoid.
_CURTAILMENT_ACTIVE_STATES = frozenset({"protected_window", "predischarging"})


class DischargeReserveManager:
    """Whether the plan's hold may apply now."""

    def __init__(self, hass: "HomeAssistant", controller: Any) -> None:
        self._hass = hass
        self._controller = controller

    def dynamic_pricing_active(self) -> bool:
        """Dynamic Pricing runs, not overridden: the plan's holds apply (#888)."""
        controller = self._controller
        return bool(
            getattr(controller, "predictive_charging_enabled", False)
            and not getattr(controller, "predictive_charging_overridden", False)
            and getattr(controller, "predictive_charging_mode", None)
            == PREDICTIVE_MODE_DYNAMIC_PRICING
        )

    def _release_guard(self) -> str | None:
        """Return the first live reason a hold must not apply, if any."""
        controller = self._controller

        if self._manual_control_active():
            return GUARD_MANUAL

        # Anti-curtailment is emptying the battery on purpose, and peak shaving
        # is spending it on a spike on purpose. A hold would fight both.
        if getattr(controller, "_curtailment_runtime_status", None) in _CURTAILMENT_ACTIVE_STATES:
            return GUARD_CURTAILMENT
        # Not while the grid import cap is overriding this very block (#506):
        # the block is already suspended for the excess, and withdrawing it
        # would drop the cap's reason to engage on the next cycle.
        if getattr(controller, "_capacity_protection_active", False) and not getattr(
            controller, "_cap_overrides_policy", False
        ):
            return GUARD_CAPACITY_PROTECTION

        return None

    def _manual_control_active(self) -> bool:
        controller = self._controller
        if getattr(controller, "manual_mode_enabled", False):
            return True
        if getattr(controller, "_manual_slot_owned", None):
            return True
        for coordinator in getattr(controller, "coordinators", []) or []:
            if getattr(coordinator, "battery_manual_mode_enabled", False):
                return True
        return False
