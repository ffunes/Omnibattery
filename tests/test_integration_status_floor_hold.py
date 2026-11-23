"""Integration Status names the Time Slot floor hold instead of "balanced" (#938)."""
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.sensor import IntegrationStatusSensor


def _status(blockers, *, grid_charging=False):
    sensor = IntegrationStatusSensor.__new__(IntegrationStatusSensor)
    sensor._controller = SimpleNamespace(
        manual_mode_enabled=False,
        predictive_charging_enabled=True,
        grid_charging_active=grid_charging,
        weekly_full_charge_enabled=False,
        charge_delay_enabled=False,
        get_charge_blockers=lambda: {},
        get_discharge_blockers=lambda: blockers,
        first_execution=False,
        previous_power=0,
    )
    for name in (
        "_ev_charger_state_key", "_balance_hold_batteries",
        "_capacity_protection_state_key", "_price_reserve_batteries",
        "_hourly_balance_state_key", "_backup_cooldown_batteries",
        "_time_slot_blocked",
    ):
        setattr(sensor, name, lambda *_a: None)
    return sensor.native_value


@pytest.mark.parametrize(
    ("blockers", "grid_charging", "state"),
    [
        ({"time_slot_floor_hold": {}}, False, "time_slot_floor_hold"),
        ({"time_slot_floor_hold": {}}, True, "grid_charging"),
        ({}, False, "balanced"),
    ],
)
def test_the_floor_hold_has_its_own_state(blockers, grid_charging, state):
    assert _status(blockers, grid_charging=grid_charging) == state
