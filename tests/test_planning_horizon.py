"""Charge planning across tomorrow once its prices are known (#710).

``planning_horizon_end`` only extends past the sunrise horizon when tomorrow's
prices AND dated solar periods for tomorrow exist (plus a learned consumption
profile). The extended plan is sized from its deadlines, so tonight's slots
compete with tomorrow's on price and tomorrow's sun is credited.

Same conventions as ``test_pricing_engine.py``: no hardware, no running Home
Assistant, stub controller. Wall clock is naive local time, as in the control
path; the stub tracker has no sunrise, so the old horizon is the next midnight.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.solar_forecast import (
    SolarForecastInput,
    SolarForecastPeriod,
)
from custom_components.omnibattery.tracking.consumption_profile import (
    ConsumptionForecast,
    INTERVAL_COUNT,
)

NOW = datetime(2026, 11, 5, 20, 0)
MIDNIGHT = datetime(2026, 11, 6, 0, 0)
CAP = datetime(2026, 11, 7, 0, 0)
UTC = timezone.utc
LOAD_KW = 0.5  # flat house load


def _slot(day_offset, hour, price):
    start = datetime(2026, 11, 5 + day_offset, hour, 0)
    return PriceSlot(start, start + timedelta(hours=1), price)


def _tonight(price):
    return [_slot(0, hour, price) for hour in range(20, 24)]


def _tomorrow(prices):
    """24 hourly slots tomorrow; ``prices`` maps hour -> price, default 0.30."""
    return [_slot(1, hour, prices.get(hour, 0.30)) for hour in range(24)]


def _solar(kwh_total):
    """Tomorrow's sun: ``kwh_total`` spread over 10:00-14:00."""
    return tuple(
        SolarForecastPeriod(
            datetime(2026, 11, 6, hour, tzinfo=UTC),
            datetime(2026, 11, 6, hour + 1, tzinfo=UTC),
            kwh_total / 4,
        )
        for hour in range(10, 14)
    )


def _forecast_between(start, end):
    """Flat load over [start, end), with the per-date shapes the planner reads."""
    shape = [LOAD_KW / 4] * INTERVAL_COUNT
    hours = (end - start).total_seconds() / 3600.0
    return ConsumptionForecast(
        LOAD_KW * hours,
        list(shape),
        "profile",
        True,
        intervals_by_date={
            (start + timedelta(days=i)).date(): list(shape) for i in range(3)
        },
    )


def _manager(solar_periods=None, soc=30.0):
    tracker = SimpleNamespace(
        consumption_profile=SimpleNamespace(),
        forecast_consumption_between=lambda start, end, **_kw: _forecast_between(start, end),
    )
    coordinator = SimpleNamespace(
        data={"battery_soc": soc, "battery_total_energy": 10.0},
        min_soc=10,
        max_soc=100,
    )
    controller = SimpleNamespace(
        _consumption_tracker=tracker,
        coordinators=[coordinator],
        _is_battery_manual_owned=lambda _c: False,
        solar_profile_mode="active",
        _predictive_safety_margin_kwh=0.0,
        max_contracted_power=3000,
        max_charge_capacity=3000,
        max_price_threshold=None,
        predictive_charging_mode="time_slot",
        dp_price_discharge_control=False,
        # Unused by the planner, read by get_future_price_slots' cache key.
        price_sensor=None,
    )
    manager = PricingManager(
        SimpleNamespace(config=SimpleNamespace(time_zone="UTC")), controller
    )
    periods = solar_periods
    manager._read_remaining_solar_input = lambda **_kw: (
        SolarForecastInput(0.0, "remaining_sensor", periods=periods)
        if periods
        else None
    )
    return manager


def _plan(manager, slots, planning_end):
    decision = {
        "avg_consumption_kwh": 0.0,
        "planned_grid_charge_kwh": 0.0,
        "should_charge": False,
        "solar_forecast_input": manager._read_remaining_solar_input(),
    }
    plan = manager._build_chronological_plan(
        now=NOW,
        slots=slots,
        decision_data=decision,
        price_ceiling=None,
        planning_end=planning_end,
    )
    return plan, decision


def _tonight_kwh(plan):
    return sum(
        a.planned_battery_kwh for a in plan.allocations if a.slot.start < MIDNIGHT
    )


# ---------------------------------------------------------------------------
# planning_horizon_end
# ---------------------------------------------------------------------------

def test_horizon_unchanged_without_tomorrow_prices():
    manager = _manager(_solar(5.0))
    assert manager.planning_horizon_end(NOW, _tonight(0.10)) == MIDNIGHT


