"""Characterization tests for the pure pricing notification formatters (PR2).

Pin the current (title, message) output of the formatters extracted from
``ChargeDischargeController`` into ``notifications.plan`` so the move is
proven cero-cambio-funcional. Pure functions: no hass, no controller — schedule
is a SimpleNamespace exposing only the attributes the formatters read.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.notifications import (
    EXECUTION,
    PLAN_CHARGE,
    PLAN_INFO,
)
from custom_components.omnibattery.notifications import plan as notifications
from custom_components.omnibattery.pricing import PriceSlot

_DAY = datetime(2999, 1, 1, 18, 0)


def _decision(**overrides):
    base = {
        "should_charge": False,
        "solar_forecast_kwh": 3.0,
        "usable_energy_kwh": 5.0,
        "avg_soc": 60.0,
        "avg_consumption_kwh": 8.0,
        "total_available_kwh": 9.0,
        "energy_deficit_kwh": 0.0,
        "days_in_history": 7,
    }
    base.update(overrides)
    return base


def _slots(prices):
    return [
        PriceSlot(start=_DAY + timedelta(minutes=15 * i), end=_DAY + timedelta(minutes=15 * (i + 1)), price=p)
        for i, p in enumerate(prices)
    ]


def _schedule(prices, *, hours_needed=1.0, charging_needed=False, estimated_cost=0.5):
    slots = _slots(prices)
    return SimpleNamespace(
        hours_needed=hours_needed,
        selected_slots=slots,
        average_price=sum(prices) / len(prices),
        estimated_cost=estimated_cost,
        charging_needed=charging_needed,
    )


# ----------------------------------------------------------------------
# format_predictive_notification_message
# ----------------------------------------------------------------------

_CFG = dict(max_contracted_power=5000, max_charge_capacity=3000,
            charging_time_slot={"start_time": "23:00", "end_time": "06:00"}, now=_DAY)


def test_predictive_safe_mode_when_no_solar():
    title, message, _ = notifications.format_predictive_notification_message(
        _decision(solar_forecast_kwh=None, should_charge=True), **_CFG
    )
    assert title == "Predictive Charging: Safe mode"
    assert "conservative mode" in message


def test_predictive_not_required():
    title, message, _ = notifications.format_predictive_notification_message(
        _decision(should_charge=False), **_CFG
    )
    assert title == "Predictive Charging: Not required"
    assert "No grid charging required." in message


def test_predictive_started():
    title, message, _ = notifications.format_predictive_notification_message(
        _decision(should_charge=True, energy_deficit_kwh=2.0), **_CFG
    )
    assert title == "Predictive Charging: STARTED"
    assert "06:00" in message  # end of charging slot
    assert "contracted: 5000W, batteries: 3000W" in message


def test_predictive_max_power_is_min_of_contracted_and_batteries():
    # #506: max_contracted_power is the single grid import cap, so the
    # announced max charge power is min(contracted, batteries), no peak limit.
    _, message, _ = notifications.format_predictive_notification_message(
        _decision(should_charge=True, energy_deficit_kwh=2.0),
        max_contracted_power=3000,
        max_charge_capacity=3500,
        charging_time_slot={"start_time": "23:00", "end_time": "06:00"},
        now=_DAY,
    )
    assert "Max charge power: 3000W" in message
    assert "peak limit" not in message


def test_predictive_started_reports_the_actual_planned_grid_charge():
    """The notification must match the energy used to calculate the SOC target."""
    _, message, _ = notifications.format_predictive_notification_message(
        _decision(
            should_charge=True,
            energy_deficit_kwh=4.63,
            planned_grid_charge_kwh=5.55,
            # This is the old complementary-headroom value.  It must not be
            # presented as the grid charge or as a guaranteed solar top-up.
            grid_charge_kwh=4.03,
            solar_surplus_kwh=2.99,
        ),
        **_CFG,
    )
    assert "Planned grid charge: 5.55 kWh" in message
    assert "Forecast solar surplus: 2.99 kWh (after expected consumption)" in message
    assert "Grid: 4.03 kWh" not in message
    assert "solar will charge the remaining" not in message


def test_predictive_floor_charge_reports_grid_deficit_not_solar():
    # Issue #46: floor-driven charge on a solar-positive day. grid_charge_kwh
    # computes to 0 and solar_surplus is large, but the battery really charges
    # energy_deficit from the grid to reach the floor. Notification must say so
    # and must not claim solar covers it.
    title, message, _ = notifications.format_predictive_notification_message(
        _decision(
            should_charge=True,
            solar_forecast_kwh=29.61,
            avg_consumption_kwh=16.67,
            energy_deficit_kwh=0.67,
            grid_charge_kwh=0.0,
            solar_surplus_kwh=3.74,
            floor_active=True,
        ),
        **_CFG,
    )
    assert title == "Predictive Charging: STARTED"
    assert "Grid: 0.67 kWh to reach guaranteed minimum SOC" in message
    assert "solar will charge the remaining" not in message


def test_predictive_daily_evaluation_expected():
    title, _, _ = notifications.format_predictive_notification_message(
        _decision(should_charge=True, energy_deficit_kwh=2.0), True, **_CFG
    )
    assert title == "Predictive Charging: Expected today"


def test_predictive_remaining_fallback_labels_horizon_and_basis():
    _, message, _ = notifications.format_predictive_notification_message(
        _decision(
            avg_consumption_kwh=12.15,
            remaining_consumption_kwh=12.15,
            consumption_scope="remaining_fallback",
            consumption_forecast_source="legacy_daily",
            consumption_accumulator_ready=True,
            solar_forecast_source="remaining_sensor",
        ),
        **_CFG,
    )

    assert "Solar remaining today: 3.00 kWh" in message
    assert "Home consumption remaining until midnight: 12.15 kWh" in message
    assert "Basis: temporary daily curve, adjusted with today's consumption" in message
    assert "7-day avg" not in message
    assert "Sufficient energy for the rest of today" in message


def test_predictive_extended_horizon_labels_sunrise_and_overnight_leg():
    """Phase 4: an extended (sunrise) horizon relabels the consumption line
    and adds a separate overnight kWh line."""
    _, message, _ = notifications.format_predictive_notification_message(
        _decision(
            avg_consumption_kwh=12.15,
            remaining_consumption_kwh=12.15,
            consumption_scope="remaining_fallback",
            consumption_forecast_source="legacy_daily",
            consumption_accumulator_ready=True,
            solar_forecast_source="remaining_sensor",
            energy_horizon_end=datetime(2999, 1, 2, 6, 30),
            overnight_consumption_kwh=1.75,
        ),
        **_CFG,
    )

    assert "Home consumption remaining until sunrise (06:30): 12.15 kWh" in message
    assert "🌙 Overnight until sunrise: 1.75 kWh" in message
    assert "Sufficient energy for the rest of today and tonight until sunrise (06:30)" in message


def test_predictive_midnight_horizon_omits_overnight_leg():
    """No extended horizon (or zero overnight leg): keep the current wording."""
    _, message, _ = notifications.format_predictive_notification_message(
        _decision(
            avg_consumption_kwh=12.15,
            remaining_consumption_kwh=12.15,
            consumption_scope="remaining_fallback",
            consumption_forecast_source="legacy_daily",
            consumption_accumulator_ready=True,
            solar_forecast_source="remaining_sensor",
            energy_horizon_end=datetime(2999, 1, 2, 0, 0),
            overnight_consumption_kwh=0.0,
        ),
        **_CFG,
    )

    assert "Home consumption remaining until midnight: 12.15 kWh" in message
    assert "Overnight until sunrise" not in message
    assert "Sufficient energy for the rest of today" in message
    assert "and tonight" not in message


def test_predictive_daily_profile_reports_profile_as_basis():
    _, message, _ = notifications.format_predictive_notification_message(
        _decision(
            avg_consumption_kwh=16.85,
            consumption_scope="daily_profile",
            consumption_forecast_source="profile",
            profile_days=23,
            solar_forecast_source="remaining_sensor",
        ),
        **_CFG,
    )

    assert "Solar remaining today: 3.00 kWh" in message
    assert "Expected home consumption today: 16.85 kWh" in message
    assert "Basis: learned 15-minute profile (23 days)" in message
    assert "7-day avg" not in message


# ----------------------------------------------------------------------
# format_dynamic_pricing_notification
# ----------------------------------------------------------------------

_DP_CFG = dict(unit="€/kWh", max_price_threshold=0.30, discharge_price_threshold=0.45,
               max_contracted_power=5000, max_charge_capacity=3000, now=_DAY)


def test_dynamic_none_schedule_not_needed():
    title, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False), None, **_DP_CFG
    )
    assert title == "Predictive Charging: Price Optimization - NOT needed"


def test_dynamic_none_schedule_no_slots():
    title, _, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, energy_deficit_kwh=4.0), None, **_DP_CFG
    )
    assert title == "Predictive Charging: Price Optimization - No slots available"


def test_dynamic_no_slots_blames_the_margin_when_it_binds():
    """Arbitrage ceiling under the static one, so the margin is the cause."""
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, energy_deficit_kwh=4.0), None,
        **{**_DP_CFG, "arbitrage_ceiling": 0.20},
    )
    assert "spread is too small" in message
    assert "not binding" not in message


def test_dynamic_no_slots_blames_the_static_ceiling_when_it_binds():
    """Regression: a low static ceiling must not be reported as a margin problem."""
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, energy_deficit_kwh=4.0), None,
        **{**_DP_CFG, "max_price_threshold": 0.10, "arbitrage_ceiling": 0.30},
    )
    assert "raise max price threshold" in message
    assert "spread is too small" not in message
    assert "(not binding)" in message


def test_dynamic_no_slots_without_gate_keeps_original_hint():
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, energy_deficit_kwh=4.0), None, **_DP_CFG
    )
    assert "raise max price threshold" in message
    assert "arbitrage" not in message


def test_dynamic_no_slots_cheaper_to_import_does_not_blame_the_price_sensor():
    """The planner chose grid import over storage: a threshold change won't help."""
    title, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(
            should_charge=True,
            energy_deficit_kwh=4.0,
            chronological_plan_reason="cheaper_to_import",
            cheaper_to_import_kwh=3.5,
        ),
        None,
        **_DP_CFG,
    )
    assert title.endswith("Grid import is cheaper")
    assert "not worth it" in message
    assert "3.50 kWh" in message
    assert "raise max price threshold" not in message


