"""Drivers name the entity suffixes they no longer own; the sensor platform removes them."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.omnibattery import sensor
from custom_components.omnibattery.const import DOMAIN
from custom_components.omnibattery.drivers.anker import AnkerModbusDriver
from custom_components.omnibattery.drivers.marstek import MarstekModbusDriver

CTX = SimpleNamespace(max_charge_power_w=2500, max_discharge_power_w=2500)


def _anker(code):
    drv = AnkerModbusDriver("1.2.3.4", 502, 1, client=MagicMock())
    drv._set_product_code(code)
    return drv


def _marstek(version, **cfg):
    config = {"host": "1.2.3.4", "port": 502, "battery_version": version, **cfg}
    return MarstekModbusDriver.from_config(config, CTX)


def test_anker_without_independent_pv_drops_solar_power():
    assert tuple(_anker("DMWH").obsolete_entity_suffixes()) == ("solar_power",)
    assert tuple(_anker("ZZZZ").obsolete_entity_suffixes()) == ("solar_power",)


def test_anker_with_independent_pv_keeps_everything():
    assert tuple(_anker("DN7M").obsolete_entity_suffixes()) == ()


@pytest.mark.parametrize("version", ("vA", "vD"))
async def test_marstek_without_connected_panels_drops_pv_entities(version):
    drv = _marstek(version, dc_pv_connected=False)
    assert tuple(drv.obsolete_entity_suffixes()) == ("solar_power", "battery_cell_power")


@pytest.mark.parametrize("version", ("vA", "vD"))
async def test_marstek_with_connected_panels_keeps_everything(version):
    assert tuple(_marstek(version, dc_pv_connected=True).obsolete_entity_suffixes()) == ()
    assert tuple(_marstek(version).obsolete_entity_suffixes()) == ()


@pytest.mark.parametrize("version", ("v2", "v3"))
async def test_marstek_without_mppt_never_drops(version):
    drv = _marstek(version, dc_pv_connected=False)
    assert tuple(drv.obsolete_entity_suffixes()) == ()


def test_platform_removes_registered_entities_of_this_entry(monkeypatch):
    registered = {
        ("sensor", DOMAIN, "h_502_solar_power"): "sensor.a",
        ("sensor", DOMAIN, "1.2.3.4_solar_power"): "sensor.b",
        ("sensor", DOMAIN, "bat_solar_power"): "sensor.c",
        ("sensor", DOMAIN, "h_502_battery_cell_power"): "sensor.keep_other_suffix",
    }
    owners = {"sensor.a": "e1", "sensor.b": "e1", "sensor.c": "other", "sensor.keep_other_suffix": "e1"}
    removed = []
    reg = SimpleNamespace(
        async_get_entity_id=lambda *k: registered.get(tuple(k)),
        async_get=lambda eid: SimpleNamespace(config_entry_id=owners[eid]),
        async_remove=removed.append,
    )
    monkeypatch.setattr("homeassistant.helpers.entity_registry.async_get", lambda hass: reg)
    coordinator = SimpleNamespace(
        device_key="h_502", host="1.2.3.4", name="bat", brand="anker",
        driver=_anker("DMWH"),
        capabilities=SimpleNamespace(has_mppt_pv=False, has_solar_telemetry=False),
    )
    sensor._remove_obsolete_solar_entities(
        MagicMock(), SimpleNamespace(entry_id="e1"), [coordinator]
    )
    assert sorted(removed) == ["sensor.a", "sensor.b"]


async def test_platform_cleans_marstek_only_under_its_device_key(monkeypatch):
    """Only Anker entities were ever keyed on host/name; Marstek keeps device_key."""
    registered = {
        ("sensor", DOMAIN, "h_502_solar_power"): "sensor.a",
        ("sensor", DOMAIN, "1.2.3.4_solar_power"): "sensor.b",
        ("sensor", DOMAIN, "bat_battery_cell_power"): "sensor.c",
    }
    removed = []
    reg = SimpleNamespace(
        async_get_entity_id=lambda *k: registered.get(tuple(k)),
        async_get=lambda eid: SimpleNamespace(config_entry_id="e1"),
        async_remove=removed.append,
    )
    monkeypatch.setattr("homeassistant.helpers.entity_registry.async_get", lambda hass: reg)
    marstek = _marstek("vD", dc_pv_connected=False)
    caps = SimpleNamespace(has_mppt_pv=True, has_solar_telemetry=False)
    known = SimpleNamespace(device_key="h_502", host="1.2.3.4", name="bat",
                            brand="marstek", driver=marstek, capabilities=caps)
    sensor._remove_obsolete_solar_entities(MagicMock(), SimpleNamespace(entry_id="e1"), [known])
    assert removed == ["sensor.a"]

    # A brand that fell back to the Marstek driver was never cleaned (plan section 8).
    removed.clear()
    unknown = SimpleNamespace(**{**vars(known), "brand": "acme"})
    sensor._remove_obsolete_solar_entities(MagicMock(), SimpleNamespace(entry_id="e1"), [unknown])
    assert removed == []
