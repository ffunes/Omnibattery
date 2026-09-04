"""Weekly full charge grid-import orchestration."""
from __future__ import annotations

import logging
from typing import Any

from ..const import (
    WEEKLY_GRID_MODE_IMMEDIATE,
    WEEKLY_GRID_MODE_SOLAR_FIRST,
)

_LOGGER = logging.getLogger(__name__)

_GRID_CHARGE_OWNER_WEEKLY = "weekly"


class WeeklyGridChargeManager:
    """Owns regulated grid import for the weekly 100% cycle."""

    def __init__(self, controller: Any) -> None:
        self._controller = controller

    def compute_targets(self) -> dict[Any, float]:
        """Return per-coordinator 100% targets for batteries still below full."""
        ctrl = self._controller
        mgr = ctrl._weekly_charge_mgr
        targets: dict[Any, float] = {}
        for coordinator in ctrl.coordinators:
            if getattr(coordinator, "battery_manual_mode_enabled", False):
                continue
            if ctrl._is_backup_function_active(coordinator):
                continue
            if not coordinator.data:
                continue
            if mgr.is_battery_full(coordinator):
                continue
            targets[coordinator] = 100.0
        return targets

    def should_wait_for_solar(self) -> bool:
        """Return True when solar_first mode should defer grid import."""
        ctrl = self._controller
        if ctrl.weekly_full_charge_grid_mode != WEEKLY_GRID_MODE_SOLAR_FIRST:
            return False
        should_delay = getattr(ctrl._charge_delay_mgr, "_should_delay_charge", None)
        if not callable(should_delay):
            return False
        # True means the forecast still expects enough sun to reach 100%.
        return bool(should_delay(100))

    def should_run(self) -> bool:
        """Return True when a weekly grid session may start or continue."""
        ctrl = self._controller
        if not ctrl.weekly_full_charge_enabled:
            return False
        if not ctrl.weekly_full_charge_grid_enabled:
            return False
        if not ctrl._weekly_charge_mgr.is_active():
            return False
        if ctrl.weekly_full_charge_complete:
            return False
        if self.should_wait_for_solar():
            return False
        return bool(self.compute_targets())

    def clear_session(self) -> None:
        """Stop a weekly-owned grid session without touching predictive sessions."""
        ctrl = self._controller
        if getattr(ctrl, "_grid_charge_owner", None) != _GRID_CHARGE_OWNER_WEEKLY:
            return
        ctrl._stop_grid_charge_session(owner=_GRID_CHARGE_OWNER_WEEKLY)
        status = getattr(ctrl, "_weekly_charge_status", {})
        if status.get("state") == "Grid charging":
            status["state"] = "Charging to 100%"

    async def handle(self) -> bool:
        """Run weekly grid charging when eligible. Return True if the cycle was owned."""
        ctrl = self._controller
        if getattr(ctrl, "_grid_charge_owner", None) == _GRID_CHARGE_OWNER_WEEKLY:
            if not self.should_run():
                self.clear_session()
                return False
            await ctrl._handle_predictive_grid_charging()
            if not ctrl.grid_charging_active:
                self.clear_session()
            return True

        if not self.should_run():
            return False

        if not ctrl._is_operation_allowed(is_charging=True):
            return False

        targets = self.compute_targets()
        if not targets:
            return False

        ctrl._grid_charge_owner = _GRID_CHARGE_OWNER_WEEKLY
        ctrl._predictive_charge_target_soc = targets
        ctrl.grid_charging_active = True
        ctrl._grid_charging_initialized = False
        ctrl._weekly_charge_status["state"] = "Grid charging"
        ctrl._weekly_charge_status.pop("completion_reason", None)
        _LOGGER.info(
            "Weekly Full Charge: starting grid import (%s mode) for %s",
            ctrl.weekly_full_charge_grid_mode,
            ", ".join(sorted(getattr(c, "name", str(c)) for c in targets)),
        )
        await ctrl._handle_predictive_grid_charging()
        if not ctrl.grid_charging_active:
            self.clear_session()
        return True
