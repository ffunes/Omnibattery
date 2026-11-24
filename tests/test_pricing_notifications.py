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
    # Only the limit that binds, as a table.
    assert message.endswith("\n\n| ⚡ Maximum power | |\n|---|---|\n| 🔌 Charge | 3000W (batteries) |")


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
    assert "| 🔌 Charge | 3000W (Max. grid import) |" in message
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
    assert "| 🔌 Grid charge | 5.55 |" in message
    assert "| ☀️ Solar surplus after consumption | 2.99 |" in message
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
    assert "| 🔌 Grid charge (guaranteed minimum SOC) | 0.67 |" in message
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

    assert "| ☀️ Solar forecast | 3.00 |" in message
    assert "| kWh | Today |" in message
    assert "| 📊 House consumption | 12.15 |" in message
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

    assert "| kWh | Today | Night → 06:30 | Total |" in message
    assert "| 📊 House consumption | 10.40 | 1.75 | 12.15 |" in message
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

    assert "| kWh | Today |" in message
    assert "| 📊 House consumption | 12.15 |" in message
    assert "Night" not in message
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

    assert "| ☀️ Solar forecast | 3.00 |" in message
    assert "| 📊 House consumption | 16.85 |" in message
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
    assert "| Slot | Price |\n|---|---|\n| 18:00–18:15 | 0.1000 €/kWh |\n" in message
    assert message.count("€/kWh |\n") == 4  # one row per slot
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
    assert "| ⚡ Energy deficit | 4.55 |" in message
    assert "Grid charge planned: 1.58 kWh" in message
    assert "Estimated cost: ~0.75 €" in message


def test_dynamic_floor_charge_names_the_floor_in_title():
    schedule = _schedule([0.089], hours_needed=1.0, charging_needed=True)
    title, _, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, floor_active=True, planned_grid_charge_kwh=1.15),
        schedule,
        **_DP_CFG,
    )
    assert title == "Predictive Charging: Price Optimization + Min SOC floor - 1h selected"
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
    assert "| kWh | Today |" in message
    assert "| 📊 House consumption | 12.15 |" in message
    assert "Basis: remaining-day estimate from 7-day daily average" in message
    assert "17.98 kWh" not in message
    assert "| ☀️ Solar forecast | 3.00 |" in message


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
    assert "| kWh | Today | Night → 06:30 | Total |" in message
    assert "| 📊 House consumption | 10.40 | 1.75 | 12.15 |" in message


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

    assert "| ☀️ Solar forecast | 3.00 |" in message
    assert "| 📊 House consumption | 11.40 |" in message
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

    assert "| ☀️ Solar forecast | 3.00 |" in message
    assert "| 📊 House consumption | 6.25 |" in message
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
        "| 🔌 Charge | 3000W (batteries) |\n| 🔋 Discharge | 800W |\n\n"
        "Load above the maximum discharge power comes from the grid: "
        "3.46 kWh, left out of the charge"
    )


def test_dynamic_omits_the_discharge_line_without_a_limit():
    schedule = _schedule([0.10, 0.12], hours_needed=0.5, charging_needed=True)
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, energy_deficit_kwh=4.0, planned_grid_charge_kwh=2.7),
        schedule,
        **_DP_CFG,
    )

    assert "| 🔋 Discharge |" not in message


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
    assert "| 🔌 Grid charge | 1.70 |" in message
    for old in ("Energy deficit", "Required before deadlines", "Flexible by price",
                "[flexible]", "[deficit]", "meet each deadline", "of charging needed"):
        assert old not in message
    # Back-to-back quarters at one price are one line, with their energy added up.
    # Each row carries its share of the 0.40 cost, by price x energy
    # (0.14 and 0.036 of 0.176); the total row replaces the two text lines.
    assert "| 🔌 18:00–18:45 | 0.1000 €/kWh | 1.40 kWh | ~0.32 € |" in message
    assert "| 🔌 18:45–19:00 | 0.1200 €/kWh | 0.30 kWh | ~0.08 € |" in message
    assert "| **Total** | 0.1035 €/kWh | 1.70 kWh | ~0.40 € |" in message
    assert "Average price" not in message and "Estimated cost" not in message
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
    assert (
        "🗓️ Plan by slot:\n\n"
        "| Slot | Price |\n|---|---|\n"
        "| 🔒 19:00–19:30 | 0.3000 €/kWh |\n\n"
        "Legend:\n🔒 Battery kept for later (the house buys from the grid)\n\n"
    ) in message
    assert (
        "💶 Selling to the grid:\n\n| Slot | Power |\n|---|---|\n"
        "| 20:00–20:30 | 2.5 kW |\n\n"
    ) in message
    assert "Solar surplus to export: 10:00–11:00" in message
    assert "Solar charge delayed by the plan" in message
    assert (
        "🔋 Making room for the sun:\n\n| Slot | Power |\n|---|---|\n"
        "| 09:00–09:15 | 2.5 kW |\n| 09:15–09:30 | 1.2 kW |\n\n"
    ) in message
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
    assert "| 🔌 Left to the grid | 3.50 |" in message and "round-trip losses" in message
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
    rows = [ln for ln in message.splitlines()
            if ln.startswith("| ") and "€/kWh" in ln and "Total" not in ln]
    assert [ln.split(" | ")[0].lstrip("| ") for ln in rows] == [
        "🔌 23:00–00:00", "🔌 Wed 25 · 00:00–01:00", "🔌 Wed 25 · 05:00–06:00",
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
        "\n\n| Slot | Price | Energy | Cost |\n|---|---|---|---|\n"
        "| 🔌 22:00–23:00 | 0.0890 €/kWh | 0.11 kWh | ~0.01 € |\n"
        "| 🔌 Wed 25 · 00:00–01:00 | 0.0890 €/kWh | 2.03 kWh | ~0.26 € |\n"
        "| 🔌 Wed 25 · 05:00–06:00 | 0.0890 €/kWh | 1.00 kWh | ~0.13 € |\n"
        "| **Total** | 0.0890 €/kWh | 3.14 kWh | ~0.40 € |\n\n"
    ) in message
    assert "•" not in message.split("|---|---|---|---|")[1].split("\n\n")[0]


