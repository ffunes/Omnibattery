"""T1 (#270 follow-up): the discharge power default must not be 0.

Before this fix, ``DEFAULT_HIGH_PRICE_DISCHARGE_MAX_POWER = 0.0`` meant a user
who turned the switch on without touching the slider got a feature that
silently sold nothing (``_config()`` rejects a power <= 0). The default now
resolves to the fleet's own discharge power wherever ``config_entry.data``
lacks the key; 0 stays a valid, honoured choice when set by hand or when no
battery is configured at all.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from custom_components.omnibattery.const import (
    CONF_HIGH_PRICE_DISCHARGE_MAX_POWER,
    DEFAULT_HIGH_PRICE_DISCHARGE_MAX_POWER,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    default_high_price_discharge_max_power,
)
from custom_components.omnibattery.control.high_price_discharge import (
    GUARD_NO_MAX_POWER,
    STATE_ACTIVE,
    STATE_INVALID_CONFIGURATION,
    HighPriceDischargeManager,
)
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.curtailment import BatterySnapshot

DAY = datetime(2026, 8, 2)
NOW = DAY + timedelta(hours=18, minutes=30)
HORIZON_END = DAY + timedelta(days=1, hours=7)
PEAK_PRICE = 0.50
BASE_PRICE = 0.10


def _battery(charge_w: int, discharge_w: int) -> dict:
    return {"max_charge_power": charge_w, "max_discharge_power": discharge_w}


def _fleet_data(*batteries: dict) -> dict:
    return {"batteries": list(batteries)}


# ----------------------------------------------------------------------
# default_high_price_discharge_max_power (pure function)
# ----------------------------------------------------------------------


def test_default_is_the_fleet_discharge_power():
    data = _fleet_data(_battery(2500, 2000), _battery(1500, 800))

    assert default_high_price_discharge_max_power(data) == 2800.0


def test_default_falls_back_to_the_sentinel_without_a_fleet():
    assert default_high_price_discharge_max_power({"batteries": []}) == (
        DEFAULT_HIGH_PRICE_DISCHARGE_MAX_POWER
    )


def test_a_hand_set_value_is_never_overridden_by_the_fleet_default():
    """A hand-set value -- including a hand-set 0 -- must survive untouched."""
    data = _fleet_data(_battery(2500, 2000))
    data[CONF_HIGH_PRICE_DISCHARGE_MAX_POWER] = 0.0

    resolved = data.get(
        CONF_HIGH_PRICE_DISCHARGE_MAX_POWER,
        default_high_price_discharge_max_power(data),
    )

    assert resolved == 0.0


# ----------------------------------------------------------------------
# End to end: the switch alone must produce a working plan
# ----------------------------------------------------------------------


def _price_slots(export: bool) -> list[PriceSlot]:
    slots = []
    cursor = DAY + timedelta(hours=18)
    while cursor < HORIZON_END:
        price = PEAK_PRICE if (export and cursor.hour == 18) else BASE_PRICE
        slots.append(PriceSlot(cursor, cursor + timedelta(hours=1), price))
        cursor += timedelta(hours=1)
    return slots


def _forecast():
    bins = [0.075] * 96
    return SimpleNamespace(
        intervals_by_date={
            DAY.date(): list(bins),
            (DAY + timedelta(days=1)).date(): list(bins),
        },
        intervals_kwh=list(bins),
        source="profile",
    )


def _pricing():
    return SimpleNamespace(
        energy_horizon_end=lambda now: HORIZON_END,
        get_future_export_price_slots=lambda horizon_end=None: _price_slots(True),
        get_future_price_slots=lambda horizon_end=None: _price_slots(False),
        _profile_remaining_consumption=lambda start, end: _forecast(),
        _curtailment_forecast_model=lambda now: (0.0, None, 0.0),
        _curtailment_battery_snapshots=lambda: [
            BatterySnapshot("battery-1", 80.0, 10.0, 100.0, 10.0, 2500.0, True, True)
        ],
        is_in_dynamic_pricing_slot=lambda: False,
    )


def _manager(max_power_w: float) -> HighPriceDischargeManager:
    controller = SimpleNamespace(
        high_price_discharge_enabled=True,
        high_price_discharge_max_power_w=max_power_w,
        min_arbitrage_margin=None,
        round_trip_efficiency=1.0,
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        consumption_sensor="sensor.grid_power",
        solar_forecast_sensor=None,
        solar_forecast_remaining_sensor=None,
        _pricing_mgr=_pricing(),
        _current_price_slot_active=False,
        _force_full_charge=False,
        _weekly_charge_mgr=None,
        _curtailment_runtime_status=None,
        _capacity_protection_active=False,
        manual_mode_enabled=False,
        _manual_slot_owned=None,
        is_discharge_blocked=lambda *a, **k: False,
        _apply_meter_transform=lambda state: 0.0,
        _sensor_is_within_stale_tolerance=lambda reported_at, now=None: True,
        set_setpoint_override=lambda source, value_w, priority=0: None,
        remove_setpoint_override=lambda source: None,
    )
    hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda entity_id: SimpleNamespace(last_reported=None))
    )
    manager = HighPriceDischargeManager(hass, controller)
    manager._now = lambda: NOW
    return manager


def test_switch_on_without_touching_the_slider_builds_a_plan():
    """The scenario the bug report describes: switch ON, slider untouched."""
    data = _fleet_data(_battery(2500, 2000), _battery(1500, 800))
    # No CONF_HIGH_PRICE_DISCHARGE_MAX_POWER key: exactly an untouched slider,
    # resolved the same way __init__.py resolves it for the controller.
    resolved_power = data.get(
        CONF_HIGH_PRICE_DISCHARGE_MAX_POWER,
        default_high_price_discharge_max_power(data),
    )

    manager = _manager(resolved_power)
    manager.refresh_override()

    assert manager.get_status()["state"] == STATE_ACTIVE


def test_an_empty_fleet_still_fails_safe_instead_of_crashing():
    data = _fleet_data()
    resolved_power = data.get(
        CONF_HIGH_PRICE_DISCHARGE_MAX_POWER,
        default_high_price_discharge_max_power(data),
    )

    manager = _manager(resolved_power)
    manager.refresh_override()

    assert manager.get_status()["state"] == STATE_INVALID_CONFIGURATION
    assert manager.get_status()["reason"] == GUARD_NO_MAX_POWER
