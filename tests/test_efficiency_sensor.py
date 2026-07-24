"""Regression coverage for calculated round-trip efficiency."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.omnibattery.sensors.calculated_sensors import (
    MarstekVenusEfficiencySensor,
)


def _lifetime_efficiency_sensor(charge_kwh: float, discharge_kwh: float):
    """Build the non-MPPT sensor path without a live HA coordinator."""
    sensor = object.__new__(MarstekVenusEfficiencySensor)
    sensor._integrate_mode = False
    sensor.coordinator = SimpleNamespace(
        data={
            "total_charging_energy": charge_kwh,
            "total_discharging_energy": discharge_kwh,
        }
    )
    sensor._dependency_keys = {
        "charge": "total_charging_energy",
        "discharge": "total_discharging_energy",
    }
    return sensor


def test_lifetime_counter_efficiency_is_capped_at_physical_maximum():
    """Independent Anker counter baselines must not surface 300% efficiency."""
    sensor = _lifetime_efficiency_sensor(charge_kwh=10.0, discharge_kwh=30.0)

    assert sensor.native_value == 100.0


def test_lifetime_counter_efficiency_preserves_valid_ratio():
    sensor = _lifetime_efficiency_sensor(charge_kwh=10.0, discharge_kwh=9.2)

    assert sensor.native_value == pytest.approx(92.0)
