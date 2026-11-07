"""Runtime owner of the price-aware discharge reserve.

The reserve is the energy the fleet must keep back because a dearer hour is
still ahead and the household will need energy there. Each battery keeps the
same share of what it holds above its floor, and
``_refresh_price_reserve_blocks`` turns that into a per-battery
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

import copy
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any

from ..const import CHARGE_EFFICIENCY, PREDICTIVE_MODE_DYNAMIC_PRICING
from ..drivers.base import has_solar_production_source
from ..pricing.curtailment import BatterySnapshot, distribute_solar_forecast
from ..pricing.discharge_reserve import (
    ReservePlan,
    STATUS_DISABLED,
    battery_reserve_soc_pct,
    consumption_by_slot,
    eligible_capacity_kwh,
    first_sun_covered_start,
    free_space_kwh,
    net_demand_by_slot,
    plan_discharge_reserve,
    reserve_soc_pct as _reserve_soc_pct,
    usable_energy_kwh,
)
from ..pricing.solar_timeline import provider_energy_between, provider_weights
from ..solar_forecast import solar_forecast_local_timezone

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

# When the reserve claims everything stored, floor + the battery's reserve is
# the SOC itself, give or take float rounding (5 + 22.999999999999996 against 28). Far below
# any SOC a battery reports, far above the rounding.
RESERVE_SOC_TOLERANCE_PCT = 1e-6

# The credit is only as good as the forecast behind it, and overnight there is
# nothing to correct it: a day-ahead figure that releases the whole reserve at
# 00:30 buys the evening peak at peak price if the morning turns out cloudy.
# Crediting three quarters of the expected surplus leaves the release to firm up
# through the day, as the 5-minute rebuild walks the real production in.
#
# Applied last, over a figure the charge power and the charge losses have
# already trimmed: this is forecast uncertainty, not physics. Haircutting the
# raw AC surplus first would discount sun that was never going to fit in the
# battery anyway, and read as if the uncertainty had been accounted for.
SURPLUS_CREDIT_FACTOR = 0.75

# Today's remaining sun follows what the panels have produced so far against
# what the provider expected by now, once it expected enough to judge, clamped
# so that a morning of fog cannot write the whole afternoon off.
MEASURED_SOLAR_MIN_EXPECTED_KWH = 0.3
MEASURED_SOLAR_RATIO_MIN = 0.25
MEASURED_SOLAR_RATIO_MAX = 1.5

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
        # Date the sun was last seen covering the house, so the horizon does not
        # flip back to a morning slot when a refreshed forecast dips later on.
        self._sun_took_over_on = None
        # Measured over expected solar so far today, None when not judged.
        self._measured_solar_ratio_value: float | None = None
        self._last_rebuild_mono: float | None = None
        # This cycle's reserve per battery, from reserve_soc_pct().
        self._battery_pct: dict[Any, float] = {}
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

        Synchronous and side-effect free apart from the status snapshot and the
        per-battery split, so the per-battery floor refresh can call it directly.
        """
        self._battery_pct = {}
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
        batteries = self._fleet()
        if batteries is None:
            # Not knowing what the battery holds is not a reason to hold it.
            self._set_status(0.0, "released", GUARD_FLEET_UNKNOWN)
            return 0.0
        fleet = [snapshot for _, snapshot in batteries]
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
        shares = battery_reserve_soc_pct(reserve_kwh, fleet)
        self._battery_pct = {
            coordinator: share for (coordinator, _), share in zip(batteries, shares)
        }
        self._set_status(pct, "reserving" if pct > 0 else "released", reason, plan)
        self._status["battery_reserve_soc_pct"] = {
            snapshot.name: round(share, 2) for snapshot, share in zip(fleet, shares)
        }
        return pct

    def plan_horizon_end(self) -> datetime | None:
        """Where today's reserve plan ends, or None without a plan."""
        plan = self._plan
        if plan is None or (
            self._plan_date is not None and self._now().date() != self._plan_date
        ):
            return None
        return plan.horizon_end

    def projected_reserve_fn(
        self,
    ) -> Callable[[datetime, float, float], float | None] | None:
        """What the reserve would keep at a later slot, for the projection.

        The live reserve only sees what is stored now, so with an empty battery
        the Daily Operation projection spent the midday sun at once although the
        reserve keeps it for the dearer evening as soon as it lands. The
        callable runs the same decision at a later slot's start, at that slot's
        price and with the usable energy and free space the projection has
        there. It works on a copy of the plan, so the status the entities show
        is not touched; None where no slot of the plan covers the time.
        """
        if not self.feature_enabled() or self._release_guard() is not None:
            return None
        plan = self._plan
        if plan is None or (
            self._plan_date is not None and self._now().date() != self._plan_date
        ):
            return None
        snapshot = copy.copy(plan)
        snapshot.min_saving = max(
            0.0,
            float(getattr(self._controller, "discharge_reserve_min_saving", 0.0) or 0.0),
        )
        slots = list(snapshot.slots)
        if not slots:
            return None
        naive = slots[0].start.tzinfo is None

        def reserve_at(start: datetime, usable_kwh: float, free_kwh: float) -> float | None:
            if naive and start.tzinfo is not None:
                start = start.replace(tzinfo=None)
            elif not naive and start.tzinfo is None:
                start = start.replace(tzinfo=slots[0].start.tzinfo)
            slot = next((s for s in slots if s.start <= start < s.end), None)
            if slot is None:
                return None
            try:
                reserve_kwh, _claiming, _reason = snapshot.reserve_kwh_at(
                    start, slot.price, usable_kwh, free_kwh
                )
            except Exception:  # noqa: BLE001 - dashboard projection is optional
                return None
            return reserve_kwh

        return reserve_at

    def battery_reserve_soc_pct(self, coordinator) -> float | None:
        """This cycle's reserve for one battery, in points above its own floor.

        None for a battery that did not back the reserve this cycle.
        """
        return self._battery_pct.get(coordinator)

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

        fleet = self._fleet_snapshots()
        if fleet is None:
            # No battery readable right now. Leaving the throttle unarmed keeps
            # a transient coordinator outage from disabling the reserve for the
            # whole rebuild interval.
            _LOGGER.debug("Discharge reserve: no readable battery, rebuild deferred")
            return

        # The sun is always read over the whole day, whatever the horizon turns
        # out to be: a remaining-today figure spread over only the night's slots
        # lands in full on the one around sunrise.
        day_end = pricing.energy_horizon_end(now)
        day_slots = pricing.get_future_price_slots(horizon_end=day_end)
        consumption, solar, fraction_fn = self._consumption_and_solar(
            pricing, day_slots, now, day_end
        )
        horizon_end = self._sun_covered_horizon_end(
            now, day_slots, consumption, solar, fraction_fn, day_end
        )
        if horizon_end == self._horizon_end(now) and self._past_solar_midpoint(
            now, fraction_fn
        ):
            # In the evening the next sunrise is the whole plan, but tomorrow's
            # dear morning runs until the sun covers the house, as it does after
            # midnight. Tomorrow's prices and dated solar are known by now.
            morning = self._tomorrow_morning(pricing, now, horizon_end)
            if morning is not None:
                horizon_end, day_slots, consumption, solar = morning
        slots = [slot for slot in day_slots if slot.start < horizon_end]
        demand, surplus = self._net_demand_and_surplus(consumption, solar, now)
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
        """Reserve for tonight, through to the next sunrise.

        Holding energy for tomorrow's evening peak would still be wrong on every
        day the sun refills the battery in between, and the planner has no model
        of tomorrow's PV. But the small hours carry no sun either, so that
        argument never covered them: a price peak at 06:00 is tonight's problem,
        and cutting at midnight left it to the grid. The shared horizon helper
        stops at sunrise for exactly this reason, which is the first moment
        tomorrow's PV can start refilling.

        Between midnight and sunrise that is *today's* sunrise, while the helper
        always answers the next day's. Following it there would bring the coming
        evening's dearer hours into the plan: they take every claim, today's PV
        pays for them, and nothing is left held for the morning.
        """
        pricing = self._controller._pricing_mgr
        horizon_end = pricing.energy_horizon_end(now - timedelta(days=1))
        if now >= horizon_end:
            horizon_end = pricing.energy_horizon_end(now)
        return horizon_end

    def _sun_covered_horizon_end(
        self, now: datetime, day_slots, consumption, solar, fraction_fn, day_end
    ) -> datetime:
        """End the morning's plan where the sun takes over, not where it rises.

        Sunrise is when PV starts, not when it carries the house: on 2 Nov the
        battery was empty at 07:15 and the house then imported 2.7 kWh at 0.586,
        including the dearest slot of the morning. So until the sun covers the
        house, those hours are still the night's problem. It also keeps the
        coming evening out, for the reason in :meth:`_horizon_end`: nothing
        before this point has any surplus, so no credit can release the hold.

        A day the sun never covers the house has no surplus to refill the battery
        at all, so the evening's hours stay in the plan. Past the solar midpoint
        the question is settled for the day: a later slot the sun happens to cover
        must not cut the evening off. Without a forecast there is nothing to go
        on, and the horizon stays where :meth:`_horizon_end` puts it.
        """
        fallback = self._horizon_end(now)
        if not solar or fraction_fn is None:
            return fallback
        hour = now.hour + now.minute / 60.0 + now.second / 3600.0
        if float(fraction_fn(hour)) >= 0.5:
            return fallback
        if self._sun_took_over_on == now.date():
            return day_end
        covered = first_sun_covered_start(day_slots, consumption, solar)
        if covered is not None and covered <= now and self._sun_covers_now() is not False:
            # Latched only once the panels agree: on 5 Nov the forecast's slot
            # passed at 10:03 while the house still imported, and the sun had
            # not covered it by 10:45. Until then a later refresh may move it.
            self._sun_took_over_on = now.date()
        if covered is None or covered <= now:
            return day_end
        return covered

    @staticmethod
    def _past_solar_midpoint(now: datetime, fraction_fn) -> bool:
        """True from the middle of today's sun until midnight."""
        hour = now.hour + now.minute / 60.0 + now.second / 3600.0
        if fraction_fn is None:
            return hour >= 12.0
        try:
            return float(fraction_fn(hour)) >= 0.5
        except (TypeError, ValueError):
            return hour >= 12.0

    def _tomorrow_morning(self, pricing, now: datetime, sunrise: datetime):
        """Tomorrow's morning up to where the sun covers the house, or None.

        Returns ``(horizon_end, slots, consumption, solar)`` over the slots to
        tomorrow noon. Tomorrow's sun comes from the provider's dated periods,
        since the forecast figure only covers what is left of today. Without
        those periods, tomorrow's prices, or a slot the sun covers before noon,
        the plan keeps ending at sunrise.
        """
        periods = getattr(self._controller, "solar_forecast_periods", None)
        if not periods:
            return None
        noon = sunrise.replace(hour=12, minute=0, second=0, microsecond=0)
        if noon <= sunrise:
            return None
        slots = [
            slot
            for slot in pricing.get_future_price_slots(horizon_end=noon)
            if slot.start < noon
        ]
        morning = [slot for slot in slots if slot.start >= sunrise]
        if not morning:
            return None
        consumption, solar, _fraction_fn = self._consumption_and_solar(
            pricing, slots, now, noon
        )
        if not consumption:
            return None
        timezone = solar_forecast_local_timezone(self._hass, self._controller, now)

        def local(value: datetime) -> datetime:
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone)
            return value.astimezone(timezone)

        energies, reason = provider_weights(
            [(local(slot.start), local(slot.end)) for slot in morning], periods
        )
        if energies is None:
            _LOGGER.debug(
                "Discharge reserve: tomorrow's solar unknown, plan ends at sunrise (%s)",
                reason,
            )
            return None
        solar = dict(solar)
        for slot, kwh in zip(morning, energies):
            solar[slot] = kwh
        covered = first_sun_covered_start(morning, consumption, solar)
        if covered is None or covered <= sunrise:
            return None
        return covered, slots, consumption, solar

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
        """The live fleet, without the coordinators."""
        batteries = self._fleet()
        if batteries is None:
            return None
        return [snapshot for _, snapshot in batteries]

    def _fleet(self) -> list[tuple[Any, BatterySnapshot]] | None:
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
        snapshots: list[tuple[Any, BatterySnapshot]] = []
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
                (
                    coordinator,
                    BatterySnapshot(
                        name=getattr(coordinator, "name", "battery"),
                        soc_pct=soc,
                        capacity_kwh=capacity,
                        max_soc_pct=max_soc,
                        floor_soc_pct=floor,
                        max_discharge_power_w=0.0,
                        eligible=True,
                        can_discharge=True,
                    ),
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
        """Per slot: the grid demand left after PV, and the PV surplus over it.

        Not used by the rebuild, which needs the solar curve in between; kept as
        the one-call seam the tests use.
        """
        consumption, solar, _fraction_fn = self._consumption_and_solar(
            pricing, slots, now, horizon_end
        )
        return self._net_demand_and_surplus(consumption, solar, now)

    def _consumption_and_solar(
        self, pricing, slots, now: datetime, horizon_end
    ) -> tuple[dict, dict, Any]:
        """Per slot: learned consumption and expected PV, plus the solar curve."""
        if not slots:
            return {}, {}, None
        forecast = pricing._profile_remaining_consumption(now, horizon_end)
        if forecast is None:
            return {}, {}, None
        consumption = consumption_by_slot(
            slots,
            getattr(forecast, "intervals_by_date", None) or {},
            getattr(forecast, "intervals_kwh", None),
        )
        if not consumption:
            return {}, {}, None

        solar: dict = {}
        fraction_fn = None
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
            solar = self._follow_measured_production(
                self._follow_provider_curve(
                    distribute_solar_forecast(
                        slots,
                        float(forecast_kwh),
                        fraction_fn,
                        normalize_future=self._forecast_is_remaining(pricing),
                    ),
                    now,
                ),
                now,
            )
        return consumption, solar, fraction_fn

    def _net_demand_and_surplus(
        self, consumption: dict, solar: dict, now: datetime
    ) -> tuple[dict, dict]:
        """Turn consumption and PV into grid demand and creditable surplus."""
        # Only the sun that can physically land in the battery pays a claim.
        # The forecast is AC-side and unbounded; the battery takes it at a
        # finite charge power and loses part of it on the way in, and the room
        # it is measured against downstream is battery-side energy.
        charge_power_w = max(
            0.0, float(getattr(self._controller, "max_charge_capacity", 0.0) or 0.0)
        )
        hold_state = self._hold_plan_state(now)
        surplus: dict = {}
        for slot in consumption:
            raw_kwh = max(
                0.0,
                float(solar.get(slot, 0.0) or 0.0)
                - float(consumption.get(slot, 0.0) or 0.0),
            )
            duration_h = max(0.0, (slot.end - slot.start).total_seconds() / 3600.0)
            absorbed_kwh = min(raw_kwh, charge_power_w * duration_h / 1000.0)
            if hold_state is not None:
                known_starts, selected_starts = hold_state
                if slot.start in known_starts and slot.start not in selected_starts:
                    # The surplus hold has decided this hour's sun is worth more
                    # exported than stored. Crediting it would release the
                    # reserve against kWh that are never going to arrive.
                    absorbed_kwh = 0.0
            surplus[slot] = SURPLUS_CREDIT_FACTOR * absorbed_kwh * CHARGE_EFFICIENCY
        return net_demand_by_slot(consumption, solar), surplus

    def _follow_provider_curve(self, solar: dict, now: datetime) -> dict:
        """Give each day's solar the shape the forecast provider expects.

        ``distribute_solar_forecast`` shapes the forecast total as a sine from
        the astronomical sunrise, as if the horizon were flat. A provider's
        dated periods (the sensor's own, or the Energy dashboard forecast) know
        when the panels really see the sun: the low morning sun, the provider's
        damping, and the terrain where the provider has a horizon profile.

        Each day keeps the energy the sine gave it, so the forecast total and
        its horizon mean exactly what they did; only the hour-by-hour split
        moves. A day the periods do not cover keeps the sine.
        """
        # Read with the forecast by ``_curtailment_forecast_model`` just before.
        periods = getattr(self._controller, "solar_forecast_periods", None)
        if not periods or not solar:
            return solar
        timezone = solar_forecast_local_timezone(self._hass, self._controller, now)

        def local(value: datetime) -> datetime:
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone)
            return value.astimezone(timezone)

        days: dict = {}
        for slot in sorted(solar, key=lambda slot: local(slot.start)):
            days.setdefault(local(slot.start).date(), []).append(slot)
        shaped = dict(solar)
        for day_slots in days.values():
            budget = sum(float(solar[slot] or 0.0) for slot in day_slots)
            lit = [slot for slot in day_slots if float(solar[slot] or 0.0) > 0.0]
            if budget <= 0.0 or not lit:
                continue
            # Daylight as the sine sees it, so a provider that leaves its dark
            # hours out (Solcast) still counts as covering the day.
            weights, reason = provider_weights(
                [(local(slot.start), local(slot.end)) for slot in day_slots],
                periods,
                solar_start=local(lit[0].start),
                solar_end=local(lit[-1].end),
            )
            if weights is None:
                _LOGGER.debug(
                    "Discharge reserve: provider solar curve not used (%s)", reason
                )
                continue
            total = sum(weights)
            for slot, weight in zip(day_slots, weights):
                shaped[slot] = budget * weight / total
        return shaped

    def _follow_measured_production(self, solar: dict, now: datetime) -> dict:
        """Scale today's remaining sun by how the panels have done so far.

        The provider's level can be far off for a whole day: on 5 Nov the
        panels made about half of it all morning, so the sun-covered slot came
        hours early and the credit paid off claims the sun never met. Today's
        slots still ahead take the measured share; tomorrow keeps the forecast.
        """
        ratio = self._measured_solar_ratio(now)
        self._measured_solar_ratio_value = ratio
        if ratio is None or not solar:
            return solar
        timezone = solar_forecast_local_timezone(self._hass, self._controller, now)
        today = now.astimezone(timezone).date() if now.tzinfo else now.date()

        def local_date(value: datetime):
            return value.astimezone(timezone).date() if value.tzinfo else value.date()

        return {
            slot: (
                float(kwh or 0.0) * ratio
                if slot.end > now and local_date(slot.start) == today
                else kwh
            )
            for slot, kwh in solar.items()
        }

    def _measured_solar_ratio(self, now: datetime) -> float | None:
        """Today's measured solar over what the provider expected by ``now``.

        None unless something measures production: without a source the daily
        accumulator still rolls over at midnight and reads 0.0 all day, which
        would cut the remaining sun to the lower clamp for good.

        Curtailment (battery full, an export limit) and a restart during the day
        also lower the ratio, without the sky being duller; the lower clamp
        bounds how far that can cut the remaining sun.
        """
        controller = self._controller
        periods = getattr(controller, "solar_forecast_periods", None)
        if not periods or not has_solar_production_source(controller):
            return None
        if getattr(controller, "_daily_solar_energy_date", None) != now.date():
            return None
        try:
            measured = float(getattr(controller, "_daily_solar_energy_kwh", None))
        except (TypeError, ValueError):
            return None
        timezone = solar_forecast_local_timezone(self._hass, self._controller, now)
        local_now = now.astimezone(timezone) if now.tzinfo else now.replace(tzinfo=timezone)
        midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        expected = provider_energy_between(periods, midnight, local_now)
        if expected is None or expected < MEASURED_SOLAR_MIN_EXPECTED_KWH or measured < 0:
            return None
        return min(MEASURED_SOLAR_RATIO_MAX, max(MEASURED_SOLAR_RATIO_MIN, measured / expected))

    def _sun_covers_now(self) -> bool | None:
        """Whether the panels carry the house right now, None if unmeasured."""
        tracker = getattr(self._controller, "_consumption_tracker", None)
        if tracker is None:
            return None
        try:
            solar_kw = tracker._read_total_solar_power_kw()
            home_kw = tracker.get_validated_home_power_kw()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Discharge reserve: measured solar or load unreadable: %s", err)
            return None
        if solar_kw is None or home_kw is None:
            return None
        return float(solar_kw) >= float(home_kw)

    def _hold_plan_state(self, now: datetime) -> tuple[set, set] | None:
        """Return ``(slots the hold planned, slots it selected)``, or None.

        None means the surplus hold has no opinion this cycle and the reserve
        must credit the sun as if it were free to land: no manager, no plan, a
        plan that cannot hold (disabled, infeasible, nothing left to absorb,
        fail-safe -- all of them the same ``status`` field), a live target it
        could not recompute, or a plan built on another day.

        Slots are matched on ``start`` alone. Both planners distribute over the
        same price calendar, so the boundaries line up; only the price side
        differs (export for the hold, import for the reserve).
        """
        manager = getattr(self._controller, "_surplus_hold_mgr", None)
        if manager is None:
            return None
        try:
            plan = manager.plan
            if plan is None or not plan.can_hold:
                return None
            if manager.live_target_unreliable:
                return None
            evaluated_at = plan.evaluation_time
            if evaluated_at is None or evaluated_at.date() != now.date():
                return None
            return (
                {slot.start for slot in plan.slots},
                {slot.start for slot in plan.selected_slots},
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Discharge reserve: surplus hold plan unreadable: %s", err)
            return None

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

    def _set_status(
        self, pct: float, state: str, reason: str, plan: ReservePlan | None = None
    ) -> None:
        """Publish the snapshot. ``plan`` only when it was recomputed this cycle.

        A guard release never reaches ``reserve_kwh_at``, so the plan it still
        holds describes the last cycle that did. Publishing those figures under
        ``state: released`` is exactly the disagreement these attributes exist
        to explain away.
        """
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
                    # The price a slot has to beat this cycle, and what the
                    # claims and the sun did with it. reserve_kwh is
                    # claimed_kwh minus pv_credit_kwh, capped at the usable
                    # energy, so the two cycles that
                    # disagree now say which of the three moved.
                    "threshold_price": (
                        None
                        if plan.threshold_price is None
                        else round(plan.threshold_price, 5)
                    ),
                    "claimed_kwh": round(plan.claimed_kwh, 3),
                    "pv_credit_kwh": round(plan.pv_credit_kwh, 3),
                    "horizon_demand_kwh": round(plan.horizon_demand_kwh, 3),
                    "horizon_surplus_kwh": round(plan.horizon_surplus_kwh, 3),
                    "measured_solar_ratio": (
                        None
                        if self._measured_solar_ratio_value is None
                        else round(self._measured_solar_ratio_value, 3)
                    ),
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
                    # Every claim, including the ones the sun paid off in full,
                    # which never reach reserved_slots.
                    "claims": [
                        {
                            "start": slot.start.isoformat(),
                            "price": round(slot.price, 5),
                            "claimed_kwh": round(claimed, 3),
                            "pv_credit_kwh": round(credit, 3),
                            "expected_surplus_kwh": round(
                                slot.expected_surplus_kwh, 3
                            ),
                        }
                        for slot, claimed, credit in plan.claim_breakdown
                    ],
                }
            )
        self._status = status
