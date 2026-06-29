"""Weekly full charge management for Marstek Venus.

Owns:
- Day-based activation logic (is_active)
- Persistence of completion / registers-written across HA restarts
- Hardware register (44000) writes to allow charging to 100% on v2 batteries
- Completion detection (top-voltage measurement, 100% SOC, or BMS cutoff at 99%)
- Register restore and hysteresis re-enable on completion
- Mid-charge abort handling when day or feature flag changes

Weekly uses the same 100% top-voltage taper/measurement flow as a user-selected
max_soc=100. Active cell balancing remains the job of the per-battery Active
Balance Mode.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from ..const import DOMAIN, NORMAL_BALANCE_TAPER_CELL_VOLTAGE, WEEKDAY_MAP

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# Inverter state raw value for "Standby" (BMS has cut off, no active charge/discharge).
_INVERTER_STATE_STANDBY = 1
# Battery power below this (W) is treated as "not charging" for BMS-cutoff detection.
_BMS_CUTOFF_POWER_W = 10
# Consecutive update cycles (~2 s each) of BMS-cutoff conditions required before
# declaring completion at 99%.  5 × 2 s = 10 s is enough to outlast the Modbus
# response delay after writing registers, but fast enough to react to a real cutoff.
_BMS_CUTOFF_REQUIRED_CYCLES = 5


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
        # Edge-trigger guard so the "already completed" debug line logs once per
        # completion, not once per is_active() call (called per-battery per-cycle).
        self._already_complete_logged = False

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
        weekly_active = self.is_active()
        for c in ctrl.coordinators:
            if not c.data:
                self._bms_cutoff_counts[c.name] = 0
                continue
            soc = c.data.get("battery_soc", 0)
            # During weekly charge, also arm the counter when cells are in the
            # top taper zone regardless of SOC — handles BMS coulomb drift where
            # the BMS cuts at ~3.65 V but reports e.g. 95% SOC.
            in_taper_zone = False
            if weekly_active:
                vmax = c.data.get("max_cell_voltage")
                try:
                    in_taper_zone = vmax is not None and float(vmax) >= NORMAL_BALANCE_TAPER_CELL_VOLTAGE
                except (TypeError, ValueError):
                    pass
            if soc >= 99 or in_taper_zone:
                power = c.data.get("battery_power", None)
                inv_state = c.data.get("inverter_state", None)
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
                    and inv_state is not None
                    and commanded > _BMS_CUTOFF_POWER_W
                    and power <= _BMS_CUTOFF_POWER_W
                    and inv_state == _INVERTER_STATE_STANDBY
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

    def is_battery_full(self, coordinator: Any) -> bool:
        """Return True if this battery counts as fully charged.

        Read-only: does not modify _bms_cutoff_counts (tick_bms_cutoff() does).
        Used by both handle_registers() (weekly completion) and
        _get_available_batteries() (normal max_soc=100% case).
        """
        if not coordinator.data:
            return False
        soc = coordinator.data.get("battery_soc", 0)
        if soc >= 100:
            return True
        # BMS cutoff confirmed — safe without SOC gate because tick_bms_cutoff
        # only increments the counter when soc >= 99 OR (weekly active AND cells
        # in taper zone), so the count is 0 outside those conditions.
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
                ctrl.weekly_full_charge_complete = False
                ctrl.weekly_full_charge_registers_written = False
                ctrl._force_full_charge = False
                ctrl._weekly_charge_status["state"] = "Idle"
                ctrl._weekly_charge_status.pop("completion_reason", None)
                # Save the cleared state asynchronously (don't await to avoid blocking)
                asyncio.create_task(self.save_state())

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
                _LOGGER.info(
                    "Weekly Full Charge: Restored state - complete=%s, "
                    "registers_written=%s, delay_unlocked=%s",
                    ctrl.weekly_full_charge_complete,
                    ctrl.weekly_full_charge_registers_written,
                    ctrl._charge_delay_unlocked,
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
            for coordinator in ctrl.coordinators:
                if ctrl._is_active_balance_mode_running(coordinator):
                    continue
                if ctrl._is_backup_function_active(coordinator):
                    continue
                if not coordinator.capabilities.hardware_soc_cutoff:
                    _LOGGER.debug("%s: No hardware cutoff register to restore (v3 battery)", coordinator.name)
                    continue
                try:
                    # Use the saved value captured before writing 100%; fall back to current max_soc
                    # only if no saved value exists (e.g. HA restarted mid-charge).
                    original_max_soc = ctrl._weekly_charge_saved_max_soc.get(
                        coordinator.name, coordinator.max_soc
                    )
                    await coordinator.set_charge_cutoff(original_max_soc)
                    _LOGGER.info("%s: Restored hardware cutoff to %d%% after mid-charge abort",
                                 coordinator.name, original_max_soc)
                except Exception as e:
                    _LOGGER.error("%s: Failed to restore charging cutoff register: %s", coordinator.name, e)
            ctrl._weekly_charge_saved_max_soc.clear()
            ctrl._weekly_charge_needs_restore = False
            ctrl._weekly_charge_status["state"] = "Idle"
            ctrl._weekly_charge_status.pop("completion_reason", None)

        if not ctrl.weekly_full_charge_enabled and not ctrl._force_full_charge:
            return
        if not self.is_active():
            return

        # Check if unified charge delay is active - if so, don't write registers yet
        # Skip delay logic when force button was pressed
        if (ctrl.charge_delay_enabled and not ctrl._charge_delay_unlocked
                and not ctrl._force_full_charge and not ctrl._balance_monitor_overrides_delay()):
            return  # Delay is handled by _is_charge_delayed() in _is_operation_allowed()

        # Write register 44000 to 100% on first activation (v2 only - v3 uses software enforcement).
        # Also re-write after HA restart: registers_written may be True (from persisted state)
        # but async_setup_entry wrote max_soc back to the hardware register.  The empty
        # _weekly_charge_saved_max_soc dict is a reliable proxy for "not yet applied this
        # session" because it is in-memory only and starts empty on every restart.
        need_write = (not ctrl.weekly_full_charge_registers_written
                      or not ctrl._weekly_charge_saved_max_soc)
        if need_write:
            is_restart_reapply = ctrl.weekly_full_charge_registers_written
            if is_restart_reapply:
                _LOGGER.info("Weekly Full Charge: Re-applying 100%% cutoff after HA restart")
            else:
                _LOGGER.info("Weekly Full Charge: Activating for compatible batteries")
            for coordinator in ctrl.coordinators:
                if ctrl._is_active_balance_mode_running(coordinator):
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
                    ctrl._weekly_charge_saved_max_soc[coordinator.name] = coordinator.max_soc
                    continue

                # v2 batteries: write hardware register
                try:
                    # Save original max_soc before overwriting the hardware register
                    ctrl._weekly_charge_saved_max_soc[coordinator.name] = coordinator.max_soc
                    # Raise the hardware charging cutoff to 100%
                    await coordinator.set_charge_cutoff(100)
                    _LOGGER.debug("%s: Set hardware charging cutoff to 100%% (saved original max_soc=%d%%)",
                                  coordinator.name, coordinator.max_soc)
                except Exception as e:
                    _LOGGER.error("%s: Failed to write charging cutoff register: %s", coordinator.name, e)

            ctrl.weekly_full_charge_registers_written = True
            ctrl._weekly_charge_status["state"] = "Charging to 100%"
            ctrl._weekly_charge_status.pop("completion_reason", None)
            # Persist state so that the next restart can restore both registers_written
            # and the status field immediately.
            asyncio.create_task(self.save_state())

        if hasattr(ctrl, "_normal_balance_reset_if_new_day"):
            ctrl._normal_balance_reset_if_new_day()

        # Completion: when every battery has reached the taper zone (top voltage
        # seen latch) or has a hardware/BMS full signal.  The 60-second delta-V
        # measurement in MaxSocChargeManager.handle_measurement continues as a
        # diagnostic but no longer gates completion.
        batteries_with_data = [
            c
            for c in ctrl.coordinators
            if c.data and not ctrl._is_active_balance_mode_running(c)
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
            if c.data and not ctrl._is_active_balance_mode_running(c)
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
        completion_batteries: dict = {}
        for coordinator in ctrl.coordinators:
            if ctrl._is_active_balance_mode_running(coordinator):
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
        self._bms_cutoff_counts.clear()

        # Restore register 44000 to original max_soc values (v2 only).
        for coordinator in ctrl.coordinators:
            if ctrl._is_active_balance_mode_running(coordinator):
                continue
            if ctrl._is_backup_function_active(coordinator):
                _LOGGER.debug("%s: Skipping cutoff restore - backup function is active", coordinator.name)
                continue

            if not coordinator.capabilities.hardware_soc_cutoff:
                _LOGGER.debug("%s: No hardware cutoff register to restore (v3 battery)", coordinator.name)
                continue

            try:
                original_max_soc = ctrl._weekly_charge_saved_max_soc.get(
                    coordinator.name, coordinator.max_soc
                )
                await coordinator.set_charge_cutoff(original_max_soc)
                _LOGGER.debug("%s: Restored hardware cutoff to %d%%",
                              coordinator.name, original_max_soc)
            except Exception as e:
                _LOGGER.error("%s: Failed to restore charging cutoff register: %s", coordinator.name, e)

        ctrl._weekly_charge_saved_max_soc.clear()

        # Clear the top-voltage latch so next week starts fresh.
        if hasattr(ctrl, "_normal_balance_top_voltage_seen"):
            ctrl._normal_balance_top_voltage_seen.clear()
        if hasattr(ctrl, "_normal_balance_pause_latch_soc"):
            ctrl._normal_balance_pause_latch_soc.clear()
        for coordinator in ctrl.coordinators:
            ctrl.remove_charge_block("normal_balance_pause", coordinator=coordinator)

        # Fire a one-shot delta-V capture for batteries that did not complete
        # the 60-second diagnostic measurement (measurement is best-effort).
        measured = getattr(ctrl, "_normal_balance_last_delta_v", {})
        for coordinator in ctrl.coordinators:
            if ctrl._is_active_balance_mode_running(coordinator):
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
            if ctrl._is_active_balance_mode_running(coordinator):
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
