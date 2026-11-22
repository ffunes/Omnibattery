"""Native mode of the batteries that regulate on their own (NOM / Manual).

Such a battery takes no watts (``supports_power_setpoint=False``); the only
thing that can be asked of it is to regulate its own meter to zero (NOM) or to
rest (Manual). Three things are kept apart:

* the **requested** mode is the user's preference and the only one persisted;
* the **effective** mode is what the device shows now, never assumed;
* a **suspension** holds Manual while the user wants NOM, for as long as any
  cause lasts. Causes are a set rebuilt from live state on every control cycle,
  so there is no release to forget.

``async_reconcile`` runs at the top of the control cycle and never waits for a
device: a mode is written from a background task and confirmed by reading it
back on later cycles. With no battery that ``supports_nom_mode`` it returns at
once.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from ..const import CONF_AUTONOMOUS_REQUESTED_MODE
from ..drivers.base import NativeOperatingMode, supports_nom_mode

_LOGGER = logging.getLogger(__name__)

NOM = NativeOperatingMode.NOM
MANUAL = NativeOperatingMode.MANUAL

# No cause of suspension for this long before NOM is restored: a noisy sensor
# must not flip the mode back and forth.
RESTORE_STABLE_S = 30.0
# Between two writes of one battery. Going to Manual does not wait for it.
MODE_DWELL_S = 30.0
# A written mode the device does not show within this counts as a failed try.
CONFIRM_S = 10.0
RETRY_BACKOFF_S = (5.0, 10.0, 20.0, 40.0, 60.0)
# Longer than this in a mode that is not the wanted one is a visible failure.
UNCONFIRMED_AFTER_S = 60.0
# A cause asserted by a control step lasts until that step stops repeating it.
CAUSE_TTL_S = 10.0

CAUSE_GLOBAL_MANUAL = "global_manual_mode"
CAUSE_GRID_CHARGE = "grid_charge"
CAUSE_EXCLUDED = "excluded_device"
CAUSE_CROSS_CHARGE = "cross_charge"
CAUSE_TELEMETRY = "telemetry_untrusted"


@dataclass
class _BatteryState:
    """Runtime state of one battery; none of it survives a restart."""

    causes: dict[str, dict] = field(default_factory=dict)
    want: NativeOperatingMode = MANUAL
    # When the causes were last seen to be gone; None while there is one.
    clear_since: float | None = None
    written: NativeOperatingMode | None = None
    written_at: float = float("-inf")
    retry_at: float = float("-inf")
    attempts: int = 0
    unconfirmed_since: float | None = None
    last_known: NativeOperatingMode | None = None
    last_change: datetime | None = None
    last_error: str | None = None
    task: asyncio.Task | None = None


class AutonomousBatteryModeManager:
    """Owner of the native mode of every battery that ``supports_nom_mode``."""

    def __init__(self, hass, config_entry, controller) -> None:
        self._hass = hass
        self._entry = config_entry
        self._controller = controller
        self._clock = time.monotonic
        self._states: dict[Any, _BatteryState] = {}
        # source -> (last asserted at, details); see ``assert_cause``.
        self._asserted: dict[str, tuple[float, dict]] = {}

    # --- what there is -------------------------------------------------------

    @property
    def batteries(self) -> list:
        """The batteries whose mode can be switched."""
        return [c for c in self._controller.coordinators if supports_nom_mode(c)]

    def _state(self, coordinator) -> _BatteryState:
        return self._states.setdefault(coordinator, _BatteryState())

    @staticmethod
    def requested(coordinator) -> NativeOperatingMode:
        """The user's preference; a battery nobody has chosen for is Manual."""
        value = getattr(coordinator, CONF_AUTONOMOUS_REQUESTED_MODE, MANUAL)
        return NOM if value == NOM else MANUAL

    @staticmethod
    def effective(coordinator) -> NativeOperatingMode | None:
        """The mode the device shows now; None when it shows neither."""
        return coordinator.driver.operating_mode_from_data(coordinator.data or {})

    # --- causes of suspension ------------------------------------------------

    def assert_cause(self, source: str, **details) -> None:
        """Hold NOM off for ``source``; repeat it every cycle while it lasts."""
        self._asserted[source] = (self._clock(), details)

    def _system_causes(self) -> dict[str, dict]:
        """Causes that suspend every battery, from live state."""
        now = self._clock()
        return {
            source: details
            for source, (at, details) in self._asserted.items()
            if now - at <= CAUSE_TTL_S
        }

    def _battery_causes(self, coordinator) -> dict[str, dict]:
        """Causes that suspend this battery only."""
        return {}

    # --- requests ------------------------------------------------------------

    async def async_set_requested_mode(self, coordinator, mode: NativeOperatingMode) -> None:
        """Store the user's choice first, then act on it."""
        mode = NativeOperatingMode(mode)
        setattr(coordinator, CONF_AUTONOMOUS_REQUESTED_MODE, mode.value)
        coordinator.persist_battery_config(CONF_AUTONOMOUS_REQUESTED_MODE, mode.value)
        state = self._state(coordinator)
        state.attempts = 0
        state.retry_at = float("-inf")
        _LOGGER.info("[%s] Operating mode requested: %s", coordinator.name, mode.value)
        await self.async_reconcile()

    # --- reconciliation ------------------------------------------------------

    async def async_reconcile(self) -> None:
        """Compare what each battery should do with what it does, and act."""
        batteries = self.batteries
        if not batteries:
            return
        system = self._system_causes()
        for coordinator in batteries:
            self._reconcile_one(coordinator, system)

    def _reconcile_one(self, coordinator, system: dict[str, dict]) -> None:
        now = self._clock()
        state = self._state(coordinator)
        effective = self.effective(coordinator)
        if effective is not None:
            state.last_known = effective
        state.causes = {**system, **self._battery_causes(coordinator)}
        if state.causes:
            state.clear_since = None
        elif state.clear_since is None:
            state.clear_since = now

        want = MANUAL
        if self.requested(coordinator) is NOM and not state.causes and (
            effective is NOM or now - state.clear_since >= RESTORE_STABLE_S
        ):
            want = NOM
        if want is not state.want:
            state.want = want
            state.attempts = 0
            state.retry_at = float("-inf")

        if effective is want:
            if state.written is want:
                state.last_change = dt_util.utcnow()
                _LOGGER.info("[%s] Operating mode is now %s", coordinator.name, want.value)
            state.written = None
            state.attempts = 0
            state.retry_at = float("-inf")
            state.unconfirmed_since = None
            state.last_error = None
            return

        if state.unconfirmed_since is None:
            state.unconfirmed_since = now
        if state.task is not None and not state.task.done():
            return
        if state.written is want:
            if now - state.written_at < CONFIRM_S:
                return
            self._failed(state, "not_confirmed")
        if now < state.retry_at:
            return
        if want is NOM and now - state.written_at < MODE_DWELL_S:
            return
        self._write(coordinator, state, want)

    def _failed(self, state: _BatteryState, reason: str) -> None:
        """Count one failed try and put the next one off."""
        delay = RETRY_BACKOFF_S[min(state.attempts, len(RETRY_BACKOFF_S) - 1)]
        state.attempts += 1
        state.retry_at = self._clock() + delay
        state.written = None
        state.last_error = reason

    def _write(self, coordinator, state: _BatteryState, mode: NativeOperatingMode) -> None:
        """Start one mode write; a slow service must not hold the control cycle."""
        state.written = mode
        state.written_at = self._clock()

        async def _run() -> None:
            result = await coordinator.apply_operating_mode(mode)
            # A newer decision may have replaced this one meanwhile.
            if not result.ok and state.written is mode:
                self._failed(state, result.failure_reason or "write_failed")

        create = getattr(self._controller, "_create_entry_background_task", None)
        if callable(create):
            state.task = create(_run(), "omnibattery_operating_mode")
        else:
            state.task = asyncio.get_running_loop().create_task(_run())

    # --- what is shown -------------------------------------------------------

    def degraded(self, coordinator) -> bool:
        """Whether the battery is not known to be in the mode it should be in."""
        state = self._state(coordinator)
        return CAUSE_TELEMETRY in state.causes or (
            state.unconfirmed_since is not None
            and self._clock() - state.unconfirmed_since >= UNCONFIRMED_AFTER_S
        )

    def status(self, coordinator) -> str:
        """One word for the battery's situation (translated by the select)."""
        state = self._state(coordinator)
        if self.degraded(coordinator):
            return "degraded"
        if self.requested(coordinator) is MANUAL:
            return "manual"
        kinds = {source.split(":")[0] for source in state.causes}
        for kind, name in (
            (CAUSE_GLOBAL_MANUAL, "nom_suspended_global_manual"),
            (CAUSE_GRID_CHARGE, "nom_suspended_grid_charge"),
            (CAUSE_EXCLUDED, "nom_suspended_excluded_load"),
            (CAUSE_CROSS_CHARGE, "nom_suspended_cross_charge"),
        ):
            if kind in kinds:
                return name
        return "nom_active" if self.effective(coordinator) is NOM else "nom_pending"

    def snapshot(self, coordinator) -> dict[str, Any]:
        """Diagnostic view of one battery."""
        state = self._state(coordinator)
        effective = self.effective(coordinator)
        return {
            "status": self.status(coordinator),
            "requested_mode": self.requested(coordinator).value,
            "effective_mode": effective.value if effective else None,
            "suspended": self.requested(coordinator) is NOM and bool(state.causes),
            "suspension_causes": {k: dict(v) for k, v in state.causes.items()},
            "last_change": state.last_change.isoformat() if state.last_change else None,
            "last_error": state.last_error,
            "failed_attempts": state.attempts,
        }