def test_dynamic_no_slots_deadline_shortfall_explains_timing():
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(
            should_charge=True,
            energy_deficit_kwh=4.0,
            chronological_plan_reason="deadline_capacity_shortfall",
        ),
        None,
        **_DP_CFG,
    )
    assert "cannot deliver it in time" in message
    assert "raise max price threshold" not in message


def test_dynamic_no_slots_with_plan_ok_does_not_blame_the_price_sensor():
    """A full battery: the planner found nothing it can store yet."""
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(
            should_charge=True,
            energy_deficit_kwh=14.97,
            chronological_plan_reason="ok",
        ),
        None,
        **{**_DP_CFG, "max_price_threshold": None, "discharge_price_threshold": None},
    )
    assert "nothing can be stored now" in message
    assert "raise max price threshold" not in message


def test_dynamic_no_slots_no_eligible_slots_points_at_price_feed():
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(
            should_charge=True,
            energy_deficit_kwh=4.0,
            chronological_plan_reason="no_eligible_slots",
        ),
        None,
        **_DP_CFG,
    )
    assert "no price slots remain" in message
    assert "Check the price sensor" in message


def test_dynamic_no_slots_price_threshold_names_cheapest_rejected_slot():
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(
            should_charge=True,
            energy_deficit_kwh=4.0,
            chronological_plan_reason="price_threshold",
            cheapest_rejected_slot={
                "start": "2999-01-01T03:00:00",
                "price": 0.3412,
                "ceiling": 0.30,
            },
        ),
        None,
        **_DP_CFG,
    )
    assert "no valid price slots found" in message
    assert "Cheapest slot: 03:00 at 0.3412 €/kWh (ceiling 0.3000 €/kWh)" in message
    assert "raise max price threshold" in message


