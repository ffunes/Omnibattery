"""Daily operation forecast where the planner holds or caps the battery.

The planner models a battery that may not discharge (price-based discharge
control, a no-discharge window, a Time Slot window, a peak-shaving hold) or
cannot cover the whole load (its discharge power) by lowering the interval's
consumption to what the battery covers. The timeline still shows the house
load and solar the forecast expects; the grid carries what the battery does
not. The planner's own intervals and the projected battery flows stay as they
are.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_TIME_SLOT,
)
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.chronological import (
    EnergyInterval,
    cap_battery_drain,
)
from custom_components.omnibattery.pricing.daily_timeline import (
    BatteryProjectionInput,
)
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.tracking.consumption_profile import (
    INTERVAL_COUNT,
    ConsumptionForecast,
)
from custom_components.omnibattery.tracking.daily_projection import (
    DailyOperationProjectionRequest,
    build_daily_operation_projection,
)

NOW = datetime(2026, 11, 10, 20, 0)
MIDNIGHT = datetime(2026, 11, 11)
LOAD_KW = 0.4
QUARTER_LOAD = LOAD_KW / 4
ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
BATTERY = BatteryProjectionInput("a", 5.0, 10.0, 10, 100, 2000, 2000)


def _forecast(start, end):
    shape = [QUARTER_LOAD] * INTERVAL_COUNT
    hours = (end - start).total_seconds() / 3600.0
    return ConsumptionForecast(
        LOAD_KW * hours, list(shape), "profile", True,
        intervals_by_date={start.date(): list(shape)},
    )


def _manager(mode, **controller):
    coordinator = SimpleNamespace(
        data={"battery_soc": 60, "battery_total_energy": 10.0},
        min_soc=10,
        max_soc=100,
    )
    values = {
        "_consumption_tracker": SimpleNamespace(
            consumption_profile=SimpleNamespace(),
            forecast_consumption_between=lambda s, e, **_kw: _forecast(s, e),
        ),
        "coordinators": [coordinator],
        "_is_battery_manual_owned": lambda _c: False,
        "solar_profile_mode": "active",
        "_predictive_safety_margin_kwh": 0.0,
        "max_contracted_power": 3000,
        "max_charge_capacity": 3000,
        "max_price_threshold": None,
        "predictive_charging_mode": mode,
        "dp_price_discharge_control": False,
        "price_sensor": None,
        "charging_time_slots": [],
        "_weekly_full_charge_pending": lambda: False,
    }
    values.update(controller)
    manager = PricingManager(
        SimpleNamespace(config=SimpleNamespace(time_zone="UTC")),
        SimpleNamespace(**values),
    )
    manager._read_remaining_solar_input = lambda **_kw: None
    return manager


def _plan(manager):
    return manager.build_extended_chronological_projection(
        now=NOW,
        slots=(),
        base_decision_data={"avg_consumption_kwh": LOAD_KW * 4},
        price_ceiling=None,
        horizon_end=MIDNIGHT + timedelta(hours=12),
    ).plan


def _publish(intervals, battery=BATTERY):
    projection = build_daily_operation_projection(
        DailyOperationProjectionRequest(
            now=NOW,
            plan_intervals=tuple(intervals),
            allocations=(),
            battery_inputs=(battery,),
            mode="normal",
            decision_data={},
        )
    )
    return {
        item["start"]: item
        for item in projection["intervals"] + projection["extended_intervals"]
    }


def _assert_house_load_on_the_grid(published, start):
    item = published[start]
    assert item["consumption_forecast_kwh"] == pytest.approx(QUARTER_LOAD)
    assert item["consumption_kwh"] == pytest.approx(QUARTER_LOAD)
    assert item["battery_to_home_kwh"] == pytest.approx(0.0)
    assert item["grid_to_home_kwh"] == pytest.approx(QUARTER_LOAD)


def test_a_price_blocked_hour_shows_the_house_load_on_the_timeline():
    manager = _manager(
        PREDICTIVE_MODE_DYNAMIC_PRICING,
        dp_price_discharge_control=True,
        discharge_price_threshold=0.20,
    )
    # 23:00-06:00 is at or below the threshold, so discharge is blocked.
    slots = [
        PriceSlot(
            NOW + timedelta(hours=hour),
            NOW + timedelta(hours=hour + 1),
            0.10 if 3 <= hour < 10 else 0.30,
        )
        for hour in range(16)
    ]
    manager.get_future_price_slots = lambda horizon_end=None: slots
    plan = _plan(manager)
    by_start = {item.start: item for item in plan.intervals}
    blocked = [NOW + timedelta(hours=3), MIDNIGHT + timedelta(hours=2)]

    # The planner keeps modelling the battery as idle there.
    for start in blocked:
        assert by_start[start].consumption_kwh == pytest.approx(0.0)
    published = _publish(plan.intervals)
    for start in blocked:
        _assert_house_load_on_the_grid(published, start)
    # Outside the block the battery covers the house as before.
    assert published[NOW]["battery_to_home_kwh"] == pytest.approx(QUARTER_LOAD)
    assert published[NOW]["grid_to_home_kwh"] == pytest.approx(0.0)


def test_a_time_slot_window_shows_the_house_load_on_the_timeline():
    manager = _manager(
        PREDICTIVE_MODE_TIME_SLOT,
        charging_time_slots=[
            {"start_time": "22:00:00", "end_time": "06:00:00", "days": ALL_DAYS}
        ],
    )
    plan = _plan(manager)
    by_start = {item.start: item for item in plan.intervals}
    held = [NOW + timedelta(hours=2), MIDNIGHT + timedelta(hours=4)]

    for start in held:
        assert by_start[start].consumption_kwh == pytest.approx(0.0)
    published = _publish(plan.intervals)
    for start in held:
        _assert_house_load_on_the_grid(published, start)
    after = MIDNIGHT + timedelta(hours=7)
    assert published[after]["battery_to_home_kwh"] == pytest.approx(QUARTER_LOAD)


def test_a_peak_shaving_hold_shows_the_whole_load_on_the_timeline():
    # Held until sunrise to the load above 100 W: the battery covers 0.3 kW
    # of the 0.4 kW load, the grid the rest.
    manager = _manager(
        PREDICTIVE_MODE_TIME_SLOT,
        _is_capacity_protection_soc_limited=lambda: True,
        capacity_protection_limit=100.0,
    )
    sunrise = MIDNIGHT + timedelta(hours=8)
    manager._solar_timeline_window = lambda _now, _tracker: (
        sunrise,
        sunrise + timedelta(hours=8),
    )
    plan = _plan(manager)
    start = MIDNIGHT + timedelta(hours=2)
    (interval,) = [item for item in plan.intervals if item.start == start]

    assert interval.consumption_kwh == pytest.approx(0.075)
    item = _publish(plan.intervals)[start]
    assert item["consumption_forecast_kwh"] == pytest.approx(QUARTER_LOAD)
    assert item["consumption_kwh"] == pytest.approx(QUARTER_LOAD)
    assert item["battery_to_home_kwh"] == pytest.approx(0.075)
    assert item["grid_to_home_kwh"] == pytest.approx(0.025)


def test_a_peak_shaving_hold_never_counts_solar_twice():
    # The hold leaves the battery none of a 0.1 kWh load, so the simulation
    # stores all 0.02 kWh of the sunrise quarter's solar.
    start = NOW + timedelta(hours=1)
    held = EnergyInterval(
        start,
        start + timedelta(minutes=15),
        0.0,
        0.02,
        forecast_consumption_kwh=0.1,
    )

    item = _publish([held])[start]
    assert item["solar_to_battery_kwh"] == pytest.approx(0.02)
    assert item["solar_to_home_kwh"] == pytest.approx(0.0)
    assert item["grid_to_home_kwh"] == pytest.approx(0.1)


@pytest.mark.parametrize(
    ("load", "solar", "to_home", "to_battery", "from_grid"),
    [
        # Solar covers part of the load; the grid carries the rest.
        (0.2, 0.15, 0.15, 0.0, 0.05),
        # Solar covers the load; only the surplus charges the battery.
        (0.1, 0.3, 0.1, 0.2, 0.0),
    ],
)
def test_an_idle_interval_shows_the_solar_the_house_uses(
    load, solar, to_home, to_battery, from_grid
):
    start = NOW + timedelta(hours=1)
    idle = EnergyInterval(
        start, start + timedelta(minutes=15), load, solar
    ).with_battery_idle()

    assert idle.consumption_kwh == pytest.approx(0.0)
    assert idle.solar_kwh == pytest.approx(max(0.0, solar - load))
    item = _publish([idle])[start]
    assert item["solar_forecast_kwh"] == pytest.approx(solar)
    assert item["consumption_forecast_kwh"] == pytest.approx(load)
    assert item["solar_kwh"] == pytest.approx(solar)
    assert item["consumption_kwh"] == pytest.approx(load)
    assert item["solar_to_home_kwh"] == pytest.approx(to_home)
    assert item["solar_to_battery_kwh"] == pytest.approx(to_battery)
    assert item["battery_to_home_kwh"] == pytest.approx(0.0)
    assert item["grid_to_home_kwh"] == pytest.approx(from_grid)


def test_a_capped_interval_shows_the_whole_house_load():
    # 1.2 kW for a quarter against an 800 W battery.
    start = NOW + timedelta(hours=1)
    (capped,) = cap_battery_drain(
        [EnergyInterval(start, start + timedelta(minutes=15), 0.3, 0.0)], 0.8
    )
    battery = BatteryProjectionInput("a", 5.0, 10.0, 10, 100, 800, 800)

    assert capped.consumption_kwh == pytest.approx(0.2)
    assert capped.house_load_kwh == pytest.approx(0.3)
    item = _publish([capped], battery)[start]
    assert item["consumption_forecast_kwh"] == pytest.approx(0.3)
    assert item["consumption_kwh"] == pytest.approx(0.3)
    assert item["battery_to_home_kwh"] == pytest.approx(0.2)
    assert item["grid_to_home_kwh"] == pytest.approx(0.1)


def test_a_held_interval_keeps_the_original_forecast_through_later_steps():
    # A peak-shaving hold leaves the battery 0.075 of a 0.1 kWh load; a price
    # block or the discharge power then lowers it further.
    start = NOW + timedelta(hours=1)
    held = EnergyInterval(
        start,
        start + timedelta(minutes=15),
        0.075,
        0.0,
        forecast_consumption_kwh=0.1,
    )

    idle = held.with_battery_idle()
    (capped,) = cap_battery_drain([held], 0.2)

    assert idle.expected_consumption_kwh == pytest.approx(0.1)
    assert capped.consumption_kwh == pytest.approx(0.05)
    assert capped.expected_consumption_kwh == pytest.approx(0.1)


# --- solar shown before the safety margin and the excluded-device claim -------

DAY = datetime(2026, 11, 10, 10, 0)
DAY_END = datetime(2026, 11, 11, 6, 0)


def _solar_plan(manager, *, claim=0.0, slots=()):
    manager._solar_timeline_window = lambda _now, _tracker: (
        DAY.replace(hour=8),
        DAY.replace(hour=16),
    )
    return manager.build_extended_chronological_projection(
        now=DAY,
        slots=tuple(slots),
        base_decision_data={
            "avg_consumption_kwh": LOAD_KW * 6,
            "solar_remaining_raw_kwh": 2.0,
            "excluded_demand_claim_kwh": claim,
            "planned_grid_charge_kwh": 3.0,
        },
        price_ceiling=None,
        horizon_end=DAY_END,
    ).plan


def test_the_timeline_solar_is_the_forecast_before_margin_and_claim():
    """A 2.0 kWh provider forecast with a 0.512 kWh margin was shown (and
    compared in the forecast history) as 1.488 kWh: -26 % for a perfect
    forecast. The planner keeps the cautious figure."""
    manager = _manager(PREDICTIVE_MODE_TIME_SLOT, _predictive_safety_margin_kwh=0.512)
    plan = _solar_plan(manager, claim=0.3)

    assert sum(item.solar_kwh for item in plan.intervals) == pytest.approx(2.0 - 0.512 - 0.3)
    assert sum(item.expected_solar_kwh for item in plan.intervals) == pytest.approx(2.0)
    published = build_daily_operation_projection(
        DailyOperationProjectionRequest(
            now=DAY,
            plan_intervals=tuple(plan.intervals),
            allocations=(),
            battery_inputs=(BATTERY,),
            mode="time_slot",
            decision_data={},
        )
    )["intervals"]
    assert sum(item["solar_forecast_kwh"] for item in published) == pytest.approx(2.0)
    assert sum(item["solar_kwh"] for item in published) == pytest.approx(2.0)
    # The battery covers the load the cautious solar left; shown solar only
    # feeds what is left of the house load, never more than the load.
    for item in published:
        assert (
            item["solar_to_home_kwh"] + item["battery_to_home_kwh"]
            <= item["consumption_kwh"] + 1e-9
        )
        assert item["grid_to_home_kwh"] >= 0.0


def test_the_forecast_solar_leaves_the_plan_unchanged(monkeypatch):
    """Everything the planner decides from must stay as it was: only the
    displayed forecast carrier is new."""
    from custom_components.omnibattery.pricing import engine

    real = engine.build_solar_timeline

    def planner_only(*args, **kwargs):
        # The old code made only the planner's call (with the margin).
        if kwargs.get("safety_margin_kwh") == 0.0:
            return SimpleNamespace(intervals_kwh=(), source="none")
        return real(*args, **kwargs)

    slots = [
        PriceSlot(DAY + timedelta(hours=hour), DAY + timedelta(hours=hour + 1), 0.1 + 0.02 * hour)
        for hour in range(6)
    ]
    # A nearly empty battery, so the plan has deadlines and books slots.
    low = SimpleNamespace(data={"battery_soc": 12, "battery_total_energy": 10.0}, min_soc=10, max_soc=100)
    kwargs = dict(_predictive_safety_margin_kwh=0.512, coordinators=[low])
    new = _solar_plan(_manager(PREDICTIVE_MODE_DYNAMIC_PRICING, **kwargs), claim=0.3, slots=slots)
    monkeypatch.setattr(engine, "build_solar_timeline", planner_only)
    old = _solar_plan(_manager(PREDICTIVE_MODE_DYNAMIC_PRICING, **kwargs), claim=0.3, slots=slots)

    assert new.allocations and new.deadlines

    assert [(i.start, i.end, i.consumption_kwh, i.solar_kwh, i.house_load_kwh) for i in new.intervals] == [
        (i.start, i.end, i.consumption_kwh, i.solar_kwh, i.house_load_kwh) for i in old.intervals
    ]
    assert new.deadlines == old.deadlines
    assert new.allocations == old.allocations
    assert new.total_required_kwh == old.total_required_kwh
    assert new.reason == old.reason
    assert [i.forecast_solar_kwh for i in old.intervals] == [i.solar_kwh for i in old.intervals]
    assert sum(i.forecast_solar_kwh for i in new.intervals) == pytest.approx(2.0)


def test_held_and_idle_intervals_keep_the_forecast_solar():
    interval = EnergyInterval(
        DAY, DAY + timedelta(minutes=15), 0.3, 0.1, forecast_solar_kwh=0.2
    )
    assert interval.with_battery_idle().expected_solar_kwh == pytest.approx(0.2)
    (capped,) = cap_battery_drain([interval], 0.4)
    assert capped.expected_solar_kwh == pytest.approx(0.2)
    assert capped.solar_kwh == pytest.approx(0.1)
