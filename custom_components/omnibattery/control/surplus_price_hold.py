"""Price-aware solar surplus absorption.

Under a dynamic contract, absorbing PV surplus the moment it appears forfeits
that quarter-hour's feed-in revenue. Feed-in prices swing hard across the day,
so the same daily charge can be taken later for materially less opportunity
cost. This manager holds the battery out of the expensive export hours and
releases it in the cheapest ones.

It owns exactly one runtime effect: the global ``surplus_price_hold`` charge
blocker. With that blocker set, ``_get_available_batteries`` yields nothing for
the charge direction, the PD command clamps to 0 W, and the PV surplus simply
flows to the grid. Nothing here commands power directly.

Split of responsibilities, forced by the control loop's shape:

* :meth:`async_rebuild_plan` is async because the absorption target comes from
  the pricing engine's remaining-horizon evaluation. It runs from the dynamic
  pricing handler, throttled.
* :meth:`is_hold_active` is synchronous and cheap. It runs from
  ``_refresh_operation_blockers`` every control cycle and only reads the cached
  plan plus the live guards.

The blocker also suppresses *wanted* grid charging, so every guard below fails
open: when in doubt, release.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any

from ..const import PREDICTIVE_MODE_DYNAMIC_PRICING
from ..pricing.curtailment import distribute_solar_forecast, estimate_consumption_by_slot
from ..pricing.surplus_absorption import (
    AbsorptionPlan,
    STATUS_DISABLED,
    calculate_absorption_target_kwh,
    calculate_free_space_kwh,
    hold_decision,
    plan_surplus_absorption,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

BLOCKER_SOURCE = "surplus_price_hold"

# The plan is a projection over price slots and a solar forecast; neither moves
# fast enough to justify rebuilding it on the 2.5 s control cycle.
REBUILD_INTERVAL_S = 300.0

# Guards that release the hold without being a fault. They are reported as-is
# on the status so the diagnostic explains why the battery is absorbing.
GUARD_DYNAMIC_PRICING_SLOT = "dynamic_pricing_slot"
GUARD_NEGATIVE_PRICE = "negative_price_charging"
GUARD_WEEKLY_FULL_CHARGE = "weekly_full_charge"
GUARD_CURTAILMENT = "curtailment_active"
GUARD_CHARGE_DELAY = "charge_delay_active"
GUARD_SOC_FLOOR = "at_soc_floor"
GUARD_EV_PAUSE = "ev_pause"
GUARD_CAPACITY_PROTECTION = "capacity_protection"
GUARD_MANUAL = "manual_control"
GUARD_NOT_ENABLED = "not_enabled"
GUARD_NO_PLAN = "no_plan"
GUARD_TARGET_UNAVAILABLE = "target_unavailable"

_CURTAILMENT_ACTIVE_STATES = frozenset({"protected_window", "predischarging"})


class SurplusPriceHoldManager:
    """Decides when PV surplus should export rather than charge the battery."""

    def __init__(self, hass: "HomeAssistant", controller: Any) -> None:
        self._hass = hass
        self._controller = controller
        self._plan: AbsorptionPlan | None = None
        self._last_rebuild_mono: float | None = None
        self._plan_date = None
        self._live_target_kwh: float | None = None
        self._live_target_failed = False
        self._planned_remaining_consumption_kwh: float | None = None
        self._status: dict[str, Any] = {
            "state": STATUS_DISABLED,
            "reason": STATUS_DISABLED,
        }

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        """Return local wall-clock time, isolated for deterministic tests."""
        return datetime.now()

    @property
    def plan(self) -> AbsorptionPlan | None:
        return self._plan

    def get_status(self) -> dict[str, Any]:
        """Return the diagnostic snapshot published by the entities."""
        return dict(self._status)

    def mark_stale(self, reason: str = "reevaluated") -> None:
        """Force the next control cycle to rebuild the plan.

        Called by the pricing engine's own re-evaluation paths (daily, pre-slot,
        evening, SOC drop) so a new schedule and a new absorption plan are always
        derived from the same inputs.
        """
        if self._plan is not None:
            _LOGGER.debug("Surplus price hold: plan marked stale (%s)", reason)
        self._last_rebuild_mono = None

    def _reset_plan_state(self) -> None:
        """Forget the cached plan without touching the blocker registry."""
        self._plan = None
        self._last_rebuild_mono = None
        self._plan_date = None
        self._live_target_kwh = None
        self._live_target_failed = False
        self._planned_remaining_consumption_kwh = None

    def clear(self, reason: str = "cleanup") -> None:
        """Drop the cached plan and release the blocker."""
        self._reset_plan_state()
        self._status = {"state": STATUS_DISABLED, "reason": reason}
        controller = self._controller
        if hasattr(controller, "remove_charge_block"):
            controller.remove_charge_block(BLOCKER_SOURCE)

    def feature_enabled(self) -> bool:
        """Return the complete scope gate for price-aware surplus absorption.

        Only dynamic pricing has a forward price curve to plan against. Real-time
        price mode reacts to the current price alone and cannot know whether a
        cheaper hour is still ahead.
        """
        controller = self._controller
        return bool(
            getattr(controller, "surplus_price_hold_enabled", False)
            and getattr(controller, "predictive_charging_enabled", False)
            # The user's runtime pause of predictive charging makes the whole
            # planner inert; this feature must not keep acting on its own.
            and not getattr(controller, "predictive_charging_overridden", False)
            and getattr(controller, "predictive_charging_mode", None)
            == PREDICTIVE_MODE_DYNAMIC_PRICING
        )

    def is_hold_active(self) -> bool:
        """Return True when surplus must export instead of charging the battery.

        Synchronous and side-effect free apart from the status snapshot, so the
        control cycle's blocker refresh can call it directly.
        """
        if not self.feature_enabled():
            self._set_status(False, STATUS_DISABLED, GUARD_NOT_ENABLED)
            return False

        guard = self._release_guard()
        if guard is not None:
            self._set_status(False, "released", guard)
            return False

        plan = self._plan
        if plan is None:
            self._set_status(False, "released", GUARD_NO_PLAN)
            return False

        now = self._now()
        if self._plan_date is not None and now.date() != self._plan_date:
            # A plan from yesterday says nothing about today's prices. Drop it
            # without touching the blocker registry: the caller owns that, and
            # this method is called from read paths too.
            self._reset_plan_state()
            self._set_status(False, "released", "new_day")
            return False

        if self._live_target_failed:
            # An unusable target is not a reason to keep charging blocked.
            self._set_status(False, "released", GUARD_TARGET_UNAVAILABLE)
            return False

        # Read the minimum saving live so the runtime slider takes effect on the
        # next cycle instead of at the next plan rebuild.
        plan.min_saving = max(
            0.0, float(getattr(self._controller, "surplus_hold_min_saving", 0.0) or 0.0)
        )
        hold, reason = hold_decision(plan, now, self._live_target_kwh)
        self._set_status(hold, "holding" if hold else "released", reason)
        return hold

    async def async_rebuild_plan(self, reason: str = "scheduled", *, force: bool = False) -> None:
        """Recalculate the absorption plan from live prices and forecasts."""
        if not self.feature_enabled():
            if self._plan is not None:
                self.clear("feature_disabled")
            return

        now = self._now()
        if not force and not self._rebuild_due(now):
            # Between rebuilds the target still tracks the live SOC, so an
            # under-delivering window releases the hold without waiting. This
            # path runs on every control cycle, so it reads battery snapshots
            # only — never the full remaining-horizon energy evaluation.
            self._refresh_live_target()
            return

        controller = self._controller
        pricing = getattr(controller, "_pricing_mgr", None)
        if pricing is None:
            self.clear("no_pricing_manager")
            return

        deadline = self._solar_deadline(now)
        slots = pricing.get_future_export_price_slots(
            horizon_end=deadline if deadline is not None else None
        )

        try:
            decision = await pricing._evaluate_remaining_grid_charging(now=now)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Surplus price hold: remaining evaluation failed: %s", err)
            decision = {}

        snapshots = pricing._curtailment_battery_snapshots()
        surplus_by_slot = self._surplus_by_slot(pricing, slots, decision, now, deadline)

        try:
            plan = plan_surplus_absorption(
                slots,
                surplus_by_slot,
                snapshots,
                remaining_consumption_kwh=decision.get("remaining_consumption_kwh"),
                usable_energy_kwh=decision.get("usable_energy_kwh"),
                safety_margin_kwh=float(
                    getattr(controller, "_predictive_safety_margin_kwh", 0.0) or 0.0
                ),
                max_charge_power_w=float(
                    getattr(controller, "max_charge_capacity", 0.0) or 0.0
                ),
                deadline=deadline,
                min_saving=float(getattr(controller, "surplus_hold_min_saving", 0.0) or 0.0),
                now=now,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Surplus absorption planner failed; releasing: %s", err)
            self.clear("planner_error")
            return

        self._plan = plan
        self._plan_date = now.date()
        self._last_rebuild_mono = monotonic()
        self._live_target_kwh = plan.target_kwh
        self._live_target_failed = False
        # Kept so the per-cycle refresh can re-derive the target from live SOC
        # without repeating the expensive remaining-horizon evaluation.
        self._planned_remaining_consumption_kwh = decision.get(
            "remaining_consumption_kwh"
        )
        _LOGGER.debug(
            "Surplus price hold: rebuilt (%s) status=%s target=%.2f kWh slots=%d/%d",
            reason, plan.status, plan.target_kwh,
            len(plan.selected_slots), len(plan.slots),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _rebuild_due(self, now: datetime) -> bool:
        if self._plan is None or self._last_rebuild_mono is None:
            return True
        if self._plan_date is not None and now.date() != self._plan_date:
            return True
        return monotonic() - self._last_rebuild_mono >= REBUILD_INTERVAL_S

    def _refresh_live_target(self) -> None:
        """Recompute the target from live battery state, cheaply.

        This is what makes an under-delivering cheap window self-correcting: the
        target rises as the battery stays emptier than projected, while the plan's
        remaining absorbable energy shrinks with every slot that passes.

        It runs on every control cycle, so it reads only battery snapshots. The
        remaining household consumption is the one taken at the last plan
        rebuild: it is a projection to midnight that moves slowly, whereas SOC
        is the term that actually changes between rebuilds. Re-running the full
        remaining-horizon evaluation here would put recorder history queries in
        the control loop.
        """
        pricing = getattr(self._controller, "_pricing_mgr", None)
        if pricing is None or self._plan is None:
            return
        try:
            snapshots = pricing._curtailment_battery_snapshots()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Surplus price hold: live target refresh failed: %s", err)
            self._live_target_failed = True
            return
        self._live_target_failed = False
        self._live_target_kwh = calculate_absorption_target_kwh(
            self._planned_remaining_consumption_kwh,
            self._usable_energy_kwh(snapshots),
            calculate_free_space_kwh(snapshots),
            float(getattr(self._controller, "_predictive_safety_margin_kwh", 0.0) or 0.0),
        )
        if self._live_target_kwh is None:
            self._live_target_failed = True

    @staticmethod
    def _usable_energy_kwh(snapshots) -> float:
        """Return the energy the eligible batteries hold above their floors."""
        total = 0.0
        for snapshot in snapshots:
            if not getattr(snapshot, "eligible", False):
                continue
            try:
                total += max(
                    0.0,
                    (snapshot.soc_pct - snapshot.floor_soc_pct)
                    / 100.0
                    * snapshot.capacity_kwh,
                )
            except (AttributeError, TypeError, ValueError):
                continue
        return total

    def _solar_deadline(self, now: datetime) -> datetime | None:
        """Return the moment PV production is expected to end today.

        Absorption can only happen while there is surplus, so that is the
        deadline: past it, holding can no longer be undone.
        """
        tracker = getattr(self._controller, "_consumption_tracker", None)
        if tracker is None:
            return None
        try:
            t_start = getattr(self._controller, "_solar_t_start", None)
            if t_start is not None:
                t_end = tracker.estimate_t_end()
            else:
                sunrise = tracker.calculate_sunrise()
                if sunrise is None:
                    return None
                t_end = 2 * tracker.calculate_solar_noon() - sunrise
            t_end = float(t_end)
        except (AttributeError, TypeError, ValueError):
            return None
        if not 0.0 < t_end <= 24.0:
            return None
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight + timedelta(hours=t_end)

    def _surplus_by_slot(
        self, pricing, slots, decision, now: datetime, deadline: datetime | None
    ) -> dict:
        """Estimate the PV surplus each future slot is expected to offer."""
        if not slots:
            return {}
        forecast, fraction_fn, _daily_consumption = pricing._curtailment_forecast_model(now)
        remaining_solar = decision.get("remaining_solar_kwh")
        remaining_load = decision.get("remaining_consumption_kwh")
        if remaining_solar is None:
            remaining_solar = forecast
        if remaining_solar is None or remaining_load is None:
            return {}

        # ``remaining_solar_kwh`` already ends at sunset, but
        # ``remaining_consumption_kwh`` runs to midnight while the slots stop at
        # the solar deadline. Spreading the evening and night load over daylight
        # hours would erase the surplus that is actually there, so scale the
        # load down to the daylight share first.
        daylight_load = self._daylight_share(float(remaining_load), now, deadline)

        solar_by_slot = distribute_solar_forecast(
            slots, float(remaining_solar), fraction_fn, normalize_future=True
        )
        load_by_slot = estimate_consumption_by_slot(
            slots, daylight_load, None, normalize_future=True
        )
        return {
            slot: max(
                0.0,
                float(solar_by_slot.get(slot, 0.0) or 0.0)
                - float(load_by_slot.get(slot, 0.0) or 0.0),
            )
            for slot in slots
        }

    def _daylight_share(
        self, remaining_load_kwh: float, now: datetime, deadline: datetime | None
    ) -> float:
        """Return the part of the remaining load expected before sunset.

        Uses the consumption tracker's own window model where available, so a
        household whose load is concentrated in the evening is not credited with
        daytime consumption it will not have.
        """
        if deadline is None or remaining_load_kwh <= 0:
            return max(0.0, remaining_load_kwh)
        now_h = now.hour + now.minute / 60.0 + now.second / 3600.0
        end_h = min(24.0, deadline.hour + deadline.minute / 60.0)
        if end_h <= now_h:
            return 0.0

        tracker = getattr(self._controller, "_consumption_tracker", None)
        in_range = getattr(tracker, "consumption_window_hours_in_range", None)
        if callable(in_range):
            try:
                until_midnight = float(in_range(now_h, 24.0))
                until_sunset = float(in_range(now_h, end_h))
                if until_midnight > 0:
                    return remaining_load_kwh * max(
                        0.0, min(1.0, until_sunset / until_midnight)
                    )
            except (TypeError, ValueError):
                pass

        # No window model: fall back to the plain share of the remaining hours.
        return remaining_load_kwh * (end_h - now_h) / (24.0 - now_h)

    def _release_guard(self) -> str | None:
        """Return the first live reason the hold must not apply, if any.

        Ordered so the reason a user sees is the one they would name themselves.
        """
        controller = self._controller
        pricing = getattr(controller, "_pricing_mgr", None)

        # Charge delay owns the charge decision while any of its blockers is
        # set, including the per-battery "charge to setpoint" phase, where it
        # deliberately lets the battery charge to its safety SOC. Read the
        # registry that _refresh_operation_blockers has just rebuilt rather than
        # calling is_charge_delayed() again: that method latches the daily unlock
        # and schedules a save, so a second call per cycle has real side effects.
        if getattr(controller, "charge_delay_enabled", False):
            if self._any_charge_delay_blocker():
                return GUARD_CHARGE_DELAY

        # A scheduled cheap grid-charging slot must not be suppressed.
        if pricing is not None and pricing.is_in_dynamic_pricing_slot():
            return GUARD_DYNAMIC_PRICING_SLOT
        if getattr(controller, "_current_price_slot_active", False):
            return GUARD_DYNAMIC_PRICING_SLOT

        # Negative import prices are a time-boxed opportunity worth more than
        # any feed-in timing gain.
        if pricing is not None:
            try:
                if (
                    pricing._negative_price_feature_enabled()
                    and pricing._opportunistic_target_pending()
                    and pricing._current_price_is_opportunistic()
                ):
                    return GUARD_NEGATIVE_PRICE
            except Exception:  # noqa: BLE001
                return GUARD_NEGATIVE_PRICE

        # Anti-curtailment holds the grid target at 0 W precisely so nothing is
        # exported; holding charge there would force the export it prevents.
        if getattr(controller, "_curtailment_runtime_status", None) in _CURTAILMENT_ACTIVE_STATES:
            return GUARD_CURTAILMENT
        if float(getattr(controller, "_curtailment_opportunistic_charge_limit_w", 0.0) or 0.0) > 0:
            return GUARD_CURTAILMENT

        # The weekly full charge needs an uninterrupted climb to 100%.
        weekly_mgr = getattr(controller, "_weekly_charge_mgr", None)
        if weekly_mgr is not None and getattr(weekly_mgr, "is_active", None) is not None:
            try:
                if weekly_mgr.is_active():
                    return GUARD_WEEKLY_FULL_CHARGE
            except Exception:  # noqa: BLE001
                return GUARD_WEEKLY_FULL_CHARGE
        if getattr(controller, "_force_full_charge", False):
            return GUARD_WEEKLY_FULL_CHARGE

        if getattr(controller, "_capacity_protection_active", False):
            return GUARD_CAPACITY_PROTECTION

        # EV pause already blocks charging; a second blocker only muddies the
        # status the user reads.
        try:
            if BLOCKER_SOURCE != "ev_pause" and "ev_pause" in controller.get_charge_blockers():
                return GUARD_EV_PAUSE
        except (AttributeError, TypeError):
            pass

        if self._manual_control_active():
            return GUARD_MANUAL

        if self._at_soc_floor():
            return GUARD_SOC_FLOOR

        return None

    def _any_charge_delay_blocker(self) -> bool:
        """Return True while charge delay holds either global or per-battery."""
        controller = self._controller
        try:
            if any(
                key.startswith("charge_delay")
                for key in controller.get_charge_blockers()
            ):
                return True
            for coordinator in getattr(controller, "coordinators", []) or []:
                if any(
                    key.startswith("charge_delay")
                    for key in controller.get_charge_blockers(coordinator)
                ):
                    return True
        except (AttributeError, TypeError):
            return False
        return False

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

    def _at_soc_floor(self) -> bool:
        """Release when a battery sits on its floor and needs energy now."""
        pricing = getattr(self._controller, "_pricing_mgr", None)
        if pricing is None:
            return False
        try:
            snapshots = pricing._curtailment_battery_snapshots()
        except Exception:  # noqa: BLE001
            return True
        for snapshot in snapshots:
            if not snapshot.eligible:
                continue
            if snapshot.soc_pct <= snapshot.floor_soc_pct + 1e-6:
                return True
        return False

    def _set_status(self, hold: bool, state: str, reason: str) -> None:
        plan = self._plan
        now = self._now()
        status: dict[str, Any] = {
            "state": state,
            "reason": reason,
            "hold": hold,
        }
        if plan is not None:
            status.update(
                {
                    "plan_status": plan.status,
                    "plan_reason": plan.reason,
                    "target_kwh": round(plan.target_kwh, 3),
                    "remaining_target_kwh": (
                        round(float(self._live_target_kwh), 3)
                        if self._live_target_kwh is not None
                        else None
                    ),
                    "absorbable_remaining_kwh": round(
                        plan.remaining_absorbable_kwh(now), 3
                    ),
                    "free_space_kwh": round(plan.free_space_kwh, 3),
                    "deadline": plan.deadline.isoformat() if plan.deadline else None,
                    "min_saving": plan.min_saving,
                    "selected_slots": [
                        {
                            "start": slot.start.isoformat(),
                            "end": slot.end.isoformat(),
                            "export_price": round(slot.export_price, 5),
                            "expected_surplus_kwh": round(slot.expected_surplus_kwh, 3),
                        }
                        for slot in plan.selected_slots
                    ],
                }
            )
            next_slot = next(
                (slot for slot in plan.selected_slots if slot.start > now), None
            )
            status["next_release_at"] = (
                next_slot.start.isoformat() if next_slot is not None else None
            )
        self._status = status