def test_dynamic_informational_lists_slots():
    schedule = _schedule([0.10, 0.12, 0.11, 0.09], charging_needed=False)
    title, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False), schedule, **_DP_CFG
    )
    assert "Price Info" in title and "cheapest" in title
    assert "Cheapest hours today (informational):" in message
    assert message.count("→") == 4  # one bullet per slot
    assert "charge ≤ 0.3000 €/kWh" in message
    assert "discharge ≥ 0.4500 €/kWh" in message
    assert "No charging will activate." in message


def test_dynamic_charging_shows_cost():
    schedule = _schedule([0.10, 0.12], hours_needed=0.5, charging_needed=True, estimated_cost=0.75)
    title, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(
            should_charge=True,
            energy_deficit_kwh=4.55,
            planned_grid_charge_kwh=1.58,
        ),
        schedule,
        **_DP_CFG,
    )
    assert "selected" in title
    assert "Selected hours (cheapest):" in message
    assert "Energy deficit: 4.55 kWh" in message
    assert "Grid charge planned: 1.58 kWh" in message
    assert "Estimated cost: ~0.75 €" in message


def test_dynamic_floor_charge_names_the_floor_in_title():
    schedule = _schedule([0.089], hours_needed=1.0, charging_needed=True)
    title, _, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, floor_active=True, planned_grid_charge_kwh=1.15),
        schedule,
        **_DP_CFG,
    )
    assert title == "Predictive Charging: Price Optimization + Min SOC floor - 1.0h selected"
    title, _, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, planned_grid_charge_kwh=1.15),
        schedule,
        **_DP_CFG,
    )
    assert "Min SOC floor" not in title


def test_dynamic_extended_plan_names_energy_in_tomorrows_slots():
    """The planned total spans tomorrow; the slots listed are tonight's only."""
    schedule = _schedule([0.10, 0.12], hours_needed=0.5, charging_needed=True)
    schedule.chronological_planning_active = True
    schedule.deadline_required_kwh = 12.02
    decision = _decision(
        should_charge=True,
        energy_deficit_kwh=16.81,
        planned_grid_charge_kwh=12.02,
        deferred_grid_charge_kwh=3.02,
    )
    message = notifications.format_dynamic_pricing_notification(
        decision, schedule, **_DP_CFG
    )[1]
    assert "Planned in tomorrow's slots (scheduled at 00:05): 3.02 kWh" in message

    decision["deferred_grid_charge_kwh"] = 0.0
    message = notifications.format_dynamic_pricing_notification(
        decision, schedule, **_DP_CFG
    )[1]
    assert "tomorrow's slots" not in message


def test_dynamic_remaining_horizon_does_not_label_remainder_as_daily_average():
    schedule = _schedule([0.10, 0.12], hours_needed=0.5, charging_needed=True)
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(
            should_charge=True,
            avg_consumption_kwh=35.52,
            daily_avg_consumption_kwh=17.98,
            remaining_consumption_kwh=12.15,
            consumption_scope="remaining",
            energy_deficit_kwh=2.0,
        ),
        schedule,
        **_DP_CFG,
    )
    assert "Home consumption remaining until midnight: 12.15 kWh" in message
    assert "Basis: remaining-day estimate from 7-day daily average" in message
    assert "17.98 kWh" not in message
    assert "Solar remaining today: 3.00 kWh" in message


def test_dynamic_extended_horizon_labels_sunrise_and_overnight_leg():
    """Phase 4: same horizon relabeling in the price-optimization formatter."""
    schedule = _schedule([0.10, 0.12], hours_needed=0.5, charging_needed=True)
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(
            should_charge=True,
            avg_consumption_kwh=35.52,
            daily_avg_consumption_kwh=17.98,
            remaining_consumption_kwh=12.15,
            consumption_scope="remaining",
            energy_deficit_kwh=2.0,
            energy_horizon_end=datetime(2999, 1, 2, 6, 30),
            overnight_consumption_kwh=1.75,
        ),
        schedule,
        **_DP_CFG,
    )
    assert "Home consumption remaining until sunrise (06:30): 12.15 kWh" in message
    assert "🌙 Overnight until sunrise: 1.75 kWh" in message


def test_dynamic_remaining_profile_scope_is_recognized():
    schedule = _schedule([0.10, 0.12], hours_needed=0.5, charging_needed=True)
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(
            should_charge=True,
            avg_consumption_kwh=11.40,
            remaining_consumption_kwh=11.40,
            consumption_scope="remaining_profile",
            consumption_forecast_source="profile",
            consumption_accumulator_ready=True,
            profile_days=21,
            energy_deficit_kwh=2.0,
        ),
        schedule,
        **_DP_CFG,
    )

    assert "Solar remaining today: 3.00 kWh" in message
    assert "Home consumption remaining until midnight: 11.40 kWh" in message
    assert "Basis: learned 15-minute profile" in message
    assert "adjusted with today's consumption" not in message
    assert "7-day avg" not in message


# ----------------------------------------------------------------------
# slot start / pre-slot / evening
# ----------------------------------------------------------------------

