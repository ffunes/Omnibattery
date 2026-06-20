"""System battery cell power aggregation (signed +charge / -discharge).

Mirrors the dashboard formula ``-ac_power - ac_offgrid_power + sum(MPPT)`` so the
SOC card's Charge/Discharge blocks link to a sensor that matches what they show.
"""
from custom_components.marstek_venus_energy_manager.aggregate_sensors import (
    MarstekVenusAggregateSensor,
)
from tests.conftest import FakeCoordinator


def _sensor(coordinators, key):
    """Build an aggregate sensor without running __init__ (no listener wiring)."""
    sensor = MarstekVenusAggregateSensor.__new__(MarstekVenusAggregateSensor)
    sensor.coordinators = coordinators
    sensor.definition = {"key": key, "precision": 0}
    return sensor


def test_cell_power_grid_plus_solar_charge():
    # vA charging 200 W from the grid (ac_power -200) while 800 W of PV feeds the
    # cells via MPPT → cell charge = -(-200) + 800 = 1000 W.
    va = FakeCoordinator(data={"ac_power": -200, "mppt1_power": 800})
    sensor = _sensor([va], "system_battery_cell_power")
    assert sensor._calculate_battery_cell_power() == 1000


def test_cell_power_solar_bypass_net_discharge():
    # PV bypassing out the AC port (ac_power +500 discharge) with 300 W on MPPT →
    # net cell power = -500 + 300 = -200 W (still discharging the cells).
    va = FakeCoordinator(data={"ac_power": 500, "mppt1_power": 300})
    sensor = _sensor([va], "system_battery_cell_power")
    assert sensor._calculate_battery_cell_power() == -200


def test_cell_power_includes_ac_offgrid_and_all_mppt():
    # Backup-port draw (ac_offgrid +50) plus four MPPT strings.
    va = FakeCoordinator(data={
        "ac_power": -100, "ac_offgrid_power": 50,
        "mppt1_power": 100, "mppt2_power": 100, "mppt3_power": 100, "mppt4_power": 100,
    })
    sensor = _sensor([va], "system_battery_cell_power")
    # -(-100) - 50 + 400 = 450
    assert sensor._calculate_battery_cell_power() == 450


def test_cell_power_falls_back_to_battery_power():
    # A driver without ac_power contributes its signed battery_power directly.
    zendure = FakeCoordinator(data={"battery_power": 400})
    sensor = _sensor([zendure], "system_battery_cell_power")
    assert sensor._calculate_battery_cell_power() == 400


def test_cell_power_sums_across_batteries():
    a = FakeCoordinator(data={"ac_power": -200, "mppt1_power": 800})  # +1000
    b = FakeCoordinator(data={"ac_power": 500, "mppt1_power": 300})   # -200
    sensor = _sensor([a, b], "system_battery_cell_power")
    assert sensor._calculate_battery_cell_power() == 800


def test_cell_power_none_when_no_available_data():
    va = FakeCoordinator(data={"ac_power": -200, "mppt1_power": 800}, is_available=False)
    sensor = _sensor([va], "system_battery_cell_power")
    assert sensor._calculate_battery_cell_power() is None
