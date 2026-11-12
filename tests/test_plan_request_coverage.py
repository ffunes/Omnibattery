"""One planner call per input path, so the refactor golden records each of them.

``tests/golden_requests.py`` records every ``ChronologicalEvaluationRequest``
of the suite.  These tests only make sure the planner is exercised through the
battery-idle masks, Time Slot windows, weekly full charge, extended horizons,
the excluded-device claim and the guaranteed floor; they assert that a plan is
built, nothing more.  Stub style as in ``test_planning_horizon.py``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.omnibattery.const import (
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_TIME_SLOT,
)
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.solar_forecast import (
    SolarForecastInput,
    SolarForecastPeriod,
)
from custom_components.omnibattery.tracking.consumption_profile import (
    INTERVAL_COUNT,
    ConsumptionForecast,
)
from custom_components.omnibattery.tracking.solar_profile import SolarProfileSnapshot

UTC = timezone.utc
NOW = datetime(2026, 11, 5, 20, 0)
MIDNIGHT = datetime(2026, 11, 6, 0, 0)
CAP = datetime(2026, 11, 7, 0, 0)
LOAD_KW = 0.5


def _slot(day_offset, hour, price):
    start = datetime(2026, 11, 5 + day_offset, hour, 0)
    return PriceSlot(start, start + timedelta(hours=1), price)


def _tonight(price=0.10):
    return [_slot(0, hour, price) for hour in range(20, 24)]


def _tomorrow(count=24, price=0.30):
    return [_slot(1, hour, price) for hour in range(count)]


def _forecast_between(start, end):
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


def _manager(*, periods=None, soc=30.0, sunrise=7.0, **controller):
    tracker = SimpleNamespace(
        consumption_profile=SimpleNamespace(),
        forecast_consumption_between=lambda s, e, **_kw: _forecast_between(s, e),
        calculate_sunrise=lambda *_a: sunrise,
        calculate_solar_noon=lambda *_a: 12.0,
    )
    attrs = dict(
        _consumption_tracker=tracker,
        coordinators=[
            SimpleNamespace(
                data={"battery_soc": soc, "battery_total_energy": 10.0},
                min_soc=10,
                max_soc=100,
            )
        ],
        _is_battery_manual_owned=lambda _c: False,
        solar_profile_mode="active",
        _predictive_safety_margin_kwh=0.0,
        max_contracted_power=3000,
        max_charge_capacity=3000,
        max_price_threshold=None,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        dp_price_discharge_control=False,
        price_sensor=None,
    )
    attrs.update(controller)
    manager = PricingManager(
        SimpleNamespace(config=SimpleNamespace(time_zone="UTC")),
        SimpleNamespace(**attrs),
    )
    manager._read_remaining_solar_input = lambda **_kw: (
        SolarForecastInput(0.0, "remaining_sensor", periods=periods)
        if periods
        else None
    )
    return manager


def _solar_periods(kwh_total=5.0):
    return tuple(
        SolarForecastPeriod(
            datetime(2026, 11, 6, hour, tzinfo=UTC),
            datetime(2026, 11, 6, hour + 1, tzinfo=UTC),
            kwh_total / 4,
        )
        for hour in range(10, 14)
    )


def _plan(manager, slots, *, now=NOW, planning_end=None, remaining=2.0, **extra):
    decision = {
        "avg_consumption_kwh": 4.0,
        "planned_grid_charge_kwh": 2.0,
        "should_charge": True,
        "solar_forecast_input": SolarForecastInput(
            remaining, "remaining_sensor", periods=None
        ),
        **extra,
    }
    plan = manager._build_chronological_plan(
        now=now,
        slots=slots,
        decision_data=decision,
        price_ceiling=None,
        planning_end=planning_end,
    )
    assert plan is not None
    assert not str(decision["chronological_plan_reason"]).startswith("fallback")
    return plan, decision


EVERY_DAY = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def test_peak_shaving_hold_active():
    manager = _manager(
        _is_capacity_protection_soc_limited=lambda: True,
        capacity_protection_limit=1000,
    )
    _plan(manager, _tonight())


def test_fleet_wide_no_discharge_window():
    manager = _manager(
        config_entry=SimpleNamespace(
            data={
                "no_discharge_time_slots": [
                    {
                        "days": EVERY_DAY,
                        "start_time": "22:00:00",
                        "end_time": "23:30:00",
                        "battery_scope": "all",
                    }
                ]
            }
        )
    )
    _plan(manager, _tonight())


def test_price_based_discharge_control_blocks_cheap_intervals():
    manager = _manager(
        dp_price_discharge_control=True, discharge_price_threshold=0.20
    )
    prices = [_slot(0, 20, 0.10), _slot(0, 21, 0.50), _slot(0, 22, 0.15), _slot(0, 23, 0.40)]
    manager.get_future_price_slots = lambda horizon_end=None: prices
    _plan(manager, prices)


def test_time_slot_mode_inside_a_configured_window():
    manager = _manager(
        predictive_charging_mode=PREDICTIVE_MODE_TIME_SLOT,
        charging_time_slots=[
            {"days": EVERY_DAY, "start_time": "21:00:00", "end_time": "23:00:00"}
        ],
    )
    _plan(manager, _tonight(0.0))


def _weekly(overnight):
    return dict(
        _weekly_full_charge_pending=lambda: True,
        _weekly_charge_mgr=SimpleNamespace(
            window_end=lambda _now: datetime(2026, 11, 6, 17, 0),
            in_overnight_extension=lambda _now: overnight,
        ),
    )


def test_weekly_full_charge_pending_overnight():
    _plan(_manager(**_weekly(True)), _tonight())


def test_weekly_full_charge_pending_not_overnight():
    _plan(_manager(**_weekly(False)), _tonight(), now=datetime(2026, 11, 5, 14, 0))


def test_extended_horizon_with_provider_dated_periods():
    manager = _manager(periods=_solar_periods())
    slots = _tonight() + _tomorrow()
    _plan(
        manager,
        slots,
        planning_end=CAP,
        solar_forecast_input=SolarForecastInput(
            0.0, "remaining_sensor", periods=_solar_periods()
        ),
    )


def test_extended_horizon_with_learned_profile_periods():
    manager = _manager(sunrise=8.0)
    tri = [min(i + 1, 96 - i) for i in range(96)]
    shape = tuple(v / sum(tri) for v in tri)
    manager._controller._consumption_tracker.solar_profile = SimpleNamespace(
        get_snapshot=lambda target_date=None, **_kw: SolarProfileSnapshot(
            shape=shape, mature=True
        ),
        recent_daily_energy_kwh=lambda _date: 6.0,
    )
    base = SolarForecastInput(0.0, "remaining_sensor", periods=None)
    _plan(
        manager,
        _tonight() + _tomorrow(18),
        planning_end=datetime(2026, 11, 6, 18, 0),
        solar_forecast_input=manager._with_learned_periods(base, NOW),
    )


def test_excluded_device_claims_todays_solar():
    manager = _manager()
    now = datetime(2026, 11, 5, 14, 0)
    slots = [_slot(0, hour, 0.10) for hour in range(14, 24)]
    _plan(manager, slots, now=now, remaining=5.0, excluded_demand_claim_kwh=2.0)


def test_guaranteed_floor_active_carries_a_premium():
    manager = _manager(
        soc=15.0,
        _predictive_min_soc_floor=30.0,
        _predictive_min_soc_floor_enabled=True,
    )
    _plan(
        manager,
        _tonight(),
        floor_active=True,
        floor_deficit_kwh=2.0,
        avg_consumption_kwh=1.0,
        total_available_kwh=0.5,
    )