def test_slot_start_notification():
    schedule = _schedule([0.10, 0.12, 0.11])
    first = schedule.selected_slots[0]
    title, message, _ = notifications.format_slot_start_notification(
        first, schedule, unit="€/kWh", max_contracted_power=5000, now=_DAY
    )
    assert title == "Predictive Charging: STARTED (0.1000 €/kWh)"
    assert "Charging at max 5000W" in message
    assert "2 slot(s) remaining" in message


def test_slot_start_last_slot():
    schedule = _schedule([0.10])
    title, message, _ = notifications.format_slot_start_notification(
        schedule.selected_slots[0], schedule, unit="€/kWh", max_contracted_power=5000, now=_DAY
    )
    assert "Last slot" in message and "No more slots today" in message


def test_dp_pre_slot_reevaluation():
    slot = _slots([0.09])[0]
    title, message, _ = notifications.format_dp_pre_slot_reevaluation_notification(
        slot, _decision(energy_deficit_kwh=1.5), unit="€/kWh", now=_DAY
    )
    assert "confirmed" in title
    assert "0.0900 €/kWh" in message
    assert "Charging will activate" in message


def test_dp_pre_slot_reevaluation_recognizes_remaining_fallback():
    slot = _slots([0.09])[0]
    _, message, _ = notifications.format_dp_pre_slot_reevaluation_notification(
        slot,
        _decision(
            avg_consumption_kwh=6.25,
            remaining_consumption_kwh=6.25,
            consumption_scope="remaining_fallback",
            consumption_forecast_source="legacy_daily",
            consumption_accumulator_ready=True,
            energy_deficit_kwh=1.5,
        ),
        unit="€/kWh",
        now=_DAY,
    )

    assert "Solar remaining today: 3.00 kWh" in message
    assert "Home consumption remaining until midnight: 6.25 kWh" in message
    assert "Basis: temporary daily curve, adjusted with today's consumption" in message
    assert "7-day avg" not in message


def test_dynamic_no_slots_awaiting_prices_does_not_advise_the_margin():
    """A5: a deficit after the last published price is not 'cheaper to import'.

    Lowering the arbitrage margin cannot help: the slots are valued as soon
    as the next day's prices are out.
    """
    title, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(
            should_charge=True,
            energy_deficit_kwh=4.0,
            chronological_plan_reason="awaiting_prices",
            cheaper_to_import_kwh=3.5,
            awaiting_prices_kwh=3.5,
        ),
        None,
        **_DP_CFG,
    )
    assert title.endswith("Waiting for prices")
    assert "not published yet" in message
    assert "3.50 kWh" in message
    assert "arbitrage margin" not in message


def test_dynamic_shows_the_discharge_limit_the_plan_used():
    """The drain projection is capped at the discharge power; say so, and how
    much of the load the grid carries instead."""
    schedule = _schedule([0.10, 0.12], hours_needed=0.5, charging_needed=True)
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(
            should_charge=True,
            energy_deficit_kwh=4.0,
            planned_grid_charge_kwh=2.7,
            battery_discharge_limit_w=800,
            grid_carried_load_kwh=3.456,
        ),
        schedule,
        **_DP_CFG,
    )

    assert message.endswith(
        "Max discharge power: 800W "
        "(load above it from the grid: 3.46 kWh, left out of the charge)"
    )
    assert "Max charge power: 3000W" in message


def test_dynamic_omits_the_discharge_line_without_a_limit():
    schedule = _schedule([0.10, 0.12], hours_needed=0.5, charging_needed=True)
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, energy_deficit_kwh=4.0, planned_grid_charge_kwh=2.7),
        schedule,
        **_DP_CFG,
    )

    assert "Max discharge power" not in message


# ----------------------------------------------------------------------
# The 2.0.0 optimizer's plan: its own words, and the orders it gives
# ----------------------------------------------------------------------


def _optimizer_schedule(prices, kwh):
    schedule = _schedule(prices, hours_needed=1.0, charging_needed=True, estimated_cost=0.4)
    schedule.slot_energy_targets_kwh = dict(zip(schedule.selected_slots, kwh))
    schedule.slot_deadlines = {s: None for s in schedule.selected_slots}
    schedule.chronological_planning_active = True
    schedule.deadline_required_kwh = 9.0
    schedule.purpose_for = lambda _s: "deficit"
    return schedule


def test_optimizer_charge_lists_blocks_without_the_old_planner_words():
    schedule = _optimizer_schedule([0.10, 0.10, 0.10, 0.12], [0.5, 0.5, 0.4, 0.3])
    title, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, energy_deficit_kwh=9.0, planned_grid_charge_kwh=1.7,
                  planner="optimizer", deadline_required_kwh=9.0),
        schedule, **_DP_CFG,
    )
    assert "Grid charge planned: 1.70 kWh" in message
    for old in ("Energy deficit", "Required before deadlines", "Flexible by price",
                "[flexible]", "[deficit]", "meet each deadline", "of charging needed"):
        assert old not in message
    # Back-to-back quarters at one price are one line, with their energy added up.
    assert "| 18:00–18:45 | 0.1000 €/kWh | 1.40 kWh |" in message
    assert "| 18:45–19:00 | 0.1200 €/kWh | 0.30 kWh |" in message
    assert message.count("→") == 0


def _slot_at(minutes, length=15):
    return PriceSlot(start=_DAY + timedelta(minutes=minutes),
                     end=_DAY + timedelta(minutes=minutes + length), price=0.3)


ORDERS = {
    "holds": frozenset({_slot_at(60), _slot_at(75)}),
    "exports": frozenset({_slot_at(-480, 60)}),
    "sales": {_slot_at(120): 2.5, _slot_at(135): 2.5},
    "solar_delay": True,
    "room": {_slot_at(-540): 2.5, _slot_at(-525): 1.2},
}


