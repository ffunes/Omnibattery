"""System SOC aggregate: capacity weighting, and the fallback without capacity.

Drivers without a hardware nominal capacity (Zendure, Sessy) only expose
``battery_total_energy`` once the user configures it, so the weighted formula
has nothing to weight by. It must still publish a SOC (discussion #438).
"""
from custom_components.omnibattery.sensors.aggregate_sensors import (
    MarstekVenusAggregateSensor,
)
from tests.conftest import FakeCoordinator


def _sensor(coordinators):
    sensor = MarstekVenusAggregateSensor.__new__(MarstekVenusAggregateSensor)
    sensor.coordinators = coordinators
    sensor.definition = {"key": "system_soc", "precision": 0}
    return sensor


def test_system_soc_weighted_by_capacity():
    small = FakeCoordinator(data={"battery_soc": 100, "battery_total_energy": 1.0})
    big = FakeCoordinator(data={"battery_soc": 50, "battery_total_energy": 3.0})
    # (1.0 + 1.5) / 4.0 = 62.5 %
    assert _sensor([small, big])._calculate_system_soc() == 62


def test_system_soc_falls_back_to_mean_without_capacity():
    a = FakeCoordinator(data={"battery_soc": 40})
    b = FakeCoordinator(data={"battery_soc": 60})
    assert _sensor([a, b])._calculate_system_soc() == 50


def test_system_soc_none_without_any_soc():
    assert _sensor([FakeCoordinator(data={"battery_power": 100})])._calculate_system_soc() is None
