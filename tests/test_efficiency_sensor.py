"""Regression coverage for calculated round-trip efficiency."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.omnibattery.sensors.calculated_sensors import (
    MarstekVenusEfficiencySensor,
)


def _lifetime_efficiency_sensor(
    charge_kwh: float,
    discharge_kwh: float,
    *,
    daily_charge_kwh: float | None = None,
    daily_discharge_kwh: float | None = None,
    backup_discharge_kwh: float | None = None,
):
    """Build the non-MPPT sensor path without a live HA coordinator."""
    sensor = object.__new__(MarstekVenusEfficiencySensor)
    sensor._integrate_mode = False
    data = {
        "total_charging_energy": charge_kwh,
        "total_discharging_energy": discharge_kwh,
    }
    if daily_charge_kwh is not None:
        data["total_daily_charging_energy"] = daily_charge_kwh
    if daily_discharge_kwh is not None:
        data["total_daily_discharging_energy"] = daily_discharge_kwh
    if backup_discharge_kwh is not None:
        data["backup_discharging_energy"] = backup_discharge_kwh
        data["effective_total_discharging_energy"] = (
            discharge_kwh + backup_discharge_kwh
        )
    sensor.coordinator = SimpleNamespace(
        data=data,
        capabilities=SimpleNamespace(has_daily_energy_counters=False),
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


def test_daily_counters_do_not_override_lifetime_efficiency():
    """Efficiency consistently uses lifetime totals even when daily data exists."""
    sensor = _lifetime_efficiency_sensor(
        charge_kwh=10.0,
        discharge_kwh=9.2,
        daily_charge_kwh=3.9,
        daily_discharge_kwh=3.7,
    )

    assert sensor.native_value == pytest.approx(92.0)


def test_backup_discharge_is_included_in_lifetime_efficiency():
    sensor = _lifetime_efficiency_sensor(
        charge_kwh=10.0,
        discharge_kwh=8.4,
        backup_discharge_kwh=0.8,
    )

    assert sensor.native_value == pytest.approx(92.0)


def test_dual_plane_sampling_falls_back_to_the_measured_ac_port():
    """Issue #467: a PV driver without an ``ac_power`` register still measures
    its own AC port, so the dual-plane legs must sample from that instead of
    leaving efficiency unknown forever."""
    sensor = object.__new__(MarstekVenusEfficiencySensor)
    sensor._integrate_mode = True
    sensor._mppt_keys = ["mppt1_power", "mppt2_power", "mppt3_power", "mppt4_power"]
    sensor._charge_ac_kwh = sensor._charge_dc_kwh = 0.0
    sensor._discharge_ac_kwh = sensor._discharge_dc_kwh = 0.0
    sensor.coordinator = SimpleNamespace(
        data={
            "battery_power": -500.0,      # discharging the cells
            "ac_delivered_power": -450.0,  # battery_power convention: delivering
            "solar_power": 0.0,
        },
        capabilities=SimpleNamespace(has_mppt_pv=False),
    )

    sensor._last_mono = None
    sensor._accumulate()          # seeds the timer
    sensor._last_mono -= 360.0    # pretend six minutes passed
    sensor._accumulate()

    assert sensor._discharge_dc_kwh == pytest.approx(0.05, abs=1e-3)
    assert sensor._discharge_ac_kwh == pytest.approx(0.045, abs=1e-3)
    assert sensor.native_value == pytest.approx(81.0, abs=0.5)