def test_optimizer_plan_without_charge_still_names_its_orders():
    title, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False, planner="optimizer"), None,
        plan_orders=ORDERS, **_DP_CFG,
    )
    assert "NOT needed" in title
    assert "Battery kept for later: 19:00–19:30" in message
    assert "Selling to the grid: 20:00–20:30 at 2.5 kW" in message
    assert "Solar surplus to export: 10:00–11:00" in message
    assert "Solar charge delayed by the plan" in message
    assert "Making room for the sun: 09:00–09:15 at 2.5 kW, 09:15–09:30 at 1.2 kW" in message
    # The old planner's words stay on its own path, and it has no orders.
    old = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False), None, plan_orders=ORDERS, **_DP_CFG)[1]
    assert "Battery kept" not in old


def test_optimizer_cheaper_to_import_is_not_a_warning():
    decision = _decision(should_charge=True, energy_deficit_kwh=4.0, planner="optimizer",
                         chronological_plan_reason="cheaper_to_import", cheaper_to_import_kwh=3.5)
    title, message, _ = notifications.format_dynamic_pricing_notification(
        decision, None, arbitrage_margin=0.0, **_DP_CFG)
    assert title.endswith("Grid import is cheaper")
    assert "⚠️" not in message and "Energy deficit" not in message
    assert "3.50 kWh" in message and "round-trip losses" in message
    assert "margin" not in message
    message = notifications.format_dynamic_pricing_notification(
        decision, None, arbitrage_margin=0.05, **_DP_CFG)[1]
    assert "0.0500 €/kWh arbitrage margin" in message


def test_optimizer_price_info_groups_the_cheap_hours_without_tags():
    schedule = _schedule([0.10, 0.10, 0.10, 0.10])
    schedule.purpose_for = lambda _s: "deficit"
    message = notifications.format_dynamic_pricing_notification(
        _decision(planner="optimizer"), schedule, **_DP_CFG)[1]
    assert "| 18:00–19:00 | 0.1000 €/kWh |" in message
    assert message.count("→") == 0 and "[deficit]" not in message


# ----------------------------------------------------------------------
# Category returned with every formatted notification (task 2.2)
# ----------------------------------------------------------------------

def _neg_schedule():
    schedule = _schedule([-0.05, -0.02], hours_needed=0.5, charging_needed=True)
    schedule.schedule_type = "negative_price"
    return schedule


_PREDICTIVE_CATEGORY_CASES = [
    (dict(decision=dict(solar_forecast_kwh=None, should_charge=True)),
     "Predictive Charging: Safe mode", PLAN_INFO),
    (dict(decision=dict(should_charge=True, chronological_deferred=True, energy_deficit_kwh=2.0)),
     "Predictive Charging: Deferred by timeline", PLAN_INFO),
    (dict(decision=dict(should_charge=False)),
     "Predictive Charging: Not required", PLAN_INFO),
    (dict(decision=dict(should_charge=True, energy_deficit_kwh=2.0), daily=True),
     "Predictive Charging: Expected today", PLAN_CHARGE),
    (dict(decision=dict(should_charge=True, energy_deficit_kwh=2.0)),
     "Predictive Charging: STARTED", EXECUTION),
]


@pytest.mark.parametrize(("case", "title", "category"), _PREDICTIVE_CATEGORY_CASES)
def test_predictive_formatter_returns_branch_category(case, title, category):
    got_title, _, got_category = notifications.format_predictive_notification_message(
        _decision(**case["decision"]), case.get("daily", False), **_CFG
    )
    assert got_title == title
    assert got_category == category


def _floor_schedule():
    return _schedule([0.089], hours_needed=1.0, charging_needed=True)


_DYNAMIC_CATEGORY_CASES = [
    (_decision(should_charge=False), lambda: None,
     "Price Optimization - NOT needed", PLAN_INFO),
    (_decision(should_charge=True, energy_deficit_kwh=3.0, deferred_to_next_day=True), lambda: None,
     "Price Optimization - nothing to charge yet", PLAN_INFO),
    (_decision(should_charge=True, energy_deficit_kwh=4.0,
               chronological_plan_reason="cheaper_to_import", cheaper_to_import_kwh=3.5), lambda: None,
     "Price Optimization - Grid import is cheaper", PLAN_INFO),
    (_decision(should_charge=True, energy_deficit_kwh=4.0,
               chronological_plan_reason="awaiting_prices", awaiting_prices_kwh=3.5), lambda: None,
     "Price Optimization - Waiting for prices", PLAN_INFO),
    (_decision(should_charge=True, energy_deficit_kwh=4.0), lambda: None,
     "Price Optimization - No slots available", PLAN_INFO),
    (_decision(should_charge=True), _neg_schedule,
     "Negative-price charging: ", PLAN_CHARGE),
    (_decision(should_charge=False), lambda: _schedule([0.10, 0.12], charging_needed=False),
     "Price Info - ", PLAN_INFO),
    (_decision(should_charge=True, energy_deficit_kwh=4.55, planned_grid_charge_kwh=1.58),
     lambda: _schedule([0.10, 0.12], hours_needed=0.5, charging_needed=True),
     "Price Optimization - ", PLAN_CHARGE),
    (_decision(should_charge=True, floor_active=True, planned_grid_charge_kwh=1.15), _floor_schedule,
     "Price Optimization + Min SOC floor - ", PLAN_CHARGE),
]


@pytest.mark.parametrize(("decision", "schedule", "title_part", "category"), _DYNAMIC_CATEGORY_CASES)
def test_dynamic_formatter_returns_branch_category(decision, schedule, title_part, category):
    title, _, got_category = notifications.format_dynamic_pricing_notification(
        decision, schedule(), **_DP_CFG
    )
    assert title_part in title
    assert got_category == category


