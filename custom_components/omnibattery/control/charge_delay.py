"""Unified charge-delay management for Marstek Venus.

Owns:
- Same-day delay latch persistence (dedicated ``charge_delay_state`` Store)
- The unified delay gate ``is_charge_delayed`` queried by the blocker registry
- The solar-forecast energy-balance decision (``_should_delay_charge``)
- The estimated unlock-time projection (``_estimate_energy_balance_unlock_h``)
- The per-day reset + proactive evaluation that keeps the sensor populated

The delay latch (``_charge_delay_unlocked``, ``_solar_t_start``,
``_delay_setpoint_reached``) lives on the controller because the weekly
full-charge manager bundles those same fields in its own Store for backward
compatibility, and the ChargeDelaySensor reads ``_charge_delay_status``
directly. This manager reads/writes those controller attributes by reference,
matching the existing extraction template.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CHARGE_EFFICIENCY,
    DELAY_SAFETY_FACTOR,
    DELAY_SOC_SETPOINT_HYSTERESIS,
    DOMAIN,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_REALTIME_PRICE,
    T_START_FALLBACK_HOUR,
)
from ..solar_forecast import (
    get_configured_solar_forecast_sensor,
    read_remaining_solar_kwh,
)

# Price-aware release only makes sense when charging is actually scheduled by
# price. In time-slot mode prices drive nothing, so a configured price_sensor
# (e.g. left over from testing) must not pull the solar-delay release around.
_PRICE_DRIVEN_MODES = frozenset(
    {PREDICTIVE_MODE_DYNAMIC_PRICING, PREDICTIVE_MODE_REALTIME_PRICE}
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
_REAL_DATETIME = datetime

# Unlock reasons that are fail-safe responses to missing/transient data, not a
# real "charging is legitimately allowed" decision. These must NOT latch the
# permanent daily unlock: otherwise a momentary gap (e.g. the solar-forecast
# sensor going unavailable at the midnight rollover) silently disables the
# charge delay for the rest of the day. Keeping them re-evaluable lets the delay
# re-arm as soon as the data comes back.
_TRANSIENT_UNLOCK_REASONS = frozenset({"no_forecast"})

# Fields that describe a completed forecast decision.  The setpoint phase exits
# before calculating a new decision, so retaining these values there makes the
# status sensor report yesterday's (or a previous-cycle) solar balance as if it
# were current.
_DECISION_STATUS_FIELDS = (
    "forecast_kwh",
    "solar_t_start",
    "solar_t_end",
    "energy_needed_kwh",
    "remaining_solar_kwh",
    "remaining_consumption_kwh",
    "net_solar_kwh",
    "consumption_forecast_source",
    "profile_coverage_ratio",
    "profile_days",
    "profile_fallback_reason",
    "solar_forecast_source",
    "solar_forecast_diagnostic_source",
    "solar_forecast_conversion",
    "charge_time_h",
    "estimated_unlock_time",
    "unlock_reason",
)


def _decision_now() -> datetime:
    """Return Home Assistant local time, preserving the legacy test clock hook."""
    now = dt_util.now()
    if datetime is not _REAL_DATETIME:
        mocked_now = datetime.now()
        now = now.replace(
            hour=int(mocked_now.hour),
            minute=int(mocked_now.minute),
            second=int(getattr(mocked_now, "second", 0)),
            microsecond=int(getattr(mocked_now, "microsecond", 0)),
        )
    return now


class ChargeDelayManager:
    """Manages the unified charge-delay gate, persistence and projection."""

    def __init__(
        self,
        hass: "HomeAssistant",
        config_entry: "ConfigEntry",
        controller: Any,
    ) -> None:
        self._hass = hass
        self._controller = controller
        self._store: Store = Store(
            hass, 1, f"{DOMAIN}.{config_entry.entry_id}.charge_delay_state"
        )
        self._save_task: asyncio.Task | None = None

    def _create_background_task(self, coroutine, name: str) -> asyncio.Task | None:
        """Create persistence work through the owning entry controller."""
        create = getattr(self._controller, "_create_entry_background_task", None)
        if callable(create):
            task = create(coroutine, name)
        else:
            create = getattr(self._hass, "async_create_task", None)
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

    def _clear_completed_decision_status(self) -> None:
        """Clear solar-decision details while charging to the SOC setpoint."""
        status = self._controller._charge_delay_status
        for key in _DECISION_STATUS_FIELDS:
            if key in status:
                status[key] = None

    async def load_state(self) -> None:
        """Restore same-day charge delay latch state from storage."""
        ctrl = self._controller
        if not ctrl.charge_delay_enabled:
            return

        try:
            data = await self._store.async_load()
            if not data:
                return

            today_iso = dt_util.now().date().isoformat()
            if data.get("date") != today_iso:
                return

            ctrl._charge_delay_unlocked = data.get("delay_unlocked", False)
            ctrl._delay_setpoint_reached = data.get("delay_setpoint_reached", False)
            if data.get("solar_t_start") is not None:
                ctrl._solar_t_start = data.get("solar_t_start")

            _LOGGER.info(
                "Charge Delay: restored state - unlocked=%s, setpoint_reached=%s",
                ctrl._charge_delay_unlocked,
                ctrl._delay_setpoint_reached,
            )
        except Exception as exc:
            _LOGGER.error("Charge Delay: failed to load persisted state: %s", exc)

    def schedule_save(self) -> None:
        """Persist charge delay latch state without blocking the control loop."""
        if not self._controller.charge_delay_enabled:
            return

        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
        self._save_task = self._create_background_task(
            self._deferred_save(), "omnibattery_charge_delay_save"
        )

    async def _deferred_save(self) -> None:
        """Let the current control-cycle state settle before saving."""
        await asyncio.sleep(0)
        await self._save_state()

    async def _save_state(self) -> None:
        """Save charge delay latch state to persistent storage."""
        ctrl = self._controller
        try:
            await self._store.async_save({
                "date": dt_util.now().date().isoformat(),
                "delay_unlocked": ctrl._charge_delay_unlocked,
                "delay_setpoint_reached": ctrl._delay_setpoint_reached,
                "solar_t_start": ctrl._solar_t_start,
                "timestamp": dt_util.now().isoformat(),
            })
        except Exception as exc:
            _LOGGER.error("Charge Delay: failed to save persisted state: %s", exc)

    async def async_flush_state(self) -> None:
        """Persist the latest latch after entry-owned saves are cancelled."""
        task = self._save_task
        current = asyncio.current_task()
        if task is not None and task is not current and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._save_task = None
        if self._controller.charge_delay_enabled:
            await self._save_state()

    def handle_daily_reset_and_eval(self) -> None:
        """Reset the delay latch on a new day, then evaluate to keep the sensor live.

        Runs once per control cycle; no-op when the feature is disabled.
        """
        ctrl = self._controller
        if not ctrl.charge_delay_enabled:
            return

        today = dt_util.now().date()
        if ctrl._charge_delay_last_date != today:
            if ctrl._charge_delay_last_date is not None:
                # Real day change: reset delay state
                ctrl._charge_delay_unlocked = False
                ctrl._delay_setpoint_reached = False
                ctrl._solar_t_start = None
                ctrl._forecast_unavailable_since = None
                ctrl._forecast_zero_since = None
            # On first cycle after HA restart (_charge_delay_last_date is None),
            # _charge_delay_unlocked may have been restored from storage by
            # _weekly_charge_mgr.load_state() — preserve it rather than wiping it.
            ctrl._charge_delay_last_date = today
            ctrl._delay_last_log_time = 0
            # Reset status dict for sensor (preserve safety_margin_min)
            saved_margin = ctrl._charge_delay_status.get("safety_margin_min")
            for key in ctrl._charge_delay_status:
                if key not in ("state", "safety_margin_min"):
                    ctrl._charge_delay_status[key] = None
            ctrl._charge_delay_status["state"] = "Idle"
            if saved_margin is not None:
                ctrl._charge_delay_status["safety_margin_min"] = saved_margin
            ctrl._charge_delay_forecast_cache = None
            ctrl._charge_delay_forecast_source_cache = None
            ctrl._charge_delay_forecast_conversion_cache = None
            ctrl._charge_delay_profile_source_cache = None
            ctrl._charge_delay_balance_needs_charge = True
            self.schedule_save()
            _LOGGER.info("Charge Delay: New day - state reset")

        # Detect solar production start (shared with weekly charge)
        ctrl._consumption_tracker.detect_solar_t_start()
        # Proactively evaluate delay to keep ChargeDelaySensor populated
        self.is_charge_delayed()

    def is_charge_delayed(self) -> bool:
        """Unified gate: check if charging should be delayed based on solar forecast.

        Returns True if charging should be blocked, False if allowed.
        Called from _is_operation_allowed() for every charge attempt.
        """
        ctrl = self._controller
        if not ctrl.charge_delay_enabled:
            ctrl._charge_delay_status["state"] = "Disabled"
            return False

        # Skip delay entirely on the weekly full charge day when opted in
        if ctrl._balance_monitor_overrides_delay():
            ctrl._charge_delay_status["state"] = "Skipped - Full Charge Day"
            return False

        target_soc = ctrl._consumption_tracker.get_today_target_soc()
        ctrl._charge_delay_status["target_soc"] = target_soc
        ctrl._charge_delay_status["soc_setpoint"] = (
            ctrl._delay_soc_setpoint if ctrl._delay_soc_setpoint_enabled else None
        )

        # Already unlocked today?
        if ctrl._charge_delay_unlocked:
            ctrl._charge_delay_status["state"] = "Charging allowed"
            return False

        # SOC setpoint: delay only kicks in once all batteries reach the setpoint.
        # Hysteresis prevents oscillation: once the setpoint is reached, charging
        # only resumes if SOC drops DELAY_SOC_SETPOINT_HYSTERESIS % below it.
        if ctrl._delay_soc_setpoint_enabled:
            min_soc = min(
                (
                    c.data.get("battery_soc", 100)
                    for c in ctrl.coordinators
                    if c.data and not getattr(c, "battery_manual_mode_enabled", False)
                ),
                default=100,
            )
            if not ctrl._delay_setpoint_reached:
                if min_soc < ctrl._delay_soc_setpoint:
                    self._clear_completed_decision_status()
                    ctrl._charge_delay_status["state"] = "Charging to setpoint"
                    return False
                ctrl._delay_setpoint_reached = True
                self.schedule_save()
            else:
                low_threshold = ctrl._delay_soc_setpoint - DELAY_SOC_SETPOINT_HYSTERESIS
                if min_soc < low_threshold:
                    ctrl._delay_setpoint_reached = False
                    self.schedule_save()
                    self._clear_completed_decision_status()
                    ctrl._charge_delay_status["state"] = "Charging to setpoint"
                    return False

        # Evaluate delay conditions
        if self._should_delay_charge(target_soc):
            return True  # Keep delay active (block charging)

        # A fail-safe unlock from missing/transient data (e.g. the forecast
        # sensor briefly unavailable at the midnight rollover) must stay
        # re-evaluable so the delay re-arms once the data returns. Allow
        # charging for this cycle, but do not latch the permanent daily unlock.
        if ctrl._charge_delay_status.get("unlock_reason") in _TRANSIENT_UNLOCK_REASONS:
            return False

        # Delay conditions genuinely no longer met - unlock permanently for today
        ctrl._charge_delay_unlocked = True
        self.schedule_save()
        _LOGGER.info("Charge Delay: Unlocked (target_soc=%d%%) - charging now allowed", target_soc)
        # Persist unlock state if on weekly charge day
        if ctrl._weekly_charge_mgr.is_active():
            self._create_background_task(
                ctrl._weekly_charge_mgr.save_state(),
                "omnibattery_weekly_charge_state_save",
            )
        return False

    def refresh_setpoint_blocks(self) -> None:
        """Enforce the SOC setpoint floor per battery during the charge delay.

        The system-level gate (``is_charge_delayed``) only leaves the
        "charging to setpoint" phase once the *minimum* SOC across all batteries
        reaches the setpoint. With mixed SOCs (e.g. a second battery added at a
        lower charge), a higher-SOC battery keeps charging from grid past the
        setpoint while the lower one holds the system-wide gate open.

        Block each battery individually once it reaches the setpoint, so only
        batteries still below the floor keep charging. Once the floor is reached
        for all of them (``_delay_setpoint_reached``), the system-wide forecast
        delay (the global ``charge_delay`` block) governs everyone equally and
        these per-battery blocks are cleared.
        """
        ctrl = self._controller
        setpoint_active = (
            ctrl.charge_delay_enabled
            and ctrl._delay_soc_setpoint_enabled
            and not ctrl._charge_delay_unlocked
            and not ctrl._delay_setpoint_reached
            and not ctrl._balance_monitor_overrides_delay()
        )
        for coordinator in ctrl.coordinators:
            if getattr(coordinator, "battery_manual_mode_enabled", False):
                ctrl.remove_charge_block(
                    "charge_delay_setpoint", coordinator=coordinator
                )
                continue
            soc = coordinator.data.get("battery_soc") if coordinator.data else None
            if (
                setpoint_active
                and soc is not None
                and soc >= ctrl._delay_soc_setpoint
            ):
                ctrl.set_charge_block(
                    "charge_delay_setpoint",
                    "charge_delay",
                    {
                        "battery": coordinator.name,
                        "soc": soc,
                        "setpoint": ctrl._delay_soc_setpoint,
                    },
                    coordinator=coordinator,
                )
            else:
                ctrl.remove_charge_block("charge_delay_setpoint", coordinator=coordinator)

    def _profile_forecast_between(
        self,
        start_h: float,
        end_h: float,
    ):
        """Return a mature profile forecast for today's local hour range."""
        ctrl = self._controller
        tracker = getattr(ctrl, "_consumption_tracker", None)
        profile = getattr(tracker, "consumption_profile", None)
        if profile is None or tracker is None or end_h <= start_h:
            return None
        try:
            current = _decision_now()
            profile_timezone = getattr(profile, "_timezone", lambda: None)()
            if profile_timezone is not None:
                current = (
                    current.astimezone(profile_timezone)
                    if current.tzinfo is not None
                    else current.replace(tzinfo=profile_timezone)
                )
            elif current.tzinfo is None:
                current = current.replace(tzinfo=dt_util.UTC)
            midnight = current.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            start = midnight + timedelta(hours=max(0.0, start_h))
            end = midnight + timedelta(hours=max(0.0, end_h))
            forecast = tracker.forecast_consumption_between(start, end, fallback="legacy_daily")
            if forecast.source in {"profile", "legacy_daily", "vacation_baseline"}:
                return forecast
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Charge Delay: profile forecast failed: %s", exc)
        return None

    def _should_delay_charge(self, target_soc: int) -> bool:
        """Determine if charging should be delayed based on solar forecast.

        Unified method for both daily (max_soc) and weekly (100%) charge delay.
        Uses the live solar forecast sensor (updated throughout the day).

        Returns True to keep delay active (block charging),
        False to unlock charging.

        Fail-safe: any failure → unlock (allow charging).

        Decision flow:
        1. No forecast sensor or unavailable → unlock immediately
        2. Energy balance check: (usable_energy + forecast) < consumption → unlock (grid needed)
           Recalculated only when forecast value changes (> 0.05 kWh).
        3. No T_start detected and past fallback hour → unlock
        4. Past T_end with no active production → unlock
        5. Batteries already at target → unlock
        6. Insufficient remaining solar energy → unlock, unless only the safety
           cushion is missing and a cheaper feasible hour lies ahead → hold
        7. Insufficient time before T_end → unlock
        8. Otherwise → keep delay active
        """
        ctrl = self._controller
        automatic_batteries = [
            coordinator for coordinator in ctrl.coordinators
            if not getattr(coordinator, "battery_manual_mode_enabled", False)
        ]

        now = _decision_now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=dt_util.UTC)
        now_h = now.hour + now.minute / 60.0
        status = ctrl._charge_delay_status
        _h_to_hhmm = ctrl._consumption_tracker.h_to_hhmm

        def _unlock(reason):
            """Set status and return False (unlock)."""
            status["unlock_reason"] = reason
            status["state"] = f"Unlocking ({reason})"
            return False

        # Update common status fields
        status["solar_t_start"] = _h_to_hhmm(ctrl._solar_t_start)

        # --- Exception 1: No solar forecast sensor or unavailable ---
        if not (
            get_configured_solar_forecast_sensor(ctrl, "remaining")
            or get_configured_solar_forecast_sensor(ctrl, "today")
        ):
            _LOGGER.info("Charge Delay: No solar forecast sensor configured - unlocking (reason: no_forecast)")
            return _unlock("no_forecast")

        # A configured forecast sensor can briefly read unavailable/unknown/invalid
        # while it updates. Treating that transient blip as "no forecast" would
        # commit a PERMANENT daily unlock (see is_charge_delayed), so a momentary
        # gap silently disables the delay for the rest of the day. Instead, hold the
        # current delay through a short grace window and only unlock if the sensor
        # stays unavailable. (A sensor that is not configured at all still unlocks
        # immediately above — that is a deliberate fail-safe, not a transient.)
        # All Charge Delay calculations consume the shared *remaining* budget.
        # In particular, legacy whole-day sensors are converted exactly once by
        # this adapter, while a remaining sensor is passed through unchanged.
        forecast = read_remaining_solar_kwh(ctrl.hass, ctrl, now=now)
        raw_forecast = (
            None if forecast.source == "fallback" else forecast.remaining_kwh
        )

        if raw_forecast is None:
            ctrl._forecast_zero_since = None
            mono = monotonic()
            if ctrl._forecast_unavailable_since is None:
                ctrl._forecast_unavailable_since = mono
            unavailable_s = mono - ctrl._forecast_unavailable_since
            if unavailable_s < ctrl._forecast_grace_s:
                status["state"] = "Waiting for forecast"
                _LOGGER.debug(
                    "Charge Delay: forecast unavailable for %.0fs (< %ds grace) - holding delay",
                    unavailable_s, ctrl._forecast_grace_s,
                )
                return True  # keep the delay active; re-evaluate when the sensor recovers
            _LOGGER.info(
                "Charge Delay: Solar forecast unavailable for %.0fs (> grace) - unlocking (reason: no_forecast)",
                unavailable_s,
            )
            return _unlock("no_forecast")

        # Forecast recovered / valid — clear the transient tracker.
        ctrl._forecast_unavailable_since = None
        status["unlock_reason"] = None
        forecast_today = raw_forecast
        forecast_origin = (
            "today" if forecast.original_source == "today_legacy"
            else forecast.original_source
        )
        # The scalar may briefly roll to zero at local midnight before the
        # provider publishes the new day's budget.  Dated periods are already
        # reconciled by read_remaining_solar_kwh; for a scalar-only zero, hold
        # through the same bounded grace used for unavailable values.  This is
        # deliberately not a permanent unlock, so a later valid update re-arms
        # the delay instead of leaving it open for the day.
        if raw_forecast <= 1e-9:
            zero_since = getattr(ctrl, "_forecast_zero_since", None)
            if zero_since is None and now_h < 1.0:
                zero_since = monotonic()
                ctrl._forecast_zero_since = zero_since
            if (
                zero_since is not None
                and monotonic() - zero_since < ctrl._forecast_grace_s
            ):
                status["forecast_kwh"] = raw_forecast
                status["solar_forecast_source"] = forecast_origin
                status["solar_forecast_diagnostic_source"] = forecast.source
                status["solar_forecast_conversion"] = forecast.conversion
                status["unlock_reason"] = None
                status["state"] = "Waiting for forecast"
                return True
        else:
            ctrl._forecast_zero_since = None

        # read_remaining_solar_kwh normalizes both configured sources to the
        # same future horizon, so downstream consumers must never subtract
        # production or apply a second temporal curve based on its origin.
        status["unlock_reason"] = None
        status["projected_unlock_time"] = None
        status["estimated_setpoint_time"] = None
        forecast_is_remaining = True
        status["solar_forecast_source"] = forecast_origin
        status["solar_forecast_diagnostic_source"] = forecast.source
        status["solar_forecast_conversion"] = forecast.conversion
        status["forecast_kwh"] = raw_forecast

        # --- Exception 2: Energy balance check (dynamic, recalculated only when forecast changes) ---
        total_capacity_kwh = sum(
            c.data.get("battery_total_energy", 0)
            for c in automatic_batteries
            if c.data
        )
        if total_capacity_kwh <= 0:
            _LOGGER.info("Charge Delay: Invalid battery capacity - unlocking")
            return _unlock("no_forecast")

        profile_balance = self._profile_forecast_between(
            now_h,
            24.0,
        )
        profile_source = profile_balance.source if profile_balance is not None else "legacy_daily"
        forecast_source = forecast_origin or forecast.source
        if (
            ctrl._charge_delay_forecast_cache is None
            or abs(forecast_today - ctrl._charge_delay_forecast_cache) > 0.05
            or getattr(ctrl, "_charge_delay_forecast_source_cache", None)
            != forecast_source
            or getattr(ctrl, "_charge_delay_forecast_conversion_cache", None)
            != forecast.conversion
            or getattr(ctrl, "_charge_delay_profile_source_cache", None)
            != profile_source
        ):
            coordinators_with_data = [c for c in automatic_batteries if c.data]
            avg_soc = (
                sum(c.data.get("battery_soc", 0) for c in coordinators_with_data)
                / len(coordinators_with_data)
            ) if coordinators_with_data else 0
            min_soc_values = [c.min_soc for c in automatic_batteries]
            min_soc = max(min_soc_values) if min_soc_values else 20
            usable_energy_kwh = max(0, ((avg_soc - min_soc) / 100) * total_capacity_kwh)
            if profile_balance is not None:
                avg_consumption_kwh = profile_balance.energy_kwh
                status["consumption_forecast_source"] = profile_balance.source
                status["profile_coverage_ratio"] = round(
                    profile_balance.coverage_ratio, 3
                )
                status["profile_days"] = profile_balance.total_days
                status["profile_fallback_reason"] = profile_balance.fallback_reason
            else:
                avg_consumption_kwh = ctrl._consumption_tracker.get_avg_daily_consumption()
            if profile_balance is None:
                remaining_window_hours = ctrl._consumption_tracker.consumption_window_hours_in_range(now_h, 24.0)
                window_hours = ctrl._consumption_tracker.get_consumption_window_hours_per_day()
                avg_consumption_kwh *= remaining_window_hours / window_hours if window_hours > 0 else 0.0
                status["consumption_forecast_source"] = "legacy_daily"
                status["profile_coverage_ratio"] = 0.0
                status["profile_days"] = 0
                status["profile_fallback_reason"] = "profile_not_mature"
            prev_cache = ctrl._charge_delay_forecast_cache
            # Binary "is grid needed today?" gate. A small deadband absorbs sensor
            # noise so a near-balanced day still holds for the cheap window.
            deadband_kwh = ctrl._charge_delay_balance_deadband_kwh
            ctrl._charge_delay_balance_needs_charge = (
                (usable_energy_kwh + raw_forecast)
                < (avg_consumption_kwh - deadband_kwh)
            )
            ctrl._charge_delay_forecast_cache = forecast_today
            ctrl._charge_delay_forecast_source_cache = forecast_source
            ctrl._charge_delay_forecast_conversion_cache = forecast.conversion
            ctrl._charge_delay_profile_source_cache = profile_source
            _LOGGER.info(
                "Charge Delay: Forecast %s (%.2f kWh) → "
                "balance: %.2f usable + %.2f solar = %.2f kWh vs %.2f kWh consumption "
                "(deadband %.2f) → %s",
                "initialised" if prev_cache is None else "changed",
                forecast_today,
                usable_energy_kwh, raw_forecast, usable_energy_kwh + raw_forecast,
                avg_consumption_kwh, deadband_kwh,
                "grid needed (unlock delay)" if ctrl._charge_delay_balance_needs_charge else "solar sufficient (keep delay)",
            )

        if ctrl._charge_delay_balance_needs_charge:
            # Genuine grid-deficit day: rather than unlocking immediately (often a
            # pre-dawn price peak), hold until the cheapest import hour before solar
            # is due, so the unavoidable grid charge lands in the cheap window (#4).
            if self._low_forecast_price_release(now_h):
                return True
            return _unlock("low_forecast")

        # --- Exception 3: No T_start detected ---
        if ctrl._solar_t_start is None:
            if now_h > T_START_FALLBACK_HOUR:
                _LOGGER.info(
                    "Charge Delay: No solar production by %.0f:00 - unlocking (reason: no_t_start)",
                    T_START_FALLBACK_HOUR
                )
                return _unlock("no_t_start")
            # Still waiting for solar production
            status["state"] = "Waiting for solar"
            return True

        # --- Get T_end ---
        t_end = ctrl._consumption_tracker.estimate_t_end()
        status["solar_t_end"] = _h_to_hhmm(t_end)

        # --- Exception 4: Past T_end with no active production ---
        if now_h >= t_end:
            any_charging = any(
                (c.data.get("battery_power", 0) or 0) > 0
                for c in automatic_batteries if c.data
            )
            if not any_charging:
                _LOGGER.info("Charge Delay: Past T_end (%.2fh) with no production - unlocking", t_end)
                return _unlock("past_t_end")

        # --- Calculate energy balance ---
        # Energy needed to reach target_soc
        energy_needed_kwh = sum(
            (target_soc - c.data.get("battery_soc", 100)) / 100.0 * c.data.get("battery_total_energy", 0)
            for c in automatic_batteries if c.data
        )

        if energy_needed_kwh <= 0:
            return _unlock("batteries_full")

        # Charge time estimate
        max_charge_power_kw = ctrl._effective_system_capacity(
            automatic_batteries,
            is_charging=True,
        ) / 1000.0
        if max_charge_power_kw <= 0:
            return _unlock("no_charge_power")
        charge_time_h = energy_needed_kwh / (max_charge_power_kw * CHARGE_EFFICIENCY)

        # Remaining solar and consumption
        if forecast_is_remaining:
            # This is already future solar; transforming it would double-count
            # production that the provider has already observed.
            remaining_solar_kwh = forecast_today
        elif ctrl._daily_solar_energy_kwh > 0:
            # Use actual measured solar production (real solar sensor + Venus MPPT)
            # to estimate the remaining production for today.
            remaining_solar_kwh = max(0.0, forecast_today - ctrl._daily_solar_energy_kwh)
            status["solar_produced_today_kwh"] = round(ctrl._daily_solar_energy_kwh, 2)
        else:
            solar_fraction_done = ctrl._consumption_tracker.get_solar_fraction_done(now_h, ctrl._solar_t_start, t_end)
            remaining_solar_kwh = forecast_today * (1.0 - solar_fraction_done)

        hours_to_t_end = max(0, t_end - now_h)
        # Household demand is measured over all 24 hours. Grid-charging energy
        # is already cancelled by the battery AC term, so predictive charging
        # windows must remain part of the consumption forecast.
        window_hours_per_day = ctrl._consumption_tracker.get_consumption_window_hours_per_day()
        profile_forecast = self._profile_forecast_between(now_h, t_end)
        if profile_forecast is not None:
            remaining_consumption_kwh = profile_forecast.energy_kwh
            status["consumption_forecast_source"] = profile_forecast.source
            status["profile_coverage_ratio"] = round(
                profile_forecast.coverage_ratio, 3
            )
            status["profile_days"] = profile_forecast.total_days
            status["profile_fallback_reason"] = profile_forecast.fallback_reason
        elif window_hours_per_day > 0 and hours_to_t_end > 0:
            avg_consumption = ctrl._consumption_tracker.get_avg_daily_consumption()
            remaining_window_hours = ctrl._consumption_tracker.consumption_window_hours_in_range(
                now_h, t_end
            )
            remaining_consumption_kwh = avg_consumption * (
                remaining_window_hours / window_hours_per_day
            )
            status["consumption_forecast_source"] = "legacy_daily"
            status["profile_coverage_ratio"] = 0.0
            status["profile_days"] = 0
            status["profile_fallback_reason"] = "profile_not_mature"
        else:
            remaining_consumption_kwh = 0
            status["consumption_forecast_source"] = "legacy_daily"
            status["profile_coverage_ratio"] = 0.0
            status["profile_days"] = 0
            status["profile_fallback_reason"] = "empty_horizon"

        net_solar_for_battery = remaining_solar_kwh - remaining_consumption_kwh

        # Time backup check
        safety_margin_h = ctrl._delay_safety_margin_h
        time_limit_reached = (now_h + charge_time_h + safety_margin_h) >= t_end
        energy_insufficient = net_solar_for_battery < (energy_needed_kwh * DELAY_SAFETY_FACTOR)

        # Update status with calculation details
        status["energy_needed_kwh"] = round(energy_needed_kwh, 2)
        status["remaining_solar_kwh"] = round(remaining_solar_kwh, 2)
        status["remaining_consumption_kwh"] = round(remaining_consumption_kwh, 2)
        status["net_solar_kwh"] = round(net_solar_for_battery, 2)
        status["charge_time_h"] = round(charge_time_h, 2)

        # Estimate unlock time: earliest of time-backup and energy-balance triggers
        time_backup_unlock_h = t_end - charge_time_h - safety_margin_h
        energy_balance_unlock_h = self._estimate_energy_balance_unlock_h(
            forecast_today, energy_needed_kwh, ctrl._solar_t_start, t_end, now_h,
            forecast_is_remaining=forecast_is_remaining,
            consumption_profile=profile_forecast,
        )
        if (
            energy_balance_unlock_h is not None
            and energy_balance_unlock_h <= now_h
            and not energy_insufficient
        ):
            energy_balance_unlock_h = None
        if energy_balance_unlock_h is not None:
            est_unlock_h = min(time_backup_unlock_h, energy_balance_unlock_h)
        else:
            est_unlock_h = time_backup_unlock_h
        status["estimated_unlock_time"] = _h_to_hhmm(max(now_h, est_unlock_h))

        # Throttled logging (every 5 minutes)
        current_time = monotonic()
        if current_time - ctrl._delay_last_log_time >= 300:
            ctrl._delay_last_log_time = current_time
            _LOGGER.info(
                "Charge Delay (target=%d%%): Solar remaining=%.1f kWh, Consumption remaining=%.1f kWh, "
                "Net for battery=%.1f kWh, Needed=%.1f kWh (×%.1f=%.1f), "
                "Charge time=%.1fh, Hours to T_end=%.1fh → %s",
                target_soc, remaining_solar_kwh, remaining_consumption_kwh,
                net_solar_for_battery, energy_needed_kwh,
                DELAY_SAFETY_FACTOR, energy_needed_kwh * DELAY_SAFETY_FACTOR,
                charge_time_h, hours_to_t_end,
                "KEEP DELAY" if not energy_insufficient and not time_limit_reached else "UNLOCK"
            )

        if energy_insufficient:
            # Inside the safety cushion (energy_needed <= net_solar < needed × factor)
            # the forecast still covers the target on its own; only the 30% margin is
            # missing. Unlocking instantly there is the expensive branch: it dumps the
            # whole self-charge into the morning export peak while the midday trough,
            # when the same kWh would cost a fraction of the forgone teruglever
            # revenue, is still ahead. Hold for the cheapest feasible hour instead,
            # bounded by the moment the BARE balance (no safety factor) is projected
            # to break, so the SOC target stays reachable and the cushion is the only
            # thing spent. Genuine deficits (net < needed) still unlock immediately.
            if net_solar_for_battery >= energy_needed_kwh:
                cushion_edge_h = self._estimate_energy_balance_unlock_h(
                    forecast_today,
                    energy_needed_kwh,
                    ctrl._solar_t_start,
                    t_end,
                    now_h,
                    safety_factor=1.0,
                    forecast_is_remaining=forecast_is_remaining,
                    consumption_profile=profile_forecast,
                )
                edge_h = (
                    time_backup_unlock_h
                    if cushion_edge_h is None
                    else min(time_backup_unlock_h, cushion_edge_h)
                )
                release_h = self._price_hold_release_h(now_h, edge_h, charge_time_h)
                if release_h is not None and now_h + 1e-6 < release_h:
                    _LOGGER.info(
                        "Charge Delay: Cushion-only shortfall (net=%.1f >= needed=%.1f, "
                        "factored=%.1f) - holding for cheaper hour %.2fh (edge %.2fh)",
                        net_solar_for_battery, energy_needed_kwh,
                        energy_needed_kwh * DELAY_SAFETY_FACTOR, release_h, edge_h,
                    )
                    return True
            _LOGGER.info(
                "Charge Delay: Insufficient solar (net=%.1f < needed=%.1f) - unlocking (reason: energy_balance)",
                net_solar_for_battery, energy_needed_kwh * DELAY_SAFETY_FACTOR
            )
            return _unlock("energy_balance")

        if time_limit_reached:
            _LOGGER.info(
                "Charge Delay: Time limit (%.2f + %.2f + %.2f = %.2f >= T_end %.2f) - unlocking (reason: time_backup)",
                now_h, charge_time_h, safety_margin_h,
                now_h + charge_time_h + safety_margin_h, t_end
            )
            return _unlock("time_backup")

        # --- Price-aware release (within the proven-feasible window only) ---
        # The checks above keep the delay until the feasibility edge
        # (energy_balance / time_backup), which is the LATEST safe release and is
        # often a pricier afternoon hour than the midday export trough. Pull the
        # release FORWARD to the cheapest export hour inside the still-feasible
        # window [now, est_unlock_h], so self-charging sacrifices the least export
        # (teruglever) revenue. SOC target stays fully protected: the window never
        # extends past the edge, and the hard energy/time unlocks above remain the
        # floor. Degrades to legacy edge-release when no price data is available.
        # charge_time_h is passed so the scorer weights the WHOLE charge window, not
        # just the starting slot (a cheap start followed by pricey hours can cost
        # more export than a slightly dearer start sitting in a sustained trough).
        release_h = self._price_hold_release_h(now_h, est_unlock_h, charge_time_h)
        if release_h is not None:
            if now_h + 1e-6 < release_h:
                # A cheaper feasible hour lies ahead, keep holding for it.
                return True
            # Current hour is the cheapest feasible export hour, release now.
            _LOGGER.info(
                "Charge Delay: now (%.2fh) is cheapest feasible export hour up to "
                "%.2fh - unlocking (reason: price_optimal)",
                now_h, est_unlock_h,
            )
            return _unlock("price_optimal")

        # All checks passed - keep delay active
        status["state"] = f"Delayed ({status['estimated_unlock_time']} est.)"
        return True

    def _price_hold_release_h(
        self, now_h: float, edge_h: float, charge_h: float | None = None
    ) -> float | None:
        """Pick the cheapest start in ``[now_h, edge_h]`` and mark the hold.

        Thin wrapper around :meth:`_price_optimal_release_h` shared by the two
        callers that can hold the delay for a cheaper hour (the cushion-only
        shortfall and the feasible-window release), so the held status is written
        in exactly one place.

        Returns the chosen start hour: a value greater than ``now_h`` means the
        delay is being held (the status dict has been updated to say so),
        ``now_h`` means the current hour is already the cheapest, and ``None``
        means there is no price-based decision to make (no usable price data, or
        no room left before the edge).
        """
        if edge_h <= now_h:
            return None
        release_h = self._price_optimal_release_h(now_h, edge_h, charge_h)
        if release_h is None or now_h + 1e-6 >= release_h:
            return release_h
        ctrl = self._controller
        hhmm = ctrl._consumption_tracker.h_to_hhmm(release_h)
        ctrl._charge_delay_status["estimated_unlock_time"] = hhmm
        ctrl._charge_delay_status["state"] = f"Delayed (price, {hhmm} est.)"
        return release_h

    def _price_optimal_release_h(
        self, now_h: float, edge_h: float, charge_h: float | None = None
    ) -> float | None:
        """Cheapest hour to begin charging within [now_h, edge_h].

        Returns the start hour (float, today) of the lowest-priced feasible start.
        Returns ``now_h`` when the current moment is itself the cheapest (within a
        small epsilon, so we never hold for a negligible gain). Returns ``None``
        when no usable price data exists, in which case the caller keeps the legacy
        edge-release behaviour.

        When ``charge_h`` is given (> 0) each candidate start is scored by the
        duration-weighted AVERAGE price over the whole charge window
        ``[start, start+charge_h]``, so the charge lands in the cheapest *sustained*
        block rather than merely starting in the cheapest single slot. A start whose
        window runs past the available price data is skipped (an incomplete tail
        must not score as artificially cheap). Without ``charge_h`` the score is the
        single slot price (legacy behaviour).

        This only ever moves the release EARLIER than the feasibility edge; it never
        defers past it, so the SOC-target safety margin enforced upstream is intact.
        """
        ctrl = self._controller
        # Only honour prices when predictive charging is actually price-driven.
        # In time-slot mode a configured price_sensor drives nothing, so it must
        # not move the solar-delay release (degrade to legacy edge-release).
        if (
            not getattr(ctrl, "predictive_charging_enabled", False)
            or getattr(ctrl, "predictive_charging_mode", None) not in _PRICE_DRIVEN_MODES
        ):
            return None
        pricing = getattr(ctrl, "_pricing_mgr", None)
        if pricing is None or not getattr(ctrl, "price_sensor", None):
            return None
        try:
            # Already today-only and future-only (slot.end > now); quiet so this
            # per-cycle poll does not spam the price-parse log.
            slots = pricing.get_future_price_slots()
        except Exception:  # defensive: a price-parse failure must not break the delay gate
            _LOGGER.debug("Charge Delay: price parse failed, skipping price-aware release", exc_info=True)
            return None
        if not slots:
            return None

        def _score(start_h):
            """Window-average price for a charge starting at ``start_h`` (or the
            single covering-slot price when no charge length is known)."""
            if charge_h and charge_h > 0:
                return self._window_avg_price(start_h, charge_h, slots)
            for s in slots:
                s_h = s.start.hour + s.start.minute / 60.0
                s_end = s_h + (s.end - s.start).total_seconds() / 3600.0
                if s_h - 1e-9 <= start_h < s_end:
                    return s.price
            return None

        candidates = []  # (start_hour, score)
        has_current = False
        for s in slots:
            s_h = s.start.hour + s.start.minute / 60.0
            if s_h > edge_h + 1e-6:  # slot starts after the feasibility edge
                continue
            score = _score(s_h)
            if score is None:  # window runs past available price data — unscorable
                continue
            candidates.append((s_h, score))
            if s_h <= now_h + 1e-9:  # a slot covers the current moment
                has_current = True
        if not candidates:
            return None

        eps = 0.005  # EUR/kWh: ignore sub-cent differences, prefer releasing sooner
        best_h, best_p = min(candidates, key=lambda c: (c[1], c[0]))
        if has_current:
            cur_score = _score(now_h)  # window starting at the current moment
            if cur_score is not None and cur_score <= best_p + eps:
                return now_h
        return best_h

    @staticmethod
    def _window_avg_price(start_h: float, charge_h: float, slots: list) -> float | None:
        """Duration-weighted average slot price over [start_h, start_h+charge_h].

        Returns ``None`` when the window is not fully covered by the available
        slots (an incomplete forecast tail, or a window crossing midnight past the
        today-only slot set), so the caller skips that start rather than scoring a
        partial — and therefore artificially cheap — window.
        """
        if charge_h <= 0:
            return None
        end_h = start_h + charge_h
        cost = 0.0
        covered = 0.0
        for s in slots:
            s_h = s.start.hour + s.start.minute / 60.0
            s_end = s_h + (s.end - s.start).total_seconds() / 3600.0
            overlap = min(end_h, s_end) - max(start_h, s_h)
            if overlap > 0:
                cost += s.price * overlap
                covered += overlap
        if covered < charge_h - 1e-6:
            return None
        return cost / charge_h

    def _low_forecast_price_release(self, now_h: float) -> bool:
        """Hold a genuine grid-deficit day until its cheapest import hour.

        On a balanced/low-forecast day the battery must take some grid charge, but
        unlocking the moment the deficit is detected (often pre-dawn) charges at a
        price peak. Instead, hold until the cheapest price slot in the window before
        solar production is expected, so the unavoidable import lands in the cheap
        window (#4). Reuses :meth:`_price_optimal_release_h` (min-price slot in a
        feasible window — the same selection serves cheapest-import here as it does
        cheapest-export for the PV-surplus hold).

        Returns True to keep holding, False to unlock now: when the current hour is
        already the cheapest, when no price data is available (legacy immediate
        unlock preserved), or when there is no pre-solar slack left.
        """
        ctrl = self._controller
        edge_h = ctrl._solar_t_start if ctrl._solar_t_start is not None else T_START_FALLBACK_HOUR
        if edge_h <= now_h:
            return False  # solar already due/past — no cheap pre-solar window
        release_h = self._price_optimal_release_h(now_h, edge_h)
        if release_h is None or now_h + 1e-6 >= release_h:
            return False  # no price data, or now is the cheapest feasible hour
        hhmm = ctrl._consumption_tracker.h_to_hhmm(release_h)
        ctrl._charge_delay_status["estimated_unlock_time"] = hhmm
        ctrl._charge_delay_status["state"] = f"Delayed (cheap import {hhmm} est.)"
        return True

    def _estimate_energy_balance_unlock_h(
        self,
        forecast_kwh: float,
        energy_needed_kwh: float,
        t_start: float,
        t_end: float,
        now_h: float,
        safety_factor: float = DELAY_SAFETY_FACTOR,
        forecast_is_remaining: bool = False,
        consumption_profile=None,
    ) -> float | None:
        """Estimate when the energy balance condition will trigger the delay unlock.

        Binary-searches for the earliest time t >= now_h where:
          remaining_solar(t) - remaining_consumption(t) < energy_needed × safety_factor

        ``safety_factor`` defaults to :data:`DELAY_SAFETY_FACTOR` (the real unlock
        edge). Callers pass ``1.0`` to project the BARE balance edge instead: the
        last moment the forecast still covers the target with no cushion left,
        which bounds how long the cushion-only price hold may wait.

        Returns the estimated hour as float, or None if it cannot be estimated.
        """
        ctrl = self._controller
        daylight_hours = t_end - t_start
        if daylight_hours <= 0:
            return None

        # Keep this aligned with _should_delay_charge(): avg_consumption is
        # measured over the full local day, not daylight hours.
        avg_consumption = ctrl._consumption_tracker.get_avg_daily_consumption()
        window_hours_per_day = ctrl._consumption_tracker.get_consumption_window_hours_per_day()
        threshold = energy_needed_kwh * safety_factor

        def profile_consumption_at(t: float) -> float | None:
            """Use the same mature profile source as the delay decision."""
            if consumption_profile is None:
                return None
            try:
                current = _decision_now()
                profile = getattr(
                    ctrl._consumption_tracker,
                    "consumption_profile",
                )
                profile_timezone = getattr(profile, "_timezone", lambda: None)()
                if profile_timezone is not None:
                    current = (
                        current.astimezone(profile_timezone)
                        if current.tzinfo is not None
                        else current.replace(tzinfo=profile_timezone)
                    )
                elif current.tzinfo is None:
                    current = current.replace(tzinfo=dt_util.UTC)
                midnight = current.replace(
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
                start = midnight + timedelta(hours=max(0.0, t))
                end = midnight + timedelta(hours=max(0.0, t_end))
                result = ctrl._consumption_tracker.forecast_consumption_between(
                    start, end, fallback="legacy_daily"
                )
                if result.source in {"profile", "vacation_baseline"}:
                    return result.energy_kwh
            except Exception:  # noqa: BLE001
                return None
            return None

        def net_solar_at(t: float) -> float:
            """Net solar available for battery at time t."""
            progress = max(0.0, min(1.0, (t - t_start) / daylight_hours))
            fraction_done = (1.0 - math.cos(math.pi * progress)) / 2.0
            if forecast_is_remaining:
                # Renormalize the provider's post-now energy over the remaining
                # daylight curve, never treating it as a full-day forecast.
                now_progress = max(0.0, min(1.0, (now_h - t_start) / daylight_hours))
                remaining_at_now = max(1e-9, 1.0 - (1.0 - math.cos(math.pi * now_progress)) / 2.0)
                remaining_solar = forecast_kwh * (1.0 - fraction_done) / remaining_at_now
            else:
                remaining_solar = forecast_kwh * (1.0 - fraction_done)
            profile_consumption = profile_consumption_at(t)
            if profile_consumption is not None:
                remaining_consumption = profile_consumption
            else:
                remaining_window_hours = ctrl._consumption_tracker.consumption_window_hours_in_range(
                    t, t_end
                )
                remaining_consumption = (
                    avg_consumption * (remaining_window_hours / window_hours_per_day)
                    if window_hours_per_day > 0 and remaining_window_hours > 0
                    else 0.0
                )
            return remaining_solar - remaining_consumption

        # If already below threshold now, return now_h
        if net_solar_at(now_h) < threshold:
            return now_h

        # If still above threshold at t_end, no energy-balance unlock expected
        if net_solar_at(t_end) >= threshold:
            return None

        # Binary search for crossing point
        lo, hi = now_h, t_end
        for _ in range(40):  # 40 iterations → precision < 1 second
            mid = (lo + hi) / 2.0
            if net_solar_at(mid) >= threshold:
                lo = mid
            else:
                hi = mid

        return (lo + hi) / 2.0
