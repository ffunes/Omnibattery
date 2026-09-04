"""Characterization tests for the pure pricing notification formatters (PR2).

Pin the current (title, message) output of the formatters extracted from
``ChargeDischargeController`` into ``pricing.notifications`` so the move is
proven cero-cambio-funcional. Pure functions: no hass, no controller — schedule
is a SimpleNamespace exposing only the attributes the formatters read.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from custom_components.omnibattery.pricing import (
    PriceSlot,
    notifications,
)

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
            charging_time_slot={"start_time": "23:00", "end_time": "06:00"})


def test_predictive_safe_mode_when_no_solar():
    title, message = notifications.format_predictive_notification_message(
        _decision(solar_forecast_kwh=None, should_charge=True), **_CFG
    )
    assert title == "Predictive Charging: Safe mode"
    assert "conservative mode" in message


def test_predictive_not_required():
    title, message = notifications.format_predictive_notification_message(
        _decision(should_charge=False), **_CFG
    )
    assert title == "Predictive Charging: Not required"
    assert "No grid charging required." in message


def test_predictive_started():
    title, message = notifications.format_predictive_notification_message(
        _decision(should_charge=True, energy_deficit_kwh=2.0), **_CFG
    )
    assert title == "Predictive Charging: STARTED"
    assert "06:00" in message  # end of charging slot
    assert "contracted: 5000W, batteries: 3000W" in message


def test_predictive_started_reports_the_actual_planned_grid_charge():
    """The notification must match the energy used to calculate the SOC target."""
    _, message = notifications.format_predictive_notification_message(
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
    title, message = notifications.format_predictive_notification_message(
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
    title, _ = notifications.format_predictive_notification_message(
        _decision(should_charge=True, energy_deficit_kwh=2.0), True, **_CFG
    )
    assert title == "Predictive Charging: Expected today"


def test_predictive_remaining_fallback_labels_horizon_and_basis():
    _, message = notifications.format_predictive_notification_message(
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

    assert "Solar remaining until midnight: 3.00 kWh" in message
    assert "Home consumption remaining until midnight: 12.15 kWh" in message
    assert "Basis: temporary daily curve, adjusted with today's consumption" in message
    assert "7-day avg" not in message
    assert "Sufficient energy for the rest of today" in message


def test_predictive_daily_profile_reports_profile_as_basis():
    _, message = notifications.format_predictive_notification_message(
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
               max_contracted_power=5000, max_charge_capacity=3000)


def test_dynamic_none_schedule_not_needed():
    title, message = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=False), None, **_DP_CFG
    )
    assert title == "Predictive Charging: Price Optimization - NOT needed"


def test_dynamic_none_schedule_no_slots():
    title, _ = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, energy_deficit_kwh=4.0), None, **_DP_CFG
    )
    assert title == "Predictive Charging: Price Optimization - No slots available"


def test_dynamic_no_slots_blames_the_margin_when_it_binds():
    """Arbitrage ceiling under the static one, so the margin is the cause."""
    _, message = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, energy_deficit_kwh=4.0), None,
        **{**_DP_CFG, "arbitrage_ceiling": 0.20},
    )
    assert "spread is too small" in message
    assert "not binding" not in message


def test_dynamic_no_slots_blames_the_static_ceiling_when_it_binds():
    """Regression: a low static ceiling must not be reported as a margin problem."""
    _, message = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, energy_deficit_kwh=4.0), None,
        **{**_DP_CFG, "max_price_threshold": 0.10, "arbitrage_ceiling": 0.30},
    )
    assert "raise max price threshold" in message
    assert "spread is too small" not in message
    assert "(not binding)" in message


def test_dynamic_no_slots_without_gate_keeps_original_hint():
    _, message = notifications.format_dynamic_pricing_notification(
        _decision(should_charge=True, energy_deficit_kwh=4.0), None, **_DP_CFG
    )
    assert "raise max price threshold" in message
    assert "arbitrage" not in message


def test_dynamic_informational_lists_slots():
    schedule = _schedule([0.10, 0.12, 0.11, 0.09], charging_needed=False)
    title, message = notifications.format_dynamic_pricing_notification(
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
    title, message = notifications.format_dynamic_pricing_notification(
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


def test_dynamic_remaining_horizon_does_not_label_remainder_as_daily_average():
    schedule = _schedule([0.10, 0.12], hours_needed=0.5, charging_needed=True)
    _, message = notifications.format_dynamic_pricing_notification(
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
    assert "Solar remaining until midnight: 3.00 kWh" in message


def test_dynamic_remaining_profile_scope_is_recognized():
    schedule = _schedule([0.10, 0.12], hours_needed=0.5, charging_needed=True)
    _, message = notifications.format_dynamic_pricing_notification(
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

    assert "Solar remaining until midnight: 3.00 kWh" in message
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
    title, message = notifications.format_slot_start_notification(
        first, schedule, unit="€/kWh", max_contracted_power=5000
    )
    assert title == "Predictive Charging STARTED (0.1000 €/kWh)"
    assert "Charging at max 5000W" in message
    assert "2 slot(s) remaining" in message


def test_slot_start_last_slot():
    schedule = _schedule([0.10])
    title, message = notifications.format_slot_start_notification(
        schedule.selected_slots[0], schedule, unit="€/kWh", max_contracted_power=5000
    )
    assert "Last slot" in message and "No more slots today" in message


def test_dp_pre_slot_reevaluation():
    slot = _slots([0.09])[0]
    title, message = notifications.format_dp_pre_slot_reevaluation_notification(
        slot, _decision(energy_deficit_kwh=1.5), unit="€/kWh"
    )
    assert "confirmed" in title
    assert "0.0900 €/kWh" in message
    assert "Charging will activate" in message


def test_dp_pre_slot_reevaluation_recognizes_remaining_fallback():
    slot = _slots([0.09])[0]
    _, message = notifications.format_dp_pre_slot_reevaluation_notification(
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
    )

    assert "Solar remaining until midnight: 3.00 kWh" in message
    assert "Home consumption remaining until midnight: 6.25 kWh" in message
    assert "Basis: temporary daily curve, adjusted with today's consumption" in message
    assert "7-day avg" not in message


def test_evening_recharge():
    slots = _slots([0.08, 0.09])
    title, message = notifications.format_evening_recharge_notification(
        2.5, slots, unit="€/kWh", avg_soc=55.0
    )
    assert title == "Predictive Charging: Evening re-evaluation"
    assert "55% avg" in message
    assert "Deficit: 2.50 kWh" in message