def test_slot_start_and_pre_slot_formatters_are_execution():
    schedule = _schedule([0.10, 0.12])
    assert notifications.format_slot_start_notification(
        schedule.selected_slots[0], schedule, unit="€/kWh", max_contracted_power=5000, now=_DAY
    )[2] == EXECUTION
    assert notifications.format_dp_pre_slot_reevaluation_notification(
        _slots([0.09])[0], _decision(energy_deficit_kwh=1.5), unit="€/kWh", now=_DAY
    )[2] == EXECUTION


# ----------------------------------------------------------------------
# Dated spans (task 3.1)
# ----------------------------------------------------------------------

_TZ = ZoneInfo("Europe/Madrid")
_TUE = datetime(2026, 11, 24, 22, 0, tzinfo=_TZ)


def _q(day, hour, hours=1, price=0.1):
    start = datetime(2026, 11, day, hour, 0, tzinfo=_TZ)
    return PriceSlot(start=start, end=start + timedelta(hours=hours), price=price)


def test_plan_crossing_midnight_lists_chronologically_with_dates():
    slots = [_q(25, 5, price=0.12), _q(24, 23, price=0.10), _q(25, 0, price=0.11)]  # out of order; prices differ so they stay apart
    schedule = _optimizer_schedule([0.1] * 3, [1.0, 1.0, 1.0])
    schedule.selected_slots = slots
    schedule.slot_energy_targets_kwh = dict(zip(slots, [1.0, 1.0, 1.0]))
    schedule.slot_deadlines = {s: None for s in slots}
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, energy_deficit_kwh=3.0, planned_grid_charge_kwh=3.0,
                  planner="optimizer", deadline_required_kwh=3.0),
        schedule, **{**_DP_CFG, "now": _TUE},
    )
    rows = [ln for ln in message.splitlines() if ln.startswith("| ") and "€/kWh" in ln]
    assert [ln.split(" | ")[0].lstrip("| ") for ln in rows] == [
        "23:00–00:00", "Wed 25 · 00:00–01:00", "Wed 25 · 05:00–06:00",
    ]


def _table_plan(lang="en", with_kwh=True):
    slots = [_q(24, 23, price=0.0890), _q(25, 0, price=0.0890), _q(25, 5, price=0.0890)]
    # Equal prices back to back would merge; 23:00 and 00:00 touch, so separate them.
    slots[0] = _q(24, 22, price=0.0890)
    schedule = _optimizer_schedule([0.1] * 3, [0.11, 2.03, 1.0])
    schedule.selected_slots = slots
    targets = dict(zip(slots, [0.11, 2.03, 1.0])) if with_kwh else {}
    schedule.slot_energy_targets_kwh = targets
    schedule.slot_deadlines = {s: None for s in slots}
    return notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, energy_deficit_kwh=3.0, planned_grid_charge_kwh=3.0,
                  planner="optimizer", deadline_required_kwh=3.0),
        schedule, **{**_DP_CFG, "now": _TUE, "lang": lang},
    )[1]


def test_optimizer_charge_slots_are_a_table_in_chronological_order():
    message = _table_plan()
    assert (
        "\n\n| Slot | Price | Energy |\n|---|---|---|\n"
        "| 22:00–23:00 | 0.0890 €/kWh | 0.11 kWh |\n"
        "| Wed 25 · 00:00–01:00 | 0.0890 €/kWh | 2.03 kWh |\n"
        "| Wed 25 · 05:00–06:00 | 0.0890 €/kWh | 1.00 kWh |\n"
    ) in message
    assert "•" not in message.split("|---|---|---|")[1].split("\n\n")[0]


def test_optimizer_table_dash_without_energy_and_spanish_header():
    assert "| 22:00–23:00 | 0.0890 €/kWh | — |" in _table_plan(with_kwh=False)
    assert "| Tramo | Precio | Energía |" in _table_plan(lang="es")


def test_two_nights_of_hold_are_dated_not_duplicated():
    orders = {"holds": frozenset({_q(25, 0, 8), _q(26, 0, 8)})}
    # One slot of 8 h each: two distinct nights, not a repeated span.
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False, planner="optimizer"), None,
        plan_orders=orders, **{**_DP_CFG, "now": _TUE},
    )
    assert (
        "🔒 Battery kept for later:\n"
        "  • Wed 25 · 00:00–08:00\n"
        "  • Thu 26 · 00:00–08:00\n"
    ) in message


def test_sale_and_room_of_tomorrow_are_dated_today_is_not():
    orders = {
        "sales": {_q(24, 23, 1): 2.5, _q(25, 1, 1): 2.5},
        "room": {_q(24, 22, 1): 1.2, _q(26, 9, 1): 1.2},
    }
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False, planner="optimizer"), None,
        plan_orders=orders, **{**_DP_CFG, "now": _TUE},
    )
    assert (
        "💶 Selling to the grid:\n"
        "  • 23:00–00:00 at 2.5 kW\n"
        "  • Wed 25 · 01:00–02:00 at 2.5 kW\n"
    ) in message
    assert "Making room for the sun: 22:00–23:00 at 1.2 kW, Thu 26 · 09:00–10:00 at 1.2 kW" in message


def test_span_with_naive_datetimes():
    now = datetime(2026, 11, 24, 22, 0)
    assert notifications._span(datetime(2026, 11, 24, 23, 0), datetime(2026, 11, 25, 0, 0), now) == "23:00–00:00"
    assert notifications._span(datetime(2026, 12, 3, 5, 0), datetime(2026, 12, 3, 6, 0), now) == "Thu 3 · 05:00–06:00"


def test_span_converts_utc_slots_to_the_local_zone():
    # 21:30-22:30 UTC is 22:30-23:30 in Madrid (CET): both ends local.
    start = datetime(2026, 11, 24, 21, 30, tzinfo=timezone.utc)
    end = datetime(2026, 11, 24, 22, 30, tzinfo=timezone.utc)
    assert notifications._span(start, end, _TUE) == "22:30–23:30"
    # 23:30 UTC is already tomorrow 00:30 locally: dated by its local start.
    start = datetime(2026, 11, 24, 23, 30, tzinfo=timezone.utc)
    assert notifications._span(start, start + timedelta(hours=1), _TUE) == "Wed 25 · 00:30–01:30"


