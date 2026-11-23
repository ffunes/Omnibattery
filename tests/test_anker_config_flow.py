"""Anker connection validation in reconfigure and options flows."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.omnibattery.drivers.anker_flow import (
    ANKER_SETUP,
    _anker_power_ceilings,
    _validate_anker_connection,
)
from custom_components.omnibattery.drivers.setup import FlowKind, SetupContext
from custom_components.omnibattery.infra.coordinator import MarstekVenusDataUpdateCoordinator
from custom_components.omnibattery.const import DOMAIN
from custom_components.omnibattery.drivers.anker import AnkerModbusDriver


def _hass_with_coordinator(coordinator) -> SimpleNamespace:
    return SimpleNamespace(
        data={
            DOMAIN: {
                "entry-1": {
                    "coordinators": [coordinator],
                }
            }
        }
    )


async def test_active_matching_anker_coordinator_avoids_second_probe(monkeypatch):
    probe = AsyncMock()
    monkeypatch.setattr(AnkerModbusDriver, "probe", probe)
    coordinator = SimpleNamespace(
        brand="anker",
        host="192.0.2.10",
        port=502,
        slave_id=1,
        is_available=True,
        data={
            "max_charge_power": 3200,
            "max_discharge_power": 3000,
        },
    )

    result = await _validate_anker_connection(
        _hass_with_coordinator(coordinator),
        "entry-1",
        "192.0.2.10",
        502,
        1,
    )

    assert result == (
        True,
        {
            "device_max_charge_power": 3200,
            "device_max_discharge_power": 3000,
        },
    )
    probe.assert_not_awaited()


async def test_changed_anker_endpoint_is_probed(monkeypatch):
    probe = AsyncMock(return_value=(True, {"device_max_charge_power": 2800}))
    monkeypatch.setattr(AnkerModbusDriver, "probe", probe)
    coordinator = SimpleNamespace(
        brand="anker",
        host="192.0.2.10",
        port=502,
        slave_id=1,
        is_available=True,
        data={},
    )

    result = await _validate_anker_connection(
        _hass_with_coordinator(coordinator),
        "entry-1",
        "192.0.2.11",
        502,
        1,
    )

    assert result == (True, {"device_max_charge_power": 2800})
    probe.assert_awaited_once_with("192.0.2.11", 502, 1)


async def test_unavailable_anker_coordinator_is_probed(monkeypatch):
    probe = AsyncMock(return_value=(False, {}))
    monkeypatch.setattr(AnkerModbusDriver, "probe", probe)
    coordinator = SimpleNamespace(
        brand="anker",
        host="192.0.2.10",
        port=502,
        slave_id=1,
        is_available=False,
        data={},
    )

    result = await _validate_anker_connection(
        _hass_with_coordinator(coordinator),
        "entry-1",
        "192.0.2.10",
        502,
        1,
    )

    assert result == (False, {})
    probe.assert_awaited_once_with("192.0.2.10", 502, 1)


async def test_matching_marstek_coordinator_is_not_reused_for_anker(monkeypatch):
    probe = AsyncMock(return_value=(True, {}))
    monkeypatch.setattr(AnkerModbusDriver, "probe", probe)
    coordinator = SimpleNamespace(
        brand="marstek",
        host="192.0.2.10",
        port=502,
        slave_id=1,
        is_available=True,
        data={},
    )

    result = await _validate_anker_connection(
        _hass_with_coordinator(coordinator),
        "entry-1",
        "192.0.2.10",
        502,
        1,
    )

    assert result == (True, {})
    probe.assert_awaited_once_with("192.0.2.10", 502, 1)


def test_flow_ceiling_accepts_combined_units_and_rejects_above():
    """Combined units report their total (#646); nothing above four units is offered."""
    assert _anker_power_ceilings(
        {"device_max_charge_power": 10500, "device_max_discharge_power": 10500}
    ) == (10500, 10500)
    assert _anker_power_ceilings(
        {"device_max_charge_power": 20000, "device_max_discharge_power": 20000}
    ) == (14000, 14000)
    assert _anker_power_ceilings({}) == (14000, 14000)


def _stored_limits(kind, battery_data):
    ctx = SetupContext(kind=kind, battery=None, entry=None, coordinator=None)
    merged = dict(battery_data)
    ANKER_SETUP.finalize_limits(merged, {}, battery_data, ctx)
    return merged["max_charge_power"], merged["max_discharge_power"]


def test_default_stored_max_stays_3500_when_registers_read_7000():
    """A lone unit's 7000 W peak reading must not raise the configured limit."""
    data = {"device_max_charge_power": 7000, "device_max_discharge_power": 7000}
    assert _stored_limits(FlowKind.CONFIG, data) == (3500, 3500)
    assert _stored_limits(FlowKind.OPTIONS, data) == (3500, 3500)
    low = {"device_max_charge_power": 2800, "device_max_discharge_power": 3000}
    assert _stored_limits(FlowKind.CONFIG, low) == (2800, 3000)


def _effective(device_w, configured_w):
    coordinator = object.__new__(MarstekVenusDataUpdateCoordinator)
    coordinator._device_max_charge_power = device_w
    coordinator._device_max_discharge_power = device_w
    coordinator._configured_max_charge_power = configured_w
    coordinator._configured_max_discharge_power = configured_w
    coordinator._recompute_effective_power_limits()
    return coordinator.effective_max_charge_power, coordinator.effective_max_discharge_power


def test_effective_limit_is_min_of_register_and_configured():
    assert _effective(7000, 3500) == (3500, 3500)  # single unit, 7000 W peak, default
    assert _effective(7000, 7000) == (7000, 7000)  # combined units, user raised it

