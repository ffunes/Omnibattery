"""Runtime owner of the price-aware discharge reserve.

The reserve is expressed as one number: the extra SOC every battery must keep
back because a dearer hour is still ahead and the household will need energy
there. ``_refresh_price_reserve_blocks`` turns it into a per-battery
``price_reserve`` discharge blocker.

Deliberately a blocker rather than a raised ``min_soc``: the configured floor is
read back by the curtailment snapshot builder, so raising it would feed this
calculation into its own input and shrink the pre-discharge budget by the
reserve. ``price_reserve`` is economic, so safety paths and the pre-discharge
planner look straight past it.

Split of responsibilities, as in the surplus-hold manager next door:

* :meth:`async_rebuild_plan` is async and throttled. It needs the price curve,
  the learned consumption profile and the solar forecast, so it runs from the
  dynamic pricing handler.
* :meth:`reserve_soc_pct` is synchronous and cheap. It runs from
  ``_refresh_price_reserve_blocks`` every control cycle, recomputing the reserve
  against the live price and the live SOC from the cached plan.

Every guard fails open. A reserve that cannot be justified is no reserve: the
battery keeps behaving exactly as it does today.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any

from ..const import PREDICTIVE_MODE_DYNAMIC_PRICING
from ..pricing.curtailment import BatterySnapshot, distribute_solar_forecast
from ..pricing.discharge_reserve import (
    ReservePlan,
    STATUS_DISABLED,
    consumption_by_slot,
    eligible_capacity_kwh,
    free_space_kwh,
    net_demand_by_slot,
    plan_discharge_reserve,
    reserve_soc_pct as _reserve_soc_pct,
    usable_energy_kwh,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# The plan is a projection over price slots, a learned load shape and a solar
# forecast. None of those moves fast enough to justify the 2.5 s control cycle.
REBUILD_INTERVAL_S = 300.0

GUARD_NOT_ENABLED = "not_enabled"
GUARD_NO_PLAN = "no_plan"
GUARD_MANUAL = "manual_control"
GUARD_CURTAILMENT = "curtailment_active"
GUARD_CAPACITY_PROTECTION = "capacity_protection"
GUARD_NEW_DAY = "new_day"
GUARD_FLEET_UNKNOWN = "battery_state_unknown"

# Pre-discharge deliberately empties the battery before a curtailment window,
# and peak shaving deliberately spends it on a spike. Holding energy back
# against either would import at exactly the moment they exist to avoid.
_CURTAILMENT_ACTIVE_STATES = frozenset({"protected_window", "predischarging"})


class DischargeReserveManager:
    """Decides how much stored energy belongs to a later, dearer hour."""

    def __init__(self, hass: "HomeAssistant", controller: Any) -> None:
        self._hass = hass
        self._controller = controller
        self._plan: ReservePlan | None = None
        self._plan_date = None
        self._last_rebuild_mono: float | None = None
        self._status: dict[str, Any] = {
            "state": STATUS_DISABLED,
            "reason": GUARD_NOT_ENABLED,
            "reserve_soc_pct": 0.0,
        }

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        """Return local wall-clock time, isolated for deterministic tests."""
        return datetime.now()

    @property
    def plan(self) -> ReservePlan | None:
        return self._plan

    def get_status(self) -> dict[str, Any]:
        """Return the diagnostic snapshot published by the entities."""
        return dict(self._status)

    def mark_stale(self, reason: str = "reevaluated") -> None:
        """Force the next control cycle to rebuild the plan."""
        if self._plan is not None:
            _LOGGER.debug("Discharge reserve: plan marked stale (%s)", reason)
        self._last_rebuild_mono = None

    def clear(self, reason: str = "cleanup") -> None:
        """Drop the cached plan so no floor is raised."""
        self._plan = None
        self._plan_date = None
        self._last_rebuild_mono = None
        self._status = {
            "state": STATUS_DISABLED,
            "reason": reason,
            "reserve_soc_pct": 0.0,
        }

    def feature_enabled(self) -> bool:
        """Return the complete scope gate for the reserve.

        Only dynamic pricing has a forward price curve. Real-time price mode
        knows the current price and nothing else, so it cannot tell whether a
        dearer hour is still ahead — which is the entire question here.
        """
        controller = self._controller
        return bool(
            getattr(controller, "discharge_reserve_enabled", False)
            and getattr(controller, "predictive_charging_enabled", False)
            and not getattr(controller, "predictive_charging_overridden", False)
            and getattr(controller, "predictive_charging_mode", None)
            == PREDICTIVE_MODE_DYNAMIC_PRICING
        )

    def reserve_soc_pct(self) -> float:
        """Return the extra SOC every battery must keep, in percentage points.

        Synchronous and side-effect free apart from the status snapshot, so the
        per-battery floor refresh can call it directly.
        """
        if not self.feature_enabled():
            self._set_status(0.0, STATUS_DISABLED, GUARD_NOT_ENABLED)
            return 0.0

        guard = self._release_guard()
        if guard is not None:
            self._set_status(0.0, "released", guard)
            return 0.0

        plan = self._plan
        if plan is None:
            self._set_status(0.0, "released", GUARD_NO_PLAN)
            return 0.0

        now = self._now()
        if self._plan_date is not None and now.date() != self._plan_date:
            # Yesterday's price curve says nothing about today. Drop it without
            # touching any registry: this method is called from read paths too.
            self._plan = None
            self._plan_date = None
            self._last_rebuild_mono = None
            self._set_status(0.0, "released", GUARD_NEW_DAY)
            return 0.0

        # Read the minimum saving live so the runtime slider takes effect on the
        # next cycle instead of at the next rebuild.
        plan.min_saving = max(
            0.0,
            float(getattr(self._controller, "discharge_reserve_min_saving", 0.0) or 0.0),
        )
        fleet = self._fleet_snapshots()
        if fleet is None:
            # Not knowing what the battery holds is not a reason to hold it.
            self._set_status(0.0, "released", GUARD_FLEET_UNKNOWN)
            return 0.0
        live_usable = usable_energy_kwh(fleet)
        live_capacity = eligible_capacity_kwh(fleet)
        live_space = free_space_kwh(fleet)
        reserve_kwh, claiming, reason = plan.reserve_kwh_at(
            now, self._current_price(), live_usable, live_space
        )
        plan.reserve_kwh = reserve_kwh
        plan.selected_slots = claiming
        # Live capacity, not the value frozen at rebuild time: a battery that
        # dropped out since then can no longer carry its share of the reserve.
        plan.total_capacity_kwh = live_capacity
        plan.usable_energy_kwh = live_usable
        plan.free_space_kwh = live_space
        pct = _reserve_soc_pct(reserve_kwh, live_capacity)
        self._set_status(pct, "reserving" if pct > 0 else "released", reason)
        return pct

    async def async_rebuild_plan(
        self, reason: str = "scheduled", *, force: bool = False
    ) -> None:
        """Recalculate the reserve plan from live prices and forecasts."""
        if not self.feature_enabled():
            if self._plan is not None:
                self.clear("feature_disabled")
            return

        now = self._now()
        if not force and not self._rebuild_due(now):
            return

        pricing = getattr(self._controller, "_pricing_mgr", None)
        if pricing is None:
            self.clear("no_pricing_manager")
            return

        horizon_end = self._horizon_end(now)
        fleet = self._fleet_snapshots()
        if fleet is None:
            # No battery readable right now. Leaving the throttle unarmed keeps
            # a transient coordinator outage from disabling the reserve for the
            # whole rebuild interval.
            _LOGGER.debug("Discharge reserve: no readable battery, rebuild deferred")
            return
        slots = pricing.get_future_price_slots(horizon_end=horizon_end)

        demand, surplus = self._demand_and_surplus_by_slot(
            pricing, slots, now, horizon_end
        )
        try:
            plan = plan_discharge_reserve(
                slots,
                demand,
                surplus_by_slot=surplus,
                free_space_kwh=free_space_kwh(fleet),
                usable_energy_kwh=usable_energy_kwh(fleet),
                total_capacity_kwh=eligible_capacity_kwh(fleet),
                current_price=self._current_price(),
                min_saving=float(
                    getattr(self._controller, "discharge_reserve_min_saving", 0.0) or 0.0
                ),
                now=now,
                horizon_end=horizon_end,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Discharge reserve planner failed; releasing: %s", err)
            self.clear("planner_error")
            return

        self._plan = plan
        self._plan_date = now.date()
        self._last_rebuild_mono = monotonic()
        _LOGGER.debug(
            "Discharge reserve: rebuilt (%s) status=%s reserve=%.2f kWh slots=%d/%d",
            reason,
            plan.status,
            plan.reserve_kwh,
            len(plan.selected_slots),
            len(plan.slots),
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

    def _horizon_end(self, now: datetime) -> datetime:
        """Reserve for tonight, never for tomorrow.

        Holding energy overnight for tomorrow's evening peak would be wrong on
        every day the sun refills the battery in between, and the planner has no
        model of tomorrow's PV. Local midnight is the honest boundary.
        """
        return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    def _current_price(self) -> float | None:
        pricing = getattr(self._controller, "_pricing_mgr", None)
        if pricing is None:
            return None
        try:
            return pricing._get_current_price()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Discharge reserve: current price unavailable: %s", err)
            return None

    def _fleet_snapshots(self) -> list[BatterySnapshot] | None:
        """Read live SOC and capacity straight from the coordinators.

        Deliberately not ``PricingManager._curtailment_battery_snapshots``: that
        builder resolves each battery's floor through the controller, which is
        where this reserve is applied, so using it would make this calculation
        an input to itself. It also derives ``can_discharge`` from the blocker
        registry this feature writes to, which would make the reserve flap on
        its own blocker. Only the configured ``min_soc`` is read here.

        Returns None when no battery could be read, so the caller releases
        instead of reserving against a guess.
        """
        snapshots: list[BatterySnapshot] = []
        for coordinator in getattr(self._controller, "coordinators", []) or []:
            data = getattr(coordinator, "data", None) or {}
            if not data or not getattr(coordinator, "is_available", False):
                continue
            if getattr(coordinator, "battery_manual_mode_enabled", False):
                continue
            if self._slot_owned(coordinator):
                continue
            if getattr(coordinator, "rs485_user_disabled", False):
                continue
            if self._controller_says_unusable(coordinator):
                continue
            try:
                soc = float(data.get("battery_soc"))
                capacity = float(data.get("battery_total_energy"))
                floor = float(coordinator.min_soc)
                max_soc = float(coordinator.max_soc)
            except (AttributeError, TypeError, ValueError):
                continue
            snapshots.append(
                BatterySnapshot(
                    name=getattr(coordinator, "name", "battery"),
                    soc_pct=soc,
                    capacity_kwh=capacity,
                    max_soc_pct=max_soc,
                    floor_soc_pct=floor,
                    max_discharge_power_w=0.0,
                    eligible=True,
                    can_discharge=True,
                )
            )
        return snapshots or None

    def _controller_says_unusable(self, coordinator) -> bool:
        """Exclusions the snapshot builder applies for reasons of its own.

        A non-responsive battery and one serving the backup port both keep their
        SOC while delivering nothing to the house, so counting their energy
        would size the reserve against kWh no dearer hour will ever see.
        """
        controller = self._controller
        non_responsive = getattr(controller, "_non_responsive", None)
        is_excluded = getattr(non_responsive, "is_excluded", None)
        if callable(is_excluded):
            try:
                if is_excluded(coordinator):
                    return True
            except Exception:  # noqa: BLE001
                return True
        backup_active = getattr(controller, "_is_backup_function_active", None)
        if callable(backup_active):
            try:
                if backup_active(coordinator):
                    return True
            except Exception:  # noqa: BLE001
                return True
        return False

    def _slot_owned(self, coordinator) -> bool:
        """True while a manual time slot owns this battery."""
        owner = getattr(self._controller, "_is_manual_slot_owned", None)
        if not callable(owner):
            return False
        try:
            return bool(owner(coordinator))
        except Exception:  # noqa: BLE001
            return True

    def _demand_and_surplus_by_slot(
        self, pricing, slots, now: datetime, horizon_end
    ) -> tuple[dict, dict]:
        """Per slot: the grid demand left after PV, and the PV surplus over it."""
        if not slots:
            return {}, {}
        forecast = pricing._profile_remaining_consumption(now, horizon_end)
        if forecast is None:
            return {}, {}
        consumption = consumption_by_slot(
            slots,
            getattr(forecast, "intervals_by_date", None) or {},
            getattr(forecast, "intervals_kwh", None),
        )
        if not consumption:
            return {}, {}

        solar: dict = {}
        try:
            forecast_kwh, fraction_fn, _daily = pricing._curtailment_forecast_model(now)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Discharge reserve: solar model unavailable: %s", err)
            forecast_kwh = None
        if forecast_kwh is not None and float(forecast_kwh) > 0:
            # ``_curtailment_forecast_model`` returns whichever sensor is
            # configured. Only a remaining-today figure may be spread over the
            # future slots alone; a whole-day figure keeps its cumulative share,
            # or the evening would be credited with the morning's production and
            # the reserve would silently never engage.
            solar = distribute_solar_forecast(
                slots,
                float(forecast_kwh),
                fraction_fn,
                normalize_future=self._forecast_is_remaining(pricing),
            )
        surplus = {
            slot: max(
                0.0,
                float(solar.get(slot, 0.0) or 0.0)
                - float(consumption.get(slot, 0.0) or 0.0),
            )
            for slot in consumption
        }
        return net_demand_by_slot(consumption, solar), surplus

    def _forecast_is_remaining(self, pricing) -> bool:
        """True when the configured solar sensor reports production still to come."""
        reader = getattr(pricing, "_solar_forecast_is_remaining", None)
        if not callable(reader):
            return False
        try:
            return bool(reader())
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Discharge reserve: solar forecast source unknown: %s", err)
            return False

    def _release_guard(self) -> str | None:
        """Return the first live reason the reserve must not apply, if any."""
        controller = self._controller

        if self._manual_control_active():
            return GUARD_MANUAL

        # Anti-curtailment is emptying the battery on purpose, and peak shaving
        # is spending it on a spike on purpose. A floor would fight both.
        if getattr(controller, "_curtailment_runtime_status", None) in _CURTAILMENT_ACTIVE_STATES:
            return GUARD_CURTAILMENT
        if getattr(controller, "_capacity_protection_active", False):
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

    def _set_status(self, pct: float, state: str, reason: str) -> None:
        plan = self._plan
        status: dict[str, Any] = {
            "state": state,
            "reason": reason,
            "reserve_soc_pct": round(float(pct), 2),
        }
        if plan is not None:
            status.update(
                {
                    "plan_status": plan.status,
                    "reserve_kwh": round(plan.reserve_kwh, 3),
                    "usable_energy_kwh": round(plan.usable_energy_kwh, 3),
                    "free_space_kwh": round(plan.free_space_kwh, 3),
                    "total_capacity_kwh": round(plan.total_capacity_kwh, 3),
                    "min_saving": plan.min_saving,
                    "reference_price": plan.reference_price,
                    "horizon_end": (
                        plan.horizon_end.isoformat() if plan.horizon_end else None
                    ),
                    "reserved_slots": [
                        {
                            "start": slot.start.isoformat(),
                            "end": slot.end.isoformat(),
                            "price": round(slot.price, 5),
                            "net_demand_kwh": round(slot.net_demand_kwh, 3),
                        }
                        for slot in plan.selected_slots
                    ],
                }
            )
        self._status = status
