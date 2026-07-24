"""Anker connection validation in reconfigure and options flows."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.omnibattery.config_flow import _validate_anker_connection
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
