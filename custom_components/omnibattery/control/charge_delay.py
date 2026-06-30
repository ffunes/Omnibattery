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
from datetime import date, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CHARGE_EFFICIENCY,
    DELAY_SAFETY_FACTOR,
    DELAY_SOC_SETPOINT_HYSTERESIS,
    DOMAIN,
    T_START_FALLBACK_HOUR,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


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
        self._save_task = asyncio.create_task(self._deferred_save())

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

    def handle_daily_reset_and_eval(self) -> None:
        """Reset the delay latch on a new day, then evaluate to keep the sensor live.

        Runs once per control cycle; no-op when the feature is disabled.
        """
        ctrl = self._controller
        if not ctrl.charge_delay_enabled:
            return

        today = date.today()
        if ctrl._charge_delay_last_date != today:
            if ctrl._charge_delay_last_date is not None:
                # Real day change: reset delay state
                ctrl._charge_delay_unlocked = False
                ctrl._delay_setpoint_reached = False
                ctrl._solar_t_start = None
                ctrl._forecast_unavailable_since = None
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

        # Already unlocked today?
        if ctrl._charge_delay_unlocked:
            ctrl._charge_delay_status["state"] = "Charging allowed"
            return False

        # SOC setpoint: delay only kicks in once all batteries reach the setpoint.
        # Hysteresis prevents oscillation: once the setpoint is reached, charging
        # only resumes if SOC drops DELAY_SOC_SETPOINT_HYSTERESIS % below it.
        if ctrl._delay_soc_setpoint_enabled:
            min_soc = min(
                (c.data.get("battery_soc", 100) for c in ctrl.coordinators if c.data),
                default=100,
            )
            if not ctrl._delay_setpoint_reached:
                if min_soc < ctrl._delay_soc_setpoint:
                    ctrl._charge_delay_status["state"] = "Charging to setpoint"
                    return False
                ctrl._delay_setpoint_reached = True
                self.schedule_save()
            else:
                low_threshold = ctrl._delay_soc_setpoint - DELAY_SOC_SETPOINT_HYSTERESIS
                if min_soc < low_threshold:
                    ctrl._delay_setpoint_reached = False
                    self.schedule_save()
                    ctrl._charge_delay_status["state"] = "Charging to setpoint"
                    return False

        # Evaluate delay conditions
        if self._should_delay_charge(target_soc):
            return True  # Keep delay active (block charging)

        # Delay conditions no longer met - unlock permanently for today
        ctrl._charge_delay_unlocked = True
        self.schedule_save()
        _LOGGER.info("Charge Delay: Unlocked (target_soc=%d%%) - charging now allowed", target_soc)
        # Persist unlock state if on weekly charge day
        if ctrl._weekly_charge_mgr.is_active():
            asyncio.create_task(ctrl._weekly_charge_mgr.save_state())
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
            and not ctrl._active_balance_overrides_delay()
        )
        for coordinator in ctrl.coordinators:
            soc = coordinator.data.get("battery_soc") if coordinator.data else None
            if setpoint_active and soc is not None and soc >= ctrl._delay_soc_setpoint:
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
        6. Insufficient remaining solar energy → unlock
        7. Insufficient time before T_end → unlock
        8. Otherwise → keep delay active
        """
        ctrl = self._controller

        now = datetime.now()
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
        if not ctrl.solar_forecast_sensor:
            _LOGGER.info("Charge Delay: No solar forecast sensor configured - unlocking (reason: no_forecast)")
            return _unlock("no_forecast")

        # A configured forecast sensor can briefly read unavailable/unknown/invalid
        # while it updates. Treating that transient blip as "no forecast" would
        # commit a PERMANENT daily unlock (see is_charge_delayed), so a momentary
        # gap silently disables the delay for the rest of the day. Instead, hold the
        # current delay through a short grace window and only unlock if the sensor
        # stays unavailable. (A sensor that is not configured at all still unlocks
        # immediately above — that is a deliberate fail-safe, not a transient.)
        forecast_state = ctrl.hass.states.get(ctrl.solar_forecast_sensor)
        raw_forecast = None
        if forecast_state is not None and forecast_state.state not in ("unknown", "unavailable"):
            try:
                raw_forecast = float(forecast_state.state)
            except (ValueError, TypeError):
                raw_forecast = None

        if raw_forecast is None:
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
        forecast_today = raw_forecast * 0.85  # 15% conservative correction
        status["forecast_kwh"] = raw_forecast

        # --- Exception 2: Energy balance check (dynamic, recalculated only when forecast changes) ---
        total_capacity_kwh = sum(
            c.data.get("battery_total_energy", 0) for c in ctrl.coordinators if c.data
        )
        if total_capacity_kwh <= 0:
            _LOGGER.info("Charge Delay: Invalid battery capacity - unlocking")
            return _unlock("no_forecast")

        if (
            ctrl._charge_delay_forecast_cache is None
            or abs(forecast_today - ctrl._charge_delay_forecast_cache) > 0.05
        ):
            coordinators_with_data = [c for c in ctrl.coordinators if c.data]
            avg_soc = (
                sum(c.data.get("battery_soc", 0) for c in coordinators_with_data)
                / len(coordinators_with_data)
            ) if coordinators_with_data else 0
            min_soc_values = [c.min_soc for c in ctrl.coordinators]
            min_soc = max(min_soc_values) if min_soc_values else 20
            usable_energy_kwh = max(0, ((avg_soc - min_soc) / 100) * total_capacity_kwh)
            avg_consumption_kwh = ctrl._consumption_tracker.get_avg_daily_consumption()
            prev_cache = ctrl._charge_delay_forecast_cache
            ctrl._charge_delay_balance_needs_charge = (
                (usable_energy_kwh + forecast_today) < avg_consumption_kwh
            )
            ctrl._charge_delay_forecast_cache = forecast_today
            _LOGGER.info(
                "Charge Delay: Forecast %s (%.2f → %.2f kWh) → "
                "balance: %.2f usable + %.2f solar = %.2f kWh vs %.2f kWh consumption → %s",
                "initialised" if prev_cache is None else "changed",
                prev_cache if prev_cache is not None else 0.0, forecast_today,
                usable_energy_kwh, forecast_today, usable_energy_kwh + forecast_today,
                avg_consumption_kwh,
                "grid needed (unlock delay)" if ctrl._charge_delay_balance_needs_charge else "solar sufficient (keep delay)",
            )

        if ctrl._charge_delay_balance_needs_charge:
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
                for c in ctrl.coordinators if c.data
            )
            if not any_charging:
                _LOGGER.info("Charge Delay: Past T_end (%.2fh) with no production - unlocking", t_end)
                return _unlock("past_t_end")

        # --- Calculate energy balance ---
        # Energy needed to reach target_soc
        energy_needed_kwh = sum(
            (target_soc - c.data.get("battery_soc", 100)) / 100.0 * c.data.get("battery_total_energy", 0)
            for c in ctrl.coordinators if c.data
        )

        if energy_needed_kwh <= 0:
            return _unlock("batteries_full")

        # Charge time estimate
        max_charge_power_kw = ctrl._effective_system_capacity(
            ctrl.coordinators,
            is_charging=True,
        ) / 1000.0
        if max_charge_power_kw <= 0:
            return _unlock("no_charge_power")
        charge_time_h = energy_needed_kwh / (max_charge_power_kw * CHARGE_EFFICIENCY)

        # Remaining solar and consumption
        if ctrl._daily_solar_energy_kwh > 0:
            # Use actual measured solar production (real solar sensor + Venus MPPT)
            # to estimate the remaining production for today.
            remaining_solar_kwh = max(0.0, forecast_today - ctrl._daily_solar_energy_kwh)
            status["solar_produced_today_kwh"] = round(ctrl._daily_solar_energy_kwh, 2)
        else:
            solar_fraction_done = ctrl._consumption_tracker.get_solar_fraction_done(now_h, ctrl._solar_t_start, t_end)
            remaining_solar_kwh = forecast_today * (1.0 - solar_fraction_done)

        hours_to_t_end = max(0, t_end - now_h)
        # avg_consumption is measured over the consumption window (outside any
        # charging_time_slot, or 24h if none is configured) — see
        # ConsumptionTracker.is_in_consumption_window. Prorate against the
        # portion of [now, t_end] that overlaps that same window.
        window_hours_per_day = ctrl._consumption_tracker.get_consumption_window_hours_per_day()
        if window_hours_per_day > 0 and hours_to_t_end > 0:
            avg_consumption = ctrl._consumption_tracker.get_avg_daily_consumption()
            remaining_window_hours = ctrl._consumption_tracker.consumption_window_hours_in_range(
                now_h, t_end
            )
            remaining_consumption_kwh = avg_consumption * (
                remaining_window_hours / window_hours_per_day
            )
        else:
            remaining_consumption_kwh = 0

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
            forecast_today, energy_needed_kwh, ctrl._solar_t_start, t_end, now_h
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
        release_h = self._price_optimal_release_h(now_h, est_unlock_h)
        if release_h is not None:
            if now_h + 1e-6 < release_h:
                # A cheaper feasible hour lies ahead, keep holding for it.
                status["estimated_unlock_time"] = _h_to_hhmm(release_h)
                status["state"] = f"Delayed (price, {_h_to_hhmm(release_h)} est.)"
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

    def _price_optimal_release_h(self, now_h: float, edge_h: float) -> float | None:
        """Cheapest export hour to begin charging within [now_h, edge_h].

        Returns the start hour (float, today) of the lowest-priced price slot whose
        start lies in the still-feasible window. Returns ``now_h`` when the current
        slot is itself the cheapest (within a small epsilon, so we never hold for a
        negligible gain). Returns ``None`` when no usable price data exists, in
        which case the caller keeps the legacy edge-release behaviour.

        This only ever moves the release EARLIER than the feasibility edge; it never
        defers past it, so the SOC-target safety margin enforced upstream is intact.
        """
        ctrl = self._controller
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

        candidates = []  # (start_hour, price)
        cur_price = None
        for s in slots:
            s_h = s.start.hour + s.start.minute / 60.0
            if s_h > edge_h + 1e-6:  # slot starts after the feasibility edge
                continue
            candidates.append((s_h, s.price))
            if s_h <= now_h + 1e-9:  # slot covering the current moment
                cur_price = s.price
        if not candidates:
            return None

        eps = 0.005  # EUR/kWh: ignore sub-cent differences, prefer releasing sooner
        best_h, best_p = min(candidates, key=lambda c: (c[1], c[0]))
        if cur_price is not None and cur_price <= best_p + eps:
            return now_h
        return best_h

    def _estimate_energy_balance_unlock_h(
        self,
        forecast_kwh: float,
        energy_needed_kwh: float,
        t_start: float,
        t_end: float,
        now_h: float,
    ) -> float | None:
        """Estimate when the energy balance condition will trigger the delay unlock.

        Binary-searches for the earliest time t >= now_h where:
          remaining_solar(t) - remaining_consumption(t) < energy_needed × DELAY_SAFETY_FACTOR

        Returns the estimated hour as float, or None if it cannot be estimated.
        """
        ctrl = self._controller
        daylight_hours = t_end - t_start
        if daylight_hours <= 0:
            return None

        # Keep this aligned with _should_delay_charge(): avg_consumption is
        # measured over the configured consumption window, not daylight hours.
        avg_consumption = ctrl._consumption_tracker.get_avg_daily_consumption()
        window_hours_per_day = ctrl._consumption_tracker.get_consumption_window_hours_per_day()
        threshold = energy_needed_kwh * DELAY_SAFETY_FACTOR

        def net_solar_at(t: float) -> float:
            """Net solar available for battery at time t."""
            progress = max(0.0, min(1.0, (t - t_start) / daylight_hours))
            fraction_done = (1.0 - math.cos(math.pi * progress)) / 2.0
            remaining_solar = forecast_kwh * (1.0 - fraction_done)
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