def test_a_slot_the_cost_pays_for_without_a_target_has_a_cost_and_no_energy():
    # A weekly-charge slot: cost energy, no plan target. Slots the schedule
    # dropped do not take a share: the rows add up to the estimated cost.
    slots = [_q(24, 22, price=0.10), _q(25, 5, price=0.20)]
    schedule = _optimizer_schedule([0.1] * 2, [1.0, 1.0])
    schedule.selected_slots = slots
    schedule.slot_energy_targets_kwh = {slots[0]: 1.0}
    schedule.slot_cost_energy_kwh = {slots[0]: 1.0, slots[1]: 1.0, _q(25, 9, price=0.5): 4.0}
    schedule.slot_deadlines = {s: None for s in slots}
    schedule.estimated_cost = 0.33
    message = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, planned_grid_charge_kwh=2.0, planner="optimizer"),
        schedule, **{**_DP_CFG, "now": _TUE},
    )[1]
    assert "| 🔌 22:00–23:00 | 0.1000 €/kWh | 1.00 kWh | ~0.11 € |" in message
    assert "| 🔌 Wed 25 · 05:00–06:00 | 0.2000 €/kWh | — | ~0.22 € |" in message
    assert "| **Total** | 0.1000 €/kWh | 1.00 kWh | ~0.33 € |" in message


def test_optimizer_table_drops_empty_columns_and_spanish_header():
    # Without energy there is no cost to share out: the text lines stay.
    bare = _table_plan(with_kwh=False)
    assert "| Slot | Price |\n|---|---|\n| 🔌 22:00–23:00 | 0.0890 €/kWh |\n" in bare
    assert "Total" not in bare and "Estimated cost: ~0.40 €" in bare
    assert "| Tramo | Precio | Energía | Coste |" in _table_plan(lang="es")


def test_two_nights_of_hold_are_dated_not_duplicated():
    orders = {"holds": frozenset({_q(25, 0, 8), _q(26, 0, 8)})}
    # One slot of 8 h each: two distinct nights, not a repeated span.
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False, planner="optimizer"), None,
        plan_orders=orders, **{**_DP_CFG, "now": _TUE},
    )
    assert (
        "| 🔒 Wed 25 · 00:00–08:00 | 0.1000 €/kWh |\n"
        "| 🔒 Thu 26 · 00:00–08:00 | 0.1000 €/kWh |\n"
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
        "💶 Selling to the grid:\n\n| Slot | Power |\n|---|---|\n"
        "| 23:00–00:00 | 2.5 kW |\n"
        "| Wed 25 · 01:00–02:00 | 2.5 kW |\n"
    ) in message
    assert "| 22:00–23:00 | 1.2 kW |\n| Thu 26 · 09:00–10:00 | 1.2 kW |\n" in message


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


