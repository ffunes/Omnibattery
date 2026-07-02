"""Aggregate power sensors across mixed-brand coordinators.

Covers System Charge/Discharge Power (Zendure coordinators only synthesise
``battery_power`` (+charge / −discharge); Marstek exposes ``ac_power``) and
System Battery Cell Power (signed, mirroring the dashboard formula
``-ac_power - ac_offgrid_power + sum(MPPT)``).
"""
from custom_components.omnibattery.sensors.aggregate_sensors import (
    MarstekVenusAggregateSensor,
)
from tests.conftest import FakeCoordinator


def _sensor(coordinators, key):
    """Build an aggregate sensor without running __init__ (no listener wiring)."""
    sensor = MarstekVenusAggregateSensor.__new__(MarstekVenusAggregateSensor)
    sensor.coordinators = coordinators
    sensor.definition = {"key": key, "precision": 0}
    return sensor


def test_ac_convention_power_prefers_ac_power():
    # Marstek: ac_power used as-is (charge negative / discharge positive).
    assert MarstekVenusAggregateSensor._ac_convention_power({"ac_power": -300}) == -300
    assert MarstekVenusAggregateSensor._ac_convention_power({"ac_power": 450}) == 450


def test_ac_convention_power_falls_back_to_negated_battery_power():
    # Zendure: only battery_power (+charge / −discharge) → negate to ac convention.
    assert MarstekVenusAggregateSensor._ac_convention_power({"battery_power": 300}) == -300
    assert MarstekVenusAggregateSensor._ac_convention_power({"battery_power": -450}) == 450


def test_ac_convention_power_ac_power_wins_over_battery_power():
    data = {"ac_power": -300, "battery_power": 999}
    assert MarstekVenusAggregateSensor._ac_convention_power(data) == -300


def test_ac_convention_power_none_when_no_power_keys():
    assert MarstekVenusAggregateSensor._ac_convention_power({"battery_soc": 50}) is None


def test_charge_power_sums_marstek_and_zendure():
    marstek = FakeCoordinator(data={"ac_power": -200})       # charging 200 W
    zendure = FakeCoordinator(data={"battery_power": 300})   # charging 300 W
    sensor = _sensor([marstek, zendure], "system_charge_power")
    assert sensor._calculate_total_charge_power() == 500


def test_discharge_power_sums_marstek_and_zendure():
    marstek = FakeCoordinator(data={"ac_power": 150})        # discharging 150 W
    zendure = FakeCoordinator(data={"battery_power": -450})  # discharging 450 W
    sensor = _sensor([marstek, zendure], "system_discharge_power")
    assert sensor._calculate_total_discharge_power() == 600


def test_charge_power_excludes_discharging_zendure():
    zendure = FakeCoordinator(data={"battery_power": -450})  # discharging
    sensor = _sensor([zendure], "system_charge_power")
    assert sensor._calculate_total_charge_power() == 0


def test_unavailable_zendure_not_counted():
    zendure = FakeCoordinator(data={"battery_power": 300}, is_available=False)
    sensor = _sensor([zendure], "system_charge_power")
    assert sensor._calculate_total_charge_power() == 0


class _FakeState:
    def __init__(self, value, unit="W"):
        self.state = str(value)
        self.attributes = {"unit_of_measurement": unit}


class _FakeHass:
    def __init__(self, states):
        self.states = type("S", (), {"get": staticmethod(states.get)})()


class _FakeEntry:
    def __init__(self, data):
        self.data = data


def _home_sensor(coordinators, grid, solar):
    sensor = _sensor(coordinators, "home_consumption")
    sensor.hass = _FakeHass({"sensor.grid": _FakeState(grid), "sensor.solar": _FakeState(solar)})
    sensor.entry = _FakeEntry({
        "consumption_sensor": "sensor.grid",
        "solar_production_sensor": "sensor.solar",
    })
    return sensor


def test_home_consumption_includes_zendure_battery_power():
    # Regression: a registerless Zendure exposes only battery_power. Home
    # Consumption summed raw ac_power, dropping the Zendure's discharge — home
    # read low and the dashboard Home node collapsed toward 0 once an excluded
    # device was subtracted. grid 236 + marstek 0 + zendure 614 + solar 2249.
    marstek = FakeCoordinator(data={"ac_power": 0})
    zendure = FakeCoordinator(data={"battery_power": -614})  # discharging 614 W
    sensor = _home_sensor([marstek, zendure], grid=236, solar=2249)
    assert sensor._calculate_home_consumption() == 3099


def test_home_consumption_charging_zendure_reduces_home():
    # Charging Zendure (battery_power +400) draws from the bus → subtracts.
    zendure = FakeCoordinator(data={"battery_power": 400})
    sensor = _home_sensor([zendure], grid=1000, solar=0)
    assert sensor._calculate_home_consumption() == 600


# --- system_battery_cell_power: signed cell power (+charge / -discharge) -------
# Mirrors the dashboard formula -ac_power - ac_offgrid_power + sum(MPPT), so the
# SOC card's Charge/Discharge blocks link to a sensor that matches what they show.

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


def test_cell_power_includes_ac_offgrid_in_backup_mode():
    # In Backup Mode (grid down) the backup port draws from the cells: count it.
    va = FakeCoordinator(data={
        "ac_power": -100, "ac_offgrid_power": 50, "inverter_state": 4,
        "mppt1_power": 100, "mppt2_power": 100, "mppt3_power": 100, "mppt4_power": 100,
    })
    sensor = _sensor([va], "system_battery_cell_power")
    # -(-100) - 50 + 400 = 450
    assert sensor._calculate_battery_cell_power() == 450


def test_cell_power_ignores_ac_offgrid_on_grid():
    # Grid present (not Backup Mode): the backup port is grid passthrough, not cell
    # draw, so it must not inflate discharge / eat charge (issue #33).
    va = FakeCoordinator(data={
        "ac_power": 2500, "ac_offgrid_power": 300, "inverter_state": 3,  # Discharge
    })
    sensor = _sensor([va], "system_battery_cell_power")
    # -2500 - 0 = -2500 (offgrid ignored, not -2800)
    assert sensor._calculate_battery_cell_power() == -2500


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
