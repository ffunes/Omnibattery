"""Tests for the active-balance blueprint event bridge."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.omnibattery.const import DOMAIN
from custom_components.omnibattery.drivers.base import DEFAULT_BALANCE_BASELINE
from custom_components.omnibattery.tracking.balance_monitor import BalanceMonitor
from custom_components.omnibattery.tracking.blueprint_measurement import (
    async_register_blueprint_balance_measurement_listener,
)


def _monitor() -> BalanceMonitor:
    monitor = BalanceMonitor.__new__(BalanceMonitor)
    monitor._hass = SimpleNamespace()
    monitor._controller = None
    monitor._store = SimpleNamespace(async_save=AsyncMock())
    monitor._data = {}
    monitor._states = {}
    monitor._sensor_groups = {}
    return monitor


def _coordinator(*, manual=True, data=None):
    return SimpleNamespace(
        name="Battery 1",
        device_key="192.0.2.10_502",
        capabilities=SimpleNamespace(balance_baseline=DEFAULT_BALANCE_BASELINE),
        battery_manual_mode_enabled=manual,
        data=data if data is not None else {
            "max_cell_voltage": 3.545,
            "min_cell_voltage": 3.520,
            "battery_soc": 99,
        },
    )


@pytest.mark.asyncio
async def test_blueprint_measurement_uses_coordinator_data_and_persists_source():
    monitor = _monitor()
    coordinator = _coordinator()

    recorded = await monitor.async_record_blueprint_balance_measurement(
        coordinator,
        phase="WAIT_MEASURE",
        measurement_id="run-1:1",
    )

    assert recorded is True
    reading = monitor._data[coordinator.device_key]["readings"][-1]
    assert reading["type"] == "top_balance_measurement"
    assert reading["source"] == "blueprint"
    assert reading["phase"] == "blueprint_wait_measure"
    assert reading["measurement_id"] == "run-1:1"
    assert reading["delta_mV"] == 25.0
    monitor._store.async_save.assert_awaited_once()


@pytest.mark.asyncio
async def test_blueprint_measurement_is_idempotent():
    monitor = _monitor()
    coordinator = _coordinator()

    assert await monitor.async_record_blueprint_balance_measurement(
        coordinator, measurement_id="run-1:1"
    ) is True
    assert await monitor.async_record_blueprint_balance_measurement(
        coordinator, measurement_id="run-1:1"
    ) is False

    assert len(monitor._data[coordinator.device_key]["readings"]) == 1
    monitor._store.async_save.assert_awaited_once()


@pytest.mark.asyncio
async def test_blueprint_measurement_rejects_unsafe_or_invalid_events():
    monitor = _monitor()

    assert await monitor.async_record_blueprint_balance_measurement(
        _coordinator(manual=False)
    ) is False
    assert await monitor.async_record_blueprint_balance_measurement(
        _coordinator(data={"max_cell_voltage": 3.5, "min_cell_voltage": 3.6})
    ) is False
    assert await monitor.async_record_blueprint_balance_measurement(
        _coordinator(), phase="DISCHARGE"
    ) is False

    assert monitor._data == {}
    monitor._store.async_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_event_resolves_selected_device_and_records_it(monkeypatch):
    from homeassistant.helpers import device_registry as dr

    coordinator = _coordinator()
    monitor = SimpleNamespace(
        async_record_blueprint_balance_measurement=AsyncMock(return_value=True)
    )
    device = SimpleNamespace(identifiers={(DOMAIN, coordinator.device_key)})
    registry = SimpleNamespace(async_get=Mock(return_value=device))
    monkeypatch.setattr(dr, "async_get", lambda _hass: registry)

    callback = None
    hass = SimpleNamespace(
        bus=SimpleNamespace(
            async_listen=Mock(side_effect=lambda _event_type, listener: listener)
        )
    )
    unsubscribe = async_register_blueprint_balance_measurement_listener(
        hass, [coordinator], monitor
    )
    callback = unsubscribe

    await callback(SimpleNamespace(data={
        "device_id": "device-1",
        "phase": "WAIT_MEASURE",
        "measurement_id": "run-1:1",
    }))

    monitor.async_record_blueprint_balance_measurement.assert_awaited_once_with(
        coordinator,
        phase="WAIT_MEASURE",
        measurement_id="run-1:1",
    )