def test_clock_today_and_other_day():
    now = datetime(2026, 11, 24, 22, 0, tzinfo=_TZ)
    assert notifications._clock(datetime(2026, 11, 24, 23, 30, tzinfo=_TZ), now) == "23:30"
    assert notifications._clock(datetime(2026, 11, 25, 5, 0, tzinfo=_TZ), now) == "Wed 25 · 05:00"
    utc = datetime(2026, 11, 24, 23, 30, tzinfo=timezone.utc)  # 00:30 Madrid, next day
    assert notifications._clock(utc, now) == "Wed 25 · 00:30"


def _dated_schedule(*starts):
    slots = [
        PriceSlot(start=s, end=s + timedelta(hours=1), price=0.1) for s in starts
    ]
    return SimpleNamespace(selected_slots=slots)


@pytest.mark.parametrize(
    "day, span, nxt",
    [
        (25, "Wed 25 · 05:00–06:00", "Next slot: Wed 25 · 07:00"),
        (24, "05:00–06:00", "Next slot: 07:00"),
    ],
)
def test_slot_start_notification_dates_only_other_days(day, span, nxt):
    now = datetime(2026, 11, 24, 4, 0 if day == 24 else 0, tzinfo=_TZ)
    now = now.replace(hour=22) if day == 25 else now
    first = datetime(2026, 11, day, 5, 0, tzinfo=_TZ)
    schedule = _dated_schedule(first, first.replace(hour=7))
    _, message, _ = notifications.format_slot_start_notification(
        schedule.selected_slots[0], schedule, unit="€/kWh", max_contracted_power=5000, now=now
    )
    assert f"Slot: {span}\n" in message
    assert nxt in message


@pytest.mark.parametrize("day, span, at", [
    (25, "Wed 25 · 05:00–06:00", "Wed 25 · 05:00"),
    (24, "05:00–06:00", "05:00"),
])
def test_pre_slot_notification_dates_only_other_days(day, span, at):
    now = datetime(2026, 11, 24, 22 if day == 25 else 4, 0, tzinfo=_TZ)
    slot = _dated_schedule(datetime(2026, 11, day, 5, 0, tzinfo=_TZ)).selected_slots[0]
    title, message, _ = notifications.format_dp_pre_slot_reevaluation_notification(
        slot, _decision(energy_deficit_kwh=1.5), unit="€/kWh", now=now
    )
    assert f"slot {at} confirmed" in title
    assert f"Slot: {span} @" in message
    assert f"activate at {at}" in message


# ----------------------------------------------------------------------
# Grouped layout (task 3.2)
# ----------------------------------------------------------------------


def test_one_hold_and_one_sale_stay_inline_two_become_lists():
    one = {"holds": frozenset({_q(24, 23, 1)}), "sales": {_q(25, 20, 1): 1.5}}
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False, planner="optimizer"), None,
        plan_orders=one, **{**_DP_CFG, "now": _TUE},
    )
    assert "🔒 Battery kept for later: 23:00–00:00\n" in message
    assert "💶 Selling to the grid: Wed 25 · 20:00–21:00 at 1.5 kW\n" in message
    two = {"sales": {_q(24, 23, 1): 2.5, _q(25, 20, 1): 1.5}}
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False, planner="optimizer"), None,
        plan_orders=two, **{**_DP_CFG, "now": _TUE},
    )
    assert (
        "💶 Selling to the grid:\n"
        "  • 23:00–00:00 at 2.5 kW\n"
        "  • Wed 25 · 20:00–21:00 at 1.5 kW\n"
    ) in message


def test_basis_has_an_emoji_in_the_plan_and_in_safe_mode():
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False), None, **_DP_CFG
    )
    assert "   🧮 Basis: " in message
    _, message, _ = notifications.format_predictive_notification_message(
        _decision(solar_forecast_kwh=None, should_charge=True), **_CFG
    )
    assert "   🧮 Basis: " in message


def _optimizer_plan_message():
    slots = [_q(24, 23, price=0.10), _q(25, 0, price=0.10), _q(25, 5, price=0.12)]
    schedule = _optimizer_schedule([0.1] * 3, [1.0, 1.0, 1.0])
    schedule.selected_slots = slots
    schedule.slot_energy_targets_kwh = dict(zip(slots, [1.0, 1.0, 0.5]))
    schedule.slot_deadlines = {s: None for s in slots}
    orders = {
        "holds": frozenset({_q(25, 0, 8), _q(26, 0, 8)}),
        "sales": {_q(25, 20, 1): 1.5},
    }
    return notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, energy_deficit_kwh=3.0, planned_grid_charge_kwh=2.5,
                  planner="optimizer", deadline_required_kwh=3.0,
                  battery_discharge_limit_w=2500),
        schedule, plan_orders=orders, **{**_DP_CFG, "now": _TUE},
    )[1]


def test_optimizer_plan_ends_with_thresholds_and_power_after_a_blank_line():
    message = _optimizer_plan_message()
    assert message.endswith(
        "\n\n⚙️ Price thresholds: charge ≤ 0.3000 €/kWh | discharge ≥ 0.4500 €/kWh\n"
        "Max charge power: 3000W (ICP: 5000W, batteries: 3000W)\n"
        "Max discharge power: 2500W"
    )
    assert message.count("Price thresholds") == 1 and "\n\n\n" not in message


def test_not_needed_ends_with_thresholds_after_a_blank_line():
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False), None, **_DP_CFG
    )
    lines = message.split("\n")
    assert lines[-1].startswith("⚙️ Price thresholds: ")
    assert lines[-2] == ""
    assert lines[-3] == "No grid charging required."


