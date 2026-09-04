"""Regression coverage for driver-declared backup discharge energy (#321)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.backup_discharge_store import (
    BackupDischargeEnergyStore,
)
from custom_components.omnibattery.energy import (
    BackupDischargeAccumulator,
    effective_total_discharging_energy,
)
from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
)
from custom_components.omnibattery.sensor import MarstekVenusSensor
from custom_components.omnibattery.sensors.calculated_sensors import (
    CumulativeDailyEnergySensor,
    MarstekVenusCycleSensor,
    _CumulativeDailyEnergyData,
)


def test_accumulator_counts_only_positive_driver_normalized_output():
    accumulator = BackupDischargeAccumulator()

    assert accumulator.observe(
        now_monotonic=100.0,
        power_w=1000,
        local_date="2026-08-30",
    ) is False
    assert accumulator.observe(
        now_monotonic=460.0,
        power_w=1000,
        local_date="2026-08-30",
    ) is True
    assert accumulator.kwh == pytest.approx(0.1)
    assert accumulator.daily_kwh == pytest.approx(0.1)

    assert accumulator.observe(
        now_monotonic=560.0,
        power_w=0,
        local_date="2026-08-30",
    ) is False
    assert accumulator.observe(
        now_monotonic=660.0,
        power_w=-1000,
        local_date="2026-08-30",
    ) is False
    assert accumulator.kwh == pytest.approx(0.1)


def test_accumulator_rejects_stalled_sample_gap():
    accumulator = BackupDischargeAccumulator()
    accumulator.observe(
        now_monotonic=0.0,
        power_w=1000,
        local_date="2026-08-30",
    )

    assert accumulator.observe(
        now_monotonic=601.0,
        power_w=1000,
        local_date="2026-08-30",
    ) is False
    assert accumulator.kwh == 0.0


def test_transition_from_inactive_does_not_count_previous_interval():
    accumulator = BackupDischargeAccumulator()

    accumulator.observe(
        now_monotonic=0.0,
        power_w=0,
        local_date="2026-08-30",
    )
    accumulator.observe(
        now_monotonic=2.0,
        power_w=1000,
        local_date="2026-08-30",
    )

    assert accumulator.kwh == 0.0
    assert accumulator.observe(
        now_monotonic=4.0,
        power_w=1000,
        local_date="2026-08-30",
    ) is True
    assert accumulator.kwh == pytest.approx(1000 * 2 / 3_600_000)


def test_publish_preserves_raw_counter_and_exposes_corrected_total():
    data = {"total_discharging_energy": 95.74}
    accumulator = BackupDischargeAccumulator(
        kwh=1.25,
        daily_kwh=0.25,
        reset_date="2026-08-30",
    )

    accumulator.publish(data)

    assert data["total_discharging_energy"] == 95.74
    assert data["backup_discharging_energy"] == 1.25
    assert data["backup_daily_discharging_energy"] == 0.25
    assert data["effective_total_discharging_energy"] == pytest.approx(96.99)
    assert effective_total_discharging_energy(data) == pytest.approx(96.99)


def test_coordinator_persists_only_after_a_fresh_counted_sample(monkeypatch):
    coordinator = object.__new__(MarstekVenusDataUpdateCoordinator)
    coordinator.data = {
        "total_discharging_energy": 10.0,
        "ac_offgrid_power": 1000,
        "inverter_state": 4,
    }
    coordinator._backup_discharge_accumulator = BackupDischargeAccumulator(
        reset_date=dt_util.now().date().isoformat()
    )
    coordinator._backup_discharge_store = SimpleNamespace(set=Mock())
    coordinator._backup_discharge_store_key = "entry:0"
    coordinator.driver = SimpleNamespace(
        supplemental_discharge_power_w=Mock(return_value=1000.0)
    )
    samples = iter((100.0, 460.0))
    monkeypatch.setattr(
        "custom_components.omnibattery.infra.coordinator.time.monotonic",
        lambda: next(samples),
    )

    coordinator._update_backup_discharge_energy({"ac_offgrid_power": 1000})
    coordinator._backup_discharge_store.set.assert_not_called()
    coordinator._update_backup_discharge_energy({"ac_offgrid_power": 1000})

    assert coordinator.data["effective_total_discharging_energy"] == pytest.approx(10.1)
    coordinator._backup_discharge_store.set.assert_called_once_with(
        "entry:0",
        total_kwh=pytest.approx(0.1),
        daily_kwh=pytest.approx(0.1),
        reset_date=dt_util.now().date().isoformat(),
    )
    coordinator.driver.supplemental_discharge_power_w.assert_called_with(
        coordinator.data,
        frozenset({"ac_offgrid_power"}),
    )


def test_public_total_and_cycles_use_corrected_discharge():
    data = {
        "total_charging_energy": 10.0,
        "total_discharging_energy": 8.4,
        "backup_discharging_energy": 0.8,
        "effective_total_discharging_energy": 9.2,
        "battery_total_energy": 5.0,
    }
    total_sensor = object.__new__(MarstekVenusSensor)
    total_sensor.coordinator = SimpleNamespace(data=data)
    total_sensor.definition = {"key": "total_discharging_energy"}

    cycle_sensor = object.__new__(MarstekVenusCycleSensor)
    cycle_sensor.coordinator = SimpleNamespace(
        data=data,
        capabilities=SimpleNamespace(cycles_from_discharge_only=False),
    )
    cycle_sensor._dependency_keys = {
        "charge": "total_charging_energy",
        "discharge": "total_discharging_energy",
        "capacity": "battery_total_energy",
    }

    assert total_sensor.native_value == pytest.approx(9.2)
    assert total_sensor.extra_state_attributes == {
        "hardware_discharging_energy": 8.4,
        "backup_discharging_energy": 0.8,
    }
    assert cycle_sensor.native_value == pytest.approx(1.9)


def test_daily_discharge_uses_corrected_lifetime_total():
    today = dt_util.now().date().isoformat()
    sensor = object.__new__(CumulativeDailyEnergySensor)
    sensor.coordinator = SimpleNamespace(
        data={
            "total_discharging_energy": 10.0,
            "backup_discharging_energy": 0.2,
            "backup_daily_discharging_energy": 0.2,
            "effective_total_discharging_energy": 10.2,
        }
    )
    sensor._source_key = "total_discharging_energy"
    sensor._energy_data = _CumulativeDailyEnergyData(
        kwh=0.0,
        last_total=10.0,
        reset_date=today,
    )

    sensor._accumulate()

    assert sensor._energy_data.kwh == 0.0
    assert sensor._energy_data.last_total == pytest.approx(10.0)
    sensor._key = "total_daily_discharging_energy"
    sensor._precision = 2
    sensor._publish_daily()
    assert sensor.native_value == pytest.approx(0.2)
    assert sensor.coordinator.data["total_daily_discharging_energy"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_store_load_discards_malformed_totals():
    store = object.__new__(BackupDischargeEnergyStore)
    store._store = SimpleNamespace(
        async_load=AsyncMock(
            return_value={
                "entry:0": {
                    "total_kwh": "1.25",
                    "daily_kwh": "0.25",
                    "reset_date": "2026-08-30",
                },
                "entry:1": {"total_kwh": -1},
                "entry:2": {"total_kwh": "unavailable"},
                "entry:3": {"total_kwh": float("inf")},
            }
        )
    )

    await store.async_load()

    assert store._data == {
        "entry:0": {
            "total_kwh": 1.25,
            "daily_kwh": 0.25,
            "reset_date": "2026-08-30",
        }
    }

    assert store.get("entry:0", "2026-08-30") == {
        "total_kwh": 1.25,
        "daily_kwh": 0.25,
        "reset_date": "2026-08-30",
    }
    assert store.get("entry:0", "2026-08-31") == {
        "total_kwh": 1.25,
        "daily_kwh": 0.0,
        "reset_date": "2026-08-31",
    }
