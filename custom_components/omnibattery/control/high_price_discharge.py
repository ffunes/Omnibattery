"""Deliberate export in high-price slots (#270, trigger 2).

The pure planner lives in ``pricing/high_price_discharge.py``; this module is
its runtime adapter. It builds the horizon it needs, caches the plan, and
turns the allocation covering *now* into a single setpoint override.

Three things are worth knowing before changing anything here.

**The timezone boundary is this module.** The planner rejects naive datetimes
and fails safe, because subtracting two aware datetimes that share a ``tzinfo``
object compares wall clocks: an hour across a DST change reads as 2 h or 0 h,
which over-sells or drops a slot. The price layer, on the other hand, is naive
local end to end (``calculations.py`` strips tzinfo in a dozen parsers). Fixing
that layer is a separate project, so the two meet here: everything below runs
on naive local wall clocks like its siblings, and :meth:`_aware` re-attaches
the local zone at the exact moment a value crosses into the planner.

**One entry point.** :meth:`refresh_override` runs synchronously from
``_refresh_operation_blockers`` every control cycle and rebuilds the plan
itself when the throttle is due. There is deliberately no async rebuild hook
and no ``mark_stale`` plumbing: the plan is re-derived when the configuration
it was built from changes (see ``_plan_config``), and withdrawal — the only
direction that matters for safety — happens on the very next cycle through the
guards, not through a rebuild.

**Every uncertainty removes the override.** A missing plan, a coverage gap, a
guard, an expired slot: all of them fall through to
``remove_setpoint_override`` before returning.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from ..const import PREDICTIVE_MODE_DYNAMIC_PRICING
from ..pricing.curtailment import distribute_solar_forecast
from ..pricing.discharge_reserve import consumption_by_slot
from ..pricing.high_price_discharge import (
    STATUS_FAIL_SAFE,
    STATUS_PLANNED,
    HighPriceDischargePlan,
    HorizonSlot,
    plan_high_price_discharge,
)
from ..solar_forecast import get_configured_solar_forecast_sensor

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

OVERRIDE_SOURCE = "high_price_discharge"

# Below both curtailment overrides on purpose. ``curtailment_negative_window``
# (6) forces 0 W precisely so nothing is injected while injection is penalised,
# and ``curtailment_predischarge`` (5) exports to avoid clipping losses that are
# already happening. Both are risk decisions; this one is an opportunity, and
# RF-034 puts the opportunity last. ``capacity_protection`` (10) still wins.
OVERRIDE_PRIORITY = 4

# A day-ahead curve and a consumption profile do not move on the 2.5 s control
# cycle. The live price is therefore revalidated at this cadence, not per cycle.
REBUILD_INTERVAL_S = 300.0

STATE_DISABLED = "disabled"
STATE_INVALID_CONFIGURATION = "invalid_configuration"
STATE_NO_DATA = "no_data"
STATE_BLOCKED = "blocked"
STATE_WAITING = "waiting"
STATE_ACTIVE = "active"

GUARD_NOT_ENABLED = "not_enabled"
GUARD_NO_MAX_POWER = "no_max_export_power"
GUARD_NO_PRICING = "no_pricing_manager"
GUARD_NO_PLAN = "no_plan"
GUARD_CHARGE_ORDER = "charge_order_active"
GUARD_WEEKLY_FULL_CHARGE = "weekly_full_charge"
GUARD_CURTAILMENT = "curtailment_active"
GUARD_CAPACITY_PROTECTION = "capacity_protection"
GUARD_MANUAL = "manual_control"
GUARD_DISCHARGE_BLOCKED = "discharge_blocked"
GUARD_GRID_METER = "invalid_grid_meter"
REASON_NO_ALLOCATION = "no_allocation_now"
REASON_PLANNED = "high_price_slot"

_CURTAILMENT_ACTIVE_STATES = frozenset({"protected_window", "predischarging"})


class HighPriceDischargeManager:
    """Sells stored energy into a price peak, one slot at a time."""

    def __init__(self, hass: "HomeAssistant", controller: Any) -> None:
        self._hass = hass
        self._controller = controller
        self._plan: HighPriceDischargePlan | None = None
        self._last_rebuild_mono: float | None = None
        self._plan_config: tuple[float, float] | None = None
        self._status: dict[str, Any] = {
            "state": STATE_DISABLED,
            "reason": GUARD_NOT_ENABLED,
        }

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def plan(self) -> HighPriceDischargePlan | None:
        return self._plan

    def get_status(self) -> dict[str, Any]:
        """Return the diagnostic snapshot (RF-037, RF-038)."""
        return dict(self._status)

    def feature_enabled(self) -> bool:
        """Return the complete scope gate (RF-001, RF-003, RF-004).

        Only dynamic pricing carries the forward import curve the buy-back
        threshold is computed from.
        """
        controller = self._controller
        return bool(
            getattr(controller, "high_price_discharge_enabled", False)
            and getattr(controller, "predictive_charging_enabled", False)
            and not getattr(controller, "predictive_charging_overridden", False)
            and getattr(controller, "predictive_charging_mode", None)
            == PREDICTIVE_MODE_DYNAMIC_PRICING
        )

    def refresh_override(self) -> None:
        """Apply or withdraw the deliberate-export setpoint for this cycle."""
        if not self.feature_enabled():
            self._release(STATE_DISABLED, GUARD_NOT_ENABLED)
            return

        config = self._config()
        if config is None:
            self._release(STATE_INVALID_CONFIGURATION, GUARD_NO_MAX_POWER)
            return

        self._maybe_rebuild(config)

        plan = self._plan
        if plan is None or plan.status == STATUS_FAIL_SAFE:
            self._release(
                STATE_NO_DATA, plan.reason if plan is not None else GUARD_NO_PLAN
            )
            return

        guard = self._block_guard()
        if guard is not None:
            self._release(STATE_BLOCKED, guard)
            return

        allocation = plan.allocation_at(self._aware(self._now()))
        if allocation is None or allocation.power_w <= 0:
            self._release(
                STATE_WAITING,
                plan.reason if plan.status != STATUS_PLANNED else REASON_NO_ALLOCATION,
            )
            return

        # A *net* grid target, not a battery target: while solar is already
        # exporting, PD credits it against this figure and the battery delivers
        # the remainder. The sale still happens at the planned power, the
        # contractual ceiling is never exceeded, and the battery keeps the
        # energy solar covered for it.
        self._controller.set_setpoint_override(
            OVERRIDE_SOURCE, -allocation.power_w, priority=OVERRIDE_PRIORITY
        )
        self._set_status(STATE_ACTIVE, REASON_PLANNED, power_w=-allocation.power_w)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        """Local wall clock, naive like the rest of the pricing layer."""
        return datetime.now()

    @staticmethod
    def _aware(moment: datetime) -> datetime:
        """Re-attach the local zone before a value crosses into the planner."""
        if moment.tzinfo is not None:
            return moment
        return moment.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)

    def _release(self, state: str, reason: str) -> None:
        self._controller.remove_setpoint_override(OVERRIDE_SOURCE)
        self._set_status(state, reason)

    def _config(self) -> tuple[float, float] | None:
        """Return ``(max_export_power_w, additional_cost)``, or None if invalid.

        RF-040: a positive export power is part of a valid activation, so zero
        is an invalid configuration rather than a silent no-op.
        """
        controller = self._controller
        try:
            power = float(
                getattr(controller, "high_price_discharge_max_power_w", 0.0) or 0.0
            )
            cost = float(
                getattr(controller, "high_price_discharge_additional_cost", 0.0) or 0.0
            )
        except (TypeError, ValueError):
            return None
        if not math.isfinite(power) or not math.isfinite(cost):
            return None
        if power <= 0 or cost < 0:
            return None
        return power, cost

    def _maybe_rebuild(self, config: tuple[float, float]) -> None:
        """Rebuild the plan when the throttle is due or the config changed."""
        due = (
            self._plan is None
            or self._last_rebuild_mono is None
            or self._plan_config != config
            or monotonic() - self._last_rebuild_mono >= REBUILD_INTERVAL_S
        )
        if not due:
            return

        self._last_rebuild_mono = monotonic()
        self._plan_config = config
        pricing = getattr(self._controller, "_pricing_mgr", None)
        if pricing is None:
            self._plan = None
            return

        now = self._now()
        try:
            horizon_end = pricing.energy_horizon_end(now)
            slots = self._build_horizon(pricing, now, horizon_end)
            max_power_w, additional_cost = config
            plan = plan_high_price_discharge(
                slots,
                pricing._curtailment_battery_snapshots(),
                now=self._aware(now),
                horizon_end=self._aware(horizon_end),
                enabled=True,
                additional_cost_per_kwh=additional_cost,
                max_export_power_w=max_power_w,
                # Reuses the arbitrage knob rather than adding a second one.
                # It is the round trip, so it understates the AC energy a full
                # battery can deliver — the safe direction for a sale.
                discharge_efficiency=float(
                    getattr(self._controller, "round_trip_efficiency", 1.0) or 1.0
                ),
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("High-price discharge: plan rebuild failed: %s", err)
            self._plan = None
            return

        self._plan = plan
        _LOGGER.debug(
            "High-price discharge: rebuilt status=%s reason=%s slots=%d allocated=%.2f kWh",
            plan.status, plan.reason, len(slots), plan.total_allocated_kwh,
        )

    def _build_horizon(
        self, pricing: Any, now: datetime, horizon_end: datetime
    ) -> list[HorizonSlot]:
        """Build the atomic slot timeline the planner consumes.

        Returns an empty list — which the planner turns into a fail-safe —
        whenever a required source is missing, gapped or immature (RF-030).
        The export curve provides the timeline; consumption and solar are
        integrated onto it, and the import price is sampled from its own curve.
        """
        slots = pricing.get_future_export_price_slots(horizon_end=horizon_end)
        if not slots:
            return []
        import_slots = pricing.get_future_price_slots(horizon_end=horizon_end)
        if not import_slots:
            # The planner would read this as "no eligible candidate" and report
            # waiting. A broken price sensor is missing data, and has to say so.
            return []

        forecast_kwh, fraction_fn, _daily = pricing._curtailment_forecast_model(now)
        if forecast_kwh is None:
            controller = self._controller
            if get_configured_solar_forecast_sensor(
                controller, "remaining"
            ) or get_configured_solar_forecast_sensor(controller, "today"):
                # A configured forecast that cannot be read is missing data,
                # never zero production (RF-030).
                return []
            # No solar at all: zero is the truth, not a guess.
            forecast_kwh, fraction_fn = 0.0, None
        solar_by_slot = distribute_solar_forecast(
            slots, forecast_kwh, fraction_fn, normalize_future=True
        )

        # The learned quarter-hour shape mapped onto the price slots, exactly
        # as the discharge reserve does it: one shape per date, so a horizon
        # crossing midnight does not apply tonight's evening to tomorrow's.
        # Empty means no usable profile, and demand this feature cannot measure
        # is demand it must not sell against (RF-030).
        forecast = pricing._profile_remaining_consumption(now, horizon_end)
        if forecast is None:
            return []
        consumption = consumption_by_slot(
            slots,
            getattr(forecast, "intervals_by_date", None) or {},
            getattr(forecast, "intervals_kwh", None),
        )
        if not consumption:
            return []

        horizon: list[HorizonSlot] = []
        for slot in slots:
            horizon.append(
                HorizonSlot(
                    start=self._aware(slot.start),
                    end=self._aware(slot.end),
                    export_price=slot.price,
                    import_price=self._import_price(import_slots, slot),
                    consumption_kwh=float(consumption.get(slot, 0.0) or 0.0),
                    solar_kwh=float(solar_by_slot.get(slot, 0.0) or 0.0),
                )
            )
        return horizon

    @staticmethod
    def _import_price(import_slots: list, slot: Any) -> float | None:
        """Return the highest import price overlapping ``slot``, or None.

        ponytail: O(n·m) over at most a day of slots, rebuilt every 5 minutes.
        Taking the maximum instead of splitting the timeline on the union of
        both curves' boundaries (RF-005) is the conservative simplification: a
        finer import curve can only raise the buy-back threshold, never lower
        it, so a sale is never authorised on an averaged-away spike.
        """
        overlapping = [
            other.price
            for other in import_slots
            if other.start < slot.end and slot.start < other.end
        ]
        if not overlapping:
            return None
        return max(overlapping)

    def _block_guard(self) -> str | None:
        """Return the first live reason not to export, if any (RF-033..RF-035).

        Ordered so the reason a user sees is the one they would name first.
        """
        controller = self._controller
        pricing = getattr(controller, "_pricing_mgr", None)
        if pricing is None:
            return GUARD_NO_PRICING

        # Never charge and discharge at the same time.
        try:
            if pricing.is_in_dynamic_pricing_slot():
                return GUARD_CHARGE_ORDER
        except Exception:  # noqa: BLE001
            return GUARD_CHARGE_ORDER
        if getattr(controller, "_current_price_slot_active", False):
            return GUARD_CHARGE_ORDER

        if getattr(controller, "_force_full_charge", False):
            return GUARD_WEEKLY_FULL_CHARGE
        weekly = getattr(controller, "_weekly_charge_mgr", None)
        if weekly is not None:
            try:
                if weekly.is_active():
                    return GUARD_WEEKLY_FULL_CHARGE
            except Exception:  # noqa: BLE001
                return GUARD_WEEKLY_FULL_CHARGE

        # Anti-curtailment owns the grid target whenever it is acting, and its
        # negative-injection window exists to stop exactly this export.
        if (
            getattr(controller, "_curtailment_runtime_status", None)
            in _CURTAILMENT_ACTIVE_STATES
        ):
            return GUARD_CURTAILMENT

        if getattr(controller, "_capacity_protection_active", False):
            return GUARD_CAPACITY_PROTECTION

        # Per-battery manual ownership already drops out of the snapshots; this
        # is the global control the user took.
        if getattr(controller, "manual_mode_enabled", False) or getattr(
            controller, "_manual_slot_owned", None
        ):
            return GUARD_MANUAL

        # The registry this cycle just rebuilt: price floor, time slots, EV,
        # temperature, reserves, user blocks and export prohibitions all land
        # here, so none of them needs its own guard.
        try:
            if controller.is_discharge_blocked():
                return GUARD_DISCHARGE_BLOCKED
        except (AttributeError, TypeError):
            return GUARD_DISCHARGE_BLOCKED

        return self._grid_meter_guard()

    def _grid_meter_guard(self) -> str | None:
        """Refuse deliberate export without a trustworthy net measurement.

        RF-026: the configured ceiling is a net figure at the connection point.
        Without a live meter PD holds its last command, which is exactly how a
        contractual export limit gets exceeded.
        """
        controller = self._controller
        states = getattr(self._hass, "states", None)
        sensor = getattr(controller, "consumption_sensor", None)
        if states is None or not sensor:
            return GUARD_GRID_METER
        try:
            state = states.get(sensor)
            if controller._apply_meter_transform(state) is None:
                return GUARD_GRID_METER
            reported_at = getattr(state, "last_reported", None)
            if reported_at is not None and not controller._sensor_is_within_stale_tolerance(
                reported_at
            ):
                return GUARD_GRID_METER
        except Exception:  # noqa: BLE001
            return GUARD_GRID_METER
        return None

    def _set_status(self, state: str, reason: str, *, power_w: float | None = None) -> None:
        plan = self._plan
        status: dict[str, Any] = {
            "state": state,
            "reason": reason,
            "enabled": bool(
                getattr(self._controller, "high_price_discharge_enabled", False)
            ),
            "target_w": round(power_w, 1) if power_w is not None else None,
        }
        if plan is not None:
            status.update(
                {
                    "plan_status": plan.status,
                    "plan_reason": plan.reason,
                    "horizon_end": (
                        plan.horizon_end.isoformat() if plan.horizon_end else None
                    ),
                    "protected_demand_kwh": round(plan.protected_demand_kwh, 3),
                    "usable_energy_kwh": round(plan.usable_energy_kwh, 3),
                    "total_allocated_kwh": round(plan.total_allocated_kwh, 3),
                    "allocations": [
                        {
                            "start": allocation.start.isoformat(),
                            "end": allocation.end.isoformat(),
                            "export_price": round(allocation.export_price, 5),
                            "threshold": round(allocation.threshold, 5),
                            "energy_kwh": round(allocation.energy_kwh, 3),
                            "power_w": round(allocation.power_w, 1),
                        }
                        for allocation in plan.allocations
                    ],
                }
            )
        self._status = status
