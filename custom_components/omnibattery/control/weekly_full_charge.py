"""Weekly full charge management for Marstek Venus.

Owns:
- Day-based activation logic (is_active)
- Persistence of completion / registers-written across HA restarts
- Hardware register (44000) writes to allow charging to 100% on v2 batteries
- Completion detection (top-voltage measurement, 100% SOC, or BMS cutoff at 99%)
- Register restore and hysteresis re-enable on completion
- Mid-charge abort handling when day or feature flag changes

Weekly uses the same 100% top-voltage taper/measurement flow as a user-selected
max_soc=100. Active balancing, when desired, is provided by the optional
Home Assistant blueprint.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .pack_soc import pack_socs, soc_vs_ceiling
from ..const import (
    DOMAIN,
    NORMAL_BALANCE_BMS_CUTOFF_VERSIONS,
    NORMAL_BALANCE_RECAL_CUTOFF_CYCLES,
    NORMAL_BALANCE_TAPER_CELL_VOLTAGE,
    WEEKDAY_MAP,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# Inverter state raw value for "Standby" (BMS has cut off, no active charge/discharge).
_INVERTER_STATE_STANDBY = 1
# Battery power below this (W) is treated as "not charging" for BMS-cutoff detection.
_BMS_CUTOFF_POWER_W = 10
# Consecutive update cycles (~2 s each) of BMS-cutoff conditions required before
# declaring completion at 99%. Keep this shared with the Venus A/D retry-acceptance
# debounce: 5 × 2 s = 10 s outlasts the Modbus response delay while reacting quickly.
_BMS_CUTOFF_REQUIRED_CYCLES = NORMAL_BALANCE_RECAL_CUTOFF_CYCLES


class WeeklyFullChargeManager:
    """Manages weekly full charge state, persistence and register writes."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        controller: Any,
    ) -> None:
        self._hass = hass
        self._controller = controller
        # Bundled store: weekly charge flags + delay_unlocked + solar_t_start.
        # Format preserved for backward-compat with existing user installs.
        self._store = Store(hass, 1, f"{DOMAIN}.{config_entry.entry_id}.weekly_charge_state")
        # Per-battery debounce counter for BMS-cutoff detection (in-memory only).
        self._bms_cutoff_counts: dict[str, int] = {}
        # Re-apply the temporary 100% cutoff once per HA session.  This set is
        # intentionally not persisted: an empty set after restart makes the
        # active weekly run re-assert 100% while retaining the persisted
        # original SOC values for the eventual restore.
        self._cutoff_applied_names: set[str] = set()
        # Edge-trigger guard so the "already completed" debug line logs once per
        # completion, not once per is_active() call (called per-battery per-cycle).
        self._already_complete_logged = False

    def _create_background_task(self, coroutine, name: str) -> asyncio.Task | None:
        """Create persistence work through the owning entry controller."""
        create = getattr(self._controller, "_create_entry_background_task", None)
        if callable(create):
            task = create(coroutine, name)
        else:
            hass = getattr(self, "_hass", None)
            if hass is None:
                close = getattr(coroutine, "close", None)
                if callable(close):
                    close()
                return None
            create = getattr(hass, "async_create_task", None)
            if callable(create):
                try:
                    task = create(coroutine, name=name)
                except TypeError:
                    task = create(coroutine)
            else:
                try:
                    task = asyncio.get_running_loop().create_task(coroutine, name=name)
                except TypeError:
                    task = asyncio.get_running_loop().create_task(coroutine)
        if isinstance(task, asyncio.Task):
            return task
        close = getattr(coroutine, "close", None)
        if callable(close):
            close()
        return None

    @property
    def store(self) -> Store:
        """Expose the underlying Store (for legacy attribute compatibility)."""
        return self._store

    def tick_bms_cutoff(self) -> None:
        """Update per-battery BMS-cutoff counters for the current cycle.

        Must be called exactly once per update cycle, unconditionally, at the
        top of handle_registers() before any early returns.  Both
        handle_registers() and _get_available_batteries() then query
        is_battery_full() as a read-only state check.
        """
        ctrl = self._controller
        self.is_active()  # side effect: day-boundary flag reset
        top_charge_manager = getattr(ctrl, "_max_soc_mgr", None)
        tick_retry_acceptance = getattr(
            top_charge_manager, "tick_bms_cutoff_retry_acceptance", None
        )
        if tick_retry_acceptance is not None:
            # The acceptance debounce must advance once here, not from
            # is_battery_full()/availability checks which can run repeatedly
            # during the same control cycle.
            tick_retry_acceptance()
        for c in ctrl.coordinators:
            if getattr(c, "battery_manual_mode_enabled", False):
                # Individual manual mode owns the battery. Freeze its cutoff
                # evidence so weekly charge cannot classify or reconfigure it.
                continue
            if not c.data:
                self._bms_cutoff_counts[c.name] = 0
                continue
            soc = c.data.get("battery_soc", 0)
            # Also arm the counter when cells are in the top taper zone regardless
            # of SOC — handles BMS coulomb drift where the BMS cuts at ~3.65 V but
            # reports e.g. 95-98% SOC. Not gated on weekly charge: an everyday BMS
            # cutoff below 99% during normal charging needs this too, otherwise the
            # charge hysteresis never latches and we keep commanding a refusing BMS.
            in_taper_zone = False
            vmax = c.data.get("max_cell_voltage")
            try:
                in_taper_zone = vmax is not None and float(vmax) >= NORMAL_BALANCE_TAPER_CELL_VOLTAGE
            except (TypeError, ValueError):
                pass
            # On a coupled-pack battery the taper clause alone is a false
            # positive machine (issue #350): a pack that finished hours ago holds
            # the top cell high while later packs are still filling, so a lull in
            # acceptance during a hand-over gets counted as a cutoff at 89% SOC.
            # Require the *least* full pack to be at the top as well. Only where
            # packs actually report: the taper clause exists precisely to fire
            # below 99% aggregate SOC (coulomb drift), so falling back to the
            # aggregate here would disable it for every other battery.
            packs = pack_socs(c)
            if in_taper_zone and packs and min(packs) < 99:
                in_taper_zone = False
            if soc >= 99 or in_taper_zone:
                power = c.data.get("battery_power", None)
                inv_state = c.data.get("inverter_state", None)
                is_zendure = getattr(c, "brand", None) == "zendure"
                # A real top-of-charge cutoff only counts when we are actually
                # commanding the battery to charge but it refuses (≤10 W +
                # Standby). A battery that is merely idle — not allocated charge
                # this cycle (solar lull, PD prioritising another battery, or
                # already excluded as full) — reads the same ≤10 W + Standby
                # without being full. Gating on the commanded setpoint is what
                # separates a real BMS cut from "nobody asked it to charge", and
                # stops an idle battery from racking up cutoff cycles forever.
                commanded = getattr(c, "commanded_charge_power", 0) or 0
                cutoff = (
                    power is not None
                    and commanded > _BMS_CUTOFF_POWER_W
                    and power <= _BMS_CUTOFF_POWER_W
                    and (
                        inv_state == _INVERTER_STATE_STANDBY
                        # Zendure's local API has no inverter_state field.  Its
                        # commanded setpoint is the equivalent idle guard, and
                        # the SOC >= 99 gate above prevents lower-SOC false hits.
                        or (is_zendure and inv_state is None and soc >= 99)
                    )
                )
                if cutoff:
                    count = self._bms_cutoff_counts.get(c.name, 0) + 1
                    self._bms_cutoff_counts[c.name] = count
                    if count == 1:
                        _LOGGER.info(
                            "%s: SOC %d%%, commanded=%.0fW, power=%.1fW, inverter=Standby — "
                            "possible BMS cutoff, confirming (%d/%d cycles)",
                            c.name, soc, commanded, power, count, _BMS_CUTOFF_REQUIRED_CYCLES,
                        )
                    elif count == _BMS_CUTOFF_REQUIRED_CYCLES:
                        _LOGGER.info(
                            "%s: BMS cutoff confirmed at %d%% (%d cycles at ≤%.0fW + Standby "
                            "while commanded to charge)",
                            c.name, soc, count, _BMS_CUTOFF_POWER_W,
                        )
                elif power is not None and power > _BMS_CUTOFF_POWER_W:
                    # Battery is accepting charge → genuinely not full. Reset.
                    if self._bms_cutoff_counts.get(c.name, 0) > 0:
                        _LOGGER.debug(
                            "%s: BMS cutoff condition cleared (charging at %.1fW) — "
                            "resetting counter",
                            c.name, power,
                        )
                    self._bms_cutoff_counts[c.name] = 0
                # else: idle / not commanded to charge this cycle — freeze the
                # counter (neither increment nor reset) so a confirmed cutoff
                # latches through the charge-exclusion that follows it (a full
                # battery stops being commanded, which would otherwise reset it).
            else:
                self._bms_cutoff_counts[c.name] = 0

    def reset_bms_cutoff_confirmation(self, coordinator: Any) -> None:
        """Forget a provisional cutoff before the one-shot retry begins."""
        self._bms_cutoff_counts.pop(getattr(coordinator, "name", coordinator), None)
        # Accept older/test state keyed directly by coordinator identity.
        self._bms_cutoff_counts.pop(coordinator, None)

    def is_battery_full(self, coordinator: Any) -> bool:
        """Return True if this battery counts as fully charged.

        The cutoff counter is advanced only by tick_bms_cutoff(); a provisional
        Venus A/D cutoff may be promoted to the one-shot retry here before the
        battery is considered complete.
        Used by both handle_registers() (weekly completion) and
        _get_available_batteries() (normal max_soc=100% case).
        """
        if getattr(coordinator, "battery_manual_mode_enabled", False):
            return False
        if not coordinator.data:
            return False
        # The least full pack decides on a coupled-pack battery: its aggregate
        # reaches 100% while a later pack is still filling (issue #350). Returns
        # the aggregate unchanged for every battery without per-pack telemetry.
        soc = soc_vs_ceiling(coordinator, coordinator.data.get("battery_soc", 0))
        top_charge_manager = getattr(self._controller, "_max_soc_mgr", None)
        prepare_retry = getattr(top_charge_manager, "prepare_bms_cutoff_retry", None)
        if prepare_retry is not None:
            retry_state = prepare_retry(coordinator)
            if retry_state in {"pending", "active"}:
                # A provisional first refusal must neither complete a weekly
                # charge nor block the normal path permanently.
                return False
        if soc >= 100:
            # Venus A/D can expose 100% as soon as the first coupled pack is
            # full. When the voltage taper is active, let the top-charge path
            # keep the command alive until the shared BMS detector confirms the
            # real cutoff for all packs.
            should_charge_to_bms = getattr(
                top_charge_manager, "should_charge_to_bms_cutoff", None
            )
            if should_charge_to_bms is not None and should_charge_to_bms(coordinator, 100):
                return False
            return True
        # BMS cutoff confirmed — safe without SOC gate because tick_bms_cutoff
        # only increments the counter when soc >= 99 OR (weekly active AND cells
        # in taper zone), so the count is 0 outside those conditions.
        return self._bms_cutoff_counts.get(coordinator.name, 0) >= _BMS_CUTOFF_REQUIRED_CYCLES

    def is_bms_cutoff_confirmed(self, coordinator: Any) -> bool:
        """Return only the debounced BMS-cutoff state, independent of SOC."""
        return self._bms_cutoff_counts.get(coordinator.name, 0) >= _BMS_CUTOFF_REQUIRED_CYCLES

    def is_active(self) -> bool:
        """Check if weekly full charge is currently active.

        Returns True if:
        - Feature is enabled
        - Today is the selected day
        - NOT all batteries have reached 100% yet

        Also handles day boundary transitions to reset the flag.
        """
        ctrl = self._controller
        if not ctrl.weekly_full_charge_enabled:
            return False

        # Re-arm the "already completed" edge-trigger log whenever completion clears
        # (day boundary, feature toggle, force-charge reset — wherever it happens).
        if not ctrl.weekly_full_charge_complete:
            self._already_complete_logged = False

        now = datetime.now()
        current_weekday = now.weekday()
        target_weekday = WEEKDAY_MAP[ctrl.weekly_full_charge_day]

        # Handle day boundary transitions
        if ctrl.last_checked_weekday is not None and ctrl.last_checked_weekday != current_weekday:
            # Day changed - check if we're exiting the target day
            if ctrl.last_checked_weekday == target_weekday and current_weekday != target_weekday:
                # Just exited the target day - reset flags for next week
                _LOGGER.info("Weekly Full Charge: Exited %s, resetting flags for next week",
                            ctrl.weekly_full_charge_day.upper())
                if ctrl.weekly_full_charge_registers_written and not ctrl.weekly_full_charge_complete:
                    # The charge may not have reached its completion condition
                    # before midnight. Resetting the flags alone would leave a
                    # Zendure socSet=100 in place until the next weekly run.
                    ctrl._weekly_charge_needs_restore = True
                    _LOGGER.info(
                        "Weekly Full Charge: Mid-charge day rollover detected - hardware restore pending"
                    )
                ctrl.weekly_full_charge_complete = False
                ctrl.weekly_full_charge_registers_written = False
                ctrl._force_full_charge = False
                ctrl._weekly_charge_status["state"] = "Idle"
                ctrl._weekly_charge_status.pop("completion_reason", None)
                grid_mgr = getattr(ctrl, "_weekly_grid_charge_mgr", None)
                if grid_mgr is not None:
                    grid_mgr.clear_session()
                # Save the cleared state asynchronously (don't await to avoid blocking)
                self._create_background_task(
                    self.save_state(), "omnibattery_weekly_charge_state_save"
                )

        ctrl.last_checked_weekday = current_weekday

        # Check if we're on the target day and haven't completed yet
        is_target_day = current_weekday == target_weekday

        # Force full charge button overrides the day check
        if ctrl._force_full_charge:
            if ctrl.weekly_full_charge_complete:
                return False
            return True

        if not is_target_day:
            return False

        if ctrl.weekly_full_charge_complete:
            if not self._already_complete_logged:
                _LOGGER.debug("Weekly Full Charge: On target day but already completed - using normal max_soc")
                self._already_complete_logged = True
            return False

        # Active: on target day and not yet complete
        return True

    async def load_state(self) -> None:
        """Load persisted weekly charge state from storage.

        This ensures that if Home Assistant is reloaded after the weekly charge
        completes, the system remembers not to restart the charging process.
        """
        ctrl = self._controller
        if not ctrl.weekly_full_charge_enabled:
            return

        try:
            data = await self._store.async_load()
            if data is None:
                _LOGGER.debug("Weekly Full Charge: No persisted state found")
                return

            today_iso = date.today().isoformat()
            stored_date = data.get("date")

            # Only restore state if saved on the same calendar date (prevents last week's
            # completion from being incorrectly restored on the same weekday next week)
            if stored_date == today_iso:
                ctrl.weekly_full_charge_complete = data.get("complete", False)
                ctrl.weekly_full_charge_registers_written = data.get("registers_written", False)
                # Restore visible status so the sensor reflects the correct state immediately.
                # (handle_registers() will also correct it on the next tick, but this avoids
                # a transient "Idle" flash and is correct for the "Complete" case too.)
                saved_state = data.get("state")
                if saved_state:
                    ctrl._weekly_charge_status["state"] = saved_state
                # Restore delay state
                ctrl._charge_delay_unlocked = data.get("delay_unlocked", False)
                ctrl._solar_t_start = data.get("solar_t_start")
                ctrl._weekly_charge_needs_restore = bool(data.get("restore_pending", False))
                self._restore_saved_max_soc(data.get("saved_max_soc"))
                getattr(self, "_cutoff_applied_names", set()).clear()
                _LOGGER.info(
                    "Weekly Full Charge: Restored state - complete=%s, "
                    "registers_written=%s, delay_unlocked=%s, restore_pending=%s",
                    ctrl.weekly_full_charge_complete,
                    ctrl.weekly_full_charge_registers_written,
                    ctrl._charge_delay_unlocked,
                    ctrl._weekly_charge_needs_restore,
                )
            else:
                # A run may span midnight, or HA may restart the next day. The
                # normal same-day restoration path cannot run in that case, so
                # carry the original limits forward and restore them before the
                # next weekly evaluation.
                restore_pending = bool(data.get("restore_pending", False))
                unfinished_run = bool(data.get("registers_written", False)) and not data.get("complete", False)
                self._restore_saved_max_soc(data.get("saved_max_soc"))
                getattr(self, "_cutoff_applied_names", set()).clear()
                if restore_pending or unfinished_run:
                    ctrl.weekly_full_charge_complete = bool(data.get("complete", False))
                    ctrl.weekly_full_charge_registers_written = False
                    ctrl._weekly_charge_needs_restore = True
                    ctrl._weekly_charge_status["state"] = (
                        "Complete" if ctrl.weekly_full_charge_complete else "Idle"
                    )
                    _LOGGER.info(
                        "Weekly Full Charge: Stored state from %s needs cutoff restoration before continuing",
                        stored_date,
                    )
                else:
                    _LOGGER.debug("Weekly Full Charge: Stored state is from %s, today is %s - ignoring",
                                  stored_date, today_iso)

        except Exception as e:
            _LOGGER.error("Weekly Full Charge: Failed to load persisted state: %s", e)

    async def save_state(self) -> None:
        """Save weekly charge state to persistent storage."""
        ctrl = self._controller
        if not ctrl.weekly_full_charge_enabled:
            return

        try:
            now = datetime.now()
            data = {
                "complete": ctrl.weekly_full_charge_complete,
                "registers_written": ctrl.weekly_full_charge_registers_written,
                "state": ctrl._weekly_charge_status.get("state", "Idle"),
                "restore_pending": bool(getattr(ctrl, "_weekly_charge_needs_restore", False)),
                "saved_max_soc": dict(getattr(ctrl, "_weekly_charge_saved_max_soc", {})),
                "date": date.today().isoformat(),
                "timestamp": now.isoformat(),
                # Delay state (bundled in the same store for legacy reasons)
                "delay_unlocked": ctrl._charge_delay_unlocked,
                "solar_t_start": ctrl._solar_t_start,
            }
            await self._store.async_save(data)
            _LOGGER.debug("Weekly Full Charge: Saved state to storage")
        except Exception as e:
            _LOGGER.error("Weekly Full Charge: Failed to save state: %s", e)

    async def async_flush_state(self) -> None:
        """Persist the coherent weekly state during config-entry unload."""
        await self.save_state()

    def _restore_saved_max_soc(self, stored: Any) -> None:
        """Restore per-battery SOC values saved before a 100% override."""
        saved = getattr(self._controller, "_weekly_charge_saved_max_soc", None)
        if saved is None:
            return
        saved.clear()
        if not isinstance(stored, dict):
            return
        for name, value in stored.items():
            try:
                value_int = int(value)
            except (TypeError, ValueError):
                continue
            if 0 <= value_int <= 100:
                saved[str(name)] = value_int

    @staticmethod
    def _get_configured_max_soc(coordinator: Any) -> int:
        """Return the persisted user ceiling before falling back to live telemetry."""
        fallback = getattr(coordinator, "max_soc", 100)
        config_entry = getattr(coordinator, "_config_entry", None)
        entry_data = getattr(config_entry, "data", {})
        battery_configs = entry_data.get("batteries", []) if isinstance(entry_data, dict) else []
        for battery_config in battery_configs:
            if battery_config.get("name") == getattr(coordinator, "name", None):
                fallback = battery_config.get("max_soc", fallback)
                break
        try:
            return max(0, min(100, int(fallback)))
        except (TypeError, ValueError):
            return 100

    async def _restore_hardware_cutoffs(self, reason: str) -> bool:
        """Restore saved hardware SOC cutoffs and report whether all writes worked."""
        ctrl = self._controller
        all_ok = True
        saved = getattr(ctrl, "_weekly_charge_saved_max_soc", {})
        for coordinator in ctrl.coordinators:
            if getattr(coordinator, "battery_manual_mode_enabled", False):
                continue
            if ctrl._is_backup_function_active(coordinator):
                continue
            if not coordinator.capabilities.hardware_soc_cutoff:
                _LOGGER.debug(
                    "%s: No hardware cutoff to restore (software-enforced battery)",
                    coordinator.name,
                )
                continue

            original_max_soc = saved.get(
                coordinator.name, self._get_configured_max_soc(coordinator)
            )
            try:
                ok = await coordinator.set_charge_cutoff(original_max_soc)
            except Exception as e:
                ok = False
                _LOGGER.error(
                    "%s: Failed to restore charging cutoff to %d%% after %s: %s",
                    coordinator.name,
                    original_max_soc,
                    reason,
                    e,
                )
            if ok:
                _LOGGER.info(
                    "%s: Restored hardware cutoff to %d%% after %s",
                    coordinator.name,
                    original_max_soc,
                    reason,
                )
            else:
                all_ok = False
                _LOGGER.error(
                    "%s: Charging cutoff restore to %d%% was rejected after %s; will retry",
                    coordinator.name,
                    original_max_soc,
                    reason,
                )
        return all_ok

    async def handle_registers(self) -> None:
        """Manage weekly full charge register writes and completion detection.

        Runs independently of control mode (predictive/normal) to ensure
        hardware registers are properly configured when weekly charge is active.

        Responsibilities:
        - Write register 44000 to 100% on first activation (v2 only)
        - Detect completion (all batteries at 100% or BMS cutoff at 99%)
        - Restore register 44000 to configured max_soc when complete
        - Re-enable hysteresis after completion
        """
        ctrl = self._controller

        # Always tick BMS-cutoff counters unconditionally — _get_available_batteries()
        # reads is_battery_full() later in the same cycle for the normal max_soc=100% case.
        self.tick_bms_cutoff()

        # Mid-charge abort: day changed (or feature disabled) while registers were already at 100%.
        # Restore hardware cutoff to max_soc before anything else.
        if ctrl._weekly_charge_needs_restore:
            _LOGGER.info("Weekly Full Charge: Restoring hardware cutoff registers after mid-charge abort")
            was_complete = ctrl.weekly_full_charge_complete
            restored = await self._restore_hardware_cutoffs("mid-charge abort")
            if not restored:
                await self.save_state()
                return
            ctrl._weekly_charge_saved_max_soc.clear()
            getattr(self, "_cutoff_applied_names", set()).clear()
            ctrl._weekly_charge_needs_restore = False
            if not was_complete:
                ctrl._weekly_charge_status["state"] = "Idle"
                ctrl._weekly_charge_status.pop("completion_reason", None)
            await self.save_state()

        if not ctrl.weekly_full_charge_enabled and not ctrl._force_full_charge:
            return
        if not self.is_active():
            return

        automatic_batteries = [
            coordinator
            for coordinator in ctrl.coordinators
            if not getattr(coordinator, "battery_manual_mode_enabled", False)
        ]
        if not automatic_batteries:
            # Do not mark a weekly run as active when every battery is owned by
            # individual manual mode. Nothing may be written until a battery
            # returns to the automatic pool.
            ctrl._weekly_charge_status["state"] = "Idle"
            ctrl._weekly_charge_status.pop("completion_reason", None)
            return

        # Check if unified charge delay is active - if so, don't write registers yet
        # Skip delay logic when force button was pressed
        if (ctrl.charge_delay_enabled and not ctrl._charge_delay_unlocked
                and not ctrl._force_full_charge and not ctrl._balance_monitor_overrides_delay()):
            return  # Delay is handled by _is_charge_delayed() in _is_operation_allowed()

        # Write register 44000 to 100% on first activation (v2 only - v3 uses software enforcement).
        # Also re-write after HA restart: registers_written may be True (from persisted state)
        # but async_setup_entry wrote max_soc back to the hardware register.  The
        # in-memory _cutoff_applied_names set starts empty on every restart, so the
        # persisted original SOC can be retained while 100% is re-applied.
        applied_names = getattr(self, "_cutoff_applied_names", set())
        pending_hardware = {
            coordinator.name
            for coordinator in ctrl.coordinators
            if not getattr(coordinator, "battery_manual_mode_enabled", False)
            and not ctrl._is_backup_function_active(coordinator)
            and coordinator.capabilities.hardware_soc_cutoff
        }
        need_write = (
            not ctrl.weekly_full_charge_registers_written
            or not ctrl._weekly_charge_saved_max_soc
            or bool(pending_hardware - applied_names)
        )
        if need_write:
            is_restart_reapply = ctrl.weekly_full_charge_registers_written
            if is_restart_reapply:
                _LOGGER.info("Weekly Full Charge: Re-applying 100%% cutoff after HA restart")
            else:
                _LOGGER.info("Weekly Full Charge: Activating for compatible batteries")
            for coordinator in ctrl.coordinators:
                if getattr(coordinator, "battery_manual_mode_enabled", False):
                    continue
                if ctrl._is_backup_function_active(coordinator):
                    _LOGGER.debug("%s: Skipping weekly full charge - backup function is active", coordinator.name)
                    continue

                if not coordinator.capabilities.hardware_soc_cutoff:
                    _LOGGER.debug(
                        "%s: Weekly full charge - no hardware cutoff register (v3 battery). "
                        "Using software enforcement to 100%%.",
                        coordinator.name
                    )
                    # v3 batteries: mark as verified so the restart-proxy check doesn't
                    # loop forever when all coordinators are v3.
                    ctrl._weekly_charge_saved_max_soc.setdefault(
                        coordinator.name, int(coordinator.max_soc)
                    )
                    applied_names.add(coordinator.name)
                    continue

                # v2 batteries: write hardware register
                try:
                    # Save original max_soc before overwriting the hardware register.
                    # setdefault preserves the value restored from storage after HA
                    # restarts, when the live poll already reports 100%.
                    ctrl._weekly_charge_saved_max_soc.setdefault(
                        coordinator.name, self._get_configured_max_soc(coordinator)
                    )
                    # Raise the hardware charging cutoff to 100%
                    ok = await coordinator.set_charge_cutoff(100)
                    if ok:
                        applied_names.add(coordinator.name)
                        _LOGGER.debug(
                            "%s: Set hardware charging cutoff to 100%% (saved original max_soc=%d%%)",
                            coordinator.name,
                            ctrl._weekly_charge_saved_max_soc[coordinator.name],
                        )
                    else:
                        _LOGGER.error(
                            "%s: Failed to set hardware charging cutoff to 100%%; will retry",
                            coordinator.name,
                        )
                except Exception as e:
                    _LOGGER.error("%s: Failed to write charging cutoff register: %s", coordinator.name, e)

            ctrl.weekly_full_charge_registers_written = True
            ctrl._weekly_charge_status["state"] = "Charging to 100%"
            ctrl._weekly_charge_status.pop("completion_reason", None)
            # Persist state so that the next restart can restore both registers_written
            # and the status field immediately.
            self._create_background_task(
                self.save_state(), "omnibattery_weekly_charge_state_save"
            )

        if hasattr(ctrl, "_normal_balance_reset_if_new_day"):
            ctrl._normal_balance_reset_if_new_day()

        # Completion: when every battery has reached the taper zone (top voltage
        # seen latch) or has a hardware/BMS full signal.  The 60-second delta-V
        # measurement in MaxSocChargeManager.handle_measurement continues as a
        # diagnostic but no longer gates completion.
        batteries_with_data = [
            c
            for c in ctrl.coordinators
            if c.data
            and not getattr(c, "battery_manual_mode_enabled", False)
        ]
        all_batteries_full = bool(batteries_with_data) and all(
            self.is_battery_full(c)
            for c in batteries_with_data
        )

        ctrl._weekly_charge_status["batteries"] = {
            c.name: {
                "soc": (c.data or {}).get("battery_soc"),
                "bms_cutoff_cycles": self._bms_cutoff_counts.get(c.name, 0),
                "is_full": self.is_battery_full(c),
            }
            for c in ctrl.coordinators
            if c.data
            and not getattr(c, "battery_manual_mode_enabled", False)
        }

        if all_batteries_full and not ctrl.weekly_full_charge_complete:
            await self._complete_weekly_charge("all_batteries_full")

    async def _complete_weekly_charge(self, reason: str) -> None:
        """Mark weekly full charge complete and restore configured limits."""
        ctrl = self._controller
        _LOGGER.info("Weekly Full Charge: Complete (%s) - reverting to configured limits", reason)
        ctrl.weekly_full_charge_complete = True
        ctrl._weekly_charge_status["state"] = "Complete"
        ctrl._weekly_charge_status["completion_reason"] = reason
        grid_mgr = getattr(ctrl, "_weekly_grid_charge_mgr", None)
        if grid_mgr is not None:
            grid_mgr.clear_session()
        completion_batteries: dict = {}
        for coordinator in ctrl.coordinators:
            if getattr(coordinator, "battery_manual_mode_enabled", False):
                continue
            data = coordinator.data or {}
            soc = data.get("battery_soc")
            bms_cutoff_confirmed = self.is_battery_full(coordinator)
            if soc is not None and soc >= 100:
                per_battery_reason = "soc_100"
            elif bms_cutoff_confirmed:
                per_battery_reason = "bms_cutoff"
            else:
                per_battery_reason = "not_confirmed"
            completion_batteries[coordinator.name] = {
                "soc_at_completion": soc,
                "max_cell_voltage_at_completion": data.get("max_cell_voltage"),
                "completion_reason": per_battery_reason,
                "bms_cutoff_cycles": self._bms_cutoff_counts.get(coordinator.name, 0),
            }
        ctrl._weekly_charge_status["batteries"] = completion_batteries

        # Preserve the post-cutoff measurement request while clearing the
        # debounce counters used by the weekly completion detector. A Venus E
        # pack may be cut by its BMS below 3.60 V, so this is not exclusive to
        # the coupled Venus A/D path.
        measurement_state = getattr(
            ctrl, "_normal_balance_bms_cutoff_measurement", None
        )
        if measurement_state is not None:
            for coordinator in ctrl.coordinators:
                data = coordinator.data or {}
                try:
                    vmax = float(data.get("max_cell_voltage"))
                except (TypeError, ValueError):
                    vmax = None
                taper_latched = getattr(
                    ctrl, "_normal_balance_voltage_tapered", {}
                ).get(coordinator, False)
                in_taper_zone = (
                    vmax is not None
                    and vmax >= NORMAL_BALANCE_TAPER_CELL_VOLTAGE
                )
                if (
                    self._bms_cutoff_counts.get(coordinator.name, 0)
                    >= _BMS_CUTOFF_REQUIRED_CYCLES
                    and (
                        getattr(coordinator, "battery_version", None)
                        in NORMAL_BALANCE_BMS_CUTOFF_VERSIONS
                        or in_taper_zone
                        or taper_latched
                    )
                ):
                    measurement_state[coordinator] = "pending"
        self._bms_cutoff_counts.clear()

        # Restore the saved hardware ceiling. Keep the saved values when a write
        # fails so the next control cycle can retry instead of silently
        # completing with the device still at 100%.
        restored = await self._restore_hardware_cutoffs("weekly charge completion")
        if restored:
            ctrl._weekly_charge_saved_max_soc.clear()
            getattr(self, "_cutoff_applied_names", set()).clear()
            ctrl._weekly_charge_needs_restore = False
        else:
            ctrl._weekly_charge_needs_restore = True

        # Clear the normal top-charge pause so next week starts fresh.

        # Fire a one-shot delta-V capture for batteries that did not complete
        # the 60-second diagnostic measurement (measurement is best-effort).
        measured = getattr(ctrl, "_normal_balance_last_delta_v", {})
        for coordinator in ctrl.coordinators:
            if getattr(coordinator, "battery_manual_mode_enabled", False):
                continue
            if (
                measurement_state is not None
                and measurement_state.get(coordinator) == "pending"
            ):
                # A BMS cutoff measurement gets its 60 s rest window in the
                # normal measurement handler; do not replace it with the
                # immediate completion snapshot.
                continue
            if coordinator in measured:
                continue
            if not coordinator.data:
                continue
            try:
                vmax = float(coordinator.data.get("max_cell_voltage"))
                vmin = float(coordinator.data.get("min_cell_voltage"))
            except (TypeError, ValueError):
                continue
            soc = coordinator.data.get("battery_soc")
            _LOGGER.info(
                "%s: Recording top-balance snapshot at completion: vmax=%.3f vmin=%.3f delta=%.4f V",
                coordinator.name, vmax, vmin, round(vmax - vmin, 4),
            )
            ctrl._normal_balance_last_delta_v[coordinator] = round(vmax - vmin, 4)
            if ctrl._balance_monitor is not None:
                await ctrl._balance_monitor.async_record_top_balance_measurement(
                    coordinator,
                    vmax,
                    vmin,
                    soc,
                    phase="top_charge_taper_complete",
                )

        # Re-enable hysteresis for batteries that have it configured.
        for coordinator in ctrl.coordinators:
            if getattr(coordinator, "battery_manual_mode_enabled", False):
                continue
            if coordinator.enable_charge_hysteresis:
                coordinator._hysteresis_active = True
                current_soc = coordinator.data.get("battery_soc", 100) if coordinator.data else 100
                coordinator._hysteresis_base_soc = current_soc
                _LOGGER.debug(
                    "%s: Re-enabled hysteresis after weekly full charge (base SOC: %.1f%%)",
                    coordinator.name,
                    coordinator._hysteresis_base_soc,
                )

        await self.save_state()
