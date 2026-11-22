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
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from ..const import CONF_AUTONOMOUS_REQUESTED_MODE, DOMAIN
from ..drivers.base import (
    NativeOperatingMode,
    can_take_setpoint,
    is_observed,
    supports_nom_mode,
)
from .residual_load import _battery_ac_power_w

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
# The same as the wait of a grid charge, which then starts with the warning on.
UNCONFIRMED_AFTER_S = 60.0
# Failed tries to go back to NOM, seconds without a power reading, and opposite
# flows within CROSS_EPISODES_WINDOW_S that become a repair.
RESTORE_FAILED_AFTER = 3
TELEMETRY_LOST_S = 300.0
CROSS_EPISODES = 3
CROSS_EPISODES_WINDOW_S = 3600.0
# A grid charge waits this long, once, for Manual to be confirmed; then it goes
# ahead with a visible warning. Charging is never called off.
GRID_CHARGE_WAIT_S = UNCONFIRMED_AFTER_S
# A cause asserted by a control step lasts until that step stops repeating it.
CAUSE_TTL_S = 10.0

# One battery discharging while another charges, both by more than this: below it
# is standby draw, sign noise and inverter losses.
CROSS_THRESHOLD_W = 50.0
# It takes two readings of the autonomous battery. They must be this far apart
# (a commanded battery needs some seconds to ramp down when the sun goes) and be
# two different reports, unless the source only reports changes.
CROSS_SAMPLE_S = 5.0
CROSS_REPEATED_S = 60.0
# How long NOM stays off after the last opposite flow, or after its charge was
# last seen over the import cap.
LATCH_S = 300.0

# A target further than this from 0 W means importing or exporting on purpose
# (sale, pre-discharge, hourly balance): a battery that holds the meter at 0 W
# would undo it from its own cells, or fill up with what is being sold. Small
# standing targets are left alone.
GRID_TARGET_W = 100.0

CAUSE_GLOBAL_MANUAL = "global_manual_mode"
CAUSE_GRID_CHARGE = "grid_charge"
CAUSE_EXCLUDED = "excluded_device"
CAUSE_CROSS_CHARGE = "cross_charge"
CAUSE_IMPORT_CAP = "import_cap"
CAUSE_GRID_TARGET = "grid_target"
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
    blind_since: float | None = None
    mode_seen_at: float | None = None
    task: asyncio.Task | None = None
    # Opposite flow seen in the last reading: how many readings, when, which.
    cross_samples: int = 0
    cross_at: float = float("-inf")
    cross_token: Any = None
    # Latched cause -> (until, details).
    latched: dict[str, tuple[float, dict]] = field(default_factory=dict)