def test_sales_are_a_table_of_one_or_more_rows():
    one = {"holds": frozenset({_q(24, 23, 1)}), "sales": {_q(25, 20, 1): 1.5}}
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False, planner="optimizer"), None,
        plan_orders=one, **{**_DP_CFG, "now": _TUE},
    )
    assert "| 🔒 23:00–00:00 | 0.1000 €/kWh |\n" in message
    assert "| Wed 25 · 20:00–21:00 | 1.5 kW |\n" in message
    two = {"sales": {_q(24, 23, 1): 2.5, _q(25, 20, 1): 1.5}}
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False, planner="optimizer"), None,
        plan_orders=two, **{**_DP_CFG, "now": _TUE},
    )
    assert (
        "💶 Selling to the grid:\n\n| Slot | Power |\n|---|---|\n"
        "| 23:00–00:00 | 2.5 kW |\n"
        "| Wed 25 · 20:00–21:00 | 1.5 kW |\n"
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
        "\n| ⚡ Maximum power | |\n|---|---|\n"
        "| 🔌 Charge | 3000W (batteries) |\n| 🔋 Discharge | 2500W |"
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
# The plan's estimated saving against the battery with no plan
# ----------------------------------------------------------------------

_SAVING = "💰 Estimated saving of the plan: ~{} € (against using the battery with no plan)"


def test_saving_line_closes_a_charge_plan_after_its_table():
    schedule = _optimizer_schedule([0.10, 0.10, 0.10, 0.12], [0.5, 0.5, 0.4, 0.3])
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, planned_grid_charge_kwh=1.7, planner="optimizer"),
        schedule, plan_saving=0.248, **_DP_CFG,
    )
    # After the table and its legend, a blank line apart.
    assert (
        "| **Total** | 0.1035 €/kWh | 1.70 kWh | ~0.40 € |\n\n"
        "Legend:\n🔌 Grid charge\n\n" + _SAVING.format("0.25")
    ) in message


def test_saving_line_shows_on_a_plan_that_only_holds():
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False, planner="optimizer"), None,
        plan_orders={"holds": frozenset({_slot_at(60)})}, plan_saving=0.05, **_DP_CFG,
    )
    assert (
        "| 🔒 19:00–19:15 | 0.3000 €/kWh |\n\n"
        "Legend:\n🔒 Battery kept for later (the house buys from the grid)\n\n"
        + _SAVING.format("0.05")
    ) in message


@pytest.mark.parametrize("planner, saving", [
    ("optimizer", None), ("optimizer", 0.004), ("optimizer", -0.30), ("old", 0.25),
], ids=["unknown", "under-a-cent", "loss", "old-planner"])
def test_saving_line_hidden(planner, saving):
    _, message, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False, planner=planner), None,
        plan_orders={"holds": frozenset({_slot_at(60)})}, plan_saving=saving, **_DP_CFG,
    )
    assert "Estimated saving" not in message


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


# ----------------------------------------------------------------------
# The plan's quarters: when it really charges and holds inside a slot
# ----------------------------------------------------------------------


def _quarter(day, hour, minute, kind, price, kwh):
    start = datetime(2026, 11, day, hour, minute, tzinfo=_TZ)
    return (start, start + timedelta(minutes=15), kind, price, kwh)


def _screenshot_message(lang="en"):
    """The 2026-11-25 plan: one hourly slot 07:00-08:00 at 0.089 charges
    0.31 kWh (one quarter at 4.6 kW) and holds the rest of the hour; the
    battery empties that evening."""
    slot = _q(25, 7, price=0.0890)
    schedule = _optimizer_schedule([0.1], [0.31])
    schedule.selected_slots = [slot]
    schedule.slot_energy_targets_kwh = {slot: 0.31}
    schedule.slot_deadlines = {slot: None}
    schedule.average_price = 0.0890
    schedule.estimated_cost = 0.03
    orders = {
        "holds": frozenset({slot}),
        "quarters": (
            _quarter(25, 7, 0, "charge", 0.0890, 0.31),
            _quarter(25, 7, 15, "hold", 0.0890, 0.10),
            _quarter(25, 7, 30, "hold", 0.0890, 0.10),
            _quarter(25, 7, 45, "hold", 0.0890, 0.12),
            _quarter(25, 21, 30, "empty", 0.20, 0.30),
            _quarter(25, 21, 45, "empty", 0.10, 0.10),
        ),
    }
    return notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, energy_deficit_kwh=0.31, planned_grid_charge_kwh=0.31,
                  planner="optimizer"),
        schedule, plan_orders=orders, **{**_DP_CFG, "now": _TUE, "lang": lang},
    )


def test_a_slot_that_charges_one_quarter_shows_that_quarter_then_the_hold():
    title, message, _ = _screenshot_message()
    assert title.endswith("- 0.25h selected")
    assert "Average price" not in message and "Estimated cost" not in message
    # One table, in time order. The hold is what the house buys meanwhile
    # (0.32 kWh x 0.089): same price as the charge before it, its own row.
    assert (
        "| 🔌 Wed 25 · 07:00–07:15 | 0.0890 €/kWh | 0.31 kWh | ~0.03 € |\n"
        "| 🔒 Wed 25 · 07:15–08:00 | 0.0890 €/kWh | 0.32 kWh | ~0.03 € |\n"
        "| 🪫 Wed 25 · 21:30–21:45 |"
    ) in message
    assert "07:00–08:00" not in message
    assert message.count("| Slot |") == 1 and "Charge slots" not in message
    assert "\n\n\n" not in message