def test_horizon_unchanged_without_tomorrow_solar_periods():
    manager = _manager(None)
    slots = _tonight(0.10) + _tomorrow({})
    assert manager.planning_horizon_end(NOW, slots) == MIDNIGHT


def test_horizon_unchanged_when_periods_do_not_reach_tomorrow():
    today_only = (SolarForecastPeriod(datetime(2026, 11, 5, 9, tzinfo=UTC), datetime(2026, 11, 5, 10, tzinfo=UTC), 1.0),)
    manager = _manager(today_only)
    slots = _tonight(0.10) + _tomorrow({})
    assert manager.planning_horizon_end(NOW, slots) == MIDNIGHT


def test_horizon_unchanged_without_a_learned_consumption_profile():
    manager = _manager(_solar(5.0))
    manager._profile_remaining_consumption = lambda *_a: None
    slots = _tonight(0.10) + _tomorrow({})
    assert manager.planning_horizon_end(NOW, slots) == MIDNIGHT


def test_horizon_unchanged_when_the_solar_profile_mode_is_off():
    manager = _manager(_solar(5.0))
    manager._controller.solar_profile_mode = "off"
    slots = _tonight(0.10) + _tomorrow({})
    assert manager.planning_horizon_end(NOW, slots) == MIDNIGHT


def test_horizon_extends_to_the_end_of_the_price_data():
    manager = _manager(_solar(5.0))
    slots = _tonight(0.10) + _tomorrow({})[:18]
    assert manager.planning_horizon_end(NOW, slots) == datetime(2026, 11, 6, 18, 0)


def test_horizon_is_bounded_to_the_sunrise_after_tomorrow():
    manager = _manager(_solar(5.0))
    slots = _tonight(0.10) + _tomorrow({}) + [_slot(2, hour, 0.30) for hour in range(24)]
    assert manager.planning_horizon_end(NOW, slots) == CAP


# ---------------------------------------------------------------------------
# Sizing across the extended horizon
# ---------------------------------------------------------------------------

def test_weak_tomorrow_sun_charges_tonight_for_tomorrows_evening_up_to_headroom():
    """Tonight is cheapest and tomorrow's sun cannot carry the evening."""
    manager = _manager(_solar(3.0))
    slots = _tonight(0.10) + _tomorrow({18: 0.50, 19: 0.50, 20: 0.50})
    plan, decision = _plan(manager, slots, CAP)

    headroom = 7.0
    # More than tonight's own gap (5 kWh to 10:00), capped by what fits.
    assert _tonight_kwh(plan) > 5.5
    assert _tonight_kwh(plan) <= headroom + 1e-6
    # The scalar the schedule and notification read agrees with the plan.
    assert decision["should_charge"] is True
    assert decision["planned_grid_charge_kwh"] == pytest.approx(plan.total_required_kwh)


def test_strong_tomorrow_sun_buys_no_extra_energy_tonight():
    manager = _manager(_solar(20.0))
    slots = _tonight(0.10) + _tomorrow({18: 0.50, 19: 0.50, 20: 0.50})
    plan, decision = _plan(manager, slots, CAP)

    # Only tonight's own gap (consumption until the sun is up, less what is stored).
    assert _tonight_kwh(plan) == pytest.approx(5.0, abs=0.3)
    assert plan.total_required_kwh == pytest.approx(_tonight_kwh(plan), abs=0.3)
    # A scalar balance that netted tomorrow's sun would have said "no charge".
    assert decision["should_charge"] is True


def test_cheaper_tomorrow_midday_is_not_pre_bought_tonight():
    manager = _manager(_solar(3.0))
    cheap_midday = {hour: 0.05 for hour in range(10, 14)}
    slots = _tonight(0.20) + _tomorrow(cheap_midday)
    plan, _ = _plan(manager, slots, CAP)

    tomorrow_kwh = sum(
        a.planned_battery_kwh for a in plan.allocations if a.slot.start >= MIDNIGHT
    )
    assert tomorrow_kwh > 1.0
    # Tonight only covers the gap before the sun, not the evening.
    assert _tonight_kwh(plan) == pytest.approx(5.0, abs=0.3)


def test_unextended_plan_is_sized_from_the_scalar_as_before():
    manager = _manager(None)
    plan, decision = _plan(manager, _tonight(0.10), None)

    assert plan is not None
    assert plan.total_required_kwh == 0.0
    assert decision["should_charge"] is False
    assert decision["planned_grid_charge_kwh"] == 0.0


# ---------------------------------------------------------------------------
# Balance, executable part of the plan, notification
# ---------------------------------------------------------------------------

