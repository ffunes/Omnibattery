"""Both dynamic-pricing planners active on the same day.

`surplus_price_hold` blocks charging while `price_reserve` blocks discharging,
so the battery is held in both directions at once. That is the intended
outcome of two correct decisions -- the cheap feed-in window is still ahead
and the evening peak still needs the stored energy -- but it is also the state
a user is most likely to report as "my battery does nothing", so each blocker
must name itself and each manager must publish a reason the panel can show.

No Home Assistant and no Modbus: both managers talk to one stand-in controller
(SimpleNamespace) carrying the union of the attributes their guards read.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import PREDICTIVE_MODE_DYNAMIC_PRICING
from custom_components.omnibattery.control.discharge_reserve import (
    DischargeReserveManager,
)
from custom_components.omnibattery.control.surplus_price_hold import (
    BLOCKER_SOURCE,
    SurplusPriceHoldManager,
)
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.curtailment import BatterySnapshot
from custom_components.omnibattery.pricing.surplus_absorption import (
    REASON_CHEAPER_WINDOW_AHEAD,
    plan_surplus_absorption,
)

DAY = datetime(2026, 9, 4)
NOW = DAY + timedelta(hours=9, minutes=30)
DEADLINE = DAY + timedelta(hours=19)

CAPACITY_KWH = 10.0
EVENING_DEMAND_KWH = 4.0


class _HashableNamespace(SimpleNamespace):
    """Test double usable as a coordinator-keyed dict key."""

    __hash__ = object.__hash__
    __eq__ = object.__eq__


def _slot(hour: float, price: float) -> PriceSlot:
    start = DAY + timedelta(hours=hour)
    return PriceSlot(start, start + timedelta(hours=1), price)


# The day both planners see: cheap feed-in still ahead at 12:00, and an
# evening peak at 18:00-20:00 that the stored energy is wanted for.
SLOTS = [_slot(9, 0.28), _slot(12, 0.12), _slot(18, 0.45), _slot(19, 0.45)]


def _coordinator(soc: float = 45.0):
    return _HashableNamespace(
        name="battery-1",
        data={"battery_soc": soc, "battery_total_energy": CAPACITY_KWH},
        is_available=True,
        battery_manual_mode_enabled=False,
        min_soc=10.0,
        max_soc=100.0,
    )


def _snapshot(soc: float = 80.0) -> BatterySnapshot:
    return BatterySnapshot("battery-1", soc, CAPACITY_KWH, 100.0, 10.0, 2500.0, True)


def _evening_profile():
    """A learned profile with all of its energy in the 18:00-20:00 block."""
    intervals = [0.0] * 96
    for index in range(72, 80):
        intervals[index] = EVENING_DEMAND_KWH / 8.0
    return SimpleNamespace(
        intervals_kwh=intervals,
        intervals_by_date={DAY.date(): intervals},
        energy_kwh=EVENING_DEMAND_KWH,
        source="profile",
    )


def _pricing():
    """One pricing stand-in serving both managers."""
    return SimpleNamespace(
        # Read by the discharge reserve.
        get_future_price_slots=lambda horizon_end=None: list(SLOTS),
        _profile_remaining_consumption=lambda start, end: _evening_profile(),
        _curtailment_forecast_model=lambda now: (0.0, None, None),
        _get_current_price=lambda: 0.28,
        # Read by the surplus hold.
        is_in_dynamic_pricing_slot=lambda: False,
        _negative_price_feature_enabled=lambda: False,
        _opportunistic_target_pending=lambda: False,
        _current_price_is_opportunistic=lambda: False,
        _curtailment_battery_snapshots=lambda: [_snapshot()],
    )


def _controller(coordinator=None):
    coordinator = coordinator or _coordinator()
    charge_blockers: dict[str, dict] = {}
    discharge_blockers: dict[str, dict] = {}

    controller = SimpleNamespace(
        # Shared scope.
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        manual_mode_enabled=False,
        coordinators=[coordinator],
        _manual_slot_owned=None,
        _is_manual_slot_owned=lambda coordinator: False,
        _capacity_protection_active=False,
        _curtailment_runtime_status=None,
        # Surplus hold.
        surplus_price_hold_enabled=True,
        surplus_hold_min_saving=0.0,
        charge_delay_enabled=False,
        _charge_delay_mgr=None,
        _current_price_slot_active=False,
        _curtailment_opportunistic_charge_limit_w=0.0,
        _weekly_charge_mgr=None,
        _force_full_charge=False,
        _predictive_safety_margin_kwh=0.0,
        max_charge_capacity=2500.0,
        _solar_t_start=6.0,
        _consumption_tracker=SimpleNamespace(
            estimate_t_end=lambda: 19.0,
            calculate_sunrise=lambda: 6.0,
            calculate_solar_noon=lambda: 13.0,
        ),
        # Discharge reserve.
        discharge_reserve_enabled=True,
        discharge_reserve_min_saving=0.05,
        _price_reserve_soc_pct=0.0,
    )
    controller._pricing_mgr = _pricing()

    controller.get_charge_blockers = lambda coordinator=None: dict(charge_blockers)
    controller.set_charge_block = (
        lambda source, reason, details=None, coordinator=None: charge_blockers.__setitem__(
            source, {"reason": reason, "details": details}
        )
    )
    controller.remove_charge_block = (
        lambda source, coordinator=None: charge_blockers.pop(source, None)
    )
    controller.get_discharge_blockers = (
        lambda coordinator=None: dict(discharge_blockers)
    )
    controller.set_discharge_block = (
        lambda source, reason, details=None, coordinator=None: discharge_blockers.__setitem__(
            source, {"reason": reason, "details": details}
        )
    )
    controller.remove_discharge_block = (
        lambda source, coordinator=None: discharge_blockers.pop(source, None)
    )
    # The effective floor is the configured one here: no slot override is in
    # play, so the reserve is added straight on top of min_soc.
    controller._effective_discharge_min_soc = lambda coordinator: (10.0, None)
    controller._price_discharge_reserve_pct = (
        ChargeDischargeController._price_discharge_reserve_pct.__get__(controller)
    )
    return controller


def _holding_plan():
    """A surplus plan that holds at NOW: the cheap window is still ahead."""
    slots = [SLOTS[0], SLOTS[1]]
    plan = plan_surplus_absorption(
        slots,
        {slot: 2.0 for slot in slots},
        [_snapshot()],
        remaining_consumption_kwh=1.0,
        usable_energy_kwh=0.0,
        max_charge_power_w=2500.0,
        deadline=DEADLINE,
        min_saving=0.0,
        now=NOW,
    )
    assert plan.status == "planned"
    return plan


def _both_managers(controller):
    hold = SurplusPriceHoldManager(None, controller)
    hold._now = lambda: NOW
    hold._plan = _holding_plan()
    hold._plan_date = NOW.date()
    hold._live_target_kwh = hold._plan.target_kwh
    controller._surplus_hold_mgr = hold

    reserve = DischargeReserveManager(SimpleNamespace(), controller)
    reserve._now = lambda: NOW
    asyncio.get_event_loop().run_until_complete(reserve.async_rebuild_plan("test"))
    controller._discharge_reserve_mgr = reserve
    return hold, reserve


def _refresh(controller) -> None:
    """Run the two blocker refreshes the control cycle runs."""
    ChargeDischargeController._refresh_surplus_price_hold_block(controller)
    ChargeDischargeController._refresh_price_reserve_blocks(controller)


def test_both_planners_can_hold_the_battery_on_the_same_day():
    """Charge held for a cheaper feed-in hour, discharge held for the peak."""
    controller = _controller()
    hold, reserve = _both_managers(controller)

    assert hold.is_hold_active() is True
    # The evening wants 4 kWh; only 3.5 kWh sits above the 10% floor, so the
    # reserve claims all of it: 35 SOC points on a 10 kWh battery.
    assert reserve.reserve_soc_pct() == pytest.approx(35.0)

    _refresh(controller)

    assert BLOCKER_SOURCE in controller.get_charge_blockers()
    assert "price_reserve" in controller.get_discharge_blockers()


def test_a_battery_frozen_both_ways_says_why_in_both_directions():
    """The state a user reports as "it does nothing" must be explainable."""
    controller = _controller()
    hold, reserve = _both_managers(controller)
    _refresh(controller)

    charge = controller.get_charge_blockers()[BLOCKER_SOURCE]
    assert charge["reason"] == BLOCKER_SOURCE
    assert charge["details"]["reason"] == REASON_CHEAPER_WINDOW_AHEAD

    discharge = controller.get_discharge_blockers()["price_reserve"]
    assert discharge["reason"] == "price_reserve"
    assert discharge["details"]["reserve_soc_pct"] == pytest.approx(35.0)
    assert discharge["details"]["reserved_floor"] == pytest.approx(45.0)

    # Both managers publish their own reason, so the panel never has to
    # attribute the standstill to the wrong feature.
    assert hold.get_status()["reason"] == REASON_CHEAPER_WINDOW_AHEAD
    assert reserve.get_status()["state"] == "reserving"


def test_neither_hold_swallows_the_other_when_one_releases():
    """Releasing one direction leaves the other exactly as it was."""
    controller = _controller()
    hold, reserve = _both_managers(controller)
    _refresh(controller)
    assert BLOCKER_SOURCE in controller.get_charge_blockers()
    assert "price_reserve" in controller.get_discharge_blockers()

    # The saving the evening peak offers no longer clears the slider.
    controller.discharge_reserve_min_saving = 0.50
    _refresh(controller)
    assert BLOCKER_SOURCE in controller.get_charge_blockers()
    assert "price_reserve" not in controller.get_discharge_blockers()

    # And back the other way: the reserve holds while the surplus hold is off.
    controller.discharge_reserve_min_saving = 0.05
    controller.surplus_price_hold_enabled = False
    _refresh(controller)
    assert BLOCKER_SOURCE not in controller.get_charge_blockers()
    assert "price_reserve" in controller.get_discharge_blockers()


def test_the_reserve_never_holds_a_battery_below_its_own_floor():
    """A battery already at the reserved floor is blocked, above it is free."""
    coordinator = _coordinator(soc=80.0)
    controller = _controller(coordinator)
    _both_managers(controller)

    # Floor 10 + reserve 40 = 50: 80% is above it and may still discharge.
    _refresh(controller)
    assert "price_reserve" not in controller.get_discharge_blockers()

    coordinator.data = {"battery_soc": 45.0, "battery_total_energy": CAPACITY_KWH}
    _refresh(controller)
    assert "price_reserve" in controller.get_discharge_blockers()
