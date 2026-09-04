"""Runtime dynamic-pricing / real-time-price engine.

``PricingManager`` owns the pricing evaluation, scheduling, control-loop handlers
and discharge-block logic extracted from ``ChargeDischargeController`` (module-8
PR3). Following the ``MaxSocChargeManager`` template, the manager owns the logic
but the runtime *state* stays on the controller by reference
(``_dynamic_pricing_schedule``, ``_dp_*``, ``_realtime_price_charging``,
``_price_based_discharge_blocked``, ``_current_price_slot_active``,
``_price_data_status``, ``_last_decision_data``,
``_last_chronological_diagnostics``) because ``sensor.py`` /
``binary_sensor.py`` read it and the PD section of the control loop consumes the
discharge block. The manager reaches all controller state and collaborators via
``self._controller``; price math goes straight to the pure ``calculations``
helpers. No persistence (no Store).
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from time import monotonic
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Optional

from homeassistant.helpers import issue_registry as ir

from ..const import (
    DOMAIN,
    PRICE_DATA_ISSUE_DELAY_S,
    PRICE_HEALTH_CHECK_INTERVAL_S,
    PRICE_INTEGRATION_NORDPOOL,
    PRICE_INTEGRATION_PVPC,
    PRICE_INTEGRATION_CKW,
    PRICE_INTEGRATION_EPEX,
    PRICE_INTEGRATION_ENTSOE,
    PRICE_INTEGRATION_TIBBER,
    NORDPOOL_REFRESH_MINUTES,
    TIBBER_REFRESH_MINUTES,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_REALTIME_PRICE,
    NOTIFICATION_ID_PREFIX,
    SOC_REEVALUATION_THRESHOLD,
    EVENING_REEVAL_HOURS_BEFORE_TEND,
    EVENING_REEVAL_FALLBACK_HOUR,
    EVENING_DEFICIT_THRESHOLD_KWH,
    T_START_FALLBACK_HOUR,
    FLOOR_HYSTERESIS_PCT,
    CHARGE_EFFICIENCY,
    normalize_solar_profile_mode,
)
from ..solar_forecast import (
    SolarForecastInput,
    get_configured_solar_forecast_sensor,
    read_remaining_solar_kwh,
    read_solar_forecast_kwh,
    solar_forecast_local_timezone,
    solar_forecast_period_energy_between,
)
from ..tracking.consumption_profile import adjust_remaining_fallback_energy
from . import (
    DynamicPricingSchedule,
    PriceSlot,
    SLOT_PURPOSE_COMBINED,
    SLOT_PURPOSE_DEFICIT,
    SLOT_PURPOSE_NEGATIVE_PRICE,
    calculations,
    notifications,
)
from .chronological import (
    ChronologicalEvaluationRequest,
    ChronologicalEvaluationResult,
    ChronologicalPlan,
    EnergyInterval,
    build_energy_deadlines,
    evaluate_chronological_request,
    normalize_energy_shape,
)
from .solar_timeline import build_boundaries, build_solar_timeline
from .curtailment import (
    BatterySnapshot,
    CurtailmentPlan,
    EXPORT_MODE_AUTOMATIC,
    EXPORT_MODE_CUSTOM,
    EXPORT_MODE_SELF_CONSUMPTION,
    calculate_opportunistic_space_kwh,
    normalize_export_mode,
    plan_curtailment,
)
from .nordpool import (
    NORDPOOL_DOMAIN,
    NORDPOOL_GET_PRICES_SERVICE,
    OfficialNordPoolSource,
    resolve_official_nordpool_source,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# A configured forecast may briefly be unavailable while its provider refreshes
# around midnight.  This is a retry grace period, not a delay applied to every
# predictive time slot.  The controller's existing charge-delay grace can
# override it at runtime; the local default keeps lightweight callers safe.
TIME_SLOT_FORECAST_GRACE_S = 300.0

# A plan is intentionally not rebuilt on every control sample: that would make
# the selected high-value blocks chatter while SOC telemetry is settling.  It
# is rebuilt when the available headroom has moved enough to change the answer,
# with a short cooldown to keep the Modbus/control path stable.
CURTAILMENT_AUTO_REPLAN_HEADROOM_DELTA_KWH = 0.5
CURTAILMENT_AUTO_REPLAN_COOLDOWN_S = 60.0

# Sensor attributes each integration expects to hold a LIST of price entries.
# Used only to detect an attribute that arrived as a string; PVPC is absent
# because it reads scalar per-hour attributes, not a list.
_PRICE_LIST_ATTRS = {
    PRICE_INTEGRATION_NORDPOOL: ("raw_today", "raw_tomorrow"),
    PRICE_INTEGRATION_CKW: ("prices",),
    PRICE_INTEGRATION_EPEX: ("data",),
    PRICE_INTEGRATION_ENTSOE: ("prices_today", "prices_tomorrow"),
}

# These values describe the forecast/timeline simulation itself.  They are
# deliberately kept separate from the current balance decision because the
# latter is replaced by pre-slot and evening re-evaluations that do not run
# the chronological planner.
_CHRONOLOGICAL_DIAGNOSTIC_KEYS = (
    "chronological_source",
    "solar_timeline_source",
    "solar_forecast_original_source",
    "solar_forecast_conversion",
    "solar_remaining_raw_kwh",
    "solar_safety_margin_kwh",
    "solar_remaining_effective_kwh",
    "solar_timeline_effective_kwh",
    "solar_timeline_energy_error_kwh",
    "solar_timeline_fallback_reason",
    "solar_profile_mature",
    "solar_profile_days",
    "solar_profile_coverage_ratio",
    "solar_profile_generation",
    "solar_shadow_selected_source",
    "curtailment_timeline_mismatch",
    "earliest_projected_depletion",
    "minimum_projected_energy_kwh",
    "minimum_projected_soc",
    "deadline_required_kwh",
    "flexible_required_kwh",
    "deadline_shortfall_kwh",
    "total_shortfall_kwh",
    "energy_deadlines",
    "chronological_plan_reason",
    "guaranteed_floor_deadline",
)


@dataclass(frozen=True)
class ChronologicalProjectionResult:
    """Read-only dashboard projection adapted from current runtime inputs.

    Unlike :class:`ChronologicalEvaluationResult`, this contains the source and
    solar diagnostics gathered by ``PricingManager``.  It never aliases or
    mutates the caller's base decision mapping and never persists controller
    diagnostics.
    """

    plan: ChronologicalPlan | None
    diagnostics: Mapping[str, Any]


class DynamicPricingEvaluationHorizon(Enum):
    """Energy horizon used to construct a dynamic-pricing calendar.

    The caller must choose deliberately: the automatic 00:05 run plans the
    complete day, whereas every later reconstruction only plans what remains
    until midnight.  Keeping this as an enum rather than inferring it from the
    clock prevents a manual rebuild or a delayed retry from double-counting
    energy that has already been consumed or produced.
    """

    DAILY = "daily"
    REMAINING = "remaining"


class PricingManager:
    """Dynamic-pricing / real-time-price engine for one config entry."""

    def __init__(self, hass: "HomeAssistant", controller: Any) -> None:
        self._hass = hass
        self._controller = controller
        self._future_price_slots_cache_key: tuple[Any, ...] | None = None
        self._future_price_slots_cache: tuple[PriceSlot, ...] = ()

    def _now(self) -> datetime:
        """Return local wall-clock time, isolated for deterministic slot tests."""
        return datetime.now()

    @staticmethod
    def evaluate_chronological_projection(
        request: ChronologicalEvaluationRequest,
    ) -> ChronologicalEvaluationResult:
        """Run the side-effect-free chronological evaluator.

        This small forwarding API is intentionally safe for dashboard callers:
        it receives an immutable request and cannot inspect or alter the
        controller.  Runtime-control code is responsible for adapting live
        state and explicitly persisting any diagnostics it wants to retain.
        """
        return evaluate_chronological_request(request)

    def build_extended_chronological_projection(
        self,
        *,
        now: datetime,
        slots: Sequence[PriceSlot],
        base_decision_data: Mapping[str, Any] | None,
        price_ceiling: float | None,
        horizon_end: datetime,
    ) -> ChronologicalProjectionResult:
        """Build a cross-midnight dashboard projection without runtime writes.

        This is the only controller-aware adapter intended for Daily
        Operation.  It reads the current forecast/profile collaborators, but
        works on a private copy of ``base_decision_data`` and explicitly
        disables diagnostic persistence.  The required extended horizon keeps
        it distinct from the control planner's end-of-local-day contract.
        """
        daily_horizon_end = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        comparable_horizon_end = horizon_end
        if comparable_horizon_end.tzinfo is None and now.tzinfo is not None:
            comparable_horizon_end = comparable_horizon_end.replace(tzinfo=now.tzinfo)
        elif comparable_horizon_end.tzinfo is not None and now.tzinfo is None:
            comparable_horizon_end = comparable_horizon_end.replace(tzinfo=None)
        elif comparable_horizon_end.tzinfo is not None:
            comparable_horizon_end = comparable_horizon_end.astimezone(now.tzinfo)
        if comparable_horizon_end <= daily_horizon_end:
            raise ValueError(
                "dashboard projection horizon must extend beyond the local day"
            )

        diagnostics_data = dict(base_decision_data or {})
        # Daily Operation refreshes independently of predictive decisions. Its
        # curve must use the live remaining forecast, not the budget retained
        # by a morning evaluation. Read without updating controller diagnostics:
        # this adapter is deliberately a read-only dashboard boundary.
        live_solar_input = self._read_remaining_solar_input(
            now=now,
            update_controller=False,
        )
        if live_solar_input is not None and live_solar_input.source != "fallback":
            diagnostics_data["solar_forecast_input"] = live_solar_input
        plan = self._build_chronological_plan_for_horizon(
            now=now,
            slots=list(slots),
            decision_data=diagnostics_data,
            price_ceiling=price_ceiling,
            diagnostic_only=True,
            horizon_end=comparable_horizon_end,
            persist_diagnostics=False,
        )
        return ChronologicalProjectionResult(
            plan=plan,
            diagnostics=self._freeze_chronological_projection_diagnostics(
                diagnostics_data
            ),
        )

    async def async_refresh_chronological_diagnostics(
        self, *, now: datetime | None = None
    ) -> bool:
        """Refresh only the canonical end-of-day diagnostic snapshot.

        This is deliberately separate from both executable pricing plans and
        the Daily Operation view.  It builds a private current-horizon balance
        and persists only the chronological fields consumed by diagnostic
        entities.  In particular it must not replace the live decision,
        schedule, charge-delay state, or issue a battery command.
        """
        current = now if isinstance(now, datetime) else self._now()
        horizon_end = current.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        if horizon_end <= current:
            return False

        try:
            # The balance calculation is intentionally local.  It supplies
            # the remaining-day scalar inputs needed by the canonical planner,
            # but is never assigned to ``_last_decision_data`` here.
            decision_data = dict(
                await self._current_horizon_grid_charging_decision(now=current)
            )
            plan = self._build_chronological_plan(
                now=current,
                slots=[],
                decision_data=decision_data,
                price_ceiling=None,
                diagnostic_only=True,
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics must not gate setup
            _LOGGER.debug(
                "Chronological diagnostics refresh failed: %s",
                exc,
                exc_info=True,
            )
            return False
        return plan is not None

    @staticmethod
    def _freeze_chronological_projection_diagnostics(
        diagnostics: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Return the projection fields as a mapping no caller can mutate."""
        result: dict[str, Any] = {
            key: diagnostics[key]
            for key in _CHRONOLOGICAL_DIAGNOSTIC_KEYS
            if key in diagnostics
        }
        result["chronological_planning_active"] = bool(
            diagnostics.get("chronological_planning_active", False)
        )
        if isinstance(result.get("energy_deadlines"), list):
            result["energy_deadlines"] = tuple(
                MappingProxyType(dict(item)) if isinstance(item, dict) else item
                for item in result["energy_deadlines"]
            )
        return MappingProxyType(result)

    def _time_slot_forecast_grace_s(self) -> float:
        """Return the configured forecast retry grace for Time Slot mode."""
        raw_grace = getattr(
            self._controller,
            "_forecast_grace_s",
            TIME_SLOT_FORECAST_GRACE_S,
        )
        try:
            grace = float(raw_grace)
        except (TypeError, ValueError):
            grace = TIME_SLOT_FORECAST_GRACE_S
        if not math.isfinite(grace):
            grace = TIME_SLOT_FORECAST_GRACE_S
        return max(0.0, grace)

    def _time_slot_forecast_unavailable_elapsed_s(self, now: datetime) -> float:
        """Return seconds since the current slot began, tolerating old state."""
        entry_time = getattr(self._controller, "_slot_entry_time", None)
        if not isinstance(entry_time, datetime):
            return 0.0
        try:
            return max(0.0, (now - entry_time).total_seconds())
        except (TypeError, ValueError):
            # A restored aware/naive mismatch must not create an indefinite
            # retry.  Restart the grace clock from the current cycle instead.
            self._controller._slot_entry_time = now
            return 0.0

    def _reset_predictive_demand_runtime(self) -> None:
        """Clear demand-protection state whenever a predictive slot aborts."""
        reset = getattr(self._controller, "_reset_predictive_demand_runtime", None)
        if callable(reset):
            reset()
            return
        # Lightweight controller stand-ins and older restored runtimes.
        self._controller._predictive_charge_suspended_for_demand = False
        self._controller._predictive_demand_state = "charging"
        self._controller._predictive_demand_fresh_samples = 0
        self._controller._predictive_demand_recovery_samples = 0
        self._controller._predictive_protection_command_w = 0.0
        self._controller._predictive_protection_reason = None

    def _record_predictive_shortfall(self, mode: str) -> float:
        """Record target energy still missing when a non-calendar slot ends."""
        targets = getattr(self._controller, "_predictive_charge_target_soc", None) or {}
        missing = 0.0
        for coordinator, target_soc in targets.items():
            data = getattr(coordinator, "data", None) or {}
            try:
                capacity = max(0.0, float(data.get("battery_total_energy", 0.0) or 0.0))
                current = float(data.get("battery_soc", 0.0) or 0.0)
                missing += max(0.0, float(target_soc) - current) * capacity / 100.0
            except (TypeError, ValueError):
                continue
        if missing <= 0.01:
            self._controller._predictive_charge_target_soc = None
            return 0.0
        decision = getattr(self._controller, "_last_decision_data", None)
        if not isinstance(decision, dict):
            decision = {}
            self._controller._last_decision_data = decision
        # RT has no future calendar. Time Slot may immediately replace this
        # with a rebuilt quota for a later configured window.
        decision["predictive_shortfall_kwh"] = round(missing, 3)
        decision["deadline_shortfall_kwh"] = round(missing, 3)
        decision["shortfall_mode"] = mode
        self._controller._predictive_charge_target_soc = None
        _LOGGER.info("%s predictive slot ended with %.2f kWh pending", mode, missing)
        return missing

    # =========================================================================
    # Startup
    # =========================================================================

    async def startup_evaluation(self) -> None:
        """Run dynamic pricing evaluation at startup if the 00:05 window was missed.

        Called once via async_create_task after integration load. Waits 15 s for
        coordinators to complete their first poll, then evaluates if today's schedule
        has not been built yet (e.g. HA restarted after 00:05).
        """
        now = datetime.now()

        # Nothing to do if we're still before the normal 00:05 window
        eval_cutoff = now.replace(hour=0, minute=5, second=0, microsecond=0)
        if now < eval_cutoff:
            _LOGGER.debug("Dynamic pricing: startup check skipped — before 00:05 window")
            return

        # Already evaluated today (00:05 ran before the restart)
        if self._controller._dynamic_pricing_evaluated_date == now.date():
            _LOGGER.debug("Dynamic pricing: startup check skipped — already evaluated today")
            return

        # Give coordinators time to finish their first Modbus poll cycle
        await asyncio.sleep(15)

        if (
            not self._controller.predictive_charging_enabled
            or getattr(self._controller, "predictive_charging_overridden", False)
        ):
            return  # Unloaded during sleep

        coordinators_with_data = [
            c for c in self._controller.coordinators
            if c.data and not getattr(c, "battery_manual_mode_enabled", False)
        ]
        if not coordinators_with_data:
            _LOGGER.warning(
                "Dynamic pricing: startup evaluation skipped — no coordinator data after 15 s"
            )
            return

        _LOGGER.info(
            "Dynamic pricing: running startup evaluation "
            "(restarted at %s, schedule not yet built for %s)",
            now.strftime("%H:%M"), now.date()
        )
        await self._evaluate_dynamic_pricing(
            horizon=DynamicPricingEvaluationHorizon.REMAINING,
            extended_horizon=True,
        )

    # =========================================================================
    # DYNAMIC PRICING: Price reading
    # =========================================================================

    def _get_price_unit(self) -> str:
        """Return the price unit label for the configured integration."""
        if self._controller.price_integration_type == PRICE_INTEGRATION_CKW:
            return "CHF/kWh"
        states = getattr(self._hass, "states", None)
        if states is not None and self._controller.price_sensor:
            state = states.get(self._controller.price_sensor)
            if state is not None:
                if (
                    self._controller.price_integration_type
                    == PRICE_INTEGRATION_NORDPOOL
                    and "raw_today" in state.attributes
                ):
                    # HACS may expose cents and/or MWh/Wh, but its parser
                    # normalizes every price to major currency/kWh.
                    currency = state.attributes.get("currency", "EUR")
                    return f"{currency}/kWh".replace("EUR/", "€/")
                unit = state.attributes.get("unit_of_measurement")
                if unit:
                    return str(unit).replace("EUR/", "€/")
        return "€/kWh"

    def _get_current_price(self) -> Optional[float]:
        """Return the current period price from the configured price sensor."""
        # Tibber is service-based: read the cached slots, not a sensor.
        if self._controller.price_integration_type == PRICE_INTEGRATION_TIBBER:
            now = datetime.now()
            for slot in self._controller._tibber_price_slots:
                if slot.start <= now < slot.end:
                    return slot.price
            return None

        # The official Nord Pool service cache and sensor use the same
        # currency/kWh scale. Prefer the interval data so any official Nord Pool
        # price entity can be used to identify the desired market area.
        if self._official_nordpool_source() is not None:
            now = datetime.now()
            for slot in self._controller._nordpool_price_slots:
                if slot.start <= now < slot.end:
                    return slot.price

        if not self._controller.price_sensor:
            return None

        price_state = self._hass.states.get(self._controller.price_sensor)
        if price_state is None:
            return None

        if self._controller.price_integration_type == PRICE_INTEGRATION_CKW:
            now = datetime.now()
            for slot in calculations.parse_ckw_prices(price_state.attributes):
                if slot.start <= now < slot.end:
                    return slot.price

        if self._controller.price_integration_type == PRICE_INTEGRATION_EPEX:
            now = datetime.now()
            for slot in calculations.parse_epex_prices(price_state.attributes):
                if slot.start <= now < slot.end:
                    return slot.price

        if self._controller.price_integration_type == PRICE_INTEGRATION_ENTSOE:
            now = datetime.now()
            for slot in calculations.parse_entsoe_prices(price_state.attributes):
                if slot.start <= now < slot.end:
                    return slot.price

        try:
            if self._controller.price_integration_type == PRICE_INTEGRATION_NORDPOOL:
                return calculations.normalize_nordpool_hacs_price(
                    price_state.state,
                    price_state.attributes,
                )
            return float(price_state.state)
        except (ValueError, TypeError):
            return None

    def _official_nordpool_source(self) -> OfficialNordPoolSource | None:
        """Return source metadata when the selected sensor is official Nord Pool."""
        if self._controller.price_integration_type != PRICE_INTEGRATION_NORDPOOL:
            return None
        if not self._controller.price_sensor:
            return None

        state = self._hass.states.get(self._controller.price_sensor)
        attributes = state.attributes if state is not None else None
        if attributes is not None and "raw_today" in attributes:
            return None
        services = getattr(self._hass, "services", None)
        if services is None or not services.has_service(
            NORDPOOL_DOMAIN,
            NORDPOOL_GET_PRICES_SERVICE,
        ):
            return None
        return resolve_official_nordpool_source(
            self._hass,
            self._controller.price_sensor,
        )

    async def _maybe_refresh_tibber_prices(self, *, force: bool = False) -> None:
        """Poll ``tibber.get_prices`` and cache the slots when stale.

        Tibber has no price sensor. Without an explicit ``end``, the service
        defaults to today only (start of today to start of tomorrow); tomorrow's
        already-published slots (available after ~13:00) are only returned if
        ``end`` reaches past tomorrow midnight, so ``end`` is requested as the
        start of the day after tomorrow. Refreshes when the cache is empty,
        older than ``TIBBER_REFRESH_MINUTES``, or when ``force`` (before each
        evaluation). No-op for every other integration type.
        """
        if self._controller.price_integration_type != PRICE_INTEGRATION_TIBBER:
            return

        now = datetime.now()
        fetched = self._controller._tibber_prices_fetched_at
        if (
            not force
            and self._controller._tibber_price_slots
            and fetched is not None
            and (now - fetched) < timedelta(minutes=TIBBER_REFRESH_MINUTES)
        ):
            return

        if not self._hass.services.has_service("tibber", "get_prices"):
            _LOGGER.warning("Dynamic pricing: tibber.get_prices service not available")
            return

        from homeassistant.util import dt as dt_util

        end = dt_util.start_of_local_day() + timedelta(days=2)
        try:
            response = await self._hass.services.async_call(
                "tibber",
                "get_prices",
                {"end": end.isoformat()},
                blocking=True,
                return_response=True,
            )
        except Exception as exc:
            _LOGGER.warning("Dynamic pricing: tibber.get_prices call failed: %s", exc)
            return

        slots = calculations.parse_tibber_prices((response or {}).get("prices") or {})
        if slots:
            self._controller._tibber_price_slots = slots
            self._controller._tibber_prices_fetched_at = now
            _LOGGER.info("Dynamic pricing: refreshed %d Tibber price slots", len(slots))
        else:
            _LOGGER.warning("Dynamic pricing: tibber.get_prices returned no usable slots")

    async def _maybe_refresh_nordpool_prices(self, *, force: bool = False) -> None:
        """Poll the official Nord Pool service for today's prices when stale.

        HACS Nordpool sensors keep using ``raw_today`` / ``raw_tomorrow`` and
        never enter this path. The official integration's entities only expose
        scalar prices, so their selected entity supplies the config entry and
        market area for ``nordpool.get_prices_for_date``.
        """
        source = self._official_nordpool_source()
        if source is None:
            return

        from homeassistant.util import dt as dt_util

        now = datetime.now()
        service_date = dt_util.now().date()
        source_key = (source.config_entry_id, source.area, service_date)
        fetched = self._controller._nordpool_prices_fetched_at
        cached_source_key = getattr(
            self._controller,
            "_nordpool_price_source_key",
            None,
        )
        if (
            not force
            and self._controller._nordpool_price_slots
            and cached_source_key == source_key
            and fetched is not None
            and (now - fetched) < timedelta(minutes=NORDPOOL_REFRESH_MINUTES)
        ):
            return

        service_data: dict[str, Any] = {
            "config_entry": source.config_entry_id,
            "date": service_date,
        }
        if source.area:
            service_data["areas"] = [source.area]

        try:
            response = await self._hass.services.async_call(
                NORDPOOL_DOMAIN,
                NORDPOOL_GET_PRICES_SERVICE,
                service_data,
                blocking=True,
                return_response=True,
            )
        except Exception as exc:
            _LOGGER.warning(
                "Dynamic pricing: nordpool.get_prices_for_date call failed: %s",
                exc,
            )
            return

        slots = calculations.parse_nordpool_service_prices(response or {}, source.area)
        if slots:
            self._controller._nordpool_price_slots = slots
            self._controller._nordpool_prices_fetched_at = now
            self._controller._nordpool_price_source_key = source_key
            _LOGGER.info(
                "Dynamic pricing: refreshed %d official Nord Pool price slots for %s",
                len(slots),
                source.area or "the configured area",
            )
        else:
            _LOGGER.warning(
                "Dynamic pricing: nordpool.get_prices_for_date returned no usable slots"
            )

    async def _maybe_refresh_service_prices(self, *, force: bool = False) -> None:
        """Refresh whichever service-based price provider is configured."""
        await self._maybe_refresh_tibber_prices(force=force)
        await self._maybe_refresh_nordpool_prices(force=force)

    def maybe_check_price_data_health(self) -> None:
        """Periodically re-parse prices and raise/clear the price-data Repairs issue.

        ``_price_data_status`` is only refreshed when something actually asks for
        prices. In dynamic-pricing mode that is the 00:05 evaluation and whatever
        the control loop happens to need, so a price sensor that stops delivering
        usable slots can go unnoticed for days: charging silently falls back to its
        no-price behaviour and only an attribute buried on the predictive-charging
        binary sensor says why. Poll it on a slow timer instead and surface a
        sustained failure in Repairs.
        """
        ctrl = self._controller
        mono = monotonic()

        if not self._prices_are_load_bearing():
            # Feature off, or a mode that does not consume price slots: prices
            # cannot be "broken" here. Clear a stale issue (it is persistent, so it
            # can outlive the run that raised it) and stop tracking.
            ctrl._price_data_bad_since = None
            self._clear_price_data_issue()
            return

        last = ctrl._price_health_last_check
        if last is not None and mono - last < PRICE_HEALTH_CHECK_INTERVAL_S:
            return
        ctrl._price_health_last_check = mono
        self._parse_price_data(quiet=True)  # refreshes _price_data_status
        self._update_price_data_issue(mono)

    def _prices_are_load_bearing(self) -> bool:
        """Whether something in this configuration actually consumes price slots.

        Dynamic pricing schedules from them directly. Real-time price mode charges
        off the scalar current price, so slots only matter there for the charge
        delay's price-aware release — without it a slot-less sensor is no defect
        and must not raise a repair.
        """
        ctrl = self._controller
        if not getattr(ctrl, "predictive_charging_enabled", False):
            return False
        mode = getattr(ctrl, "predictive_charging_mode", None)
        if mode == PREDICTIVE_MODE_DYNAMIC_PRICING:
            return True
        return mode == PREDICTIVE_MODE_REALTIME_PRICE and bool(
            getattr(ctrl, "charge_delay_enabled", False)
        )

    def _clear_price_data_issue(self) -> None:
        """Delete the price-data issue, at most once per controller run.

        The issue is created with ``is_persistent=True``, so it survives a restart
        while ``_price_data_issue_created`` does not. Keying the delete on that flag
        would therefore strand an issue raised in an earlier run forever. Key it on
        a separate "already cleared this run" flag instead, so the first healthy
        check after any start removes a stale issue.
        """
        ctrl = self._controller
        if ctrl._price_data_issue_cleared:
            return
        ctrl._price_data_issue_cleared = True
        ctrl._price_data_issue_created = False
        ir.async_delete_issue(
            ctrl.hass, DOMAIN, f"price_data_unusable_{ctrl.config_entry.entry_id}"
        )

    def _update_price_data_issue(self, mono: float) -> None:
        """Create or clear the Repairs issue for unusable price data.

        Only a failure sustained for ``PRICE_DATA_ISSUE_DELAY_S`` raises the issue,
        so an integration reload, a provider outage or the day-ahead publication gap
        does not flap it. Mirrors the slow-sensor repair: at most one creation per
        controller run, cleared as soon as prices parse again.
        """
        ctrl = self._controller
        status = ctrl._price_data_status or ""

        if status.startswith("ok"):
            ctrl._price_data_bad_since = None
            self._clear_price_data_issue()
            return

        if ctrl._price_data_bad_since is None:
            ctrl._price_data_bad_since = mono
            return
        if (
            ctrl._price_data_issue_created
            or mono - ctrl._price_data_bad_since < PRICE_DATA_ISSUE_DELAY_S
        ):
            return

        ctrl._price_data_issue_created = True
        ctrl._price_data_issue_cleared = False
        _LOGGER.warning(
            "Dynamic pricing: price data unusable (%s) for over %.0f minutes - "
            "price-aware charging is inactive",
            status, PRICE_DATA_ISSUE_DELAY_S / 60,
        )
        ir.async_create_issue(
            ctrl.hass,
            DOMAIN,
            f"price_data_unusable_{ctrl.config_entry.entry_id}",
            is_fixable=False,
            is_persistent=True,
            issue_domain=DOMAIN,
            severity=ir.IssueSeverity.WARNING,
            translation_key="price_data_unusable",
            translation_placeholders={
                "sensor": ctrl.price_sensor or "-",
                "status": status,
                "minutes": f"{PRICE_DATA_ISSUE_DELAY_S / 60:.0f}",
            },
        )

    def get_future_price_slots(self, horizon_end=None) -> list:
        """Public accessor for parsed future PriceSlots (today, future-only).

        Thin, quiet wrapper around :meth:`_parse_price_data` for other managers
        (e.g. the charge-delay price-aware release) that poll prices every control
        cycle and must not spam the log. Logging is demoted to debug level here.
        """
        now = self._now()
        state = None
        states = getattr(self._hass, "states", None)
        price_sensor = getattr(self._controller, "price_sensor", None)
        if states is not None and price_sensor:
            try:
                state = states.get(price_sensor)
            except (AttributeError, TypeError):
                state = None
        cache_key = (
            horizon_end,
            now.date(),
            now.hour,
            now.minute // 15,
            price_sensor,
            id(state),
            getattr(state, "last_updated", None),
            id(getattr(self._controller, "_tibber_price_slots", None)),
            getattr(self._controller, "_tibber_prices_fetched_at", None),
            id(getattr(self._controller, "_nordpool_price_slots", None)),
            getattr(self._controller, "_nordpool_prices_fetched_at", None),
        )
        if cache_key == self._future_price_slots_cache_key:
            return list(self._future_price_slots_cache)
        slots = self._parse_price_data(horizon_end=horizon_end, quiet=True)
        self._future_price_slots_cache_key = cache_key
        self._future_price_slots_cache = tuple(slots)
        return list(slots)

    def _parse_price_data(self, *, horizon_end=None, quiet=False) -> list:
        """Read price sensor and return list[PriceSlot] for remaining slots up to horizon_end.

        Dispatches to the correct parser based on price_integration_type.
        When horizon_end is None, defaults to end of current day (today 23:59:59).
        Returns empty list on error. When quiet=True, status logging is demoted to
        debug so high-frequency callers do not spam the log.
        """
        _warn = _LOGGER.debug if quiet else _LOGGER.warning
        official_nordpool = self._official_nordpool_source()
        if official_nordpool is not None:
            # Official integration: use the service cache refreshed by
            # _maybe_refresh_nordpool_prices. HACS remains in the sensor branch.
            raw_slots = list(self._controller._nordpool_price_slots)
            if not raw_slots:
                _warn("Dynamic pricing: no official Nord Pool price data cached")
                self._controller._price_data_status = "no_slots"
                return []
        elif self._controller.price_integration_type == PRICE_INTEGRATION_TIBBER:
            # Service-based: use the cached slots refreshed by _maybe_refresh_tibber_prices.
            raw_slots = list(self._controller._tibber_price_slots)
            if not raw_slots:
                _warn("Dynamic pricing: no Tibber price data cached")
                self._controller._price_data_status = "no_slots"
                return []
        elif not self._controller.price_sensor:
            _warn("Dynamic pricing: no price sensor configured")
            self._controller._price_data_status = "no_sensor"
            return []
        else:
            state = self._hass.states.get(self._controller.price_sensor)
            if state is None or state.state in ("unknown", "unavailable"):
                _warn("Dynamic pricing: price sensor %s unavailable", self._controller.price_sensor)
                self._controller._price_data_status = "sensor_unavailable"
                return []

            attrs = state.attributes
            # A template-built price sensor whose attribute renders to something
            # Home Assistant cannot literal_eval (e.g. a list containing datetime
            # objects) lands here as a plain string. Iterating it would walk single
            # characters, and every per-entry parse failure is debug-level, so the
            # integration would silently run without prices. Catch the type here.
            stringified = [
                key
                for key in _PRICE_LIST_ATTRS.get(self._controller.price_integration_type, ())
                if isinstance(attrs.get(key), str)
            ]
            if stringified:
                _warn(
                    "Dynamic pricing: price sensor %s exposes attribute(s) %s as a string "
                    "instead of a list — the sensor's template most likely renders values "
                    "(e.g. datetimes) that Home Assistant cannot convert back to a list. "
                    "Emit ISO-8601 strings instead.",
                    self._controller.price_sensor, ", ".join(stringified),
                )
                self._controller._price_data_status = "bad_format"
                return []

            if self._controller.price_integration_type == PRICE_INTEGRATION_PVPC:
                raw_slots = calculations.parse_pvpc_prices(attrs)
            elif self._controller.price_integration_type == PRICE_INTEGRATION_CKW:
                raw_slots = calculations.parse_ckw_prices(attrs)
            elif self._controller.price_integration_type == PRICE_INTEGRATION_EPEX:
                raw_slots = calculations.parse_epex_prices(attrs)
            elif self._controller.price_integration_type == PRICE_INTEGRATION_ENTSOE:
                raw_slots = calculations.parse_entsoe_prices(attrs)
            else:
                # Nordpool
                raw_slots = calculations.parse_nordpool_prices(attrs)

            if not raw_slots:
                _warn(
                    "Dynamic pricing: no price data parsed from %s (integration=%s)",
                    self._controller.price_sensor, self._controller.price_integration_type
                )
                self._controller._price_data_status = "no_slots"
                return []

        # Filter to remaining slots within the requested horizon.
        # Default (horizon_end=None) keeps today-only semantics so mid-day restarts
        # do not pull in tomorrow — callers that need cross-midnight slots pass an explicit horizon.
        now = datetime.now()
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
        effective_horizon = horizon_end if horizon_end is not None else end_of_day
        filtered = [s for s in raw_slots if s.end > now and s.start <= effective_horizon]
        # Slots that parse but all lie in the past leave price-aware charging just as
        # dead as a parse failure (e.g. a template sensor frozen on yesterday's
        # entries), so this is a distinct status rather than "ok (0 slots)" — the
        # latter reads as healthy to the health check. In normal operation the
        # horizon only empties in the last minutes before midnight, far short of the
        # sustained window that raises a repair.
        if not filtered:
            self._controller._price_data_status = "no_future_slots"
        else:
            self._controller._price_data_status = f"ok ({len(filtered)} slots)"
        (_LOGGER.debug if quiet else _LOGGER.info)(
            "Dynamic pricing: parsed %d slots (%d within horizon)", len(raw_slots), len(filtered)
        )
        return filtered

    # =========================================================================
    # DYNAMIC PRICING: Scheduling helpers
    # =========================================================================

    def is_in_dynamic_pricing_slot(self) -> bool:
        """Return True if current time falls within a selected cheap slot."""
        if not self._controller._dynamic_pricing_schedule:
            return False
        now = datetime.now()
        return any(s.start <= now < s.end for s in self._controller._dynamic_pricing_schedule.selected_slots)

    def _negative_price_feature_enabled(self) -> bool:
        """Return the complete scope gate for opportunistic import charging."""
        controller = self._controller
        return bool(
            getattr(controller, "negative_price_charging_enabled", False)
            and getattr(controller, "predictive_charging_enabled", False)
            and getattr(controller, "predictive_charging_mode", None)
            == PREDICTIVE_MODE_DYNAMIC_PRICING
        )

    def _opportunistic_target_for(self, coordinator) -> float:
        """Return this battery's configured maximum SOC opportunity ceiling."""
        return float(coordinator.max_soc)

    def _opportunistic_battery_eligible(self, coordinator) -> bool:
        """Return whether runtime ownership allows this battery to participate."""
        if (
            getattr(coordinator, "data", None) is None
            or not getattr(coordinator, "is_available", True)
            or getattr(coordinator, "battery_manual_mode_enabled", False)
            or not getattr(coordinator, "allow_charge", True)
            or getattr(coordinator, "rs485_user_disabled", False)
        ):
            return False
        controller = self._controller
        tracker = getattr(controller, "_non_responsive", None)
        if tracker is not None and tracker.is_excluded(coordinator):
            return False
        for method_name in ("_is_backup_function_active", "_is_manual_slot_owned"):
            method = getattr(controller, method_name, None)
            if method is not None and method(coordinator):
                return False
        return True

    def _negative_price_energy_needed_kwh(self) -> float:
        """Battery energy still required to reach all opportunistic SOC targets."""
        needed = 0.0
        for coordinator in getattr(self._controller, "coordinators", []):
            data = getattr(coordinator, "data", None)
            if not self._opportunistic_battery_eligible(coordinator):
                continue
            try:
                capacity = float(data.get("battery_total_energy", 0.0) or 0.0)
                soc = float(data.get("battery_soc", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if capacity <= 0:
                continue
            needed += max(0.0, self._opportunistic_target_for(coordinator) - soc) / 100.0 * capacity
        return needed

    def _opportunistic_target_pending(self) -> bool:
        """Return whether at least one battery remains below its own target."""
        for coordinator in getattr(self._controller, "coordinators", []):
            data = getattr(coordinator, "data", None)
            if not self._opportunistic_battery_eligible(coordinator):
                continue
            try:
                if float(data.get("battery_soc", 0.0) or 0.0) < self._opportunistic_target_for(coordinator):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _current_price_is_opportunistic(self) -> bool:
        """Validate the live normalized import price against the inclusive gate."""
        if not self._negative_price_feature_enabled():
            return False
        price = self._get_current_price()
        if price is None or not math.isfinite(price):
            return False
        return price < 0.0

    @staticmethod
    def _merge_slot_purpose(left: str | None, right: str) -> str:
        """Combine independent reasons assigned to the same physical interval."""
        if left is None or left == right:
            return right
        return SLOT_PURPOSE_COMBINED

    @staticmethod
    def _schedule_type_from_purposes(purposes) -> str:
        """Summarise typed slots as deficit, negative_price or combined."""
        values = set(purposes)
        if SLOT_PURPOSE_COMBINED in values or {
            SLOT_PURPOSE_DEFICIT,
            SLOT_PURPOSE_NEGATIVE_PRICE,
        }.issubset(values):
            return SLOT_PURPOSE_COMBINED
        if SLOT_PURPOSE_NEGATIVE_PRICE in values:
            return SLOT_PURPOSE_NEGATIVE_PRICE
        return SLOT_PURPOSE_DEFICIT

    def _slot_overlaps_curtailment_risk(self, slot: PriceSlot) -> bool:
        """Return whether Smart Pre-discharge protects this solar-risk window."""
        if not self._smart_predischarge_enabled():
            return False
        plan = getattr(self._controller, "_curtailment_plan", None)
        return bool(
            plan
            and not bool(getattr(plan, "is_fail_safe", False))
            and any(
                slot.start < risk.end and risk.start < slot.end
                for risk in getattr(plan, "risk_slots", [])
            )
        )

    def _curtailment_opportunistic_space(self, plan: CurtailmentPlan) -> float:
        """Return live free space after protecting the remaining solar reserve."""
        controller_space = getattr(
            self._controller, "_curtailment_opportunistic_space_kwh", None
        )
        if controller_space is not None:
            try:
                value = float(controller_space)
                if math.isfinite(value):
                    return max(0.0, value)
            except (TypeError, ValueError):
                pass

        explicit_space = getattr(plan, "opportunistic_space_kwh", None)
        try:
            explicit_value = float(explicit_space or 0.0)
        except (TypeError, ValueError):
            explicit_value = 0.0
        if math.isfinite(explicit_value) and explicit_value > 1e-6:
            return explicit_value

        # This fallback makes manually-created legacy plans safe and useful in
        # tests: derive the value from their old headroom fields when present.
        try:
            free = float(getattr(plan, "current_headroom_kwh", 0.0) or 0.0)
            reserve = float(
                getattr(plan, "solar_reserve_remaining_kwh", None)
                or getattr(plan, "required_headroom_kwh", 0.0)
                or 0.0
            )
        except (TypeError, ValueError):
            return 0.0
        return calculate_opportunistic_space_kwh(free, reserve)

    def _curtailment_opportunistic_targets(
        self, space_kwh: float
    ) -> dict[object, float] | None:
        """Allocate only the spare, non-reserved SOC to import charging."""
        if space_kwh <= 1e-6:
            return None
        entries: list[tuple[object, float, float, float]] = []
        for coordinator in getattr(self._controller, "coordinators", []):
            if not self._opportunistic_battery_eligible(coordinator):
                continue
            data = getattr(coordinator, "data", None) or {}
            try:
                soc = float(data.get("battery_soc", 0.0) or 0.0)
                capacity = float(data.get("battery_total_energy", 0.0) or 0.0)
                max_soc = float(getattr(coordinator, "max_soc", 100.0))
            except (TypeError, ValueError):
                continue
            free = max(0.0, (max_soc - soc) / 100.0 * capacity)
            if capacity > 0 and free > 1e-6:
                entries.append((coordinator, soc, capacity, free))
        total_free = sum(entry[3] for entry in entries)
        if total_free <= 1e-6:
            return None

        allowed = min(max(0.0, space_kwh), total_free)
        targets: dict[object, float] = {}
        for coordinator, soc, capacity, free in entries:
            allocation = allowed * free / total_free
            targets[coordinator] = min(
                float(getattr(coordinator, "max_soc", 100.0)),
                soc + allocation / capacity * 100.0,
            )
        return targets or None

    def _prepare_curtailment_opportunistic_charge(
        self,
        plan: CurtailmentPlan,
        slot: PriceSlot | None,
        purpose: str | None,
    ) -> bool:
        """Apply a transient SOC ceiling for a risk-window opportunity charge."""
        controller = self._controller
        limited = bool(
            slot is not None
            and purpose in {SLOT_PURPOSE_NEGATIVE_PRICE, SLOT_PURPOSE_COMBINED}
            and self._slot_overlaps_curtailment_risk(slot)
        )
        was_limited = bool(
            getattr(controller, "_curtailment_opportunity_limited", False)
        )
        had_transient_target = getattr(
            controller, "_curtailment_opportunistic_target_soc", None
        ) is not None
        controller._curtailment_opportunity_limited = limited
        if not limited:
            # A slot can remain active while the live risk window moves past
            # it. Remove the old reserve ceiling in that transition so normal
            # negative-price charging resumes up to the configured SOC max.
            controller._curtailment_opportunistic_target_soc = None
            if purpose in {SLOT_PURPOSE_NEGATIVE_PRICE, SLOT_PURPOSE_COMBINED} and (
                was_limited or had_transient_target
            ):
                controller._predictive_charge_target_soc = None
                controller._grid_charging_initialized = False
            return False

        space = self._curtailment_opportunistic_space(plan)
        targets = self._curtailment_opportunistic_targets(space)
        if not targets:
            controller._curtailment_opportunity_limited = False
            return False

        # A combined slot still has to satisfy its ordinary predictive deficit.
        # The solar-space ceiling only limits the opportunistic part; it must
        # never turn a required charge (especially the guaranteed-minimum-SOC
        # safety exception) into an under-charge.
        if purpose == SLOT_PURPOSE_COMBINED:
            deficit_targets = getattr(
                controller, "_predictive_deficit_target_soc", None
            )
            if not deficit_targets:
                compute_deficit = getattr(
                    controller, "_compute_deficit_target_soc", None
                )
                if callable(compute_deficit):
                    try:
                        deficit_targets = compute_deficit()
                    except (AttributeError, TypeError, ValueError):
                        deficit_targets = None
            if isinstance(deficit_targets, dict):
                for coordinator, deficit_target in deficit_targets.items():
                    if coordinator in targets:
                        targets[coordinator] = max(
                            targets[coordinator], float(deficit_target)
                        )

        # The guaranteed floor is an explicit safety override even for a pure
        # opportunity slot.  Keep it per battery so another battery's spare
        # headroom cannot mask a battery that is below its own floor.
        if getattr(controller, "_predictive_min_soc_floor_enabled", False):
            floor = float(
                getattr(controller, "_predictive_min_soc_floor", 0.0) or 0.0
            )
            for coordinator in list(targets):
                try:
                    current_soc = float(
                        (getattr(coordinator, "data", None) or {}).get(
                            "battery_soc", 0.0
                        )
                        or 0.0
                    )
                except (AttributeError, TypeError, ValueError):
                    continue
                if current_soc < floor:
                    targets[coordinator] = max(targets[coordinator], floor)

        # The controller normally recomputes its typed target when the grid
        # handler is uninitialised.  Mark this transient target as initialized so
        # that it cannot silently replace the solar reserve with max_soc.
        controller._predictive_charge_target_soc = targets
        controller._curtailment_opportunistic_target_soc = targets
        if not getattr(controller, "_grid_charging_initialized", False):
            max_power = min(
                max(0.0, float(getattr(controller, "max_contracted_power", 0.0) or 0.0)),
                max(0.0, float(getattr(controller, "max_charge_capacity", 0.0) or 0.0)),
            )
            controller.previous_power = -max_power
            controller.previous_error = 0
            controller._grid_charging_initialized = True
            controller.first_execution = False
        return True

    def _effective_slot_purpose(self, slot: PriceSlot) -> str | None:
        """Return the purpose still authorised for a slot at this instant."""
        schedule = getattr(self._controller, "_dynamic_pricing_schedule", None)
        if schedule is None:
            return None
        purpose = (
            schedule.purpose_for(slot)
            if hasattr(schedule, "purpose_for")
            else getattr(schedule, "slot_purposes", {}).get(slot, SLOT_PURPOSE_DEFICIT)
        )

        pre_purposes = getattr(self._controller, "_dp_pre_evaluated_purposes", {})
        if slot.start in pre_purposes:
            purpose = pre_purposes[slot.start]
            if purpose is None:
                return None

        has_deficit = purpose in {SLOT_PURPOSE_DEFICIT, SLOT_PURPOSE_COMBINED}
        has_opportunity = purpose in {
            SLOT_PURPOSE_NEGATIVE_PRICE,
            SLOT_PURPOSE_COMBINED,
        }
        deficit_needed = bool(
            getattr(
                schedule,
                "deficit_charging_needed",
                getattr(schedule, "charging_needed", True),
            )
        )
        if (
            slot.start in getattr(self._controller, "_dp_pre_evaluated_slots", {})
            and slot.start not in pre_purposes
        ):
            deficit_needed = bool(
                self._controller._dp_pre_evaluated_slots[slot.start]
            )

        opportunity_needed = bool(
            has_opportunity
            and self._current_price_is_opportunistic()
            and self._opportunistic_target_pending()
            and (
                not self._slot_overlaps_curtailment_risk(slot)
                or self._curtailment_opportunistic_space(
                    getattr(self._controller, "_curtailment_plan", None)
                    or CurtailmentPlan()
                )
                > 1e-6
            )
        )
        deficit_needed = has_deficit and deficit_needed
        if deficit_needed and opportunity_needed:
            return SLOT_PURPOSE_COMBINED
        if opportunity_needed:
            return SLOT_PURPOSE_NEGATIVE_PRICE
        if deficit_needed:
            return SLOT_PURPOSE_DEFICIT
        return None

    def _prune_completed_opportunities(self) -> None:
        """Drop future opportunistic work once every battery reached its target."""
        schedule = getattr(self._controller, "_dynamic_pricing_schedule", None)
        if schedule is None or not hasattr(schedule, "slot_purposes"):
            return
        keep = []
        purposes = {}
        deficit_needed = bool(getattr(schedule, "deficit_charging_needed", False))
        for slot in schedule.selected_slots:
            purpose = schedule.purpose_for(slot)
            if purpose == SLOT_PURPOSE_NEGATIVE_PRICE:
                continue
            if purpose == SLOT_PURPOSE_COMBINED:
                if not deficit_needed:
                    continue
                purpose = SLOT_PURPOSE_DEFICIT
            keep.append(slot)
            purposes[slot] = purpose
        schedule.selected_slots = keep
        schedule.slot_purposes = purposes
        schedule.negative_price_charging_needed = False
        schedule.negative_price_hours_needed = 0.0
        schedule.negative_price_energy_kwh = 0.0
        schedule.charging_needed = deficit_needed
        schedule.schedule_type = self._schedule_type_from_purposes(purposes.values())
        schedule.hours_needed = sum(
            max(0.0, (slot.end - slot.start).total_seconds() / 3600.0)
            for slot in keep
        )
        if keep:
            schedule.average_price = sum(slot.price for slot in keep) / len(keep)
            effective_power_kw = min(
                float(getattr(self._controller, "max_contracted_power", 0.0)),
                float(getattr(self._controller, "max_charge_capacity", 0.0)),
            ) / 1000.0
            schedule.estimated_cost = sum(
                slot.price
                * effective_power_kw
                * max(0.0, (slot.end - slot.start).total_seconds() / 3600.0)
                for slot in keep
            )
        if not keep:
            self._controller._dynamic_pricing_schedule = None

    def clear_negative_price_runtime(self, reason: str = "cleanup") -> None:
        """Clear transient opportunity state without disturbing deficit charging."""
        self._prune_completed_opportunities()
        controller = self._controller
        if getattr(controller, "predictive_charging_mode", None) != PREDICTIVE_MODE_DYNAMIC_PRICING:
            controller._predictive_charge_suspended_for_demand = False
        active_purpose = getattr(controller, "_active_dynamic_slot_purpose", None)
        if active_purpose == SLOT_PURPOSE_NEGATIVE_PRICE:
            controller.grid_charging_active = False
            controller._current_price_slot_active = False
            controller._grid_charging_initialized = False
            controller._predictive_charge_suspended_for_demand = False
            controller._active_dynamic_slot_purpose = None
            controller._predictive_charge_target_soc = None
            controller.previous_power = 0
            controller.previous_error = 0
        elif active_purpose == SLOT_PURPOSE_COMBINED:
            controller._active_dynamic_slot_purpose = SLOT_PURPOSE_DEFICIT
            deficit_target = getattr(
                controller, "_predictive_deficit_target_soc", None
            )
            controller._predictive_charge_target_soc = deficit_target
            if deficit_target is None:
                controller._grid_charging_initialized = False
        elif active_purpose == SLOT_PURPOSE_DEFICIT:
            # Cleanup is opportunity-specific; an ordinary active deficit slot
            # keeps both ownership and its already-calculated target.
            pass
        else:
            controller._active_dynamic_slot_purpose = None
            controller._predictive_charge_target_soc = None
        if hasattr(controller, "_dp_pre_evaluated_purposes"):
            controller._dp_pre_evaluated_purposes = {}
        _LOGGER.debug("Negative-price charging runtime cleared: %s", reason)

    # =========================================================================
    # SMART PREDISCHARGE / ANTI-CURTAILMENT
    # =========================================================================

    def _smart_predischarge_enabled(self) -> bool:
        """Return the feature gate, including the dynamic-pricing scope."""
        return bool(
            getattr(self._controller, "smart_predischarge_enabled", False)
            and getattr(self._controller, "predictive_charging_enabled", False)
            and getattr(self._controller, "predictive_charging_mode", None)
            == PREDICTIVE_MODE_DYNAMIC_PRICING
        )

    def _curtailment_plan_slots(self, slots: list[PriceSlot], now: datetime) -> list[PriceSlot]:
        """Keep the daily solar plan on the current local day only."""
        return [slot for slot in slots if slot.end > now and slot.start.date() == now.date()]

    @staticmethod
    def _future_slot_matches_operation_block(slot: PriceSlot, operation_slot: dict) -> bool:
        """Return whether a recurring user slot forbids discharge in ``slot``."""
        if not operation_slot.get("enabled", True) or operation_slot.get("allow_discharge", False):
            return False
        days = operation_slot.get("days", [])
        start_text = operation_slot.get("start_time")
        end_text = operation_slot.get("end_time")
        if not start_text or not end_text:
            return False
        try:
            start = datetime.strptime(str(start_text), "%H:%M").time()
            end = datetime.strptime(str(end_text), "%H:%M").time()
        except (TypeError, ValueError):
            return False

        def matches(moment: datetime) -> bool:
            day = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[moment.weekday()]
            if days and day not in days:
                return False
            if start <= end:
                return start <= moment.time() < end
            return moment.time() >= start or moment.time() < end

        # Checking both ends catches ordinary and cross-midnight recurring slots.
        return matches(slot.start) or matches(slot.end - timedelta(seconds=1))

    def _curtailment_operation_blocked_slots(self, slots: list[PriceSlot]) -> list[PriceSlot]:
        configured = self._controller.config_entry.data.get("no_discharge_time_slots", [])
        if not configured:
            return []
        if isinstance(configured, dict):
            configured = [configured]
        return [
            slot
            for slot in slots
            if any(self._future_slot_matches_operation_block(slot, item) for item in configured)
        ]

    def _curtailment_battery_snapshots(self) -> list[BatterySnapshot]:
        """Read live battery limits without bypassing any user ownership."""
        snapshots: list[BatterySnapshot] = []
        reserve = max(0.0, float(getattr(self._controller, "predischarge_reserve_soc", 0.0) or 0.0))
        guaranteed = (
            float(getattr(self._controller, "_predictive_min_soc_floor", 0.0) or 0.0)
            if getattr(self._controller, "_predictive_min_soc_floor_enabled", False)
            else 0.0
        )
        for coordinator in self._controller.coordinators:
            data = coordinator.data or {}
            eligible = bool(
                data
                and coordinator.is_available
                and not getattr(coordinator, "battery_manual_mode_enabled", False)
                and not self._controller._non_responsive.is_excluded(coordinator)
                and not self._controller._is_backup_function_active(coordinator)
                and not coordinator.rs485_user_disabled
                and not self._controller._is_manual_slot_owned(coordinator)
            )
            # A user/time-slot blocker must prevent a forced plan.  Price and
            # this feature's own transient blockers are deliberately ignored:
            # the former changes with each future slot and the latter is rebuilt.
            blockers = self._controller.get_discharge_blockers(coordinator)
            hard_blockers = set(blockers) - {
                "price_discharge",
                "curtailment_negative_window",
                "curtailment_floor",
            }
            can_discharge = eligible and not hard_blockers
            try:
                soc = float(data.get("battery_soc"))
                capacity = float(data.get("battery_total_energy"))
                max_discharge = float(self._controller._battery_power_limit(coordinator, False))
                max_soc = float(coordinator.max_soc)
                floor = max(
                    float(self._controller._effective_discharge_min_soc(coordinator)[0]),
                    guaranteed,
                    reserve,
                )
            except (AttributeError, TypeError, ValueError):
                eligible = False
                can_discharge = False
                soc = capacity = max_discharge = max_soc = floor = 0.0
            snapshots.append(
                BatterySnapshot(
                    name=coordinator.name,
                    soc_pct=soc,
                    capacity_kwh=capacity,
                    max_soc_pct=max_soc,
                    floor_soc_pct=floor,
                    max_discharge_power_w=max_discharge,
                    eligible=eligible,
                    can_discharge=can_discharge,
                )
            )
        return snapshots

    def _refresh_curtailment_floor_blocks(
        self, snapshots: list[BatterySnapshot]
    ) -> None:
        """Guard the configured smart reserve while a smart discharge is live."""
        controller = self._controller
        for coordinator in controller.coordinators:
            controller.remove_discharge_block("curtailment_floor", coordinator=coordinator)

        snapshots_by_name = {snapshot.name: snapshot for snapshot in snapshots}
        for coordinator in controller.coordinators:
            snapshot = snapshots_by_name.get(coordinator.name)
            if snapshot is None or not snapshot.eligible or not snapshot.can_discharge:
                continue
            if snapshot.soc_pct <= snapshot.floor_soc_pct + 1e-6:
                controller.set_discharge_block(
                    "curtailment_floor",
                    "predischarge_reserve_or_soc_floor",
                    {
                        "battery": snapshot.name,
                        "soc": snapshot.soc_pct,
                        "floor_soc": snapshot.floor_soc_pct,
                    },
                    coordinator=coordinator,
                )

    def _curtailment_forecast_model(self, now: datetime) -> tuple[float | None, object | None, float | None]:
        """Return the forecast and matching future horizon for curtailment."""
        forecast = read_solar_forecast_kwh(self._hass, self._controller)
        if forecast is None:
            return None, None, None
        forecast_kwh = forecast.kwh
        is_remaining = forecast.source == "remaining"

        tracker = getattr(self._controller, "_consumption_tracker", None)
        if tracker is None:
            return None, None, None
        try:
            t_start = getattr(self._controller, "_solar_t_start", None)
            if t_start is None:
                t_start = tracker.calculate_sunrise()
            if t_start is None:
                return None, None, None
            if getattr(self._controller, "_solar_t_start", None) is not None:
                t_end = tracker.estimate_t_end()
            else:
                t_end = 2 * tracker.calculate_solar_noon() - t_start
            if t_end <= t_start:
                return None, None, None
            fraction_fn = lambda hour: tracker.get_solar_fraction_done(hour, t_start, t_end)
            daily_consumption = float(tracker.get_avg_daily_consumption())
            # A remaining solar sensor applies only to future slots.  Use the
            # matching remaining load instead of comparing it to a full day.
            if is_remaining:
                now_h = now.hour + now.minute / 60.0 + now.second / 3600.0
                window_hours = tracker.get_consumption_window_hours_per_day()
                remaining_window = tracker.consumption_window_hours_in_range(now_h, 24.0)
                daily_consumption *= remaining_window / window_hours if window_hours > 0 else 0.0
        except (AttributeError, TypeError, ValueError):
            return None, None, None
        return forecast_kwh, fraction_fn, daily_consumption

    def _curtailment_export_settings(self) -> tuple[str, float]:
        """Return the selector mode and deliberate-export limit.

        The configuration migration is intentionally outside this module.  The
        runtime accepts the likely new attribute names and falls back to the old
        ``predischarge_max_export_power_w`` value, so old entries keep their
        exact meaning during a rolling upgrade.
        """
        controller = self._controller
        mode = None
        for name in (
            "predischarge_export_mode",
            "smart_predischarge_export_mode",
            "curtailment_export_mode",
        ):
            value = getattr(controller, name, None)
            if value is not None:
                mode = value
                break

        limit = None
        for name in (
            "predischarge_export_limit_w",
            "predischarge_custom_export_power_w",
            "predischarge_max_export_power_w",
        ):
            value = getattr(controller, name, None)
            if value is not None:
                limit = value
                break
        try:
            limit_w = max(0.0, float(limit or 0.0))
        except (TypeError, ValueError):
            limit_w = 0.0

        normalized = normalize_export_mode(mode, limit_w)
        if normalized != EXPORT_MODE_CUSTOM:
            limit_w = 0.0
        return normalized, limit_w

    @staticmethod
    def _mapping_value(mapping: object, key: PriceSlot, fallback: float) -> float:
        """Read a finite non-negative value from a slot mapping."""
        if not hasattr(mapping, "get"):
            return max(0.0, fallback)
        try:
            value = float(mapping.get(key, fallback) or 0.0)
        except (TypeError, ValueError):
            return max(0.0, fallback)
        return value if math.isfinite(value) else max(0.0, fallback)

    def _curtailment_actual_solar_mapping(self, plan: CurtailmentPlan) -> object | None:
        """Return optional per-slot measured solar energy supplied by telemetry.

        The normal controller publishes a daily accumulator, while tests and
        future telemetry providers may expose per-slot values.  Only the latter
        can safely identify how much of each protected interval actually arrived;
        absent such data the forecast remains the conservative source of truth.
        """
        for owner in (self._controller, plan):
            for name in (
                "_curtailment_actual_solar_by_slot",
                "curtailment_actual_solar_by_slot",
                "actual_solar_by_slot",
            ):
                mapping = getattr(owner, name, None)
                if hasattr(mapping, "get"):
                    return mapping
        return None

    def _curtailment_daily_solar_ratio(
        self, plan: CurtailmentPlan, now: datetime
    ) -> float | None:
        """Estimate actual-versus-expected PV performance from today's accumulator.

        The accumulator is a daily total, so comparing it with the full-day
        forecast would make the reserve jump at sunrise.  Compare it with the
        forecast fraction that should have arrived by ``now`` instead.  Keep a
        small warm-up threshold to avoid making a decision from the first few
        watts of the day; until then the forecast remains the safe fallback.
        """
        if getattr(plan, "solar_forecast_is_remaining", False):
            # A daily production accumulator cannot be compared with a scalar
            # that represents only the post-evaluation forecast horizon.
            return None

        controller = self._controller
        actual_date = getattr(controller, "_daily_solar_energy_date", None)
        if actual_date is not None and actual_date != now.date():
            return None
        try:
            actual_kwh = float(getattr(controller, "_daily_solar_energy_kwh", 0.0) or 0.0)
            forecast_kwh = float(getattr(plan, "solar_forecast_kwh", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None
        if (
            not math.isfinite(actual_kwh)
            or not math.isfinite(forecast_kwh)
            or forecast_kwh <= 1e-6
        ):
            return None

        tracker = getattr(controller, "_consumption_tracker", None)
        try:
            t_start = getattr(controller, "_solar_t_start", None)
            if t_start is None:
                t_start = tracker.calculate_sunrise()
            if t_start is None:
                return None
            t_end = tracker.estimate_t_end()
            if t_end <= t_start:
                return None
            now_hour = now.hour + now.minute / 60.0 + now.second / 3600.0
            expected_fraction = float(
                tracker.get_solar_fraction_done(now_hour, t_start, t_end)
            )
        except (AttributeError, TypeError, ValueError):
            return None
        if not math.isfinite(expected_fraction):
            return None
        expected_fraction = max(0.0, min(1.0, expected_fraction))
        expected_to_now = forecast_kwh * expected_fraction
        if expected_to_now < max(0.1, forecast_kwh * 0.05):
            return None
        return max(0.0, min(2.0, actual_kwh / expected_to_now))

    def _update_curtailment_opportunistic_diagnostics(
        self,
        plan: CurtailmentPlan,
        snapshots: list[BatterySnapshot],
        now: datetime,
    ) -> tuple[float, float]:
        """Refresh solar reserve and usable opportunistic space from live SOC.

        This is deliberately recalculated every control cycle.  If measured
        per-slot solar is lower than forecast, the reserve shrinks and more
        import charging is admitted; if it is higher, the reserve grows and the
        charge target is reduced or stopped.  A missing measurement never shrinks
        the forecast reserve.
        """
        risk_slots = [
            slot for slot in getattr(plan, "risk_slots", []) if slot.end > now
        ]
        forecast_reserves = getattr(plan, "solar_reserve_by_slot", {}) or {}
        forecast_solar = getattr(plan, "solar_forecast_by_slot", {}) or {}
        forecast_consumption = getattr(plan, "consumption_forecast_by_slot", {}) or {}
        actual_mapping = self._curtailment_actual_solar_mapping(plan)
        actual_total_ratio: float | None = None
        if (
            actual_mapping is None
            and risk_slots
            and not getattr(plan, "solar_forecast_is_remaining", False)
        ):
            for name in (
                "_curtailment_actual_solar_kwh",
                "curtailment_actual_solar_kwh",
            ):
                raw_actual = getattr(self._controller, name, None)
                try:
                    actual_total = float(raw_actual)
                    forecast_total = float(getattr(plan, "solar_forecast_kwh", 0.0) or 0.0)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(actual_total) and math.isfinite(forecast_total) and forecast_total > 1e-6:
                    actual_total_ratio = max(0.0, min(2.0, actual_total / forecast_total))
                    break
            if actual_total_ratio is None:
                actual_total_ratio = self._curtailment_daily_solar_ratio(plan, now)

        reserve = 0.0
        measured_underproduction = False
        measured_overproduction = False
        legacy_reserve_per_slot = 0.0
        if risk_slots and not forecast_reserves:
            legacy_reserve_per_slot = max(
                0.0,
                float(getattr(plan, "required_headroom_kwh", 0.0) or 0.0),
            ) / len(risk_slots)
        for slot in risk_slots:
            fallback_reserve = self._mapping_value(
                forecast_reserves,
                slot,
                legacy_reserve_per_slot,
            )
            forecast_value = self._mapping_value(forecast_solar, slot, 0.0)
            consumption = self._mapping_value(forecast_consumption, slot, 0.0)
            if actual_mapping is None or not hasattr(actual_mapping, "get"):
                if actual_total_ratio is not None:
                    reserve += fallback_reserve * actual_total_ratio
                    measured_underproduction |= actual_total_ratio < 1.0 - 1e-6
                    measured_overproduction |= actual_total_ratio > 1.0 + 1e-6
                else:
                    reserve += fallback_reserve
                continue

            actual_value = self._mapping_value(actual_mapping, slot, forecast_value)
            forecast_surplus = max(0.0, forecast_value - consumption)
            actual_surplus = max(0.0, actual_value - consumption)
            if actual_surplus + 1e-6 < forecast_surplus:
                measured_underproduction = True
            elif actual_surplus > forecast_surplus + 1e-6:
                measured_overproduction = True
            if forecast_surplus <= 1e-6:
                reserve += fallback_reserve
            else:
                # Preserve the planner's charge-power cap by scaling the
                # already-capped forecast reserve by the observed surplus ratio.
                reserve += fallback_reserve * actual_surplus / forecast_surplus

        if risk_slots:
            reserve += max(0.0, float(getattr(plan, "headroom_margin_kwh", 0.0) or 0.0))
        else:
            reserve = 0.0

        current_headroom = sum(
            max(
                0.0,
                (snapshot.max_soc_pct - snapshot.soc_pct)
                / 100.0
                * snapshot.capacity_kwh,
            )
            for snapshot in snapshots
            if snapshot.eligible
        )
        space = calculate_opportunistic_space_kwh(current_headroom, reserve)
        plan.current_headroom_kwh = current_headroom
        plan.solar_reserve_remaining_kwh = reserve
        plan.opportunistic_space_kwh = space

        if not risk_slots:
            reason = "no_solar_risk_reserve"
        elif measured_overproduction:
            reason = "solar_overproduction_reduced_space"
        elif space <= 1e-6:
            reason = "solar_reserve_protected"
        elif measured_underproduction:
            reason = "solar_underproduction_released_space"
        else:
            reason = "solar_reserve_space_available"
        plan.opportunistic_charge_reason = reason

        # This is a diagnostic ceiling, not a replacement for the controller's
        # per-battery charge limits.  The actual power path clamps again.
        max_charge_power = max(
            0.0, float(getattr(self._controller, "max_charge_capacity", 0.0) or 0.0)
        )
        remaining_hours = sum(
            max(0.0, (slot.end - max(now, slot.start)).total_seconds() / 3600.0)
            for slot in risk_slots
        )
        plan.opportunistic_charge_limit_w = (
            min(max_charge_power, space / remaining_hours * 1000.0)
            if remaining_hours > 1e-6
            else 0.0
        )
        controller = self._controller
        controller._curtailment_solar_reserve_remaining_kwh = reserve
        controller._curtailment_opportunistic_space_kwh = space
        controller._curtailment_opportunistic_charge_reason = reason
        controller._curtailment_opportunistic_target_soc = None
        controller._curtailment_opportunistic_charge_limit_w = (
            plan.opportunistic_charge_limit_w
        )
        return reserve, space

    def _maybe_rebuild_curtailment_plan(
        self,
        plan: CurtailmentPlan,
        snapshots: list[BatterySnapshot],
        now: datetime,
    ) -> bool:
        """Rebuild a stale pre-discharge plan after a material SOC change.

        The daily dynamic-pricing evaluation is made before the battery has
        necessarily completed its morning charge.  Without this refresh, a plan
        that was valid at midnight can have no future discharge slot left when
        the battery fills later in the day; pressing the re-evaluation button
        then appears to "fix" it.  Keep the refresh local and synchronous so it
        can run from the existing control-cycle blocker refresh.
        """
        controller = self._controller
        if not self._smart_predischarge_enabled() or plan.status == "fail_safe":
            return False

        future_risk_slots = [
            slot for slot in getattr(plan, "risk_slots", []) if slot.end > now
        ]
        if not future_risk_slots:
            return False

        # Once the first risk window has started, pre-discharge cannot create
        # useful headroom for it anymore.  Live reserve accounting continues to
        # be updated by the normal runtime path.
        if now >= min(slot.start for slot in future_risk_slots):
            return False

        current_headroom = sum(
            max(
                0.0,
                (snapshot.max_soc_pct - snapshot.soc_pct)
                / 100.0
                * snapshot.capacity_kwh,
            )
            for snapshot in snapshots
            if snapshot.eligible
        )
        planned_headroom = getattr(
            controller, "_curtailment_last_planned_headroom_kwh", None
        )
        if planned_headroom is None:
            controller._curtailment_last_planned_headroom_kwh = current_headroom
            return False

        selected_future_slot = any(
            slot.end > now for slot in getattr(plan, "selected_discharge_slots", [])
        )
        required = max(
            0.0,
            float(
                getattr(plan, "solar_reserve_remaining_kwh", None)
                or getattr(plan, "required_headroom_kwh", 0.0)
                or 0.0
            ),
        )
        headroom_changed = (
            abs(current_headroom - float(planned_headroom))
            >= CURTAILMENT_AUTO_REPLAN_HEADROOM_DELTA_KWH
        )
        missing_future_slot = (
            current_headroom + 1e-6 < required and not selected_future_slot
        )
        if not headroom_changed and not missing_future_slot:
            return False

        last_replan = getattr(
            controller, "_curtailment_last_auto_replan", None
        )
        if last_replan is not None:
            try:
                if (now - last_replan).total_seconds() < CURTAILMENT_AUTO_REPLAN_COOLDOWN_S:
                    return False
            except (TypeError, ValueError):
                pass

        slots = self.get_future_price_slots()
        if not slots:
            return False
        schedule = getattr(controller, "_dynamic_pricing_schedule", None)
        reserved_slots = list(getattr(schedule, "selected_slots", []) or [])
        _LOGGER.info(
            "Smart pre-discharge: rebuilding stale plan (headroom %.2f -> %.2f kWh)",
            float(planned_headroom),
            current_headroom,
        )
        self._build_curtailment_plan(slots, reserved_slots, now=now)
        controller._curtailment_last_auto_replan = now
        return True

    def _build_curtailment_plan(
        self,
        slots: list[PriceSlot],
        reserved_slots: list[PriceSlot] | None = None,
        *,
        now: datetime | None = None,
    ) -> CurtailmentPlan:
        """Calculate and publish a new non-persistent smart plan."""
        evaluated_at = now or datetime.now()
        if not self._smart_predischarge_enabled():
            plan = CurtailmentPlan(
                status="disabled", reason="feature_disabled", evaluation_time=evaluated_at
            )
            self._controller._curtailment_plan = plan
            # Small pricing-engine test doubles and legacy startup paths may not
            # expose the full controller blocker API.  Runtime cleanup is owned
            # by the real controller's refresh/update paths.
            if (
                hasattr(self._controller, "remove_setpoint_override")
                and hasattr(self._controller, "coordinators")
            ):
                self.clear_curtailment_runtime("disabled", preserve_plan=True)
            return plan

        daily_slots = self._curtailment_plan_slots(slots, evaluated_at)
        forecast, solar_model, daily_consumption = self._curtailment_forecast_model(evaluated_at)
        is_remaining = getattr(self._controller, "solar_forecast_source", None) == "remaining"
        if forecast is None or solar_model is None or daily_consumption is None:
            plan = CurtailmentPlan(
                status="fail_safe",
                reason="missing_forecast_or_solar_model",
                evaluation_time=evaluated_at,
            )
            self._controller._curtailment_plan = plan
            self.clear_curtailment_runtime(plan.reason, preserve_plan=True)
            self._set_curtailment_runtime("fail_safe", plan.reason)
            return plan

        reserved = list(reserved_slots or [])
        reserved.extend(self._curtailment_operation_blocked_slots(daily_slots))
        export_mode, export_limit_w = self._curtailment_export_settings()
        try:
            plan = plan_curtailment(
                daily_slots,
                forecast,
                daily_consumption,
                self._curtailment_battery_snapshots(),
                negative_injection_threshold=float(
                    getattr(self._controller, "negative_injection_threshold", 0.0)
                ),
                predischarge_reserve_soc=float(
                    getattr(self._controller, "predischarge_reserve_soc", 0.0)
                ),
                headroom_margin_kwh=float(
                    getattr(self._controller, "_predictive_safety_margin_kwh", 0.0)
                ),
                charge_power_w=float(getattr(self._controller, "max_charge_capacity", 0.0) or 0.0),
                max_export_power_w=export_limit_w,
                export_mode=export_mode,
                solar_fraction_fn=solar_model,
                solar_forecast_is_remaining=is_remaining,
                consumption_forecast_is_remaining=is_remaining,
                reserved_slots=reserved,
                now=evaluated_at,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Smart pre-discharge planner failed; using fail-safe: %s", err)
            plan = CurtailmentPlan(
                status="fail_safe",
                reason="planner_error",
                evaluation_time=evaluated_at,
            )
            self._controller._curtailment_plan = plan
            self.clear_curtailment_runtime(plan.reason, preserve_plan=True)
            self._set_curtailment_runtime("fail_safe", plan.reason)
            return plan
        self._controller._curtailment_plan = plan
        self._controller._curtailment_last_evaluation = evaluated_at
        snapshots = self._curtailment_battery_snapshots()
        self._update_curtailment_opportunistic_diagnostics(
            plan,
            snapshots,
            evaluated_at,
        )
        self._controller._curtailment_last_planned_headroom_kwh = (
            plan.current_headroom_kwh
        )
        self._set_curtailment_runtime(plan.status, plan.reason)
        return plan

    def _set_curtailment_runtime(
        self, status: str, reason: str, active_export_target_w: float | None = None
    ) -> None:
        """Publish runtime state and log only transitions."""
        controller = self._controller
        target = 0.0 if active_export_target_w is None else float(active_export_target_w)
        old = (
            getattr(controller, "_curtailment_runtime_status", "disabled"),
            getattr(controller, "_curtailment_runtime_reason", "disabled"),
            round(getattr(controller, "_curtailment_active_export_target_w", 0.0), 1),
        )
        controller._curtailment_runtime_status = status
        controller._curtailment_runtime_reason = reason
        controller._curtailment_active_export_target_w = target
        new = (status, reason, round(target, 1))
        if old != new:
            _LOGGER.info(
                "Smart pre-discharge: %s (%s)%s",
                status,
                reason,
                f", export target={target:.0f}W" if status == "predischarging" else "",
            )

    def clear_curtailment_runtime(self, reason: str = "cleanup", *, preserve_plan: bool = False) -> None:
        """Remove every smart override/block and optionally keep diagnostics."""
        controller = self._controller
        controller.remove_setpoint_override("curtailment_predischarge")
        controller.remove_setpoint_override("curtailment_negative_window")
        controller.remove_discharge_block("curtailment_negative_window")
        for coordinator in controller.coordinators:
            controller.remove_discharge_block("curtailment_negative_window", coordinator=coordinator)
            controller.remove_discharge_block("curtailment_floor", coordinator=coordinator)
        controller._curtailment_active = False
        controller._curtailment_active_export_target_w = 0.0
        preserved = getattr(controller, "_curtailment_plan", None) if preserve_plan else None
        controller._curtailment_solar_reserve_remaining_kwh = max(
            0.0,
            float(
                getattr(preserved, "solar_reserve_remaining_kwh", 0.0) or 0.0
            ),
        ) if preserved is not None else 0.0
        controller._curtailment_opportunistic_space_kwh = max(
            0.0,
            float(getattr(preserved, "opportunistic_space_kwh", 0.0) or 0.0),
        ) if preserved is not None else 0.0
        controller._curtailment_opportunistic_charge_limit_w = max(
            0.0,
            float(getattr(preserved, "opportunistic_charge_limit_w", 0.0) or 0.0),
        ) if preserved is not None else 0.0
        controller._curtailment_opportunistic_charge_reason = (
            getattr(preserved, "opportunistic_charge_reason", reason)
            if preserved is not None
            else reason
        )
        controller._curtailment_opportunistic_target_soc = None
        controller._curtailment_opportunity_limited = False
        if not preserve_plan:
            controller._curtailment_plan = CurtailmentPlan(
                status="disabled", reason=reason, evaluation_time=datetime.now()
            )
            controller._curtailment_last_planned_headroom_kwh = None
            controller._curtailment_last_auto_replan = None
        self._set_curtailment_runtime("disabled" if not preserve_plan else getattr(
            getattr(controller, "_curtailment_plan", None), "status", "disabled"
        ), reason)

    def _current_curtailment_risk_slot(self, plan: CurtailmentPlan, now: datetime) -> PriceSlot | None:
        return next((slot for slot in plan.risk_slots if slot.start <= now < slot.end), None)

    def _current_predischarge_slot(self, plan: CurtailmentPlan, now: datetime):
        return next((slot for slot in plan.selected_discharge_slots if slot.start <= now < slot.end), None)

    def refresh_curtailment_runtime(self) -> None:
        """Apply or clear the live action, with fail-safe cleanup on errors."""
        try:
            self._refresh_curtailment_runtime()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Smart pre-discharge runtime failed; cleaning up: %s", err)
            try:
                self.clear_curtailment_runtime("runtime_error", preserve_plan=True)
            except Exception as cleanup_err:  # noqa: BLE001
                _LOGGER.error("Smart pre-discharge cleanup also failed: %s", cleanup_err)
            try:
                self._set_curtailment_runtime("fail_safe", "runtime_error")
            except Exception:  # noqa: BLE001
                # Do not let diagnostics compromise the PD loop's safety path.
                pass

    def _refresh_curtailment_runtime(self) -> None:
        """Apply or clear the live block/override for the current slot."""
        if not self._smart_predischarge_enabled():
            self.clear_curtailment_runtime("feature_disabled")
            return
        plan = getattr(self._controller, "_curtailment_plan", None)
        if plan is None:
            self.clear_curtailment_runtime("no_plan", preserve_plan=True)
            self._set_curtailment_runtime("fail_safe", "no_plan")
            return
        if plan.status == "fail_safe":
            self.clear_curtailment_runtime(plan.reason, preserve_plan=True)
            self._set_curtailment_runtime("fail_safe", plan.reason)
            return
        meter_state = self._hass.states.get(getattr(self._controller, "consumption_sensor", None))
        if self._controller._apply_meter_transform(meter_state) is None:
            self.clear_curtailment_runtime("invalid_grid_meter", preserve_plan=True)
            self._set_curtailment_runtime("fail_safe", "invalid_grid_meter")
            return
        reported_at = getattr(meter_state, "last_reported", None)
        if reported_at is not None and hasattr(self._controller, "_sensor_is_within_stale_tolerance"):
            try:
                if not self._controller._sensor_is_within_stale_tolerance(reported_at):
                    self.clear_curtailment_runtime("stale_grid_meter", preserve_plan=True)
                    self._set_curtailment_runtime("fail_safe", "stale_grid_meter")
                    return
            except (TypeError, ValueError):
                self.clear_curtailment_runtime("invalid_grid_meter_timestamp", preserve_plan=True)
                self._set_curtailment_runtime("fail_safe", "invalid_grid_meter_timestamp")
                return
        current_price = self._get_current_price()
        if current_price is None or not math.isfinite(float(current_price)):
            self.clear_curtailment_runtime("invalid_price", preserve_plan=True)
            self._set_curtailment_runtime("fail_safe", "invalid_price")
            return
        if plan.status == "no_risk":
            self.clear_curtailment_runtime(plan.reason, preserve_plan=True)
            self._set_curtailment_runtime("no_risk", plan.reason)
            return

        now = datetime.now()
        snapshots = self._curtailment_battery_snapshots()
        if self._maybe_rebuild_curtailment_plan(plan, snapshots, now):
            plan = getattr(self._controller, "_curtailment_plan", plan)
        risk_slot = self._current_curtailment_risk_slot(plan, now)
        # Keep the old per-battery blocker cleanup for plans created by older
        # versions, then replace it during the active risk window with a net-zero
        # grid target.  A blanket discharge block makes the house import from
        # the grid even when the battery could safely cover domestic load.
        for coordinator in self._controller.coordinators:
            self._controller.remove_discharge_block(
                "curtailment_negative_window", coordinator=coordinator
            )
            self._controller.remove_discharge_block(
                "curtailment_floor", coordinator=coordinator
            )
        self._controller.remove_setpoint_override("curtailment_predischarge")
        self._controller._curtailment_active = False

        self._update_curtailment_opportunistic_diagnostics(plan, snapshots, now)

        if risk_slot is not None:
            self._refresh_curtailment_floor_blocks(snapshots)
            overrides = getattr(self._controller, "_setpoint_overrides", {}) or {}
            if overrides.get("curtailment_negative_window") != (6, 0.0):
                self._controller.set_setpoint_override(
                    "curtailment_negative_window", 0.0, priority=6
                )
            self._set_curtailment_runtime("protected_window", "negative_injection_window")
            return

        self._controller.remove_setpoint_override("curtailment_negative_window")
        current_headroom = plan.current_headroom_kwh
        required = max(
            0.0,
            float(
                getattr(plan, "solar_reserve_remaining_kwh", None)
                or getattr(plan, "required_headroom_kwh", 0.0)
                or 0.0
            ),
        )
        if current_headroom + 1e-6 >= required:
            self._set_curtailment_runtime("target_reached", "headroom_sufficient")
            return

        active_slot = self._current_predischarge_slot(plan, now)
        if active_slot is None:
            self._set_curtailment_runtime("planned", plan.reason)
            return

        self._refresh_curtailment_floor_blocks(snapshots)
        remaining_kwh = max(0.0, required - current_headroom)
        hours_left = max(0.0, (active_slot.end - now).total_seconds() / 3600.0)
        max_power = sum(
            snapshot.max_discharge_power_w
            for snapshot in snapshots
            if snapshot.eligible and snapshot.can_discharge
        )
        export_mode, export_limit = self._curtailment_export_settings()
        if hours_left <= 0 or max_power <= 0 or remaining_kwh <= 1e-6:
            self._set_curtailment_runtime("shortfall", "no_live_discharge_capacity")
            return
        needed_export_w = min(max_power, remaining_kwh / hours_left * 1000.0)
        if export_mode == EXPORT_MODE_SELF_CONSUMPTION:
            # Zero means domestic self-consumption only.  A target of 0 W lets
            # normal PD cover household load but never deliberately export.
            target_export = 0.0
        elif export_mode == EXPORT_MODE_CUSTOM:
            target_export = -min(export_limit, needed_export_w)
        else:
            # Automatic mode deliberately exports only the power required to
            # create the missing headroom, never the battery's full capability.
            target_export = -needed_export_w
        self._controller.set_setpoint_override(
            "curtailment_predischarge", target_export, priority=5
        )
        self._controller._curtailment_active = True
        self._set_curtailment_runtime("predischarging", "selected_discharge_slot", target_export)

    # =========================================================================
    # DYNAMIC PRICING: Evaluation and notification methods
    # =========================================================================

    def _solar_timeline_window(
        self,
        now: datetime,
        tracker: Any,
    ) -> tuple[datetime | None, datetime | None]:
        """Resolve today's direct/astronomical solar window for pure mapping."""
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        profile = getattr(tracker, "solar_profile", None) if tracker is not None else None
        day = None
        if profile is not None:
            day = getattr(profile, "_days", {}).get(now.date())
        observed_start = getattr(day, "solar_start", None) if day is not None else None
        observed_end = getattr(day, "solar_end", None) if day is not None else None
        if observed_start is not None and observed_start.tzinfo is None:
            observed_start = observed_start.replace(tzinfo=now.tzinfo)
        if observed_end is not None and observed_end.tzinfo is None:
            observed_end = observed_end.replace(tzinfo=now.tzinfo)
        if now.tzinfo is None:
            if observed_start is not None and observed_start.tzinfo is not None:
                observed_start = observed_start.replace(tzinfo=None)
            if observed_end is not None and observed_end.tzinfo is not None:
                observed_end = observed_end.replace(tzinfo=None)
        if observed_start is not None and observed_start <= now:
            start = observed_start
        else:
            # Charge Delay owns its persisted start marker. Once disabled it
            # may legitimately remain in runtime state across midnight, but it
            # must no longer define the read-only dashboard forecast window.
            start_hour = (
                getattr(self._controller, "_solar_t_start", None)
                if bool(getattr(self._controller, "charge_delay_enabled", False))
                else None
            )
            if start_hour is None and tracker is not None:
                try:
                    start_hour = tracker.calculate_sunrise()
                except (AttributeError, TypeError, ValueError):
                    start_hour = None
            if start_hour is None:
                return None, None
            start = midnight + timedelta(hours=float(start_hour))

        end: datetime | None = None
        # An observed end is safe only after it is in the past; while the
        # window is live it is merely the last positive sample, not a future
        # sunset prediction.
        if observed_end is not None and getattr(day, "complete", False):
            end = observed_end
        if end is None and tracker is not None:
            try:
                # Keep both ends of the dashboard window on the same temporal
                # basis. ``estimate_t_end()`` uses Charge Delay's persisted
                # ``_solar_t_start`` and may extend a past estimate to
                # ``now + 1 hour`` while a battery is charging. Reusing it here
                # could compress the entire remaining solar budget into that
                # moving hour even after Charge Delay was disabled.
                end_hour = 2 * tracker.calculate_solar_noon() - (
                    start - midnight
                ).total_seconds() / 3600.0
            except (AttributeError, TypeError, ValueError):
                end_hour = None
            if end_hour is not None:
                end = midnight + timedelta(hours=float(end_hour))
        if end is None or end <= start:
            return None, None
        return start, end

    def _solar_timeline_input(
        self,
        now: datetime,
        decision_data: dict[str, Any],
        *,
        horizon_end: datetime | None = None,
    ) -> SolarForecastInput:
        """Read one normalized forecast input while preserving dated periods."""
        provided = decision_data.get("solar_forecast_input")
        if isinstance(provided, SolarForecastInput):
            solar_input = provided
        else:
            existing_conversion = str(
                decision_data.get("solar_forecast_conversion", "none") or "none"
            )
            raw = (
                None
                if "extended_dated_periods" in existing_conversion
                else decision_data.get("solar_remaining_raw_kwh")
            )
            if raw is None:
                raw = decision_data.get(
                    "remaining_solar_kwh",
                    decision_data.get("solar_forecast_kwh"),
                )
            periods = decision_data.get("solar_forecast_periods")
            temporal_shape = decision_data.get("solar_temporal_shape")
            if temporal_shape is not None:
                try:
                    temporal_shape = tuple(temporal_shape)
                except TypeError:
                    temporal_shape = None
            solar_input = (
                None
                if raw is None
                else SolarForecastInput(
                    raw,
                    decision_data.get("solar_forecast_diagnostic_source")
                    or decision_data.get("solar_forecast_source")
                    or "remaining",
                    temporal_shape=temporal_shape,
                    periods=tuple(periods) if periods else None,
                    original_source=decision_data.get(
                        "solar_forecast_original_source"
                    ),
                    conversion=decision_data.get(
                        "solar_forecast_conversion", "none"
                    ),
                )
            )

        if solar_input is None:
            try:
                solar_input = read_remaining_solar_kwh(
                    self._hass,
                    self._controller,
                    now=now,
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Solar timeline: forecast adapter failed: %s", exc)
                solar_input = SolarForecastInput(
                    0.0,
                    "fallback",
                    original_source=None,
                    conversion="unsafe_zero",
                )

        periods = solar_input.periods
        if not periods:
            return solar_input

        timezone = solar_forecast_local_timezone(
            self._hass,
            self._controller,
            now,
        )
        local_now = (
            now.replace(tzinfo=timezone)
            if now.tzinfo is None
            else now.astimezone(timezone)
        )
        day_end = local_now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ) + timedelta(days=1)
        local_horizon_end = day_end
        if horizon_end is not None:
            local_horizon_end = (
                horizon_end.replace(tzinfo=timezone)
                if horizon_end.tzinfo is None
                else horizon_end.astimezone(timezone)
            )

        remaining = solar_input.remaining_kwh
        conversions = []
        if solar_input.conversion and solar_input.conversion != "none":
            conversions.append(solar_input.conversion)
        current_day_periods = solar_forecast_period_energy_between(
            periods,
            local_now,
            min(day_end, local_horizon_end),
            timezone=timezone,
        )
        if remaining <= 1e-9 and current_day_periods > 1e-9:
            remaining = current_day_periods
            conversions.append("dated_periods_zero_scalar")

        if local_horizon_end > day_end:
            extension = solar_forecast_period_energy_between(
                periods,
                day_end,
                local_horizon_end,
                timezone=timezone,
            )
            if extension > 1e-9:
                remaining += extension
                conversions.append("extended_dated_periods")

        conversion = "+".join(dict.fromkeys(conversions)) or "none"
        if (
            abs(remaining - solar_input.remaining_kwh) <= 1e-9
            and conversion == solar_input.conversion
        ):
            return solar_input
        return SolarForecastInput(
            remaining,
            solar_input.source,
            temporal_shape=solar_input.temporal_shape,
            periods=periods,
            original_source=solar_input.original_source,
            conversion=conversion,
            horizon=("extended" if local_horizon_end > day_end else solar_input.horizon),
        )

    def _store_chronological_diagnostics(
        self, decision_data: dict[str, Any]
    ) -> None:
        """Retain the latest successful timeline simulation independently.

        ``_last_decision_data`` is intentionally mutable runtime state and is
        replaced by several cheaper balance checks.  Keep only the fields
        produced by the chronological planner so those checks cannot turn a
        valid forecast diagnostic into ``unknown`` in the status entity.
        """
        snapshot = {
            key: decision_data[key]
            for key in _CHRONOLOGICAL_DIAGNOSTIC_KEYS
            if key in decision_data
        }
        if "energy_deadlines" in snapshot and isinstance(
            snapshot["energy_deadlines"], list
        ):
            snapshot["energy_deadlines"] = [
                dict(item) if isinstance(item, dict) else item
                for item in snapshot["energy_deadlines"]
            ]
        if snapshot:
            self._controller._last_chronological_diagnostics = snapshot

    def _build_chronological_plan(
        self,
        *,
        now: datetime,
        slots: list[PriceSlot],
        decision_data: dict[str, Any],
        price_ceiling: float | None,
        diagnostic_only: bool = False,
    ) -> ChronologicalPlan | None:
        """Build the controller-owned plan, strictly through local midnight."""
        horizon_end = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        return self._build_chronological_plan_for_horizon(
            now=now,
            slots=slots,
            decision_data=decision_data,
            price_ceiling=price_ceiling,
            diagnostic_only=diagnostic_only,
            horizon_end=horizon_end,
            persist_diagnostics=True,
        )

    def _build_chronological_plan_for_horizon(
        self,
        *,
        now: datetime,
        slots: list[PriceSlot],
        decision_data: dict[str, Any],
        price_ceiling: float | None,
        diagnostic_only: bool,
        horizon_end: datetime,
        persist_diagnostics: bool,
    ) -> ChronologicalPlan | None:
        """Adapt live forecasts for a bounded control or display horizon.

        A diagnostic-only build simulates the same horizon without claiming
        that an executable chronological charge calendar is active.  This is
        useful when the balance is already sufficient: the projection is still
        valuable even though no grid charge will be scheduled. Only the
        read-only projection adapter may request a cross-midnight horizon.
        """
        tracker = getattr(self._controller, "_consumption_tracker", None)
        profile = getattr(tracker, "consumption_profile", None)
        if profile is None:
            return None

        daily_horizon_end = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        if horizon_end.tzinfo is None:
            horizon_end = horizon_end.replace(tzinfo=now.tzinfo)
        elif now.tzinfo is None:
            horizon_end = horizon_end.replace(tzinfo=None)
        else:
            horizon_end = horizon_end.astimezone(now.tzinfo)
        is_extended_horizon = horizon_end > daily_horizon_end
        if horizon_end <= now:
            return None
        try:
            solar_profile_mode = normalize_solar_profile_mode(
                getattr(self._controller, "solar_profile_mode", None)
            )
            forecast = tracker.forecast_consumption_between(
                now, horizon_end, fallback="legacy_daily"
            )
            boundaries = build_boundaries(now, horizon_end)
            consumption_raw: list[float] = []
            intervals_by_date = getattr(forecast, "intervals_by_date", None) or {}
            for start, end in boundaries:
                index = start.hour * 4 + start.minute // 15
                # The range API preserves its original one-day contract by
                # aggregating matching wall-clock bins.  A cross-midnight
                # dashboard horizon may contain the same bin on two dates,
                # though, so use the retained date-specific shape whenever it
                # is available.  This also leaves repeated DST bins as two
                # physical intervals of their nominal daily value.
                date_intervals = intervals_by_date.get(
                    start.date(), forecast.intervals_kwh
                )
                bucket = (
                    float(date_intervals[index])
                    if index < len(date_intervals)
                    else 0.0
                )
                fraction = max(
                    0.0,
                    (end.timestamp() - start.timestamp()) / 900.0,
                )
                consumption_raw.append(max(0.0, bucket * fraction))

            # The normal planner receives a remaining-day balance from the
            # controller.  An explicitly extended diagnostic horizon instead
            # needs the profile's energy for the whole requested range;
            # otherwise tomorrow's intervals would merely dilute today's
            # remainder across the extra hours.
            forecast_total = (
                forecast.energy_kwh
                if horizon_end > daily_horizon_end
                else decision_data.get("avg_consumption_kwh", forecast.energy_kwh)
            )
            consumption_total = max(0.0, float(forecast_total or 0.0))
            consumption = normalize_energy_shape(consumption_raw, consumption_total)

            solar_input = self._solar_timeline_input(
                now,
                decision_data,
                horizon_end=horizon_end,
            )
            safety = max(
                0.0,
                float(
                    getattr(
                        self._controller, "_predictive_safety_margin_kwh", 0.0
                    )
                    or 0.0
                ),
            )
            solar_start_dt, solar_end_dt = self._solar_timeline_window(now, tracker)
            solar_profile = getattr(tracker, "solar_profile", None) if tracker is not None else None
            learned_snapshot = None
            if solar_profile is not None and solar_profile_mode != "off":
                try:
                    future_start = future_end = None
                    if solar_start_dt is not None and solar_end_dt is not None:
                        daylight_seconds = solar_end_dt.timestamp() - solar_start_dt.timestamp()
                        if daylight_seconds > 0:
                            future_start = max(
                                0.0,
                                min(
                                    1.0,
                                    (now.timestamp() - solar_start_dt.timestamp())
                                    / daylight_seconds,
                                ),
                            )
                            future_end = max(
                                future_start,
                                min(
                                    1.0,
                                    (horizon_end.timestamp() - solar_start_dt.timestamp())
                                    / daylight_seconds,
                                ),
                            )
                    try:
                        learned_snapshot = solar_profile.get_snapshot(
                            target_date=now.date(),
                            future_progress_start=future_start,
                            future_progress_end=future_end,
                        )
                    except TypeError:
                        # Keep lightweight tracker doubles and older profile
                        # implementations compatible during the rollout.
                        learned_snapshot = solar_profile.get_snapshot(
                            target_date=now.date()
                        )
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("Solar timeline: learned profile unavailable: %s", exc)
            provider_periods = solar_input.periods
            temporal_shape = solar_input.temporal_shape
            if is_extended_horizon and provider_periods:
                # A cross-midnight provider curve may contain a normal overnight
                # gap. Map its dated periods directly onto the exact boundaries;
                # the single-day daylight coverage validator cannot represent
                # two separate solar windows safely.
                timezone = solar_forecast_local_timezone(
                    self._hass,
                    self._controller,
                    now,
                )
                temporal_shape = tuple(
                    solar_forecast_period_energy_between(
                        provider_periods,
                        start,
                        end,
                        timezone=timezone,
                    )
                    for start, end in boundaries
                )
                provider_periods = None

            timeline = build_solar_timeline(
                boundaries,
                solar_input.remaining_kwh,
                safety_margin_kwh=safety,
                provider_periods=provider_periods,
                temporal_shape=temporal_shape,
                learned_shape=(learned_snapshot.shape if learned_snapshot else None),
                learned_mature=bool(learned_snapshot and learned_snapshot.mature),
                solar_start=solar_start_dt,
                solar_end=solar_end_dt,
                mode=solar_profile_mode,
            )
            solar = list(timeline.intervals_kwh)
            solar_source = timeline.source
            intervals = [
                EnergyInterval(start, end, consumption[index], solar[index])
                for index, (start, end) in enumerate(boundaries)
            ]

            eligible = [
                c for c in self._controller.coordinators
                if c.data and not self._controller._is_battery_manual_owned(c)
            ]
            total_capacity = sum(
                float(c.data.get("battery_total_energy", 0) or 0)
                for c in eligible
            )
            weighted_min_soc = (
                sum(
                    float(c.min_soc)
                    * float(c.data.get("battery_total_energy", 0) or 0)
                    for c in eligible
                )
                / total_capacity
                if total_capacity > 0
                else 0.0
            )
            usable = sum(
                max(0.0, (float(c.data.get("battery_soc", 0) or 0) - float(c.min_soc)) / 100.0
                    * float(c.data.get("battery_total_energy", 0) or 0))
                for c in eligible
            )
            headroom = sum(
                max(0.0, (float(c.max_soc) - float(c.data.get("battery_soc", 0) or 0)) / 100.0
                    * float(c.data.get("battery_total_energy", 0) or 0))
                for c in eligible
            )
            deadlines = build_energy_deadlines(intervals, usable)
            required = max(0.0, float(decision_data.get("planned_grid_charge_kwh", 0.0) or 0.0))
            if (
                not diagnostic_only
                and getattr(self._controller, "_predictive_min_soc_floor_enabled", False)
                and solar_start_dt is not None
                and solar_start_dt > now
            ):
                floor = max(
                    0.0,
                    float(
                        getattr(
                            self._controller, "_predictive_min_soc_floor", 0.0
                        )
                        or 0.0
                    ),
                )
                reserve = sum(
                    max(0.0, floor - float(c.min_soc)) / 100.0
                    * float(c.data.get("battery_total_energy", 0) or 0)
                    for c in eligible
                )
                pre_solar = [item for item in intervals if item.end <= solar_start_dt]
                floor_deadlines = build_energy_deadlines(
                    pre_solar,
                    max(0.0, usable - reserve),
                    kind="guaranteed_floor",
                )
                floor_required = max(
                    (item.required_cumulative_kwh for item in floor_deadlines),
                    default=0.0,
                )
                hysteresis_kwh = sum(
                    float(c.data.get("battery_total_energy", 0) or 0)
                    for c in eligible
                ) * FLOOR_HYSTERESIS_PCT / 100.0
                if floor_required > hysteresis_kwh:
                    # The floor requirement dominates ordinary depletion until
                    # sunrise. Preserve later, larger ordinary requirements.
                    combined: list = []
                    maximum = 0.0
                    for item in sorted(
                        deadlines + floor_deadlines,
                        key=lambda value: value.deadline,
                    ):
                        if item.required_cumulative_kwh > maximum + 1e-9:
                            combined.append(item)
                            maximum = item.required_cumulative_kwh
                    deadlines = combined
                    margin_pct = max(
                        0.0,
                        float(
                            getattr(
                                self._controller,
                                "_predictive_grid_charge_margin_pct",
                                0.0,
                            )
                            or 0.0
                        ),
                    )
                    required = max(required, floor_required * (1.0 + margin_pct / 100.0))
                    decision_data["should_charge"] = True
                    decision_data["floor_active"] = True
                    decision_data["energy_deficit_kwh"] = max(
                        float(decision_data.get("energy_deficit_kwh", 0.0) or 0.0),
                        floor_required,
                    )
                    decision_data["planned_grid_charge_kwh"] = min(required, headroom)
                    decision_data["guaranteed_floor_deadline"] = solar_start_dt.isoformat()
            power_kw = min(
                max(0.0, float(self._controller.max_contracted_power)),
                max(0.0, float(self._controller.max_charge_capacity)),
            ) / 1000.0
            evaluation = self.evaluate_chronological_projection(
                ChronologicalEvaluationRequest(
                    now=now,
                    horizon_end=horizon_end,
                    intervals=tuple(intervals),
                    price_slots=tuple(slots),
                    total_required_kwh=required,
                    effective_power_kw=power_kw,
                    headroom_kwh=headroom,
                    usable_initial_kwh=usable,
                    max_price_threshold=price_ceiling,
                    deadlines=tuple(deadlines),
                )
            )
            plan = evaluation.plan
            diagnostics = evaluation.diagnostics
            decision_data.update({
                "chronological_planning_active": not diagnostic_only,
                "chronological_source": forecast.source,
                "solar_timeline_source": solar_source,
                "solar_forecast_original_source": solar_input.original_source,
                "solar_forecast_conversion": solar_input.conversion,
                "solar_remaining_raw_kwh": timeline.remaining_raw_kwh,
                "solar_safety_margin_kwh": timeline.safety_margin_kwh,
                "solar_remaining_effective_kwh": timeline.remaining_effective_kwh,
                "solar_timeline_effective_kwh": timeline.timeline_effective_kwh,
                "solar_timeline_energy_error_kwh": timeline.energy_error_kwh,
                "solar_timeline_fallback_reason": timeline.fallback_reason,
                "solar_profile_mature": bool(learned_snapshot and learned_snapshot.mature),
                "solar_profile_days": learned_snapshot.eligible_days if learned_snapshot else 0,
                "solar_profile_coverage_ratio": learned_snapshot.future_coverage_ratio if learned_snapshot else 0.0,
                "solar_profile_generation": learned_snapshot.generation if learned_snapshot else None,
                "solar_shadow_selected_source": timeline.shadow_selected_source,
                "curtailment_timeline_mismatch": bool(
                    solar_profile_mode == "active"
                    and solar_source != "sinusoidal"
                ),
                "earliest_projected_depletion": (
                    diagnostics.earliest_projected_depletion.isoformat()
                    if diagnostics.earliest_projected_depletion
                    else None
                ),
                "minimum_projected_energy_kwh": round(
                    diagnostics.minimum_projected_energy_kwh, 3
                ),
                "minimum_projected_soc": round(
                    weighted_min_soc
                    + diagnostics.minimum_projected_energy_kwh
                    / total_capacity
                    * 100.0,
                    2,
                )
                if total_capacity > 0
                else None,
                "deadline_required_kwh": round(diagnostics.deadline_required_kwh, 3),
                "flexible_required_kwh": round(diagnostics.flexible_required_kwh, 3),
                "deadline_shortfall_kwh": round(diagnostics.deadline_shortfall_kwh, 3),
                "total_shortfall_kwh": round(diagnostics.total_shortfall_kwh, 3),
                "chronological_plan_reason": diagnostics.reason,
                "energy_deadlines": [
                    {
                        "deadline": item.deadline.isoformat(),
                        "required_kwh": round(item.required_cumulative_kwh, 3),
                        "kind": item.kind,
                    }
                    for item in diagnostics.energy_deadlines
                ],
            })
            # The dashboard's explicit cross-midnight simulation is a view,
            # not a new control decision. Retaining its expanded solar budget
            # would make the next refresh add tomorrow's periods a second time
            # and would leak a multi-day total into predictive diagnostics.
            if persist_diagnostics and not is_extended_horizon:
                self._store_chronological_diagnostics(decision_data)
            return plan
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            _LOGGER.warning(
                "Dynamic pricing: chronological planning fallback: %s",
                exc,
                exc_info=True,
            )
            decision_data.update({
                "chronological_planning_active": False,
                "chronological_plan_reason": f"fallback: {exc}",
            })
            return None

    def _time_slot_price_slots(self, now: datetime) -> list[PriceSlot]:
        """Materialize today's configured predictive windows as price slots.

        Time Slot has no price ranking, so every window receives the same
        synthetic price. Splitting overnight windows at midnight keeps the
        energy horizon strict while retaining the currently active portion.
        """
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        horizon_end = midnight + timedelta(days=1)
        return self._time_slot_price_slots_for_horizon(now, horizon_end)

    def _time_slot_price_slots_for_horizon(
        self,
        now: datetime,
        horizon_end: datetime,
    ) -> list[PriceSlot]:
        """Materialize configured Time Slot windows within a preview horizon.

        This is for diagnostic/dashboard projections only.  Runtime control
        must continue to call :meth:`_time_slot_price_slots`, whose horizon is
        strictly the current local day.  Each calendar date is evaluated with
        the same ``days`` semantics as the existing helper, so an overnight
        window is represented by its two local-day portions.
        """
        if horizon_end.tzinfo is None and now.tzinfo is not None:
            horizon_end = horizon_end.replace(tzinfo=now.tzinfo)
        elif now.tzinfo is None and horizon_end.tzinfo is not None:
            horizon_end = horizon_end.replace(tzinfo=None)
        elif now.tzinfo is not None:
            horizon_end = horizon_end.astimezone(now.tzinfo)
        if horizon_end <= now:
            return []

        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        result: list[PriceSlot] = []
        current_day = midnight
        while current_day < horizon_end:
            day_name = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][
                current_day.weekday()
            ]
            next_midnight = current_day + timedelta(days=1)
            for configured in getattr(self._controller, "charging_time_slots", []):
                if not isinstance(configured, dict):
                    continue
                if configured.get("enabled", True) is False:
                    continue
                days = configured.get("days", []) or []
                if days and day_name not in days:
                    continue
                try:
                    start_time = datetime.strptime(
                        str(configured["start_time"]), "%H:%M"
                    ).time()
                    end_time = datetime.strptime(
                        str(configured["end_time"]), "%H:%M"
                    ).time()
                except (KeyError, TypeError, ValueError):
                    continue
                start = datetime.combine(
                    current_day.date(), start_time, tzinfo=now.tzinfo
                )
                end = datetime.combine(
                    current_day.date(), end_time, tzinfo=now.tzinfo
                )
                pieces = (
                    [(start, end)]
                    if start_time <= end_time
                    else [(current_day, end), (start, next_midnight)]
                )
                result.extend(
                    PriceSlot(piece_start, min(piece_end, horizon_end), 0.0)
                    for piece_start, piece_end in pieces
                    if piece_end > now
                    and piece_start < horizon_end
                    and piece_end > piece_start
                )
            current_day = next_midnight
        return sorted(set(result), key=lambda slot: (slot.start, slot.end))

    def _apply_time_slot_chronological_plan(
        self,
        decision_data: dict[str, Any],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        """Apply the active configured-window quota to a Time Slot decision."""
        controller = self._controller
        controller._active_time_slot_quota_kwh = None
        candidates = self._time_slot_price_slots(now)
        if not candidates:
            return decision_data
        plan = self._build_chronological_plan(
            now=now,
            slots=candidates,
            decision_data=decision_data,
            price_ceiling=None,
        )
        if plan is None:
            return decision_data

        controller._time_slot_chronological_plan = plan
        quota = sum(
            allocation.planned_battery_kwh
            for allocation in plan.allocations
            if allocation.slot.start <= now < allocation.slot.end
        )
        aggregate_should_charge = bool(decision_data.get("should_charge", False))
        decision_data["aggregate_should_charge"] = aggregate_should_charge
        decision_data["time_slot_chronological_active"] = True
        decision_data["active_slot_energy_target_kwh"] = round(quota, 3)
        decision_data["should_charge"] = quota > 1e-6
        if quota > 1e-6:
            controller._active_time_slot_quota_kwh = quota
        elif aggregate_should_charge:
            decision_data["chronological_deferred"] = True
            decision_data["reason"] = (
                "Energy is assigned to another configured window or cannot be "
                "delivered before its deadline"
            )
        return decision_data

    async def _ensure_time_slot_chronological_preview(
        self, *, now: datetime
    ) -> None:
        """Build today's Time Slot balance and any remaining-window plan."""
        controller = self._controller
        if getattr(controller, "_time_slot_chronological_preview_date", None) == now.date():
            return
        evaluation_start = now.replace(
            hour=0, minute=5, second=0, microsecond=0
        )
        # The balance diagnostics remain useful after the final configured
        # window.  In particular, an integration reload must not leave the
        # predictive status entity with only timeline fields and ``unknown``
        # SOC/consumption/deficit attributes until tomorrow.  The planner below
        # already treats an empty candidate list as a balance-only evaluation.
        if now < evaluation_start:
            return
        forecast_configured = bool(
            get_configured_solar_forecast_sensor(controller, "remaining")
            or get_configured_solar_forecast_sensor(controller, "today")
        )
        if (
            forecast_configured
            and read_solar_forecast_kwh(self._hass, controller) is None
        ):
            return
        decision_data = await self._current_horizon_grid_charging_decision()
        decision_data = self._apply_time_slot_chronological_plan(
            decision_data,
            now=now,
        )
        controller._last_decision_data = decision_data
        controller._time_slot_chronological_preview_date = now.date()
        if float(decision_data.get("deadline_shortfall_kwh", 0.0) or 0.0) > 0:
            await self._send_predictive_charging_notification(
                decision_data=decision_data
            )

    async def _evaluate_dynamic_pricing(
        self,
        *,
        horizon: DynamicPricingEvaluationHorizon,
        extended_horizon: bool = False,
    ) -> None:
        """Build a dynamic-pricing calendar for an explicit energy horizon.

        ``DAILY`` is reserved for the scheduled 00:05 evaluation.  All later
        reconstructions pass ``REMAINING`` so the balance uses only consumption
        and solar still expected before midnight.
        """
        if not isinstance(horizon, DynamicPricingEvaluationHorizon):
            raise ValueError("Dynamic pricing evaluation requires an explicit horizon")

        now = datetime.now()
        today = now.date()

        # A new full-day evaluation starts a fresh diagnostic snapshot.  Later
        # balance-only re-evaluations intentionally leave it intact.
        if horizon is DynamicPricingEvaluationHorizon.DAILY:
            self._controller._last_chronological_diagnostics = None

        _LOGGER.info(
            "Dynamic pricing: running %s-horizon evaluation at %s",
            horizon.value,
            now.strftime("%H:%M"),
        )

        # Cleared up front: the early returns below can still send a notification,
        # and a ceiling from a previous evaluation must not be reported as today's.
        self._controller._dp_arbitrage_ceiling = None
        # A failed or interrupted reevaluation must never leave yesterday's
        # negative setpoint/block active.  The new plan is rebuilt below.
        if self._smart_predischarge_enabled():
            self.clear_curtailment_runtime("reevaluation")

        # Ensure service-based provider slots are current before evaluating.
        await self._maybe_refresh_service_prices(force=True)

        # Step 1: Energy balance.  The full-day forecast is valid only for the
        # scheduled 00:05 run.  Any later reconstruction starts from the live
        # remainder, including already-produced solar.
        if (
            horizon is DynamicPricingEvaluationHorizon.DAILY
            and not get_configured_solar_forecast_sensor(
                self._controller, "remaining"
            )
        ):
            decision_data = await self._controller._should_activate_grid_charging()
        else:
            decision_data = await self._evaluate_remaining_grid_charging(now=now)

        # Keep the first full-day forecast separate from the live remaining
        # horizon used by later reevaluations. With a configured remaining-today
        # sensor, its value at 00:05 is the full-day forecast; the legacy today
        # path already returns that same full-day quantity.
        if horizon is DynamicPricingEvaluationHorizon.DAILY:
            tracker = getattr(self._controller, "_consumption_tracker", None)
            capture = getattr(tracker, "capture_daily_solar_forecast", None)
            if callable(capture):
                capture(decision_data.get("solar_forecast_kwh"))
        self._controller._last_decision_data = decision_data
        # Reference SOC for the SOC-drop re-evaluation (#411): this is read before
        # the overnight discharge, so a battery that drains far below it must be
        # able to re-plan upward in time for the cheap midday slots.
        self._controller._dp_last_eval_soc = decision_data.get("avg_soc")
        deficit_charging_needed = bool(decision_data["should_charge"])

        # Step 2: Parse price data (always, even without deficit — for diagnostics)
        if extended_horizon:
            end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
            price_horizon = max(end_of_day, now + timedelta(hours=12))
        else:
            price_horizon = None
        slots = self._parse_price_data(horizon_end=price_horizon)
        if slots:
            self._controller._dp_daily_avg_price = sum(s.price for s in slots) / len(slots)
            _LOGGER.debug("Dynamic pricing: daily average price %.4f from %d slots", self._controller._dp_daily_avg_price, len(slots))
        if not slots:
            # Price data is not required to calculate the projected solar and
            # depletion diagnostics.  Preserve those diagnostics even when the
            # evaluation has no executable calendar to build.
            self._build_chronological_plan(
                now=now,
                slots=[],
                decision_data=decision_data,
                price_ceiling=None,
                diagnostic_only=True,
            )
            self._build_curtailment_plan([], [], now=now)
            opportunity_pending = bool(
                self._negative_price_feature_enabled()
                and self._opportunistic_target_pending()
            )
            if not deficit_charging_needed and not opportunity_pending:
                # No deficit + no price data: nothing to evaluate
                self._controller._dynamic_pricing_schedule = None
                self._controller._dynamic_pricing_evaluated_date = today
                self._controller._dp_eval_retry_count = 0
                _LOGGER.info("Dynamic pricing: no charging needed and no price data available")
                await self._send_dynamic_pricing_notification(decision_data=decision_data, schedule=None)
                return
            # A deficit or pending opportunistic target needs prices: retry.
            self._controller._dp_eval_retry_count += 1
            _LOGGER.warning(
                "Dynamic pricing: no price data available at 00:05 (retry %d/4)",
                self._controller._dp_eval_retry_count
            )
            return  # Will retry up to 4 times (~30 min intervals via control loop)

        # Step 3: Build the two independent candidate calendars.  Deficit
        # selection keeps the established price ceiling/arbitrage behaviour.
        # Opportunistic selection only considers strictly negative import prices
        # and always takes the most-negative individual intervals first.
        deficit_kwh = decision_data["energy_deficit_kwh"]
        if deficit_charging_needed:
            planned_charge_kwh = decision_data.get("planned_grid_charge_kwh", deficit_kwh)
            deficit_hours_needed = calculations.calculate_charging_hours_needed(
                planned_charge_kwh,
                self._controller.max_contracted_power,
                self._controller.max_charge_capacity,
            )
        else:
            # No deficit — use daily consumption as reference so the number of
            # selected hours is meaningful (same basis the algorithm uses to decide)
            deficit_hours_needed = calculations.calculate_charging_hours_needed(
                decision_data["avg_consumption_kwh"], self._controller.max_contracted_power, self._controller.max_charge_capacity
            )
        # One instant, one computation. `ceiling` is what actually filters;
        # `arb_ceiling` is kept only so the notification can name the cause.
        eval_now = datetime.now()
        ceiling, arb_ceiling = calculations.effective_charge_ceiling(
            slots,
            deficit_hours_needed,
            self._controller.max_price_threshold,
            self._controller.min_arbitrage_margin,
            self._controller.round_trip_efficiency,
            now=eval_now,
        )
        self._controller._dp_arbitrage_ceiling = arb_ceiling
        # Do not add the legacy informational cheap-hour calendar to an active
        # opportunity-only schedule: those slots are not a deficit and must not
        # make the calendar look combined or become executable by accident.
        negative_price_energy_kwh = (
            self._negative_price_energy_needed_kwh()
            if self._negative_price_feature_enabled()
            else 0.0
        )
        negative_price_hours_needed = calculations.calculate_exact_charging_hours_needed(
            negative_price_energy_kwh,
            self._controller.max_contracted_power,
            self._controller.max_charge_capacity,
        )
        negative_price_selected = calculations.select_cheapest_slots_by_duration(
            [slot for slot in slots if math.isfinite(slot.price) and slot.price < 0.0],
            negative_price_hours_needed,
            None,
            now=eval_now,
        )
        opportunity_selected = bool(negative_price_selected)

        chronological_plan = None
        floor_enabled = getattr(
            self._controller, "_predictive_min_soc_floor_enabled", False
        )
        if deficit_charging_needed or floor_enabled:
            chronological_plan = self._build_chronological_plan(
                now=eval_now,
                slots=slots,
                decision_data=decision_data,
                price_ceiling=ceiling,
            )
            if (
                chronological_plan is not None
                and decision_data.get("floor_active", False)
            ):
                # Guaranteed-floor energy bypasses the optimization-only
                # arbitrage margin, while the user's explicit ceiling remains
                # authoritative.
                chronological_plan = self._build_chronological_plan(
                    now=eval_now,
                    slots=slots,
                    decision_data=decision_data,
                    price_ceiling=self._controller.max_price_threshold,
                )
            deficit_charging_needed = bool(decision_data.get("should_charge", False))
            if deficit_charging_needed and chronological_plan is not None:
                deficit_kwh = float(
                    decision_data.get("energy_deficit_kwh", deficit_kwh) or 0.0
                )
                deficit_hours_needed = calculations.calculate_exact_charging_hours_needed(
                    chronological_plan.total_required_kwh,
                    self._controller.max_contracted_power,
                    self._controller.max_charge_capacity,
                )
        else:
            # A sufficient balance still deserves a forecast/timeline
            # projection.  Keep it diagnostic-only so the final schedule keeps
            # its existing no-charge semantics.
            self._build_chronological_plan(
                now=eval_now,
                slots=slots,
                decision_data=decision_data,
                price_ceiling=ceiling,
                diagnostic_only=True,
            )
        if chronological_plan is not None:
            deficit_selected = [allocation.slot for allocation in chronological_plan.allocations]
        elif deficit_charging_needed or not opportunity_selected:
            deficit_selected = calculations.select_cheapest_hours(
                slots, deficit_hours_needed, ceiling, now=eval_now
            )
        else:
            deficit_selected = []

        def _combine_selected() -> tuple[list[PriceSlot], dict[PriceSlot, str]]:
            purposes: dict[PriceSlot, str] = {}
            for slot in deficit_selected:
                purposes[slot] = self._merge_slot_purpose(
                    purposes.get(slot), SLOT_PURPOSE_DEFICIT
                )
            for slot in negative_price_selected:
                purposes[slot] = self._merge_slot_purpose(
                    purposes.get(slot), SLOT_PURPOSE_NEGATIVE_PRICE
                )
            return sorted(purposes, key=lambda slot: slot.start), purposes

        selected, slot_purposes = _combine_selected()

        if not selected:
            self._build_curtailment_plan(slots, [], now=now)
            self._controller._dynamic_pricing_schedule = None
            self._controller._dynamic_pricing_evaluated_date = today
            self._controller._dp_eval_retry_count = 0
            _LOGGER.warning("Dynamic pricing: no slots selected (all above thresholds?)")
            await self._send_dynamic_pricing_notification(decision_data=decision_data, schedule=None)
            return

        # A negative-price solar-surplus slot remains protected from discharge,
        # but it is still a valid import opportunity when spare headroom exists.
        # Build once with the tentative schedule, remove ordinary deficit
        # conflicts, then build the final plan with charge slots reserved.
        # Risk detection is independent of reservations in the curtailment
        # planner. Pass the tentative charge calendar so its pre-discharge
        # candidates are coherent, then use the reported risk windows to remove
        # opportunistic overlap below.
        tentative_plan = self._build_curtailment_plan(slots, selected, now=now)
        # Guaranteed-minimum SOC is the safety exception: if that floor is the
        # reason for grid charging, keep its selected slot even when it overlaps
        # a risk window.  The risk guard still prevents battery discharge.
        if tentative_plan.risk_slots:
            def _safe(slot: PriceSlot) -> bool:
                return not any(
                    slot.start < risk.end and risk.start < slot.end
                    for risk in tentative_plan.risk_slots
                )

            safe_slots = [slot for slot in slots if _safe(slot)]
            old_selected_count = len(selected)

            # A real planner carries per-risk reserve accounting, so retain its
            # negative slots in the schedule and let the live SOC/reserve gate
            # decide how much can actually flow.  Legacy hand-built plans have
            # no accounting map; keep their old conservative filtering.
            retain_risk_opportunity = bool(
                getattr(tentative_plan, "solar_reserve_by_slot", {})
                or self._curtailment_opportunistic_space(tentative_plan) > 1e-6
            )
            negative_candidates = (
                [slot for slot in slots if math.isfinite(slot.price) and slot.price < 0.0]
                if retain_risk_opportunity
                else [
                    slot
                    for slot in safe_slots
                    if math.isfinite(slot.price) and slot.price < 0.0
                ]
            )
            # Opportunistic energy is never allowed to consume reserved solar
            # headroom.  In a guaranteed-floor slot only the necessary deficit
            # purpose keeps the protected interval.
            negative_price_selected = calculations.select_cheapest_slots_by_duration(
                negative_candidates
                if not decision_data.get("floor_active", False)
                else [slot for slot in negative_candidates if _safe(slot)],
                negative_price_hours_needed,
                None,
                now=eval_now,
            )
            if (
                deficit_charging_needed
                and not decision_data.get("floor_active", False)
            ):
                # Ordinary deficit charging also avoids the risk window, while
                # the existing guaranteed-floor safety exception may keep it.
                deficit_selected = calculations.select_cheapest_hours(
                    safe_slots, deficit_hours_needed, ceiling, now=eval_now
                )
                if chronological_plan is not None:
                    chronological_plan = self._build_chronological_plan(
                        now=eval_now,
                        slots=safe_slots,
                        decision_data=decision_data,
                        price_ceiling=ceiling,
                    )
                    if chronological_plan is not None:
                        deficit_selected = [
                            allocation.slot for allocation in chronological_plan.allocations
                        ]
            elif not deficit_charging_needed and opportunity_selected:
                # The legacy no-deficit calendar is informational only.  Do not
                # resurrect it while relocating a real opportunity, or the safe
                # negative slot would be mislabeled ``combined`` and gain a
                # fictitious deficit purpose.
                deficit_selected = []
            selected, slot_purposes = _combine_selected()
            if len(selected) != old_selected_count:
                _LOGGER.info(
                    "Dynamic pricing: adjusted %d grid-charge slots around protected solar windows",
                    abs(old_selected_count - len(selected)),
                )
            if not selected:
                self._build_curtailment_plan(slots, [], now=now)
                self._controller._dynamic_pricing_schedule = None
                self._controller._dynamic_pricing_evaluated_date = today
                self._controller._dp_eval_retry_count = 0
                await self._send_dynamic_pricing_notification(
                    decision_data=decision_data, schedule=None
                )
                return
            self._build_curtailment_plan(
                slots,
                [slot for slot in selected if _safe(slot)],
                now=now,
            )
        else:
            # Rebuild with the final charge reservations so pre-discharge never
            # competes with a legitimate selected interval.
            self._build_curtailment_plan(slots, selected, now=now)

        # Step 4: Build schedule
        avg_price = sum(s.price for s in selected) / len(selected)
        effective_power_kw = min(self._controller.max_contracted_power, self._controller.max_charge_capacity) / 1000.0
        selected_hours = sum(
            max(0.0, (slot.end - slot.start).total_seconds() / 3600.0)
            for slot in selected
        )
        estimated_cost = sum(
            slot.price
            * effective_power_kw
            * max(0.0, (slot.end - slot.start).total_seconds() / 3600.0)
            for slot in selected
        )
        opportunity_selected = bool(negative_price_selected)
        charging_needed = deficit_charging_needed or opportunity_selected
        schedule_type = self._schedule_type_from_purposes(slot_purposes.values())

        schedule = DynamicPricingSchedule(
            hours_needed=selected_hours,
            selected_slots=selected,
            average_price=avg_price,
            estimated_cost=estimated_cost,
            total_available_slots=len(slots),
            evaluation_time=now,
            energy_deficit_kwh=deficit_kwh,
            charging_needed=charging_needed,
            slot_purposes=slot_purposes,
            schedule_type=schedule_type,
            deficit_charging_needed=deficit_charging_needed,
            negative_price_charging_needed=opportunity_selected,
            deficit_hours_needed=(deficit_hours_needed if deficit_charging_needed else 0.0),
            negative_price_hours_needed=(
                negative_price_hours_needed if opportunity_selected else 0.0
            ),
            negative_price_energy_kwh=(
                negative_price_energy_kwh if opportunity_selected else 0.0
            ),
            slot_energy_targets_kwh=(
                {
                    allocation.slot: allocation.planned_battery_kwh
                    for allocation in chronological_plan.allocations
                }
                if chronological_plan is not None
                else {}
            ),
            slot_deadlines=(
                {
                    allocation.slot: allocation.deadline
                    for allocation in chronological_plan.allocations
                }
                if chronological_plan is not None
                else {}
            ),
            slot_plan_kinds=(
                {
                    allocation.slot: allocation.kind
                    for allocation in chronological_plan.allocations
                }
                if chronological_plan is not None
                else {}
            ),
            chronological_planning_active=chronological_plan is not None,
            chronological_source=decision_data.get("chronological_source"),
            solar_timeline_source=decision_data.get("solar_timeline_source"),
            earliest_depletion_at=(
                chronological_plan.earliest_depletion_at
                if chronological_plan
                else None
            ),
            deadline_required_kwh=(
                chronological_plan.deadline_required_kwh
                if chronological_plan
                else 0.0
            ),
            flexible_required_kwh=(
                chronological_plan.flexible_required_kwh
                if chronological_plan
                else 0.0
            ),
            deadline_shortfall_kwh=(
                chronological_plan.deadline_shortfall_kwh
                if chronological_plan
                else 0.0
            ),
            total_shortfall_kwh=(
                chronological_plan.total_shortfall_kwh
                if chronological_plan
                else 0.0
            ),
            energy_deadlines=(chronological_plan.deadlines if chronological_plan else []),
            chronological_plan_reason=(
                chronological_plan.reason
                if chronological_plan
                else decision_data.get("chronological_plan_reason")
            ),
        )
        self._controller._dynamic_pricing_schedule = schedule
        self._controller._dp_pre_evaluated_slots = {}
        self._controller._dp_pre_evaluated_purposes = {}
        self._controller._dp_completed_slots = set()
        self._controller._active_dynamic_slot_purpose = None
        # Use the date of the selected slots (tomorrow at eval time) so the midnight
        # reset only fires the day AFTER the slots — not before they can be used.
        slots_date = max(s.start.date() for s in selected) if selected else (now.date() + timedelta(days=1))
        self._controller._dynamic_pricing_evaluated_date = slots_date
        self._controller._dp_eval_retry_count = 0

        _LOGGER.info(
            "Dynamic pricing: evaluation complete — %d slots selected, %.2fh, avg=%.3f %s, type=%s, charging_needed=%s",
            len(selected), selected_hours, avg_price, self._get_price_unit(), schedule_type, charging_needed
        )
        await self._send_dynamic_pricing_notification(decision_data=decision_data, schedule=schedule)

    async def _send_dynamic_pricing_notification(
        self,
        decision_data: dict,
        schedule: Optional[DynamicPricingSchedule]
    ) -> None:
        """Send persistent notification for dynamic pricing evaluation."""
        title, message = notifications.format_dynamic_pricing_notification(
            decision_data,
            schedule,
            unit=self._get_price_unit(),
            max_price_threshold=self._controller.max_price_threshold,
            discharge_price_threshold=self._controller.discharge_price_threshold,
            arbitrage_ceiling=self._controller._dp_arbitrage_ceiling,
            max_contracted_power=self._controller.max_contracted_power,
            max_charge_capacity=self._controller.max_charge_capacity,
        )
        await self._hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": title,
                "message": message,
                "notification_id": f"{NOTIFICATION_ID_PREFIX}predictive_charging_evaluation",
            },
        )

    async def _send_dynamic_pricing_slot_start_notification(self, slot: PriceSlot) -> None:
        """Send notification when a cheap pricing slot starts."""
        schedule = self._controller._dynamic_pricing_schedule
        if not schedule:
            return

        title, message = notifications.format_slot_start_notification(
            slot,
            schedule,
            unit=self._get_price_unit(),
            max_contracted_power=self._controller.max_contracted_power,
        )
        await self._hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": title,
                "message": message,
                "notification_id": f"{NOTIFICATION_ID_PREFIX}predictive_charging_evaluation",
            },
        )

    async def _check_dp_pre_slot_reevaluation(self) -> None:
        """Re-evaluate each typed purpose one hour before an upcoming slot.

        If the system already charged in an earlier slot and the battery is now
        sufficiently charged (solar + current SOC covers consumption), marks the
        next slot as skippable so it does not activate unnecessarily.
        Called every 2.5 s from the dynamic pricing control loop handler.
        """
        if not self._controller._dynamic_pricing_schedule or not self._controller._dynamic_pricing_schedule.charging_needed:
            return

        now = datetime.now()
        upcoming = [s for s in self._controller._dynamic_pricing_schedule.selected_slots if s.start > now]
        if not upcoming:
            return  # No future slots left

        next_slot = upcoming[0]

        # Only act during the ±5-minute window that is exactly 1 hour before the slot
        pre_eval_time = next_slot.start - timedelta(hours=1)
        if abs((now - pre_eval_time).total_seconds()) > 5 * 60:
            return

        # Already evaluated this slot → nothing to do
        if next_slot.start in self._controller._dp_pre_evaluated_slots:
            return

        # Skip re-evaluation if we're currently charging — the battery hasn't
        # benefited from the ongoing charge yet, so the result would be the same
        # as the original 00:05 evaluation (misleading and noisy).
        # This covers back-to-back slots where the pre-eval window of slot B
        # coincides with the active charging window of slot A.
        if self._controller._current_price_slot_active:
            return

        _LOGGER.info(
            "Dynamic pricing: running pre-slot re-evaluation for slot at %s",
            next_slot.start.strftime("%H:%M")
        )
        schedule = self._controller._dynamic_pricing_schedule
        purpose = (
            schedule.purpose_for(next_slot)
            if hasattr(schedule, "purpose_for")
            else SLOT_PURPOSE_DEFICIT
        )
        has_deficit = purpose in {SLOT_PURPOSE_DEFICIT, SLOT_PURPOSE_COMBINED}
        has_opportunity = purpose in {
            SLOT_PURPOSE_NEGATIVE_PRICE,
            SLOT_PURPOSE_COMBINED,
        }

        # Do not turn a temporarily full solar reserve into a permanent
        # ``purpose=None`` decision.  A later under-production update may free
        # space during the risk slot, so the live gate must retain authority.
        curtailment_plan = getattr(self._controller, "_curtailment_plan", None)
        if (
            has_opportunity
            and curtailment_plan is not None
            and self._slot_overlaps_curtailment_risk(next_slot)
            and getattr(curtailment_plan, "solar_reserve_by_slot", {})
            and self._curtailment_opportunistic_space(curtailment_plan) <= 1e-6
        ):
            return

        decision = None
        deficit_needed = False
        if has_deficit and bool(
            getattr(schedule, "deficit_charging_needed", schedule.charging_needed)
        ):
            decision = await self._evaluate_remaining_grid_charging(now=now)
            self._controller._last_decision_data = decision
            deficit_needed = bool(decision["should_charge"])
            if (
                getattr(schedule, "chronological_planning_active", False)
                and getattr(schedule, "slot_deadlines", {}).get(next_slot)
                is not None
            ):
                # A positive aggregate balance can be caused by solar arriving
                # after this deadline. It is not evidence that the urgent slot
                # became unnecessary.
                deficit_needed = True

        opportunity_needed = False
        if has_opportunity and self._negative_price_feature_enabled():
            plan = getattr(self._controller, "_curtailment_plan", None)
            opportunity_needed = bool(
                math.isfinite(next_slot.price)
                and next_slot.price < 0.0
                and self._opportunistic_target_pending()
                and (
                    not self._slot_overlaps_curtailment_risk(next_slot)
                    or (
                        plan is not None
                        and self._curtailment_opportunistic_space(plan) > 1e-6
                    )
                )
            )

        if deficit_needed and opportunity_needed:
            effective_purpose = SLOT_PURPOSE_COMBINED
        elif opportunity_needed:
            effective_purpose = SLOT_PURPOSE_NEGATIVE_PRICE
        elif deficit_needed:
            effective_purpose = SLOT_PURPOSE_DEFICIT
        else:
            effective_purpose = None

        should_charge = effective_purpose is not None
        self._controller._dp_pre_evaluated_slots[next_slot.start] = should_charge
        if not hasattr(self._controller, "_dp_pre_evaluated_purposes"):
            self._controller._dp_pre_evaluated_purposes = {}
        self._controller._dp_pre_evaluated_purposes[next_slot.start] = effective_purpose

        # Reaching the opportunity target after an earlier slot removes every
        # remaining opportunity instead of allowing another slot to fill toward
        # max_soc. Combined slots retain only their deficit purpose.
        if has_opportunity and not self._opportunistic_target_pending():
            self._prune_completed_opportunities()

        if deficit_needed and decision is not None:
            await self._send_dp_pre_slot_reevaluation_notification(next_slot, decision)

    async def _send_dp_pre_slot_reevaluation_notification(
        self, slot: PriceSlot, decision: dict
    ) -> None:
        """Send notification when a pre-slot re-evaluation confirms charging is still needed.

        Only called when should_charge=True. Skipped slots are logged silently.
        """
        title, message = notifications.format_dp_pre_slot_reevaluation_notification(
            slot,
            decision,
            unit=self._get_price_unit(),
        )
        await self._hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": title,
                "message": message,
                "notification_id": f"{NOTIFICATION_ID_PREFIX}predictive_charging_evaluation",
            },
        )

    def _is_evening_reevaluation_time(self) -> bool:
        """Return True when it's time for the late-day battery re-evaluation.

        Triggers once per day either:
        - 1.5 h before estimated T_end (when solar T_start was detected), or
        - at EVENING_REEVAL_FALLBACK_HOUR (16:00) when no T_start was seen today.

        Does not trigger after 23:00 to avoid clashing with the 00:05 evaluation.
        """
        from datetime import datetime
        now = datetime.now()

        if self._controller._dp_evening_reevaluated_date == now.date():
            return False

        now_h = now.hour + now.minute / 60.0
        if now_h >= 23.0:
            return False

        if self._controller._solar_t_start is not None:
            trigger_h = self._controller._consumption_tracker.estimate_t_end() - EVENING_REEVAL_HOURS_BEFORE_TEND
        else:
            trigger_h = EVENING_REEVAL_FALLBACK_HOUR

        return now_h >= trigger_h

    def _is_dp_soc_drop_reeval(self) -> bool:
        """Return True when live SOC has fallen ≥ threshold below the last DP eval.

        Mirrors the time-slot handler's SOC-drop re-evaluation (#411): the 00:05
        energy-balance read happens before the overnight discharge, so a battery
        that drains far below the evaluated level must be able to re-plan upward
        in time for the cheap midday slots — not just at the late-day evening
        pass. Directional (only drops trigger); debounced by resetting
        ``_dp_last_eval_soc`` on each re-eval, so it re-arms only after another
        drop. ``None`` reference (before the 00:05 eval) never triggers.
        """
        ref = self._controller._dp_last_eval_soc
        if ref is None:
            return False
        coords = [
            c for c in self._controller.coordinators
            if c.data and not getattr(c, "battery_manual_mode_enabled", False)
        ]
        if not coords:
            return False
        current = sum(c.data.get("battery_soc", 0) for c in coords) / len(coords)
        schedule = getattr(self._controller, "_dynamic_pricing_schedule", None)
        threshold = (
            5.0
            if schedule is not None
            and getattr(schedule, "chronological_planning_active", False)
            else SOC_REEVALUATION_THRESHOLD
        )
        return (ref - current) >= threshold

    @staticmethod
    def _get_consumed_today_kwh(controller, now: datetime) -> tuple[float, bool, str]:
        """Return today's full-day home consumption for remaining forecasts.

        ``_daily_home_energy_kwh`` is the user-facing total since midnight and
        is the preferred value to subtract from the daily forecast. The adjusted
        ``_household_energy_accumulator`` now covers the same full day and remains
        the startup/reload fallback.
        """
        sources = (
            (
                "daily_home_energy",
                "_daily_home_energy_date",
                "_daily_home_energy_kwh",
            ),
            (
                "household_full_day",
                "_household_accumulator_date",
                "_household_energy_accumulator",
            ),
        )
        for source, date_attr, value_attr in sources:
            if getattr(controller, date_attr, None) != now.date():
                continue
            try:
                value = float(getattr(controller, value_attr, 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0.0:
                return value, True, source
        return 0.0, False, "none"

    def _profile_remaining_consumption(
        self,
        start: datetime,
        end: datetime,
    ):
        """Return the learned profile or its explicit daily fallback."""
        tracker = getattr(self._controller, "_consumption_tracker", None)
        profile = getattr(tracker, "consumption_profile", None)
        if profile is None or end <= start:
            return None
        try:
            if start.tzinfo is None:
                current = getattr(profile, "_timezone", lambda: None)()
                start = start.replace(tzinfo=current)
            if end.tzinfo is None:
                end = end.replace(tzinfo=start.tzinfo)
            forecast = tracker.forecast_consumption_between(
                start, end, fallback="legacy_daily"
            )
            if forecast.source in {"profile", "legacy_daily", "vacation_baseline"}:
                return forecast
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Pricing: profile forecast failed: %s", exc)
        return None

    async def _evaluate_remaining_grid_charging(self, *, now: datetime | None = None) -> dict:
        """Evaluate the energy still needed before the end of today's horizon.

        The scheduled 00:05 evaluation intentionally uses the complete daily
        consumption and solar forecasts.  Every later calendar reconstruction
        and the live pre-slot gate call this path so energy already consumed or
        produced is never counted a second time.
        """
        controller = self._controller
        tracker = getattr(controller, "_consumption_tracker", None)
        get_average = getattr(tracker, "get_dynamic_base_consumption", None)
        if tracker is None or not callable(get_average):
            # A partially initialised entry cannot calculate a trustworthy
            # remaining horizon.  Use a zero-consumption, zero-solar override
            # rather than falling back to the full-day balance and silently
            # double-counting today's energy.
            return await controller._should_activate_grid_charging(
                consumption_override_kwh=0.0,
                solar_forecast_override_kwh=0.0,
            )

        now = now or datetime.now()
        now_h = now.hour + now.minute / 60.0 + now.second / 3600.0
        avg_daily_kwh = await get_average()
        consumed_today_kwh, accumulator_ready, consumption_source = (
            self._get_consumed_today_kwh(controller, now)
        )
        get_window_hours = getattr(
            tracker, "get_consumption_window_hours_per_day", None
        )
        get_remaining_window_hours = getattr(
            tracker, "consumption_window_hours_in_range", None
        )
        if consumption_source == "daily_home_energy":
            # The live source is a full-day counter, so keep the projection on
            # the same 24-hour basis instead of applying a battery-window
            # profile to it.
            window_hours_per_day = 24.0
            remaining_window_hours = 24.0 - now_h
        else:
            window_hours_per_day = (
                get_window_hours() if callable(get_window_hours) else 24.0
            )
            remaining_window_hours = (
                get_remaining_window_hours(now_h, 24.0)
                if callable(get_remaining_window_hours)
                else 24.0 - now_h
            )
        profile_forecast = None
        local_now = now
        end_of_day = local_now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ) + timedelta(days=1)
        profile_forecast = self._profile_remaining_consumption(local_now, end_of_day)
        if profile_forecast is not None:
            remaining_consumption_kwh = profile_forecast.energy_kwh
            fallback_correction_kwh = 0.0
            if profile_forecast.source == "legacy_daily" and accumulator_ready:
                (
                    remaining_consumption_kwh,
                    fallback_correction_kwh,
                ) = adjust_remaining_fallback_energy(
                    remaining_consumption_kwh,
                    avg_daily_kwh,
                    consumed_today_kwh,
                    now_h,
                )
            consumption_rate_kwh_h = (
                remaining_consumption_kwh / remaining_window_hours
                if remaining_window_hours > 0
                else 0.0
            )
            consumption_scope = (
                "remaining_profile"
                if profile_forecast.source == "profile"
                else "remaining_fallback"
            )
        else:
            fallback_correction_kwh = 0.0
            remaining_consumption_kwh, consumption_rate_kwh_h = (
                self._project_remaining_consumption(
                    now_h,
                    consumed_today_kwh,
                    avg_daily_kwh,
                    accumulator_ready=accumulator_ready,
                    window_hours_per_day=window_hours_per_day,
                    remaining_window_hours=remaining_window_hours,
                )
            )
            consumption_scope = "remaining"
        # Keep the scalar helper as the compatibility seam used by existing
        # callers/tests; read the richer contract separately for dated periods.
        remaining_solar_kwh = self._remaining_solar_today_kwh(now)
        solar_forecast_input = self._read_remaining_solar_input(now=now)

        decision = await controller._should_activate_grid_charging(
            consumption_override_kwh=remaining_consumption_kwh,
            solar_forecast_override_kwh=remaining_solar_kwh,
        )
        # Keep explicit diagnostics for the pre-slot notification and future
        # consumers while preserving the legacy avg_consumption_kwh field.
        decision["consumption_scope"] = consumption_scope
        decision["daily_avg_consumption_kwh"] = avg_daily_kwh
        decision["consumed_today_kwh"] = consumed_today_kwh
        decision["remaining_consumption_kwh"] = remaining_consumption_kwh
        decision["consumption_fallback_correction_kwh"] = fallback_correction_kwh
        decision["remaining_solar_kwh"] = remaining_solar_kwh
        if solar_forecast_input is not None and solar_forecast_input.source != "fallback":
            decision["solar_forecast_input"] = solar_forecast_input
            decision["solar_forecast_original_source"] = solar_forecast_input.original_source
            decision["solar_forecast_conversion"] = solar_forecast_input.conversion
            decision["solar_forecast_periods"] = solar_forecast_input.periods
        decision["consumption_rate_kwh_h"] = consumption_rate_kwh_h
        decision["consumption_accumulator_ready"] = accumulator_ready
        decision["consumption_accumulator_source"] = consumption_source
        decision["consumption_forecast_source"] = (
            profile_forecast.source if profile_forecast is not None else "legacy_daily"
        )
        decision["profile_coverage_ratio"] = (
            profile_forecast.coverage_ratio if profile_forecast is not None else 0.0
        )
        decision["profile_days"] = (
            profile_forecast.total_days if profile_forecast is not None else 0
        )
        decision["profile_fallback_reason"] = (
            profile_forecast.fallback_reason if profile_forecast is not None else "profile_not_mature"
        )
        decision["solar_forecast_source"] = getattr(
            controller,
            "solar_forecast_diagnostic_source",
            getattr(controller, "solar_forecast_source", None),
        )
        return decision

    async def _current_horizon_grid_charging_decision(
        self, *, now: datetime | None = None
    ) -> dict:
        """Evaluate direct Time Slot/Real-Time Price gates coherently.

        These paths run during the day rather than solely at 00:05. When a
        provider supplies ``remaining today``, pair it with remaining load too.
        """
        if (
            get_configured_solar_forecast_sensor(
                self._controller, "remaining"
            )
            or getattr(
                getattr(self._controller, "_consumption_tracker", None),
                "consumption_profile",
                None,
            ) is not None
        ):
            return await self._evaluate_remaining_grid_charging(now=now)
        return await self._controller._should_activate_grid_charging()

    @staticmethod
    def _project_remaining_consumption(
        now_h: float,
        consumed_today_kwh: float,
        avg_daily_kwh: float,
        *,
        accumulator_ready: bool = True,
        window_hours_per_day: float = 24.0,
        remaining_window_hours: float | None = None,
    ) -> tuple[float, float]:
        """Estimate house consumption from now until midnight, plus the rate used.

        A warm same-day accumulator provides the historical unspent energy.  It
        is never allowed below the normal time-prorated remainder, which avoids
        underestimating a day whose load was concentrated earlier.  Crucially,
        an already-finished morning spike is not extrapolated over every hour
        left in the day; doing that can turn an 18 kWh daily average into a
        fictitious 40 kWh remaining forecast.

        A cold, missing, or previous-day accumulator cannot say how much of the
        average has already elapsed.  In that case use the historical hourly
        rate for the remaining hours.  Returns ``(remaining_kwh,
        rate_kwh_per_h)``.
        """
        try:
            now_h = min(24.0, max(0.0, float(now_h)))
        except (TypeError, ValueError):
            now_h = 0.0
        try:
            avg_daily_kwh = max(0.0, float(avg_daily_kwh))
        except (TypeError, ValueError):
            avg_daily_kwh = 0.0
        if not math.isfinite(avg_daily_kwh):
            avg_daily_kwh = 0.0
        hours_to_midnight = 24.0 - now_h
        try:
            window_hours_per_day = min(
                24.0, max(0.0, float(window_hours_per_day))
            )
        except (TypeError, ValueError):
            window_hours_per_day = 24.0
        if not math.isfinite(window_hours_per_day):
            window_hours_per_day = 24.0
        if remaining_window_hours is None:
            remaining_window_hours = hours_to_midnight
        try:
            remaining_window_hours = min(
                window_hours_per_day,
                max(0.0, float(remaining_window_hours)),
            )
        except (TypeError, ValueError):
            remaining_window_hours = hours_to_midnight
        if not math.isfinite(remaining_window_hours):
            remaining_window_hours = hours_to_midnight

        historical_rate = (
            avg_daily_kwh / window_hours_per_day
            if window_hours_per_day > 0.0
            else 0.0
        )
        normal_remaining = historical_rate * remaining_window_hours

        if not accumulator_ready:
            return normal_remaining, historical_rate

        try:
            consumed_today_kwh = max(0.0, float(consumed_today_kwh))
        except (TypeError, ValueError):
            consumed_today_kwh = 0.0
        if not math.isfinite(consumed_today_kwh):
            consumed_today_kwh = 0.0
        historical_remainder = max(0.0, avg_daily_kwh - consumed_today_kwh)
        return max(historical_remainder, normal_remaining), historical_rate

    def _read_remaining_solar_input(
        self,
        *,
        now: datetime | float | None = None,
        update_controller: bool = True,
    ) -> SolarForecastInput | None:
        """Read the normalized remaining forecast, retaining dated periods."""
        try:
            return read_remaining_solar_kwh(
                self._hass,
                self._controller,
                now=now,
                update_controller=update_controller,
            )
        except (AttributeError, TypeError, ValueError):
            return None

    def _remaining_solar_today_kwh(self, now_h: datetime | float) -> float:
        """Solar generation still expected today (kWh), from the forecast sensor.

        A native remaining sensor or dated provider periods are exact. A
        legacy whole-day scalar is mapped over the remaining solar curve rather
        than reduced by actual production, because missed morning forecast must
        not be moved into the evening. Before production could plausibly have
        started, the full forecast remains available for the SOC-drop
        re-evaluation (#411), which otherwise booked cheap grid slots for a
        "deficit" that solar covers.
        After the cutoff hour with no production seen, keep the conservative
        0 (solar sensor likely broken; better to book the slots than run dry).
        """
        solar_input = self._read_remaining_solar_input(now=now_h)
        return solar_input.remaining_kwh if solar_input is not None else 0.0

    async def _evaluate_evening_recharge(self) -> None:
        """Late-day re-evaluation: charge batteries cheaply if solar fell short.

        Runs once per day around T_end - 1.5h.  Checks the current battery SOC
        against the configured max_soc and, accounting for remaining solar, decides
        whether to schedule cheap slots from now through the next 12 hours (crosses midnight when tomorrow prices are available).

        Decision flow:
        1. Batteries already at target → skip.
        2. Calculate remaining solar (actual accumulator if available, else sinusoidal).
        3. Net deficit = energy_to_full - remaining_solar_for_battery.
        4. Deficit < EVENING_DEFICIT_THRESHOLD_KWH → skip.
        5. Parse today's future price slots; select cheapest to cover the deficit.
        6. Merge into existing schedule (or create a new one).
        7. Send notification.
        """
        from datetime import datetime

        now = datetime.now()
        # The evening-time once-per-day guard (_dp_evening_reevaluated_date) is set
        # by the handler only on the evening-time trigger, so a SOC-drop-triggered
        # run here does not consume the late-day pass. #411

        _LOGGER.info("Dynamic pricing: running evening re-evaluation at %s", now.strftime("%H:%M"))

        # Ensure service-based provider slots are current.
        await self._maybe_refresh_service_prices(force=True)

        # --- Battery state ---
        coordinators_with_data = [
            c for c in self._controller.coordinators
            if c.data and not getattr(c, "battery_manual_mode_enabled", False)
        ]
        if not coordinators_with_data:
            _LOGGER.info("Evening recharge: no battery data, skipping")
            return

        # SOC-drop debounce (#411): reset the reference to the current level now,
        # before any later early-return, so the next drop trigger is measured from
        # here (re-arms only after another SOC_REEVALUATION_THRESHOLD drop).
        self._controller._dp_last_eval_soc = sum(
            c.data.get("battery_soc", 0) for c in coordinators_with_data
        ) / len(coordinators_with_data)

        # Room to each battery's max_soc — the physical cap on how much the
        # evening top-up can add.
        energy_to_full_kwh = sum(
            max(0.0, (c.max_soc - (c.data.get("battery_soc", c.max_soc) or 0)) / 100.0
                * (c.data.get("battery_total_energy", 0) or 0))
            for c in coordinators_with_data
        )

        if energy_to_full_kwh <= EVENING_DEFICIT_THRESHOLD_KWH:
            _LOGGER.info(
                "Evening recharge: batteries essentially full (%.2f kWh to max SOC), skipping",
                energy_to_full_kwh,
            )
            return

        # --- Remaining solar expected today (raw generation, before consumption) ---
        now_h = now.hour + now.minute / 60.0
        remaining_solar_kwh = self._remaining_solar_today_kwh(now)

        # --- Remaining house consumption until midnight (handoff to the 00:05
        # evaluation, which re-plans the next day). Keep this identical to other
        # remaining-horizon rebuilds: never reuse consumption already spent
        # today, while retaining the normal historical remainder when today's
        # load was concentrated earlier. ---
        consumed_today_kwh, accumulator_ready, _consumption_source = (
            self._get_consumed_today_kwh(self._controller, now)
        )
        tracker = self._controller._consumption_tracker
        avg_daily_kwh = await tracker.get_dynamic_base_consumption()
        if _consumption_source == "daily_home_energy":
            window_hours_per_day = 24.0
            remaining_window_hours = 24.0 - now_h
        else:
            window_hours_per_day = tracker.get_consumption_window_hours_per_day()
            remaining_window_hours = tracker.consumption_window_hours_in_range(
                now_h, 24.0
            )
        profile_forecast = self._profile_remaining_consumption(now, now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ) + timedelta(days=1))
        if profile_forecast is not None:
            remaining_consumption_kwh = profile_forecast.energy_kwh
            consumption_rate_kwh_h = (
                remaining_consumption_kwh / remaining_window_hours
                if remaining_window_hours > 0
                else 0.0
            )
            consumption_scope = (
                "remaining_profile"
                if profile_forecast.source == "profile"
                else "remaining_fallback"
            )
        else:
            remaining_consumption_kwh, consumption_rate_kwh_h = self._project_remaining_consumption(
                now_h,
                consumed_today_kwh,
                avg_daily_kwh,
                accumulator_ready=accumulator_ready,
                window_hours_per_day=window_hours_per_day,
                remaining_window_hours=remaining_window_hours,
            )
            consumption_scope = "remaining"

        # Keep the source visible to the status/diagnostic sensor and to the
        # next decision snapshot without changing the scheduling schema.
        decision_data = self._controller._last_decision_data
        if not isinstance(decision_data, dict):
            decision_data = {}
        decision_data.update(
            {
                "consumption_scope": consumption_scope,
                "consumption_forecast_source": (
                    profile_forecast.source if profile_forecast is not None else "legacy_daily"
                ),
                "profile_coverage_ratio": (
                    profile_forecast.coverage_ratio if profile_forecast is not None else 0.0
                ),
                "profile_days": (
                    profile_forecast.total_days if profile_forecast is not None else 0
                ),
                "remaining_consumption_kwh": remaining_consumption_kwh,
                "solar_forecast_source": getattr(
                    self._controller,
                    "solar_forecast_diagnostic_source",
                    getattr(self._controller, "solar_forecast_source", None),
                ),
            }
        )
        self._controller._last_decision_data = decision_data

        # Battery energy available above the discharge floor right now.
        usable_now_kwh = sum(
            max(0.0, ((c.data.get("battery_soc", 0) or 0) - c.min_soc) / 100.0
                * (c.data.get("battery_total_energy", 0) or 0))
            for c in coordinators_with_data
        )

        # --- Net deficit: grid energy still needed to cover tonight, after what
        # the battery already holds and the solar still to come. Capped at the
        # room to max_soc. ---
        evening_deficit_kwh = min(
            energy_to_full_kwh,
            max(0.0, remaining_consumption_kwh - usable_now_kwh - remaining_solar_kwh),
        )
        planned_evening_charge_kwh = calculations.calculate_planned_grid_charge_kwh(
            evening_deficit_kwh,
            energy_to_full_kwh,
            self._controller._predictive_grid_charge_margin_pct,
        )

        if evening_deficit_kwh < EVENING_DEFICIT_THRESHOLD_KWH:
            _LOGGER.info(
                "Evening recharge: battery + solar cover tonight "
                "(need=%.2f, usable=%.2f, solar=%.2f kWh) — no action",
                remaining_consumption_kwh, usable_now_kwh, remaining_solar_kwh,
            )
            return

        _LOGGER.info(
            "Evening recharge: deficit %.2f kWh (need=%.2f, usable=%.2f, solar=%.2f, "
            "historical rate=%.2f kWh/h × %.1f remaining window hours) — "
            "searching for cheap slots",
            evening_deficit_kwh, remaining_consumption_kwh, usable_now_kwh,
            remaining_solar_kwh, consumption_rate_kwh_h, remaining_window_hours,
        )

        # This deficit belongs to the remaining energy horizon for today. Price
        # data beyond midnight may be available, but cannot cover consumption
        # that occurs before midnight.
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        slots = self._parse_price_data(horizon_end=midnight)
        slots = [slot for slot in slots if slot.end <= midnight]
        if not slots:
            _LOGGER.warning("Evening recharge: no price data available")
            return

        # Exclude slots already in the morning schedule
        if self._controller._dynamic_pricing_schedule:
            scheduled_starts = {s.start for s in self._controller._dynamic_pricing_schedule.selected_slots}
            slots = [s for s in slots if s.start not in scheduled_starts]

        if not slots:
            # The cheap slots are already in the schedule — but it may be the
            # informational 00:05 schedule (charging_needed=False) whose slots were
            # never armed. With a real deficit, promote it to actually charge those
            # upcoming slots and publish the deficit for the enforcer. #411
            sched = self._controller._dynamic_pricing_schedule
            upcoming = [s for s in sched.selected_slots if s.start > now] if sched else []
            if upcoming and not bool(
                getattr(sched, "deficit_charging_needed", sched.charging_needed)
            ):
                if not hasattr(sched, "slot_purposes"):
                    sched.slot_purposes = {
                        slot: SLOT_PURPOSE_DEFICIT for slot in sched.selected_slots
                    }
                for slot in upcoming:
                    sched.slot_purposes[slot] = self._merge_slot_purpose(
                        sched.slot_purposes.get(slot), SLOT_PURPOSE_DEFICIT
                    )
                sched.charging_needed = True
                sched.deficit_charging_needed = True
                sched.deficit_hours_needed = calculations.calculate_charging_hours_needed(
                    planned_evening_charge_kwh,
                    self._controller.max_contracted_power,
                    self._controller.max_charge_capacity,
                )
                sched.schedule_type = self._schedule_type_from_purposes(
                    sched.slot_purposes.values()
                )
                decision = self._controller._last_decision_data
                if not isinstance(decision, dict):
                    decision = {}
                decision["energy_deficit_kwh"] = evening_deficit_kwh
                decision["planned_grid_charge_kwh"] = planned_evening_charge_kwh
                self._controller._last_decision_data = decision
                _LOGGER.info(
                    "Evening recharge: promoted informational schedule to charging "
                    "(%.2f kWh deficit, %d upcoming slot(s))",
                    evening_deficit_kwh, len(upcoming),
                )
                await self._send_evening_recharge_notification(evening_deficit_kwh, upcoming)
            else:
                _LOGGER.info("Evening recharge: no additional slots available (all already scheduled)")
            return

        hours_needed = calculations.calculate_charging_hours_needed(
            planned_evening_charge_kwh,
            self._controller.max_contracted_power,
            self._controller.max_charge_capacity,
        )
        # Deliberately no arbitrage gate here. This is a deficit-driven safety
        # recharge after a bad solar day, not an arbitrage trade, and the horizon
        # is truncated: late in the evening only cheap night slots remain, so the
        # expected discharge price collapses toward the charge price and the gate
        # would refuse every recharge it exists to perform.
        selected = calculations.select_cheapest_hours(
            slots, hours_needed, self._controller.max_price_threshold
        )

        if not selected:
            _LOGGER.warning("Evening recharge: no slots below price threshold")
            return

        # --- Merge into schedule ---
        if self._controller._dynamic_pricing_schedule:
            schedule = self._controller._dynamic_pricing_schedule
            merged = sorted(
                schedule.selected_slots + selected,
                key=lambda s: s.start,
            )
            purposes = dict(getattr(schedule, "slot_purposes", {}))
            for slot in schedule.selected_slots:
                purposes.setdefault(slot, SLOT_PURPOSE_DEFICIT)
            for slot in selected:
                purposes[slot] = self._merge_slot_purpose(
                    purposes.get(slot), SLOT_PURPOSE_DEFICIT
                )
            schedule.selected_slots = merged
            schedule.slot_purposes = purposes
            schedule.charging_needed = True
            schedule.deficit_charging_needed = True
            schedule.deficit_hours_needed = max(
                float(getattr(schedule, "deficit_hours_needed", 0.0)),
                hours_needed,
            )
            schedule.schedule_type = self._schedule_type_from_purposes(
                purposes.values()
            )
            schedule.hours_needed = sum(
                max(0.0, (slot.end - slot.start).total_seconds() / 3600.0)
                for slot in merged
            )
            self._controller._dynamic_pricing_evaluated_date = max(s.start.date() for s in merged)
        else:
            avg_price = sum(s.price for s in selected) / len(selected)
            effective_power_kw = min(self._controller.max_contracted_power, self._controller.max_charge_capacity) / 1000.0
            self._controller._dynamic_pricing_schedule = DynamicPricingSchedule(
                hours_needed=hours_needed,
                selected_slots=selected,
                average_price=avg_price,
                estimated_cost=avg_price * effective_power_kw * hours_needed,
                total_available_slots=len(slots),
                evaluation_time=now,
                energy_deficit_kwh=evening_deficit_kwh,
                charging_needed=True,
                schedule_type=SLOT_PURPOSE_DEFICIT,
                deficit_charging_needed=True,
                deficit_hours_needed=hours_needed,
            )
            self._controller._dynamic_pricing_evaluated_date = max(s.start.date() for s in selected)

        # Publish the evening target so the predictive enforcer charges to *this*
        # plan, not the stale 00:05 morning deficit (which assumed the solar that
        # just fell short). The morning evaluation overwrites this dict at 00:05,
        # so the override only lasts through tonight. #409
        decision = self._controller._last_decision_data
        if not isinstance(decision, dict):
            decision = {}
        decision["energy_deficit_kwh"] = evening_deficit_kwh
        decision["planned_grid_charge_kwh"] = planned_evening_charge_kwh
        self._controller._last_decision_data = decision
        self._controller._dp_pre_evaluated_slots = {}
        self._controller._dp_pre_evaluated_purposes = {}

        _LOGGER.info(
            "Evening recharge: scheduled %d slot(s) (%.1fh) for %.2f kWh deficit",
            len(selected), hours_needed, evening_deficit_kwh,
        )
        await self._send_evening_recharge_notification(evening_deficit_kwh, selected)

    async def _send_evening_recharge_notification(
        self, deficit_kwh: float, slots: list
    ) -> None:
        """Send notification for the evening re-evaluation result."""
        avg_soc = sum(
            (c.data.get("battery_soc", 0) or 0)
            for c in self._controller.coordinators
            if c.data and not getattr(c, "battery_manual_mode_enabled", False)
        ) / max(
            1,
            sum(
                1 for c in self._controller.coordinators
                if c.data and not getattr(c, "battery_manual_mode_enabled", False)
            ),
        )
        title, message = notifications.format_evening_recharge_notification(
            deficit_kwh,
            slots,
            unit=self._get_price_unit(),
            avg_soc=avg_soc,
        )
        await self._hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": title,
                "message": message,
                "notification_id": f"{NOTIFICATION_ID_PREFIX}predictive_charging_evening_reeval",
            },
        )

    # =========================================================================
    # DYNAMIC PRICING: Control loop handler
    # =========================================================================

    def _active_predictive_targets_pending(self) -> bool:
        """Return whether a live per-battery target still has charge headroom."""
        targets = getattr(self._controller, "_predictive_charge_target_soc", None)
        if not targets:
            return False
        for coordinator, target in targets.items():
            data = getattr(coordinator, "data", None)
            if not self._opportunistic_battery_eligible(coordinator):
                continue
            try:
                if float(data.get("battery_soc", 0.0) or 0.0) < float(target):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    async def _stop_dynamic_price_slot(
        self, reason: str, *, write_idle: bool = True
    ) -> None:
        """Stop a live price-slot charge and return battery ownership safely."""
        controller = self._controller
        active_slot = getattr(controller, "_active_dynamic_price_slot", None)
        schedule = getattr(controller, "_dynamic_pricing_schedule", None)
        if (
            reason == "slot_ended"
            and active_slot is not None
            and schedule is not None
            and getattr(schedule, "chronological_planning_active", False)
            and active_slot in schedule.slot_energy_targets_kwh
        ):
            targets = getattr(controller, "_predictive_deficit_target_soc", None) or {}
            shortfall = 0.0
            for coordinator, target_soc in targets.items():
                data = getattr(coordinator, "data", None) or {}
                capacity = max(0.0, float(data.get("battery_total_energy", 0.0) or 0.0))
                current_soc = float(data.get("battery_soc", 0.0) or 0.0)
                shortfall += max(0.0, float(target_soc) - current_soc) / 100.0 * capacity
            deadline = schedule.slot_deadlines.get(active_slot)
            remaining = shortfall
            if remaining > 0.01:
                power_kw = min(
                    max(0.0, float(controller.max_contracted_power)),
                    max(0.0, float(controller.max_charge_capacity)),
                ) / 1000.0
                for slot in sorted(schedule.selected_slots, key=lambda item: item.start):
                    if (
                        slot.start < datetime.now()
                        or slot == active_slot
                        or (deadline is not None and slot.end > deadline)
                    ):
                        continue
                    capacity = power_kw * max(0.0, (slot.end - slot.start).total_seconds() / 3600.0) * CHARGE_EFFICIENCY
                    current = float(schedule.slot_energy_targets_kwh.get(slot, 0.0) or 0.0)
                    take = min(remaining, max(0.0, capacity - current))
                    if take <= 0:
                        continue
                    schedule.slot_energy_targets_kwh[slot] = current + take
                    remaining -= take
                    if remaining <= 0.01:
                        break
            if remaining > 0.01:
                schedule.deadline_shortfall_kwh += remaining
                schedule.total_shortfall_kwh += remaining
                if isinstance(getattr(controller, "_last_decision_data", None), dict):
                    controller._last_decision_data["deadline_shortfall_kwh"] = round(
                        schedule.deadline_shortfall_kwh, 3
                    )
                    controller._last_decision_data["total_shortfall_kwh"] = round(
                        schedule.total_shortfall_kwh, 3
                    )
                _LOGGER.warning(
                    "Dynamic pricing: %.2f kWh not delivered before deadline %s",
                    remaining,
                    deadline.isoformat() if deadline is not None else "unknown",
                )
        controller._current_price_slot_active = False
        controller._grid_charging_initialized = False
        controller.grid_charging_active = False
        controller._active_dynamic_slot_purpose = None
        controller._active_dynamic_price_slot = None
        controller._predictive_charge_target_soc = None
        controller._curtailment_opportunistic_target_soc = None
        controller._predictive_deficit_target_soc = None
        controller._curtailment_opportunity_limited = False
        self._reset_predictive_demand_runtime()
        controller.previous_power = 0
        controller.previous_error = 0
        controller.first_execution = True
        if write_idle:
            for coordinator in getattr(controller, "coordinators", []):
                if getattr(coordinator, "battery_manual_mode_enabled", False):
                    continue
                await controller._set_battery_power(coordinator, 0, 0)
        _LOGGER.info("Dynamic pricing: stopped active slot (%s)", reason)

    async def handle_dynamic_pricing_predictive_charging(self) -> None:
        """Handle predictive charging in dynamic pricing mode (called every 2.5s)."""
        now = datetime.now()

        # Phase 0: Keep service-based provider caches fresh.
        await self._maybe_refresh_service_prices()

        # Phase 2: Retry if prices weren't available at 00:05 (e.g. sensor update delay)
        if (
            self._controller._dynamic_pricing_evaluated_date != now.date()
            and self._controller._dp_eval_retry_count > 0
            and self._controller._dp_eval_retry_count < 5
            and now.hour == 0  # Only retry within the first hour of the day
        ):
            # Retry every 15 min starting from 00:05
            retry_minute = now.minute
            expected_retry_minute = 5 + self._controller._dp_eval_retry_count * 15
            if abs(retry_minute - expected_retry_minute) <= 2:
                _LOGGER.info("Dynamic pricing: retrying evaluation (attempt %d)", self._controller._dp_eval_retry_count + 1)
                await self._evaluate_dynamic_pricing(
                    horizon=DynamicPricingEvaluationHorizon.REMAINING,
                )
                return

        # Phase 2.5: Pre-slot re-evaluation (1h before each upcoming slot)
        await self._check_dp_pre_slot_reevaluation()

        # Phase 2.6: Re-evaluate upward when solar winds down (evening) OR when live
        # SOC has fallen far below the level the 00:05 balance assumed (#411). The
        # evening-time guard is set only on the evening-time trigger, so a SOC-drop
        # run does not consume the late-day pass.
        trigger_evening = self._is_evening_reevaluation_time()
        trigger_soc_drop = self._is_dp_soc_drop_reeval()
        if trigger_evening or trigger_soc_drop:
            if trigger_evening:
                self._controller._dp_evening_reevaluated_date = now.date()
            await self._evaluate_evening_recharge()

        # Phase 3: Daily reset at midnight
        today = now.date()
        if self._controller._dynamic_pricing_evaluated_date is not None:
            if today > self._controller._dynamic_pricing_evaluated_date:
                _LOGGER.info("Dynamic pricing: new day — resetting schedule")
                self._controller._dynamic_pricing_schedule = None
                self._controller._dynamic_pricing_evaluated_date = None
                self._controller._current_price_slot_active = False
                self._controller._dp_eval_retry_count = 0
                self._controller._dp_pre_evaluated_slots = {}
                self._controller._dp_pre_evaluated_purposes = {}
                self._controller._dp_completed_slots = set()
                self._controller._active_dynamic_slot_purpose = None
                self._controller._dp_daily_avg_price = None
                self._controller._dp_arbitrage_ceiling = None
                self._controller._dp_evening_reevaluated_date = None
                self._controller._dp_last_eval_soc = None
                self._reset_predictive_demand_runtime()
                self.clear_curtailment_runtime("new_day")

        # Reaching the opportunistic target outside the control handler (for
        # example through solar or a manual charge) invalidates every remaining
        # opportunity before the current/future calendar is considered.
        schedule = getattr(self._controller, "_dynamic_pricing_schedule", None)
        if (
            schedule is not None
            and bool(getattr(schedule, "negative_price_charging_needed", False))
            and not self._opportunistic_target_pending()
        ):
            self._prune_completed_opportunities()
            if self._controller._current_price_slot_active:
                if (
                    self._controller._active_dynamic_slot_purpose
                    == SLOT_PURPOSE_NEGATIVE_PRICE
                ):
                    await self._stop_dynamic_price_slot("soc_target_reached")
                elif (
                    self._controller._active_dynamic_slot_purpose
                    == SLOT_PURPOSE_COMBINED
                ):
                    self._controller._active_dynamic_slot_purpose = (
                        SLOT_PURPOSE_DEFICIT
                    )
                    deficit_target = getattr(
                        self._controller, "_predictive_deficit_target_soc", None
                    )
                    self._controller._predictive_charge_target_soc = deficit_target
                    if deficit_target is None:
                        self._controller._grid_charging_initialized = False

        # Phase 4: Check if we're in a selected typed price slot.
        if self._controller._dynamic_pricing_schedule and not self._controller.predictive_charging_overridden:
            in_slot = self.is_in_dynamic_pricing_slot()
            current_slot = next(
                (
                    slot
                    for slot in self._controller._dynamic_pricing_schedule.selected_slots
                    if slot.start <= now < slot.end
                ),
                None,
            )

            if in_slot and not self._controller._current_price_slot_active:
                effective_purpose = (
                    self._effective_slot_purpose(current_slot)
                    if current_slot is not None
                    else None
                )
                if (
                    current_slot is not None
                    and current_slot.start in getattr(self._controller, "_dp_completed_slots", set())
                ):
                    effective_purpose = None

                # Informational/completed schedule — no grid charging needed.
                if effective_purpose is None:
                    _LOGGER.debug(
                        "Dynamic pricing: inside selected slot but no typed purpose remains — skipping"
                    )
                    # Fall through to discharge control below (do not return early)

                # Respect charge delay: if configured and still active, hold until it unlocks
                elif self._controller.is_charge_blocked():
                    _LOGGER.info(
                        "Dynamic pricing: inside cheap slot window but charge delay is active — holding"
                    )
                    # Fall through to discharge control below (do not return early)

                else:
                    # Entering an authorised typed slot.
                    self._controller._current_price_slot_active = True
                    self._controller._grid_charging_initialized = False
                    self._controller._active_dynamic_slot_purpose = effective_purpose
                    self._controller._active_dynamic_price_slot = current_slot
                    self._controller.grid_charging_active = True
                    if current_slot:
                        await self._send_dynamic_pricing_slot_start_notification(current_slot)
                    _LOGGER.info(
                        "Dynamic pricing: entering %s slot %s",
                        effective_purpose,
                        current_slot.start.strftime("%H:%M") if current_slot else "unknown",
                    )

            elif not in_slot and self._controller._current_price_slot_active:
                # Normal PD takes ownership later in this same cycle; avoid an
                # intermediate idle write that would conflict with that command.
                await self._stop_dynamic_price_slot("slot_ended", write_idle=False)

            if self._controller._current_price_slot_active:
                # Revalidate the live import price and curtailment window every
                # cycle. A combined slot may safely downgrade to deficit; a pure
                # opportunity stops immediately when its authorization vanishes.
                if (
                    self._controller._active_dynamic_slot_purpose
                    in {SLOT_PURPOSE_NEGATIVE_PRICE, SLOT_PURPOSE_COMBINED}
                    and not self._opportunistic_target_pending()
                ):
                    self._prune_completed_opportunities()
                effective_purpose = (
                    self._effective_slot_purpose(current_slot)
                    if current_slot is not None
                    else None
                )
                if effective_purpose is None:
                    if not self._opportunistic_target_pending():
                        self._prune_completed_opportunities()
                    await self._stop_dynamic_price_slot("purpose_no_longer_valid")
                    return
                if effective_purpose != self._controller._active_dynamic_slot_purpose:
                    previous_purpose = self._controller._active_dynamic_slot_purpose
                    self._controller._active_dynamic_slot_purpose = effective_purpose
                    if (
                        previous_purpose == SLOT_PURPOSE_COMBINED
                        and effective_purpose == SLOT_PURPOSE_DEFICIT
                    ):
                        deficit_target = getattr(
                            self._controller,
                            "_predictive_deficit_target_soc",
                            None,
                        )
                        self._controller._predictive_charge_target_soc = deficit_target
                        if deficit_target is None:
                            self._controller._grid_charging_initialized = False
                    else:
                        self._controller._grid_charging_initialized = False
                        self._controller._predictive_charge_target_soc = None

                # Demand protection remains inside the predictive controller.
                # It owns the slot while it sends idle, waits for fresh meter
                # samples and, only if needed, shaves the measured excess.  Do
                # not hand the same stale charge-inclusive sample to normal PD.

                opportunity_limited = self._prepare_curtailment_opportunistic_charge(
                    getattr(self._controller, "_curtailment_plan", None)
                    or CurtailmentPlan(),
                    current_slot,
                    effective_purpose,
                )
                await self._controller._handle_predictive_grid_charging()
                if not self._controller.grid_charging_active:
                    # Reaching the temporary solar-space ceiling is not the
                    # same as reaching max_soc.  Keep the physical slot alive
                    # so a later forecast shortfall can release more space.
                    temporary_reserve_stop = bool(
                        opportunity_limited
                        and effective_purpose
                        in {SLOT_PURPOSE_NEGATIVE_PRICE, SLOT_PURPOSE_COMBINED}
                    )
                    if temporary_reserve_stop:
                        await self._stop_dynamic_price_slot(
                            "solar_reserve_reached"
                        )
                        return
                    if not self._active_predictive_targets_pending():
                        if current_slot is not None:
                            self._controller._dp_completed_slots.add(current_slot.start)
                        if not self._opportunistic_target_pending():
                            self._prune_completed_opportunities()
                        await self._stop_dynamic_price_slot("soc_target_reached")
                return

        if (
            getattr(self._controller, "_predictive_charge_suspended_for_demand", False)
            and not getattr(self._controller, "_current_price_slot_active", False)
        ):
            self._reset_predictive_demand_runtime()

        # Phase 5: Override active — resume normal PD control
        if self._controller.predictive_charging_overridden:
            if (
                self._controller.grid_charging_active
                or self._controller._current_price_slot_active
                or getattr(
                    self._controller,
                    "_predictive_charge_suspended_for_demand",
                    False,
                )
            ):
                self._controller.grid_charging_active = False
                self._controller._grid_charging_initialized = False
                self._controller._current_price_slot_active = False
                self._reset_predictive_demand_runtime()
                self._controller.first_execution = True

        # Not in a cheap slot — fall through to normal PD control (no return here)
        # Note: ``_price_based_discharge_blocked`` is computed centrally in
        # ``async_update_charge_discharge`` via ``_apply_price_discharge_block``
        # before this handler runs, so the early ``return`` at the cheap-slot path
        # above does not leave it unset for downstream enforcement.

    # =========================================================================
    # REAL-TIME PRICE: reactive charging based on current price every cycle
    # =========================================================================

    async def handle_realtime_price_predictive_charging(self) -> None:
        """Handle predictive charging in real-time price mode (called every 2.5s).

        Reads the current price every cycle and activates/deactivates grid charging
        immediately when the price crosses the threshold, with no pre-scheduling.
        If an average_price_sensor is configured its value is used as the threshold
        instead of the fixed max_price_threshold.
        """
        current_price = self._get_current_price()
        if current_price is None:
            _LOGGER.debug("Real-time price: price sensor %s unavailable", self._controller.price_sensor)
            if self._controller._realtime_price_charging:
                self._record_predictive_shortfall("realtime_price")
                self._reset_predictive_demand_runtime()
                self._controller._realtime_price_charging = False
                self._controller.grid_charging_active = False
                self._controller._grid_charging_initialized = False
                self._controller.previous_power = 0
                self._controller.previous_error = 0
            return

        # Determine threshold: average sensor if configured, else fixed threshold
        threshold = None
        if self._controller.average_price_sensor:
            avg_state = self._hass.states.get(self._controller.average_price_sensor)
            if avg_state is not None:
                try:
                    threshold = float(avg_state.state)
                except (ValueError, TypeError):
                    pass
        if threshold is None:
            threshold = self._controller.max_price_threshold

        if threshold is None:
            _LOGGER.debug("Real-time price: no threshold configured, skipping")
            return

        # Override active — stop any active charging and do not start new
        if self._controller.predictive_charging_overridden:
            if self._controller._realtime_price_charging or self._controller.grid_charging_active:
                self._record_predictive_shortfall("realtime_price")
                self._reset_predictive_demand_runtime()
                self._controller._realtime_price_charging = False
                self._controller.grid_charging_active = False
                self._controller._grid_charging_initialized = False
                self._controller.previous_power = 0
                self._controller.previous_error = 0
            return

        price_is_cheap = current_price <= threshold
        _LOGGER.debug(
            "Real-time price: current=%.4f threshold=%.4f cheap=%s charging=%s",
            current_price, threshold, price_is_cheap, self._controller._realtime_price_charging,
        )

        # Note: ``_price_based_discharge_blocked`` is set in
        # ``async_update_charge_discharge`` via ``_apply_price_discharge_block``
        # before this handler runs, so any early ``return`` above does not skip it.

        if price_is_cheap and not self._controller._realtime_price_charging:
            if not self._controller._is_operation_allowed(is_charging=True):
                if self._controller.charge_delay_enabled and self._controller._charge_delay_mgr.is_charge_delayed():
                    reason = "charge delay active"
                else:
                    reason = "time slot configuration"
                _LOGGER.debug(
                    "Real-time price: cheap price but charging NOT ALLOWED by %s",
                    reason,
                )
            else:
                # Evaluate whether charging is actually needed before starting
                decision_data = await self._current_horizon_grid_charging_decision()
                self._controller._last_decision_data = decision_data
                if decision_data["should_charge"]:
                    self._controller._realtime_price_charging = True
                    self._controller._grid_charging_initialized = False
                    self._controller.grid_charging_active = True
                    _LOGGER.info(
                        "Real-time price: charging STARTED (price=%.4f <= threshold=%.4f)",
                        current_price, threshold,
                    )
                else:
                    _LOGGER.info(
                        "Real-time price: cheap price but charging NOT needed (sufficient energy)",
                    )

        elif not price_is_cheap and self._controller._realtime_price_charging:
            self._record_predictive_shortfall("realtime_price")
            self._reset_predictive_demand_runtime()
            self._controller._realtime_price_charging = False
            self._controller.grid_charging_active = False
            self._controller._grid_charging_initialized = False
            self._controller.previous_power = 0
            self._controller.previous_error = 0
            _LOGGER.info(
                "Real-time price: charging STOPPED (price=%.4f > threshold=%.4f)",
                current_price, threshold,
            )

        if self._controller.grid_charging_active:
            if not self._controller._is_operation_allowed(is_charging=True):
                # Time slot ended while charging was active — stop immediately
                self._record_predictive_shortfall("realtime_price")
                self._reset_predictive_demand_runtime()
                self._controller._realtime_price_charging = False
                self._controller.grid_charging_active = False
                self._controller._grid_charging_initialized = False
                self._controller.previous_power = 0
                self._controller.previous_error = 0
                _LOGGER.debug(
                    "Real-time price: charging stopped — outside charge time slot",
                )
                return
            await self._controller._handle_predictive_grid_charging()

    # =========================================================================
    # TIME SLOT: predictive charging handler
    # =========================================================================

    async def handle_time_slot_predictive_charging(self) -> None:
        """Handle predictive charging in time slot mode (extracted from main loop)."""
        # Check if we're in the actual time slot
        in_time_window = (
            bool(self._controller.charging_time_slots) and
            self._controller._check_time_window()
        )
        now = self._now()

        if in_time_window:
            if self._controller.predictive_charging_overridden:
                _LOGGER.debug("Predictive charging overridden by user - continuing normal operation")
                if self._controller.grid_charging_active:
                    self._reset_predictive_demand_runtime()
                    self._controller.grid_charging_active = False
                    self._controller._grid_charging_initialized = False
                    self._controller.first_execution = True
                return

            automatic = [
                c for c in self._controller.coordinators
                if c.data and not getattr(c, "battery_manual_mode_enabled", False)
            ]
            current_avg_soc = (
                sum(c.data.get("battery_soc", 0) for c in automatic)
                / max(1, len(automatic))
            )
            is_initial_eval = self._controller.last_evaluation_soc is None

            # Record entry for diagnostics and for the bounded retry grace below.
            # A valid forecast (or no configured forecast) is evaluated in this
            # same cycle; entering a second configured window must not impose a
            # fixed five-minute delay.
            if self._controller._slot_entry_time is None:
                self._controller._slot_entry_time = now
                _LOGGER.info(
                    "Time slot entered (SOC: %.1f%%) — evaluating predictive charging",
                    current_avg_soc,
                )

            # Guaranteed-minimum-SOC floor: the 30% re-eval threshold can't fire
            # once last_evaluation_soc drifts below (floor - margin), so the battery
            # would drain past the hysteresis band unprotected.
            # floor_crossed: force a re-eval when SOC drops below (floor - margin).
            # floor_recovered: force a re-eval when SOC climbs back to the floor while
            # grid charging is active — stops charging on solar-positive days where
            # floor_crossed was the only reason to charge.
            floor = (
                self._controller._predictive_min_soc_floor
                if self._controller._predictive_min_soc_floor_enabled
                else 0.0
            )
            floor_crossed = (
                not is_initial_eval and
                floor > 0 and
                not self._controller.grid_charging_active and
                current_avg_soc < floor - FLOOR_HYSTERESIS_PCT
            )
            floor_recovered = (
                not is_initial_eval and
                floor > 0 and
                self._controller.grid_charging_active and
                current_avg_soc >= floor and
                self._controller.last_evaluation_soc < floor
            )

            should_reevaluate = (
                is_initial_eval or
                floor_crossed or
                floor_recovered or
                abs(current_avg_soc - self._controller.last_evaluation_soc) >= SOC_REEVALUATION_THRESHOLD
            )

            if should_reevaluate:
                if is_initial_eval:
                    _LOGGER.info("INITIAL evaluation of predictive grid charging (SOC: %.1f%%)", current_avg_soc)
                elif floor_recovered:
                    _LOGGER.info("RE-EVALUATING predictive grid charging: SOC recovered to floor (%.1f%% -> %.1f%%)",
                                self._controller.last_evaluation_soc, current_avg_soc)
                else:
                    _LOGGER.info("RE-EVALUATING predictive grid charging due to SOC drop (%.1f%% -> %.1f%%)",
                                self._controller.last_evaluation_soc, current_avg_soc)

                forecast_configured = bool(
                    get_configured_solar_forecast_sensor(
                        self._controller, "remaining"
                    )
                    or get_configured_solar_forecast_sensor(
                        self._controller, "today"
                    )
                )
                forecast_unavailable = False
                forecast_unavailable_elapsed_s = 0.0
                forecast_grace_s = self._time_slot_forecast_grace_s()
                if is_initial_eval and forecast_configured:
                    # ``_evaluate_remaining_grid_charging`` deliberately uses
                    # zero solar as its fail-safe when a forecast read fails.
                    # For the one-shot slot-entry evaluation, distinguish a
                    # transient failure before it is flattened to 0 kWh so the
                    # next cycle can retry instead of publishing a misleading
                    # safe-mode notification. Once the existing five-minute
                    # grace expires, the normal conservative fallback is safe.
                    # A slot that opens on the day boundary (00:00) reads the
                    # provider's exhausted previous-day remainder for the few
                    # hundred milliseconds before it republishes the new day, so
                    # a zero remaining forecast is treated as "not published
                    # yet" and retried within the same grace.
                    forecast = read_solar_forecast_kwh(self._hass, self._controller)
                    forecast_unavailable = (
                        forecast is None or forecast.remaining_kwh <= 0.0
                    )
                    if forecast_unavailable:
                        forecast_unavailable_elapsed_s = (
                            self._time_slot_forecast_unavailable_elapsed_s(now)
                        )
                        if forecast_unavailable_elapsed_s < forecast_grace_s:
                            _LOGGER.debug(
                                "Predictive charging: configured solar forecast is "
                                "unavailable (%.0f / %.0f s grace); retrying",
                                forecast_unavailable_elapsed_s,
                                forecast_grace_s,
                            )
                            return
                        _LOGGER.warning(
                            "Predictive charging: configured solar forecast has been "
                            "unavailable for %.0f s; evaluating conservatively with solar=0",
                            forecast_unavailable_elapsed_s,
                        )

                decision_data = await self._current_horizon_grid_charging_decision()

                # A provider can change state between the pre-check above and
                # the actual balance decision. Apply the same bounded retry to
                # that race, while accepting the existing solar=0 fallback once
                # the grace has elapsed.
                decision_forecast_unavailable = (
                    is_initial_eval
                    and forecast_configured
                    and not decision_data.get("solar_forecast_kwh")
                )
                if decision_forecast_unavailable:
                    if not forecast_unavailable:
                        forecast_unavailable_elapsed_s = (
                            self._time_slot_forecast_unavailable_elapsed_s(now)
                        )
                    if forecast_unavailable_elapsed_s < forecast_grace_s:
                        _LOGGER.debug(
                            "Predictive charging: configured solar forecast became "
                            "unavailable during evaluation (%.0f / %.0f s grace); retrying",
                            forecast_unavailable_elapsed_s,
                            forecast_grace_s,
                        )
                        return
                    _LOGGER.warning(
                        "Predictive charging: forecast remained unavailable for %.0f s; "
                        "accepting the conservative solar=0 evaluation",
                        forecast_unavailable_elapsed_s,
                    )
                    forecast_unavailable = True

                if forecast_unavailable:
                    decision_data["solar_forecast_fallback"] = True
                    decision_data["solar_forecast_fallback_reason"] = (
                        "unavailable_after_time_slot_grace"
                    )

                decision_data = self._apply_time_slot_chronological_plan(
                    decision_data,
                    now=now,
                )

                was_active = self._controller.grid_charging_active
                self._controller.grid_charging_active = decision_data["should_charge"]
                self._controller.last_evaluation_soc = current_avg_soc
                self._controller._last_decision_data = decision_data

                # A re-evaluation that reverses the slot's decision replaces the
                # notification: otherwise a "STARTED" notice stays on screen for
                # the rest of the slot while nothing is charging, and the
                # "Not required" result is never reported.
                if is_initial_eval or was_active != self._controller.grid_charging_active:
                    await self._send_predictive_charging_notification(
                        decision_data=decision_data
                    )

            if self._controller.grid_charging_active:
                _LOGGER.info("Predictive Grid Charging ACTIVE - target power: %dW", self._controller.max_contracted_power)
                await self._controller._handle_predictive_grid_charging()
                return
            else:
                _LOGGER.info("In predictive charging slot but charging not needed - continuing normal operation")
                return
        else:
            await self._ensure_time_slot_chronological_preview(
                now=now
            )
            # `last_evaluation_soc is not None` marks that we evaluated during a
            # slot (set on every slot's initial eval, charging or not). Including
            # it makes this a one-shot cleanup that also fires on solar-sufficient
            # days where charging never activated — otherwise last_evaluation_soc
            # kept yesterday's value, so the next day was not treated as an
            # initial eval and its notification was never sent.
            if (
                self._controller.last_evaluation_soc is not None
                or self._controller.grid_charging_active
                or self._controller._grid_charging_initialized
            ):
                _LOGGER.info("Exiting predictive grid charging slot - returning to normal mode")
                missing = self._record_predictive_shortfall("time_slot")
                if missing > 0.01:
                    # Rebuild from actual SOC so later configured windows can
                    # absorb the missed quota; an infeasible plan retains its
                    # explicit chronological shortfall in diagnostics.
                    replanned = await self._current_horizon_grid_charging_decision()
                    replanned = self._apply_time_slot_chronological_plan(
                        replanned, now=now
                    )
                    self._controller._last_decision_data = replanned
                self._reset_predictive_demand_runtime()
                self._controller.grid_charging_active = False
                self._controller.last_evaluation_soc = None
                self._controller._grid_charging_initialized = False
                self._controller.error_integral = 0.0
                self._controller.previous_error = 0.0
                self._controller.sign_changes = 0
                await self._hass.services.async_call(
                    "persistent_notification",
                    "dismiss",
                    {"notification_id": f"{NOTIFICATION_ID_PREFIX}predictive_charging_evaluation"},
                )

            self._controller._slot_entry_time = None
            self._controller._active_time_slot_quota_kwh = None

    async def _send_predictive_charging_notification(
        self,
        decision_data: dict,
        is_daily_evaluation: bool = False,
    ) -> None:
        """Send notification about predictive charging evaluation result.

        Args:
            decision_data: Dict from _should_activate_grid_charging() with decision factors
            is_daily_evaluation: True when called from daily evaluation in automation_slots mode
        """
        # Format the notification using the pricing.notifications helper
        title, message = notifications.format_predictive_notification_message(
            decision_data,
            is_daily_evaluation,
            max_contracted_power=self._controller.max_contracted_power,
            max_charge_capacity=self._controller.max_charge_capacity,
            charging_time_slot=self._controller._active_charging_slot(),
        )

        # Send the notification
        await self._hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": title,
                "message": message,
                "notification_id": f"{NOTIFICATION_ID_PREFIX}predictive_charging_evaluation",
            },
        )

    # =========================================================================
    # Price-based discharge block
    # =========================================================================

    def apply_price_discharge_block(self) -> None:
        """Set ``_price_based_discharge_blocked`` from current price vs threshold.

        Centralised so the flag is set every cycle BEFORE mode dispatch — even when
        the mode handler returns early (override active, DP cheap-slot active,
        max_soc transition, etc.). Previously the flag was set inside each handler
        and any early ``return`` left it at the cycle-start ``False`` reset, letting
        PD discharge under cheap prices.
        """
        mode = self._controller.predictive_charging_mode

        # Tibber has no price sensor (service-based); treat it as a valid price source.
        has_price_source = bool(self._controller.price_sensor) or (
            self._controller.price_integration_type == PRICE_INTEGRATION_TIBBER
        )

        if mode == PREDICTIVE_MODE_DYNAMIC_PRICING:
            if not self._controller.dp_price_discharge_control or not has_price_source:
                self._controller.remove_discharge_block("price_discharge")
                return
        elif mode == PREDICTIVE_MODE_REALTIME_PRICE:
            if not self._controller.rt_price_discharge_control or not self._controller.price_sensor:
                self._controller.remove_discharge_block("price_discharge")
                return
        else:
            self._controller.remove_discharge_block("price_discharge")
            return

        # Reactive per-cycle price check. DP uses the configured fixed threshold
        # when present, otherwise the daily slot average. RT keeps its explicit
        # average-sensor vs fixed-threshold behaviour.
        # DP no longer relies on selected_slots membership for the
        # discharge decision — the slot list governs grid-charging only.
        # This eliminates the post-restart and post-midnight blind windows
        # where _dynamic_pricing_schedule is None.
        threshold = None
        if mode == PREDICTIVE_MODE_DYNAMIC_PRICING:
            # Discharge uses its own floor when configured, opening an idle band
            # between the charge ceiling (max_price_threshold, used by
            # select_cheapest_hours) and this discharge floor: price <= floor
            # blocks discharge, price > ceiling selects no charge slot, so the
            # gap idles (PV-surplus charging via normal PD still allowed). Unset
            # → falls back to the charge ceiling, then the daily slot average, so
            # single-threshold installs are unchanged. #408
            # ponytail: a floor below the ceiling just collapses the band toward
            # current behavior — benign, not validated.
            threshold = self._controller.discharge_price_threshold
            if threshold is None:
                threshold = self._controller.max_price_threshold
            if threshold is None:
                threshold = self._controller._dp_daily_avg_price
        elif self._controller.average_price_sensor:
            avg_state = self._hass.states.get(self._controller.average_price_sensor)
            if avg_state is not None:
                try:
                    threshold = float(avg_state.state)
                except (ValueError, TypeError):
                    pass
        if threshold is None and mode == PREDICTIVE_MODE_REALTIME_PRICE:
            threshold = self._controller.max_price_threshold

        if threshold is None:
            self._controller.remove_discharge_block("price_discharge")
            return

        current_price = self._get_current_price()
        if current_price is None:
            self._controller.remove_discharge_block("price_discharge")
            return

        if current_price > threshold:
            self._controller.remove_discharge_block("price_discharge")
            self._controller._price_based_discharge_blocked = False
            return

        self._controller.set_discharge_block(
            "price_discharge",
            "price",
            {"current_price": current_price, "threshold": threshold, "mode": mode},
        )
        self._controller._price_based_discharge_blocked = True
        if self._controller._price_based_discharge_blocked:
            _LOGGER.debug(
                "Price-based discharge BLOCKED (current=%.4f <= threshold=%.4f, mode=%s)",
                current_price, threshold, mode,
            )