@dataclass
class _GridCharge:
    """One grid charge, from its first request until 30 s without one.

    Outliving short gaps is what makes the wait count once per charge: the
    flags behind a charge drop and come back within one cheap period.
    """

    last_seen: float
    wait_since: float | None = None
    started: bool = False


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
        self._charges: dict[str, _GridCharge] = {}
        # Last opposite flow between two batteries, and when the recent ones were.
        self.last_cross_flow: dict[str, Any] | None = None
        self._cross_episodes: dict[Any, deque[float]] = {}
        # Excluded device -> was it running when it was last readable.
        self._device_running: dict[str, bool] = {}
        # issue id -> raised. Unknown means this run has not looked yet, and an
        # earlier one may have left it behind.
        self._issues: dict[str, bool] = {}
        self._stopping = False

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
        causes = {
            source: details
            for source, (at, details) in self._asserted.items()
            if now - at <= CAUSE_TTL_S
        }
        if getattr(self._controller, "manual_mode_enabled", False):
            causes[CAUSE_GLOBAL_MANUAL] = {}
        for coordinator in self._controller.coordinators:
            if (
                getattr(coordinator, "battery_manual_mode_enabled", False)
                and getattr(coordinator, "manual_force_mode", None) == "Charge"
                and (getattr(coordinator, "manual_set_charge_power", 0) or 0) > 0
            ):
                causes[f"{CAUSE_GRID_CHARGE}:manual_battery"] = {
                    "battery": coordinator.name
                }
        # The cap and the reserve follow the meter: they ask nobody to move it.
        target = getattr(self._controller, "compute_active_target_excluding", None)
        if callable(target) and abs(target("capacity_protection")) > GRID_TARGET_W:
            causes[CAUSE_GRID_TARGET] = {}
        causes.update(self._excluded_causes())
        return causes

    def _excluded_devices(self) -> list[dict]:
        """The excluded devices as ``ExternalLoads`` reads them now."""
        read = getattr(
            getattr(self._controller, "_external_loads", None),
            "autonomous_discharge_interlocks", None,
        )
        return read() if callable(read) else []

    def _excluded_causes(self) -> dict[str, dict]:
        """Excluded devices that are running, or not known not to be."""
        causes = {}
        running: dict[str, bool] = {}
        for device in self._excluded_devices():
            entity_id, active = device["entity"], device["active"]
            last = self._device_running.get(entity_id)
            if active is None:
                # Never read since the start: nothing shows it is off. Lost
                # while running: it still is, until it is read again.
                hold = last is not False
                if last is not None:
                    running[entity_id] = last
                details = {"reading": "unavailable"}
            else:
                hold = running[entity_id] = active
                # No power in here: the causes are an attribute, and a figure
                # that moves on every cycle would be recorded on every cycle.
                details = {}
            if hold:
                causes[f"{CAUSE_EXCLUDED}:{entity_id}"] = details
        self._device_running = running
        return causes

    # --- grid charging -------------------------------------------------------

    def grid_charge_ready(self, source: str) -> bool:
        """Whether a grid charge of the commanded batteries may run now.

        Call it on every cycle of the charge, before its first setpoint: the
        call itself is what holds the autonomous batteries in Manual. Waits for
        them to be confirmed there, at most ``GRID_CHARGE_WAIT_S`` once per
        charge; a charge that has started is never stopped again by this.
        """
        batteries = self.batteries
        if not batteries:
            return True
        now = self._clock()
        self.assert_cause(f"{CAUSE_GRID_CHARGE}:{source}")
        charge = self._charges.get(source)
        if charge is None or now - charge.last_seen > RESTORE_STABLE_S:
            charge = self._charges[source] = _GridCharge(last_seen=now)
        charge.last_seen = now
        if charge.started:
            return True
        unconfirmed = [c.name for c in batteries if self.effective(c) is not MANUAL]
        if not unconfirmed:
            charge.started = True
            return True
        if charge.wait_since is None:
            charge.wait_since = now
            # Do not lose a cycle: the switch to Manual starts here.
            system = self._system_causes()
            for coordinator in batteries:
                self._reconcile_one(coordinator, system)
        if now - charge.wait_since >= GRID_CHARGE_WAIT_S:
            _LOGGER.warning(
                "Grid charge (%s) starts without %s being confirmed in Manual: "
                "it may discharge into the charge", source, ", ".join(unconfirmed),
            )
            charge.started = True
            return True
        return False

    def _battery_causes(self, coordinator) -> dict[str, dict]:
        """Causes that suspend this battery only."""
        causes: dict[str, dict] = {}
        state = self._state(coordinator)
        now = self._clock()
        if _battery_ac_power_w(coordinator) is not None:
            state.blind_since = None
        else:
            if state.blind_since is None:
                state.blind_since = now
            if self.requested(coordinator) is NOM:
                causes[CAUSE_TELEMETRY] = {}
        self._watch_cross_flow(coordinator, state)
        self._watch_import_cap(coordinator, state)
        causes.update(
            (source, details)
            for source, (until, details) in state.latched.items()
            if now < until
        )
        return causes

    def _watch_cross_flow(self, coordinator, state: _BatteryState) -> None:
        """Latch ``cross_charge`` while this battery discharges into another one.

        Independent of who commanded what: it covers the races, the charges the
        user starts elsewhere and the delays of the firmware that the interlocks
        cannot see coming.
        """
        delivered = _battery_ac_power_w(coordinator)
        charging = {}
        if delivered is not None and delivered > CROSS_THRESHOLD_W:
            for other in self._controller.coordinators:
                power = None if other is coordinator else _battery_ac_power_w(other)
                if power is not None and power < -CROSS_THRESHOLD_W:
                    charging[other.name] = round(-power)
        if not charging:
            state.cross_samples = 0
            return
        now = self._clock()
        token = coordinator.driver.power_sample_time()
        since = now - state.cross_at
        if state.cross_samples == 0 or (
            since >= CROSS_SAMPLE_S
            and (token is None or token != state.cross_token or since >= CROSS_REPEATED_S)
        ):
            state.cross_samples += 1
            state.cross_at = now
            state.cross_token = token
        if state.cross_samples < 2:
            return
        until, details = state.latched.get(CAUSE_CROSS_CHARGE, (float("-inf"), {}))
        if now >= until:
            details = {"charging": charging, "discharging_w": round(delivered)}
            # A new episode, not more of the one in course.
            self._cross_episodes.setdefault(coordinator, deque(maxlen=10)).append(now)
            self.last_cross_flow = {
                "battery": coordinator.name, **details, "at": dt_util.utcnow().isoformat(),
            }
            _LOGGER.warning(
                "[%s] discharges %dW while %s charge(s)%s",
                coordinator.name, details["discharging_w"], charging,
                "; taking it to Manual" if supports_nom_mode(coordinator) else "",
            )
        state.latched[CAUSE_CROSS_CHARGE] = (now + LATCH_S, details)

    def _watch_import_cap(self, coordinator, state: _BatteryState) -> None:
        """Latch ``import_cap`` while this battery's charge breaks the import cap."""
        controller = self._controller
        if not float(getattr(controller, "max_contracted_power", 0) or 0):
            return
        delivered = _battery_ac_power_w(coordinator)
        if delivered is None or delivered > -CROSS_THRESHOLD_W:
            return
        grid = controller._apply_meter_transform(
            controller.hass.states.get(controller.consumption_sensor)
        )
        cap = controller._grid_import_cap_w()
        if grid is not None and grid > cap:
            now = self._clock()
            until, details = state.latched.get(CAUSE_IMPORT_CAP, (float("-inf"), {}))
            if now >= until:
                details = {"charging_w": round(-delivered)}
            state.latched[CAUSE_IMPORT_CAP] = (now + LATCH_S, details)

    def blind_batteries(self) -> list:
        """Batteries that may be moving power nobody can see.

        Their power cannot be read and the last mode seen was not Manual, so a
        discharge of the commanded batteries might be feeding them.
        """
        return [
            c for c in self.batteries
            if _battery_ac_power_w(c) is None and self._state(c).last_known is not MANUAL
        ]

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
        if self._stopping:
            return
        for coordinator in self._controller.coordinators:
            if is_observed(coordinator) and not supports_nom_mode(coordinator):
                # Nothing to switch: the opposite flow is only written down.
                self._watch_cross_flow(coordinator, self._state(coordinator))
        batteries = self.batteries
        if not batteries:
            return
        system = self._system_causes()
        for coordinator in batteries:
            self._reconcile_one(coordinator, system)
            self._update_issues(coordinator)

    async def async_rest_all(self) -> None:
        """Take every battery to Manual for good; Home Assistant is stopping."""
        self._stopping = True
        for coordinator in self.batteries:
            if not await coordinator.standby():
                _LOGGER.error(
                    "%s: could not be confirmed in Manual while stopping", coordinator.name
                )

    def _reconcile_one(self, coordinator, system: dict[str, dict]) -> None:
        now = self._clock()
        state = self._state(coordinator)
        effective = self.effective(coordinator)
        if effective is not None:
            state.last_known = effective
            state.mode_seen_at = now
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

    def _unconfirmed(self, coordinator) -> bool:
        """Whether the battery has been out of the wanted mode for too long."""
        state = self._state(coordinator)
        return (
            state.unconfirmed_since is not None
            and self._clock() - state.unconfirmed_since >= UNCONFIRMED_AFTER_S
        )

    def degraded(self, coordinator) -> bool:
        """Whether the battery is not known to be in the mode it should be in."""
        return CAUSE_TELEMETRY in self._state(coordinator).causes or self._unconfirmed(
            coordinator
        )

    def _update_issues(self, coordinator) -> None:
        """Raise and clear this battery's repairs."""
        state = self._state(coordinator)
        now = self._clock()
        episodes = self._cross_episodes.get(coordinator, ())
        for key, wanted in (
            (
                "nom_manual_unconfirmed",
                state.want is MANUAL and self._unconfirmed(coordinator),
            ),
            (
                "nom_restore_failed",
                state.want is NOM and state.attempts >= RESTORE_FAILED_AFTER,
            ),
            (
                "nom_telemetry_lost",
                CAUSE_TELEMETRY in state.causes
                and state.blind_since is not None
                and now - state.blind_since >= TELEMETRY_LOST_S,
            ),
            (
                "nom_cross_charge",
                sum(now - at <= CROSS_EPISODES_WINDOW_S for at in episodes)
                >= CROSS_EPISODES,
            ),
        ):
            self._set_issue(coordinator, key, wanted)

    def raise_unconfirmed(self, coordinator) -> None:
        """Leave the unconfirmed-Manual repair behind when the entry unloads."""
        self._set_issue(coordinator, "nom_manual_unconfirmed", True)

    def clear_issues(self) -> None:
        """Take down the repairs of a run that ends; the next one looks again."""
        for issue_id, raised in self._issues.items():
            if raised:
                self._delete_issue(issue_id)
        self._issues.clear()

    def _set_issue(self, coordinator, key: str, wanted: bool) -> None:
        issue_id = f"{key}_{self._entry.entry_id}_{coordinator.device_key}"
        if self._issues.get(issue_id) is wanted:
            return
        if wanted and not getattr(self._hass, "is_running", True):
            # The integration that owns the battery may still be loading.
            return
        self._issues[issue_id] = wanted
        if wanted:
            self._create_issue(issue_id, key, coordinator)
        else:
            self._delete_issue(issue_id)

    def _create_issue(self, issue_id: str, key: str, coordinator) -> None:
        ir.async_create_issue(
            self._hass, DOMAIN, issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
            translation_placeholders={"battery": coordinator.name},
        )

    def _delete_issue(self, issue_id: str) -> None:
        ir.async_delete_issue(self._hass, DOMAIN, issue_id)

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
            (CAUSE_IMPORT_CAP, "nom_suspended_import_cap"),
            (CAUSE_GRID_TARGET, "nom_suspended_grid_target"),
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

    def pool_attributes(self) -> dict[str, Any]:
        """Who is in which pool, for the system sensors; empty without a NOM battery."""
        batteries = self.batteries
        if not batteries:
            return {}
        wanting = [c for c in batteries if self.requested(c) is NOM]
        return {
            "controllable_batteries": [
                c.name for c in self._controller.coordinators if can_take_setpoint(c)
            ],
            "nom_requested_batteries": [c.name for c in wanting],
            "nom_active_batteries": [
                c.name for c in batteries if self.effective(c) is NOM
            ],
            "nom_suspended_batteries": {
                c.name: sorted(self._state(c).causes)
                for c in wanting if self._state(c).causes
            },
            "nom_degraded_batteries": [c.name for c in batteries if self.degraded(c)],
            "grid_charge_waiting_for_manual": bool(
                getattr(self._controller, "_grid_charge_waiting", False)
            ),
        }

    def diagnostics(self) -> dict[str, Any]:
        """Everything above plus ages, for the downloaded diagnostics."""
        now = self._clock()

        def _battery(coordinator) -> dict[str, Any]:
            state = self._state(coordinator)
            sampled = coordinator.driver.power_sample_time()
            return {
                **self.snapshot(coordinator),
                "last_known_mode": state.last_known.value if state.last_known else None,
                "mode_read_age_s": (
                    round(now - state.mode_seen_at) if state.mode_seen_at is not None else None
                ),
                "power_w": _battery_ac_power_w(coordinator),
                "power_age_s": (
                    round((dt_util.utcnow() - sampled).total_seconds())
                    if isinstance(sampled, datetime) else None
                ),
                "last_write_age_s": (
                    round(now - state.written_at)
                    if state.written_at != float("-inf") else None
                ),
            }

        if not self.batteries and self.last_cross_flow is None:
            return {}
        return {
            **self.pool_attributes(),
            "batteries": {c.name: _battery(c) for c in self.batteries},
            "last_cross_flow": self.last_cross_flow,
            "excluded_devices": self._excluded_devices(),
            "repairs": sorted(issue for issue, raised in self._issues.items() if raised),
        }