def test_the_plan_says_when_the_battery_runs_empty_and_at_what_price():
    message = _screenshot_message()[1]
    # One row per price (0.3 kWh at 0.20, 0.1 at 0.10). The total closes the
    # one table: all the plan buys, 1.03 kWh for 0.03 + 0.03 + 0.06 + 0.01.
    assert (
        "\n\n🗓️ Plan by slot:\n\n"
        "| Slot | Price | Energy | Cost |\n|---|---|---|---|\n"
    ) in message
    assert (
        "| 🪫 Wed 25 · 21:30–21:45 | 0.2000 €/kWh | 0.30 kWh | ~0.06 € |\n"
        "| 🪫 Wed 25 · 21:45–22:00 | 0.1000 €/kWh | 0.10 kWh | ~0.01 € |\n"
        "| **Total** | 0.1224 €/kWh | 1.03 kWh | ~0.13 € |\n\n"
        "Legend:\n🔌 Grid charge\n"
        "🔒 Battery kept for later (the house buys from the grid)\n"
        "🪫 Battery empty (the house buys from the grid)\n\n"
    ) in message
    assert "\n\n\n" not in message
    assert (
        "🗓️ Plan por tramos:\n\n"
        "| Tramo | Precio | Energía | Coste |\n|---|---|---|---|\n"
        "| 🔌 mié 25 · 07:00–07:15 | 0.0890 €/kWh | 0.31 kWh | ~0.03 € |\n"
    ) in _screenshot_message("es")[1]


def test_charge_holds_and_empty_are_one_table_in_time_order():
    """A hold today, the night empty, tomorrow's charge and the hold after it
    at the same price, then empty again: one table, a row each, one total."""
    def rows(day, hour, minute, count, kind, price, kwh):
        first = datetime(2026, 11, day, hour, minute, tzinfo=_TZ)
        return [
            (first + timedelta(minutes=15 * i), first + timedelta(minutes=15 * (i + 1)),
             kind, price, kwh / count)
            for i in range(count)
        ]

    slot = PriceSlot(start=datetime(2026, 11, 25, 7, 0, tzinfo=_TZ),
                     end=datetime(2026, 11, 25, 8, 0, tzinfo=_TZ), price=0.0890)
    schedule = _optimizer_schedule([0.1], [1.04])
    schedule.selected_slots = [slot]
    schedule.slot_energy_targets_kwh = {slot: 1.04}
    schedule.slot_deadlines = {slot: None}
    schedule.estimated_cost = 0.11
    orders = {"quarters": tuple(
        rows(24, 17, 0, 4, "hold", 0.169, 0.60)
        + rows(24, 22, 0, 8, "empty", 0.169, 0.99)
        + rows(25, 0, 0, 28, "empty", 0.089, 1.98)
        + rows(25, 7, 0, 2, "charge", 0.089, 1.04)
        + rows(25, 7, 30, 2, "hold", 0.089, 0.23)
        + rows(25, 23, 45, 1, "empty", 0.169, 0.02)
    )}
    message = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, planned_grid_charge_kwh=1.04, planner="optimizer"),
        schedule, plan_orders=orders, **{**_DP_CFG, "now": _TUE, "lang": "es"},
    )[1]
    assert (
        "| Tramo | Precio | Energía | Coste |\n|---|---|---|---|\n"
        "| 🔒 17:00–18:00 | 0.1690 €/kWh | 0.60 kWh | ~0.10 € |\n"
        "| 🪫 22:00–00:00 | 0.1690 €/kWh | 0.99 kWh | ~0.17 € |\n"
        "| 🪫 mié 25 · 00:00–07:00 | 0.0890 €/kWh | 1.98 kWh | ~0.18 € |\n"
        "| 🔌 mié 25 · 07:00–07:30 | 0.0890 €/kWh | 1.04 kWh | ~0.11 € |\n"
        "| 🔒 mié 25 · 07:30–08:00 | 0.0890 €/kWh | 0.23 kWh | ~0.02 € |\n"
        "| 🪫 mié 25 · 23:45–00:00 | 0.1690 €/kWh | 0.02 kWh | ~0.00 € |\n"
        "| **Total** | 0.1155 €/kWh | 4.86 kWh | ~0.58 € |\n"
    ) in message
    assert message.count("| Tramo |") == 1
