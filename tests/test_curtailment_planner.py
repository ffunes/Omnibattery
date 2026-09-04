"""Pure tests for Smart Pre-discharge / Anti-curtailment planning."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.curtailment import (
    BatterySnapshot,
    EXPORT_MODE_AUTOMATIC,
    EXPORT_MODE_CUSTOM,
    EXPORT_MODE_SELF_CONSUMPTION,
    calculate_opportunistic_space_kwh,
    distribute_solar_forecast,
    estimate_consumption_by_slot,
    normalize_export_mode,
    plan_curtailment,
)


DAY = datetime(2026, 8, 2)


def _slot(hour: int, price: float, *, minutes: int = 60, offset: int = 0) -> PriceSlot:
    start = DAY + timedelta(hours=hour, minutes=offset)
    return PriceSlot(start, start + timedelta(minutes=minutes), price)


def _battery(
    *,
    name: str = "battery-1",
    soc: float = 80.0,
    capacity: float = 10.0,
    max_soc: float = 100.0,
    floor: float = 10.0,
    power: float = 2000.0,
    eligible: bool = True,
) -> BatterySnapshot:
    return BatterySnapshot(name, soc, capacity, max_soc, floor, power, eligible)


def test_solar_distribution_uses_cumulative_model_for_15_minute_slots():
    slots = [
        _slot(6, 0.2, minutes=15),
        _slot(6, 0.2, minutes=15, offset=15),
        _slot(23, -0.1, minutes=15),
    ]

    def solar_fraction(hour: float) -> float:
        return max(0.0, min(1.0, (hour - 6.0) / 6.0))

    distributed = distribute_solar_forecast(slots, 12.0, solar_fraction)

    assert distributed[slots[0]] == pytest.approx(0.5)
    assert distributed[slots[1]] == pytest.approx(0.5)
    assert distributed[slots[2]] == 0.0


def test_remaining_solar_distribution_is_renormalized_over_future_slots():
    slots = [_slot(9, 0.2), _slot(10, 0.2)]

    def solar_fraction(hour: float) -> float:
        return max(0.0, min(1.0, (hour - 6.0) / 6.0))

    distributed = distribute_solar_forecast(
        slots, 1.81, solar_fraction, normalize_future=True
    )

    assert sum(distributed.values()) == pytest.approx(1.81)


def test_remaining_consumption_distribution_covers_future_horizon():
    slots = [_slot(20, 0.2), _slot(21, 0.2)]

    distributed = estimate_consumption_by_slot(
        slots, 2.4, normalize_future=True
    )

    assert sum(distributed.values()) == pytest.approx(2.4)
    assert distributed[slots[0]] == pytest.approx(1.2)


def test_threshold_is_inclusive_and_hourly_slots_are_supported():
    candidates = [_slot(8, 0.30), _slot(9, 0.0), _slot(10, -0.01)]
    plan = plan_curtailment(
        candidates,
        solar_forecast_kwh=3.0,
        daily_consumption_kwh=1.0,
        batteries=[_battery(soc=95.0)],
        charge_power_w=2000.0,
        max_export_power_w=2000.0,
        solar_by_slot={candidates[0]: 0.0, candidates[1]: 0.0, candidates[2]: 2.0},
        consumption_by_slot={slot: 0.0 for slot in candidates},
        now=DAY,
    )

    assert plan.risk_slots == [candidates[2]]
    assert plan.required_headroom_kwh == pytest.approx(1.7)
    assert plan.status == "planned"
    assert plan.selected_discharge_slots


def test_no_solar_surplus_is_not_a_risk():
    slots = [_slot(9, -0.20), _slot(10, 0.10)]
    plan = plan_curtailment(
        slots,
        solar_forecast_kwh=8.0,
        daily_consumption_kwh=2.0,
        batteries=[_battery()],
        charge_power_w=3000.0,
        solar_by_slot={slot: 0.0 for slot in slots},
        consumption_by_slot={slot: 1.0 for slot in slots},
        now=DAY,
    )

    assert plan.status == "no_risk"
    assert plan.reason == "no_negative_injection_window"
    assert plan.risk_slots == []


def test_headroom_sufficient_protects_without_selecting_discharge():
    risk = _slot(12, -0.05)
    plan = plan_curtailment(
        [_slot(11, 0.25), risk],
        solar_forecast_kwh=2.0,
        daily_consumption_kwh=1.0,
        batteries=[_battery(soc=80.0, capacity=10.0)],
        charge_power_w=5000.0,
        solar_by_slot={risk: 1.0},
        consumption_by_slot={risk: 0.0},
        now=DAY,
    )

    assert plan.status == "protected"
    assert plan.reason == "headroom_sufficient"
    assert plan.selected_discharge_slots == []


def test_existing_headroom_and_kwh_margin_are_accounted_for():
    risk = _slot(12, -0.05)
    slots = [_slot(11, 0.25), risk]
    base = dict(
        price_slots=slots,
        solar_forecast_kwh=3.0,
        daily_consumption_kwh=1.0,
        batteries=[_battery(soc=90.0, capacity=10.0)],
        charge_power_w=5000.0,
        solar_by_slot={slots[0]: 0.0, risk: 2.0},
        consumption_by_slot={slots[0]: 0.0, risk: 0.0},
        max_export_power_w=2000.0,
        now=DAY,
    )

    plan = plan_curtailment(**base, headroom_margin_kwh=0.85)

    assert plan.required_headroom_kwh == pytest.approx(2.55)
    assert plan.current_headroom_kwh == pytest.approx(1.0)
    assert plan.status == "planned"


def test_multiple_risk_windows_choose_the_most_expensive_previous_slots():
    slots = [
        _slot(8, 0.10),
        _slot(9, 0.55),
        _slot(10, -0.05),
        _slot(11, 0.20),
        _slot(12, -0.10),
    ]
    solar = {slot: (1.0 if slot.price < 0 else 0.0) for slot in slots}
    plan = plan_curtailment(
        slots,
        solar_forecast_kwh=5.0,
        daily_consumption_kwh=1.0,
        batteries=[_battery(soc=100.0, power=1000.0)],
        charge_power_w=1000.0,
        solar_by_slot=solar,
        consumption_by_slot={slot: 0.0 for slot in slots},
        max_export_power_w=1000.0,
        now=DAY,
    )

    assert [slot.price for slot in plan.risk_slots] == [-0.05, -0.10]
    assert [slot.price for slot in plan.selected_discharge_slots] == [0.10, 0.55]


def test_export_cap_does_not_reduce_headroom_required_in_risk_window():
    candidate = _slot(8, 0.30)
    risk = _slot(9, -0.10)
    kwargs = dict(
        price_slots=[candidate, risk],
        solar_forecast_kwh=4.0,
        daily_consumption_kwh=1.0,
        batteries=[_battery(soc=100.0, power=3000.0)],
        charge_power_w=3000.0,
        solar_by_slot={candidate: 0.0, risk: 2.0},
        consumption_by_slot={candidate: 1.0, risk: 0.0},
        now=DAY,
    )

    no_export = plan_curtailment(**kwargs, max_export_power_w=0.0)
    with_export = plan_curtailment(**kwargs, max_export_power_w=1000.0)

    assert no_export.required_headroom_kwh == pytest.approx(1.7)
    assert with_export.required_headroom_kwh == pytest.approx(1.7)


def test_self_consumption_only_limits_plan_to_forecast_household_load():
    candidate = _slot(8, 0.30)
    risk = _slot(9, -0.10)
    plan = plan_curtailment(
        [candidate, risk],
        solar_forecast_kwh=5.0,
        daily_consumption_kwh=1.0,
        batteries=[_battery(soc=100.0, power=3000.0)],
        charge_power_w=3000.0,
        max_export_power_w=0.0,
        solar_by_slot={candidate: 0.0, risk: 3.0},
        consumption_by_slot={candidate: 0.4, risk: 0.0},
        now=DAY,
    )

    assert plan.planned_discharge_kwh == pytest.approx(0.4)
    assert plan.shortfall_kwh > 0


def test_export_cap_increases_feasible_predischarge_without_changing_risk():
    candidate = _slot(8, 0.30)
    risk = _slot(9, -0.10)
    kwargs = dict(
        price_slots=[candidate, risk],
        solar_forecast_kwh=5.0,
        daily_consumption_kwh=1.0,
        batteries=[_battery(soc=100.0, power=3000.0)],
        charge_power_w=3000.0,
        solar_by_slot={candidate: 0.0, risk: 3.0},
        consumption_by_slot={candidate: 0.4, risk: 0.0},
        now=DAY,
    )

    no_export = plan_curtailment(**kwargs, max_export_power_w=0.0)
    with_export = plan_curtailment(**kwargs, max_export_power_w=1000.0)

    assert with_export.planned_discharge_kwh == pytest.approx(1.4)
    assert with_export.planned_discharge_kwh > no_export.planned_discharge_kwh


def test_non_dischargeable_battery_headroom_still_absorbs_forecast_solar():
    risk = _slot(9, -0.10)
    plan = plan_curtailment(
        [_slot(8, 0.30), risk],
        solar_forecast_kwh=2.0,
        daily_consumption_kwh=1.0,
        batteries=[_battery(soc=80.0, power=0.0)],
        charge_power_w=3000.0,
        solar_by_slot={risk: 1.0},
        consumption_by_slot={risk: 0.0},
        now=DAY,
    )

    assert plan.status == "protected"
    assert plan.current_headroom_kwh == pytest.approx(2.0)


def test_discharge_slots_are_grouped_for_15_minute_feeds():
    candidates = [
        _slot(8, 0.10, minutes=15, offset=15 * index)
        for index in range(4)
    ]
    risk = _slot(9, -0.05, minutes=15)
    slots = candidates + [risk]
    plan = plan_curtailment(
        slots,
        solar_forecast_kwh=4.0,
        daily_consumption_kwh=1.0,
        batteries=[_battery(soc=100.0, power=1000.0)],
        charge_power_w=1000.0,
        solar_by_slot={risk: 1.0, **{slot: 0.0 for slot in candidates}},
        consumption_by_slot={slot: 0.0 for slot in slots},
        max_export_power_w=1000.0,
        now=DAY,
    )

    assert len(plan.selected_discharge_slots) == 4
    assert plan.selected_discharge_slots[0].start == candidates[0].start
    assert plan.selected_discharge_slots[-1].end == candidates[-1].end


def test_reserve_soc_and_insufficient_power_report_shortfall():
    candidates = [_slot(8, 0.4), _slot(9, 0.3)]
    risk = _slot(10, -0.1)
    slots = candidates + [risk]
    plan = plan_curtailment(
        slots,
        solar_forecast_kwh=8.0,
        daily_consumption_kwh=1.0,
        batteries=[_battery(soc=80.0, floor=10.0, power=250.0)],
        charge_power_w=5000.0,
        predischarge_reserve_soc=70.0,
        solar_by_slot={**{slot: 0.0 for slot in candidates}, risk: 5.0},
        consumption_by_slot={slot: 0.0 for slot in slots},
        now=DAY,
    )

    assert plan.status == "shortfall"
    assert plan.shortfall_kwh > 0
    assert plan.target_soc_by_battery["battery-1"] >= 70.0


def test_multibattery_headroom_and_targets_are_allocated():
    candidate = _slot(8, 0.4)
    risk = _slot(9, -0.1)
    plan = plan_curtailment(
        [candidate, risk],
        solar_forecast_kwh=4.0,
        daily_consumption_kwh=1.0,
        batteries=[
            _battery(name="a", soc=100.0, capacity=10.0, power=1000.0),
            _battery(name="b", soc=90.0, capacity=20.0, power=1000.0),
        ],
        charge_power_w=4000.0,
        solar_by_slot={candidate: 0.0, risk: 4.0},
        consumption_by_slot={candidate: 0.0, risk: 0.0},
        max_export_power_w=2000.0,
        now=DAY,
    )

    assert set(plan.target_soc_by_battery) == {"a", "b"}
    assert plan.target_soc_by_battery["a"] >= 10.0
    assert plan.target_soc_by_battery["b"] >= 10.0
    assert plan.planned_discharge_kwh > 0


def test_opportunistic_space_is_free_headroom_minus_solar_reserve():
    risk = _slot(12, -0.10)
    plan = plan_curtailment(
        [risk],
        solar_forecast_kwh=3.0,
        daily_consumption_kwh=1.0,
        batteries=[_battery(soc=60.0, capacity=10.0, power=2000.0)],
        charge_power_w=10000.0,
        solar_by_slot={risk: 1.0},
        consumption_by_slot={risk: 0.0},
        now=DAY,
    )

    # 4 kWh free, 0.85 kWh reserved for PV, 3.15 kWh available to import.
    assert plan.solar_reserve_remaining_kwh == pytest.approx(0.85)
    assert plan.opportunistic_space_kwh == pytest.approx(3.15)
    assert plan.opportunistic_charge_reason == "solar_reserve_space_available"
    assert calculate_opportunistic_space_kwh(4.0, 0.85) == pytest.approx(3.15)
    assert calculate_opportunistic_space_kwh(0.5, 1.0) == 0.0


def test_live_solar_shortfall_releases_space_and_excess_solar_closes_it():
    risk = _slot(12, -0.10)
    common = dict(
        price_slots=[risk],
        solar_forecast_kwh=6.0,
        daily_consumption_kwh=1.0,
        batteries=[_battery(soc=60.0, capacity=10.0, power=2000.0)],
        charge_power_w=10000.0,
        consumption_by_slot={risk: 0.0},
        now=DAY,
    )
    forecast = plan_curtailment(**common, solar_by_slot={risk: 4.0})
    actual_low = plan_curtailment(**common, solar_by_slot={risk: 1.0})
    actual_high = plan_curtailment(**common, solar_by_slot={risk: 6.0})

    assert actual_low.solar_reserve_remaining_kwh < forecast.solar_reserve_remaining_kwh
    assert actual_low.opportunistic_space_kwh > forecast.opportunistic_space_kwh
    assert actual_high.solar_reserve_remaining_kwh > forecast.solar_reserve_remaining_kwh
    assert actual_high.opportunistic_space_kwh < forecast.opportunistic_space_kwh


def test_export_selector_modes_and_legacy_values():
    assert normalize_export_mode(None, 0.0) == EXPORT_MODE_SELF_CONSUMPTION
    assert normalize_export_mode(None, 800.0) == EXPORT_MODE_CUSTOM
    assert normalize_export_mode("Solo autoconsumo", 800.0) == EXPORT_MODE_SELF_CONSUMPTION
    assert normalize_export_mode("Automático", 0.0) == EXPORT_MODE_AUTOMATIC
    assert normalize_export_mode("Límite personalizado", 800.0) == EXPORT_MODE_CUSTOM

    candidate = _slot(8, 0.30)
    risk = _slot(9, -0.10)
    kwargs = dict(
        price_slots=[candidate, risk],
        solar_forecast_kwh=5.0,
        daily_consumption_kwh=1.0,
        batteries=[_battery(soc=100.0, power=3000.0)],
        charge_power_w=2000.0,
        solar_by_slot={candidate: 0.0, risk: 3.0},
        consumption_by_slot={candidate: 0.0, risk: 0.0},
        now=DAY,
    )
    self_consumption = plan_curtailment(
        **kwargs, export_mode=EXPORT_MODE_SELF_CONSUMPTION
    )
    automatic = plan_curtailment(
        **kwargs, export_mode=EXPORT_MODE_AUTOMATIC
    )
    custom = plan_curtailment(
        **kwargs, export_mode=EXPORT_MODE_CUSTOM, max_export_power_w=500.0
    )

    assert self_consumption.export_mode == EXPORT_MODE_SELF_CONSUMPTION
    assert self_consumption.planned_discharge_kwh == 0.0
    assert automatic.export_mode == EXPORT_MODE_AUTOMATIC
    assert automatic.planned_discharge_kwh == pytest.approx(1.7)
    assert custom.export_limit_w == pytest.approx(500.0)
    assert custom.planned_discharge_kwh == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"price_slots": []}, "missing_prices"),
        ({"price_slots": [_slot(8, 0.2)]}, "missing_solar_forecast"),
        (
            {"price_slots": [_slot(8, 0.2)], "solar_forecast_kwh": 1.0},
            "missing_consumption",
        ),
        (
            {
                "price_slots": [_slot(8, 0.2)],
                "solar_forecast_kwh": 1.0,
                "daily_consumption_kwh": 1.0,
            },
            "missing_battery_capacity_or_soc",
        ),
    ],
)
def test_missing_inputs_fail_safe(kwargs, reason):
    plan = plan_curtailment(**kwargs, now=DAY)

    assert plan.status == "fail_safe"
    assert plan.reason == reason
    assert plan.selected_discharge_slots == []