# ----------------------------------------------------------------------
# Estimated saving against buying the energy when it is used (task 3.4)
# ----------------------------------------------------------------------

_FIRST = _DAY  # the first charge slot starts here


def _saving_message(*, deliveries, charged=2.0, planned=None, eff=0.81, cost=0.30,
                    schedule_type="deficit", weekly=False, **decision):
    schedule = _optimizer_schedule([0.10, 0.10, 0.10, 0.12], [0.5, 0.5, 0.4, 0.3])
    # The energy the cost pays for (``charged``), whatever the plan says in total.
    schedule.slot_cost_energy_kwh = dict(
        zip(schedule.selected_slots, [charged * f for f in (0.25, 0.25, 0.25, 0.25)])
    )
    planned = charged if planned is None else planned
    schedule.estimated_cost = cost
    schedule.schedule_type = schedule_type
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, planned_grid_charge_kwh=planned,
                  **{"planner": "optimizer", **decision}),
        schedule, **_DP_CFG, house_delivery=deliveries,
        round_trip_efficiency=eff, weekly=weekly,
    )
    return message


def _after(minutes, kwh, price):
    return (_FIRST + timedelta(minutes=minutes), kwh, price)


def test_saving_line_hand_computed_case():
    # E = 2.0 * 0.81 = 1.62 kWh. The quarter ending before the first charge
    # slot (0.5 kWh at 0.10) is ignored. Delivered after it: 1.0 + 0.62 = 1.62,
    # reference = (1.0*0.30 + 0.62*0.40) / 1.62 = 0.548 / 1.62 = 0.33827.
    # valued = min(1.62, 1.62); alternative = 0.548 -> "~0.55";
    # saving = 0.548 - 0.30 = 0.248 -> "~0.25".
    message = _saving_message(deliveries=(
        _after(-15, 0.5, 0.10), _after(300, 1.0, 0.30), _after(315, 0.62, 0.40),
    ))
    line = "Buying this energy when it is used: ~0.55 €. Estimated saving: ~0.25 €"
    assert line in message
    assert message.index("Estimated cost") < message.index(line)
    assert message.split("Estimated cost")[1].split("\n")[1] == line


def test_saving_line_ignores_deferred_energy():
    # The plan charges 3.0 kWh, 1.0 of it deferred to tomorrow's slots, which
    # the 2.0 kWh cost does not pay for: E = 2.0 * 0.81 = 1.62, not 3.0 * 0.81.
    # Same figures as the hand-computed case: alternative ~0.55, saving ~0.25.
    # Deliveries are doubled (3.24 kWh, same 0.33827 reference) so that using
    # 3.0 would value 2.43 kWh (0.82) and fail.
    message = _saving_message(planned=3.0, deferred_grid_charge_kwh=1.0, deliveries=(
        _after(-15, 0.5, 0.10), _after(300, 1.0, 0.30), _after(315, 0.62, 0.40),
        _after(330, 1.0, 0.30), _after(345, 0.62, 0.40),
    ))
    assert "Buying this energy when it is used: ~0.55 €. Estimated saving: ~0.25 €" in message


def test_saving_line_values_only_what_is_discharged():
    # E = 1.62 but only 0.5 kWh at 0.50 is delivered: alternative = 0.25
    # (not 1.62 * 0.50), saving = 0.25 - 0.10 = 0.15.
    message = _saving_message(cost=0.10, deliveries=(_after(60, 0.5, 0.50),))
    assert "Buying this energy when it is used: ~0.25 €. Estimated saving: ~0.15 €" in message


@pytest.mark.parametrize("kwargs", [
    {"planner": "old"},
    {"floor_active": True},
    {"weekly": True},
    {"schedule_type": "negative_price"},
    {"schedule_type": "combined"},
    {"cost": 0.60},  # alternative 0.548 < cost: no saving
    {"cost": 0.548},  # saving rounds to 0.00
    {"deliveries": ()},
    {"deliveries": (_after(-15, 1.62, 0.90),)},  # nothing after the first slot
], ids=["old-planner", "floor", "weekly", "negative", "combined", "loss",
        "zero", "no-delivery", "all-before"])
def test_saving_line_hidden(kwargs):
    kwargs = {"deliveries": (_after(300, 1.0, 0.30), _after(315, 0.62, 0.40)), **kwargs}
    assert "Estimated saving" not in _saving_message(**kwargs)


# ----------------------------------------------------------------------
# lang reaches the text catalogue; missing keys fall back to English
# ----------------------------------------------------------------------

def test_lang_picks_catalogue_text_and_falls_back_to_english(monkeypatch):
    from custom_components.omnibattery.notifications import texts

    monkeypatch.setitem(texts._TEXTS, "es", {"plan.not_required.title": "Carga: no hace falta"})
    kwargs = dict(_CFG, lang="es")
    title, message, _ = notifications.format_predictive_notification_message(
        _decision(should_charge=False), **kwargs
    )
    assert title == "Carga: no hace falta"
    assert "No grid charging required." in message  # key missing in es: English


def test_span_and_clock_use_the_catalogue_day_name(monkeypatch):
    from custom_components.omnibattery.notifications import texts

    monkeypatch.setitem(texts._TEXTS, "es", {})
    monkeypatch.setitem(texts._DAYS, "es", ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"))
    now = datetime(2026, 11, 24, 10, 0)  # Tuesday
    start = datetime(2026, 11, 25, 1, 0)  # Wednesday
    end = start + timedelta(minutes=15)
    assert notifications._span(start, end, now, "es") == "Mié 25 · 01:00–01:15"
    assert notifications._clock(start, now, "es") == "Mié 25 · 01:00"
    assert notifications._span(start, end, now) == "Wed 25 · 01:00–01:15"