def test_balance_credits_tomorrows_sun_only_on_an_extended_horizon():
    import asyncio

    manager = _manager(_solar(8.0))
    seen = []

    async def _decision(**kwargs):
        seen.append(kwargs["solar_forecast_override_kwh"])
        return {"should_charge": False}

    async def _average():
        return 12.0

    controller = manager._controller
    controller._should_activate_grid_charging = _decision
    controller._consumption_tracker.get_dynamic_base_consumption = _average
    controller._daily_home_energy_date = None
    controller._daily_home_energy_kwh = 0.0
    controller._household_accumulator_date = None
    manager._remaining_solar_today_kwh = lambda _now: 1.0

    asyncio.run(manager._evaluate_remaining_grid_charging(now=NOW))
    asyncio.run(manager._evaluate_remaining_grid_charging(now=NOW, horizon_end=CAP))

    assert seen == [pytest.approx(1.0), pytest.approx(9.0)]


def test_plan_within_drops_allocations_outside_the_control_slots():
    manager = _manager(_solar(3.0))
    cheap_midday = {hour: 0.05 for hour in range(10, 14)}
    slots = _tonight(0.20) + _tomorrow(cheap_midday)
    plan, _ = _plan(manager, slots, CAP)

    trimmed = manager._plan_within(plan, _tonight(0.20))

    assert trimmed.allocations
    assert all(a.slot.start < MIDNIGHT for a in trimmed.allocations)
    assert len(plan.allocations) > len(trimmed.allocations)
    assert manager._plan_within(plan, slots) is plan


def test_notification_names_energy_deferred_to_tomorrow():
    from custom_components.omnibattery.pricing import notifications

    title, message = notifications.format_dynamic_pricing_notification(
        {"should_charge": True, "energy_deficit_kwh": 3.0, "deferred_to_next_day": True},
        None,
        unit="EUR/kWh",
        max_price_threshold=None,
        discharge_price_threshold=None,
        max_contracted_power=3000,
        max_charge_capacity=3000,
    )

    assert "nothing to charge yet" in title
    assert "tomorrow" in message


def test_extended_horizon_notification_labels_tomorrow_not_tonight():
    """#721: a horizon ending at tomorrow's midnight was labelled "until
    midnight", its whole-day overnight leg "until sunrise", and today's solar
    plus tomorrow's forecast "Solar remaining today"."""
    import asyncio

    from custom_components.omnibattery.pricing import notifications

    manager = _manager(_solar(8.0))

    async def _decision(**kwargs):
        return {
            "should_charge": False,
            "solar_forecast_kwh": kwargs["solar_forecast_override_kwh"],
            "usable_energy_kwh": 5.0,
            "avg_soc": 50.0,
            "total_available_kwh": 30.0,
            "energy_deficit_kwh": 0.0,
        }

    async def _average():
        return 12.0

    controller = manager._controller
    controller._should_activate_grid_charging = _decision
    controller._consumption_tracker.get_dynamic_base_consumption = _average
    controller._daily_home_energy_date = None
    controller._daily_home_energy_kwh = 0.0
    controller._household_accumulator_date = None
    manager._remaining_solar_today_kwh = lambda _now: 0.0

    def _message(decision):
        return notifications.format_predictive_notification_message(
            decision,
            max_contracted_power=3000,
            max_charge_capacity=3000,
            charging_time_slot=None,
        )[1]

    message = _message(
        asyncio.run(manager._evaluate_remaining_grid_charging(now=NOW, horizon_end=CAP))
    )
    assert "remaining until tomorrow midnight: 14.00 kWh" in message
    assert "🌙 Of which tomorrow (00:00-24:00): 12.00 kWh" in message
    assert "Solar remaining today: 0.00 kWh" in message
    assert "Solar tomorrow: 8.00 kWh" in message
    assert "until sunrise" not in message
    assert "Sufficient energy for the rest of today and tomorrow" in message

    # The unextended horizon ends at tonight's midnight: wording unchanged.
    message = _message(asyncio.run(manager._evaluate_remaining_grid_charging(now=NOW)))
    assert "remaining until midnight: 2.00 kWh" in message
    assert "Solar tomorrow" not in message
    assert "Sufficient energy for the rest of today\n" in message


def test_extended_planning_end_is_none_until_tomorrow_is_planable():
    manager = _manager(_solar(5.0))
    manager.get_future_price_slots = lambda horizon_end=None: _tonight(0.10)
    assert manager._extended_planning_end(NOW) is None

    manager.get_future_price_slots = lambda horizon_end=None: _tonight(0.10) + _tomorrow({})
    assert manager._extended_planning_end(NOW) == CAP
