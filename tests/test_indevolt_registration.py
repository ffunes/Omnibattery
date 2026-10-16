"""Registration of the Indevolt brand: flows, coordinator factory and translations.

These are the only places outside the Indevolt files that know the brand.
"""

from __future__ import annotations

import glob
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.omnibattery import drivers
from custom_components.omnibattery.config_flow import (
    MarstekVenusConfigFlow,
    OptionsFlowHandler,
)
from custom_components.omnibattery.drivers.indevolt import (
    PACK_TEMPERATURE_DEFINITIONS,
    SENSOR_DEFINITIONS,
    IndevoltDriver,
)
from custom_components.omnibattery.infra.indevolt_client import (
    REQUEST_SPEED_SLOT_S,
    SLOT_S,
    IndevoltAuthenticationError,
)
from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
)

_INPUT = {"name": "Garage Indevolt", "host": " 192.0.2.50 ", "port": 8080}
_PROBE = {
    "type": "CMS-SF2000", "model": "SolidFlex 2000", "known_model": True, "serial": "1",
    "firmware": "V140E.0B.003E", "energy_mode": 1.0, "realtime_mode": False,
    "max_charge_power": 2400, "max_discharge_power": 1800, "native_min_soc": 15.0,
    "battery_capacity_kwh": 7.18,
}


def _fields(form):
    return {marker.schema: selector for marker, selector in form["data_schema"].schema.items()}


def test_driver_is_exported():
    assert drivers.IndevoltDriver is IndevoltDriver


async def test_setup_flow_probes_read_only_and_notes_realtime_mode(monkeypatch):
    flow = MarstekVenusConfigFlow()
    flow.config_data = {"num_batteries": 1}
    flow._current_battery_data = {}

    form = await flow.async_step_battery_brand()
    selector = next(iter(form["data_schema"].schema.values()))
    assert "indevolt" in {option["value"] for option in selector.config["options"]}

    form = await flow.async_step_battery_brand({"brand": "indevolt"})
    assert form["step_id"] == "battery_connection_indevolt"
    fields = _fields(form)
    assert set(_fields(form)) == {"name", "host", "port"}  # plain HTTP, no password

    probe = AsyncMock(return_value=None)
    monkeypatch.setattr(IndevoltDriver, "probe", probe)
    form = await flow.async_step_battery_connection_indevolt(dict(_INPUT))
    assert form["errors"] == {"base": "cannot_connect"}

    probe.return_value = dict(_PROBE)
    form = await flow.async_step_battery_connection_indevolt(dict(_INPUT))
    probe.assert_awaited_with("192.0.2.50", 8080)
    assert form["step_id"] == "indevolt_mode_notice"
    assert flow._current_battery_data == {
        "brand": "indevolt", "name": "Garage Indevolt", "host": "192.0.2.50", "port": 8080,
        "device_max_charge_power": 2400, "device_max_discharge_power": 1800,
    }

    form = await flow.async_step_indevolt_mode_notice({})
    assert form["step_id"] == "indevolt_request_speed"
    fields = _fields(form)
    assert set(fields) == {"indevolt_request_speed"}
    assert fields["indevolt_request_speed"].config["options"] == ["maximum", "fast", "normal", "conservative"]
    markers = {marker.schema: marker for marker in form["data_schema"].schema}
    assert markers["indevolt_request_speed"].default() == "conservative"

    limits = await flow.async_step_indevolt_request_speed({"indevolt_request_speed": "fast"})
    assert flow._current_battery_data["indevolt_request_speed"] == "fast"
    assert limits["step_id"] == "battery_limits"
    fields = _fields(limits)
    assert fields["max_charge_power"].config["max"] == 2400
    assert fields["max_discharge_power"].config["max"] == 1800
    assert fields["min_soc"].config["min"] == 5
    markers = {marker.schema: marker for marker in limits["data_schema"].schema}
    assert markers["min_soc"].default() == 15
    # The SF2000 reports its capacity, so no capacity field.
    assert "battery_capacity_kwh" not in fields
    assert "full_charge_voltage_taper_enabled" not in fields


async def test_setup_flow_explains_an_http_digest_api(monkeypatch):
    flow = MarstekVenusConfigFlow()
    flow.config_data = {"num_batteries": 1}
    flow._current_battery_data = {"brand": "indevolt"}
    monkeypatch.setattr(IndevoltDriver, "probe", AsyncMock(side_effect=IndevoltAuthenticationError("401")))
    form = await flow.async_step_battery_connection_indevolt(dict(_INPUT))
    assert form["errors"] == {"base": "indevolt_http_digest"}


async def test_probe_reports_http_digest_instead_of_unreachable():
    async def digest(method, url):
        return 401, b""

    async def refused(method, url):
        raise OSError("connection refused")

    with pytest.raises(IndevoltAuthenticationError):
        await IndevoltDriver.probe("192.0.2.40", transport=digest)
    assert await IndevoltDriver.probe("192.0.2.41", transport=refused) is None


async def test_setup_flow_skips_the_notice_in_realtime_mode(monkeypatch):
    flow = MarstekVenusConfigFlow()
    flow.config_data = {"num_batteries": 1}
    flow._current_battery_data = {"brand": "indevolt"}
    monkeypatch.setattr(IndevoltDriver, "probe", AsyncMock(return_value={**_PROBE, "realtime_mode": True}))
    form = await flow.async_step_battery_connection_indevolt(dict(_INPUT))
    assert form["step_id"] == "indevolt_request_speed"


async def test_options_flow_offers_and_configures_indevolt(monkeypatch):
    entry = SimpleNamespace(entry_id="test-entry", data={"batteries": [
        {"brand": "indevolt", "name": "Garage Indevolt", "host": "192.0.2.50", "port": 8080,
         "indevolt_request_speed": "fast"},
    ]})
    flow = OptionsFlowHandler(entry)
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda entry_id: entry if entry_id == entry.entry_id else None
        )
    )
    flow.handler = entry.entry_id
    form = await flow.async_step_battery_brand()
    selector = next(iter(form["data_schema"].schema.values()))
    assert "indevolt" in {option["value"] for option in selector.config["options"]}
    form = await flow.async_step_battery_brand({"brand": "indevolt"})
    assert form["step_id"] == "battery_connection_indevolt"

    monkeypatch.setattr(IndevoltDriver, "probe", AsyncMock(return_value=dict(_PROBE)))
    flow.async_step_battery_limits = AsyncMock(return_value={"type": "form", "step_id": "battery_limits"})
    form = await flow.async_step_battery_connection_indevolt(dict(_INPUT))
    assert form["step_id"] == "indevolt_mode_notice"
    form = await flow.async_step_indevolt_mode_notice({})
    assert form["step_id"] == "indevolt_request_speed"
    markers = {marker.schema: marker for marker in form["data_schema"].schema}
    assert markers["indevolt_request_speed"].default() == "fast"  # the stored speed
    form = await flow.async_step_indevolt_request_speed({"indevolt_request_speed": "maximum"})
    assert form["step_id"] == "battery_limits"
    assert flow._current_battery_data["device_max_discharge_power"] == 1800
    assert flow._current_battery_data["indevolt_request_speed"] == "maximum"


async def test_reconfigure_routes_indevolt_to_its_form(monkeypatch):
    entry = SimpleNamespace(
        entry_id="test-entry",
        data={"batteries": [{"brand": "indevolt", "name": "Garage Indevolt", "host": "old",
                             "port": 8080, "min_soc": 20}]},
    )
    flow = MarstekVenusConfigFlow()
    flow.battery_index = 0
    flow._reconfigure_batteries = []
    flow._get_reconfigure_entry = lambda: entry
    flow._migrate_battery_registry_ids = MagicMock()
    form = await flow.async_step_reconfigure_battery()
    assert form["step_id"] == "reconfigure_battery_indevolt"

    monkeypatch.setattr(IndevoltDriver, "probe", AsyncMock(return_value=dict(_PROBE)))
    flow.async_update_reload_and_abort = MagicMock(return_value={"type": "abort"})
    form = await flow.async_step_reconfigure_battery_indevolt(dict(_INPUT))
    assert form["step_id"] == "reconfigure_battery_indevolt_request_speed"
    # Nothing is migrated yet: leaving the flow on the speed screen changes nothing.
    flow._migrate_battery_registry_ids.assert_not_called()
    result = await flow.async_step_reconfigure_battery_indevolt_request_speed({"indevolt_request_speed": "normal"})
    assert result == {"type": "abort"}
    flow._migrate_battery_registry_ids.assert_called_once_with(
        entry, "old", 8080, "192.0.2.50", 8080
    )
    assert flow._reconfigure_batteries[0]["min_soc"] == 20  # other settings kept
    assert flow._reconfigure_batteries[0]["host"] == "192.0.2.50"
    assert flow._reconfigure_batteries[0]["indevolt_request_speed"] == "normal"


async def test_reconfigure_shows_and_keeps_the_request_speed(monkeypatch):
    battery = {"brand": "indevolt", "name": "Garage Indevolt", "host": "192.0.2.50",
               "port": 8080, "indevolt_request_speed": "fast"}
    entry = SimpleNamespace(entry_id="test-entry", data={"batteries": [battery]})
    flow = MarstekVenusConfigFlow()
    flow.battery_index = 0
    flow._reconfigure_batteries = []
    flow._get_reconfigure_entry = lambda: entry
    flow._migrate_battery_registry_ids = MagicMock()
    monkeypatch.setattr(IndevoltDriver, "probe", AsyncMock(return_value=dict(_PROBE)))
    flow.async_update_reload_and_abort = MagicMock(return_value={"type": "abort"})
    form = await flow.async_step_reconfigure_battery_indevolt(dict(_INPUT))
    markers = {marker.schema: marker for marker in form["data_schema"].schema}
    assert markers["indevolt_request_speed"].default() == "fast"

    await flow.async_step_reconfigure_battery_indevolt_request_speed({"indevolt_request_speed": "conservative"})
    assert flow._reconfigure_batteries[0]["indevolt_request_speed"] == "conservative"


def test_coordinator_builds_the_indevolt_driver(monkeypatch):
    monkeypatch.setattr(DataUpdateCoordinator, "__init__", lambda *_args, **_kwargs: None)
    coordinator = MarstekVenusDataUpdateCoordinator(
        MagicMock(), "Garage Indevolt", "192.0.2.50", 8080, "sensor.grid_power",
        brand="indevolt", max_charge_power=2400, max_discharge_power=2400, min_soc=20,
    )
    driver = coordinator.driver
    assert isinstance(driver, IndevoltDriver)
    assert driver.capabilities.hardware_soc_cutoff is False
    assert driver.capabilities.has_force_mode is False
    assert driver.capabilities.readback_latency_s > 1.5  # the core's slow-device path
    # The settings sync stores into this battery's config through the coordinator.
    assert driver._settings._persist == coordinator.persist_battery_config
    assert driver._settings._stored("min_soc") is None  # no config entry attached
    assert coordinator.battery_device_info["manufacturer"] == "Indevolt"
    assert getattr(coordinator, "full_charge_voltage_taper_enabled") is False
    assert driver._slot_s == SLOT_S  # a battery config without a speed gets the default


@pytest.mark.parametrize(("speed", "slot_s"), [("maximum", 1.0), ("fast", 2.0), ("normal", 3.0),
                                                ("conservative", 5.0), ("warp", SLOT_S)])
def test_the_coordinator_hands_the_chosen_speed_to_the_driver(monkeypatch, speed, slot_s):
    monkeypatch.setattr(DataUpdateCoordinator, "__init__", lambda *_args, **_kwargs: None)
    coordinator = MarstekVenusDataUpdateCoordinator(
        MagicMock(), "Garage Indevolt", "192.0.2.50", 8080, "sensor.grid_power",
        brand="indevolt", max_charge_power=2400, max_discharge_power=2400, min_soc=20,
        indevolt_request_speed=speed,
    )
    assert coordinator.driver._slot_s == slot_s


def test_every_new_string_exists_in_every_language():
    sensor_keys = {d["key"] for d in SENSOR_DEFINITIONS + PACK_TEMPERATURE_DEFINITIONS}
    for path in ["custom_components/omnibattery/strings.json"] + sorted(
        glob.glob("custom_components/omnibattery/translations/*.json")
    ):
        data = json.load(open(path, encoding="utf-8"))
        for step in ("battery_connection_indevolt", "indevolt_mode_notice", "indevolt_request_speed",
                     "reconfigure_battery_indevolt", "reconfigure_battery_indevolt_request_speed"):
            assert data["config"]["step"][step]["title"], (path, step)
        for step in ("battery_connection_indevolt", "indevolt_mode_notice", "indevolt_request_speed"):
            assert data["options"]["step"][step]["title"], (path, step)
        for section in ("config", "options"):
            assert data[section]["error"]["indevolt_http_digest"], (path, section)
        assert "password" not in data["config"]["step"]["battery_connection_indevolt"]["data"]
        for section, step in (("config", "indevolt_request_speed"),
                              ("config", "reconfigure_battery_indevolt_request_speed"),
                              ("options", "indevolt_request_speed")):
            texts = data[section]["step"][step]
            assert texts["description"], (path, section, step)
            assert texts["data"]["indevolt_request_speed"], (path, section, step)
            assert texts["data_description"]["indevolt_request_speed"], (path, section, step)
        speeds = data["selector"]["indevolt_request_speed"]["options"]
        assert sorted(speeds) == sorted(REQUEST_SPEED_SLOT_S), path
        for speed, slot_s in REQUEST_SPEED_SLOT_S.items():
            assert f"{slot_s:.0f}s" in speeds[speed], (path, speed)
        # The Request Speed sensor reports the speed key; every language names it.
        states = data["entity"]["sensor"]["request_speed"]["state"]
        assert sorted(states) == sorted(REQUEST_SPEED_SLOT_S), path
        for speed, slot_s in REQUEST_SPEED_SLOT_S.items():
            assert f"({slot_s:.0f}s)" in states[speed], (path, speed)
        names = data["entity"]["sensor"]
        missing = sorted(k for k in sensor_keys if not names.get(k, {}).get("name"))
        assert missing == [], (path, missing)


async def _numbers_for(coordinator):
    from types import SimpleNamespace

    from custom_components.omnibattery.const import DOMAIN
    from custom_components.omnibattery.number import async_setup_entry as setup_numbers

    entry = SimpleNamespace(entry_id="e", data={})
    hass = SimpleNamespace(data={DOMAIN: {"e": {"coordinators": [coordinator]}}})
    numbers: list = []
    await setup_numbers(hass, entry, numbers.extend)
    return numbers


async def test_the_device_backed_min_soc_replaces_its_software_twin(monkeypatch):
    """Indevolt's reserve is the min SOC: one entity, written to the device. Max SOC
    has no device setting and keeps the software slider."""
    from custom_components.omnibattery.number import MarstekVenusNumber, SoftSocLimitNumber

    monkeypatch.setattr(DataUpdateCoordinator, "__init__", lambda *_args, **_kwargs: None)
    coordinator = MarstekVenusDataUpdateCoordinator(
        MagicMock(), "Garage Indevolt", "192.0.2.50", 8080, "sensor.grid_power",
        brand="indevolt", max_charge_power=2400, max_discharge_power=2400, min_soc=20,
    )
    numbers = await _numbers_for(coordinator)
    ids = [n.unique_id for n in numbers]
    assert len(ids) == len(set(ids))
    by_id = {n.unique_id: n for n in numbers}
    key = coordinator.device_key
    assert isinstance(by_id[f"{key}_discharging_cutoff_capacity"], MarstekVenusNumber)
    assert isinstance(by_id[f"{key}_charging_cutoff_capacity"], SoftSocLimitNumber)
    for power in ("max_charge_power", "max_discharge_power"):
        assert isinstance(by_id[f"{key}_{power}"], MarstekVenusNumber)


async def test_other_software_soc_brands_keep_both_sliders(monkeypatch):
    from custom_components.omnibattery.number import SoftSocLimitNumber

    monkeypatch.setattr(DataUpdateCoordinator, "__init__", lambda *_args, **_kwargs: None)
    coordinator = MarstekVenusDataUpdateCoordinator(
        MagicMock(), "Venus", "192.0.2.20", 502, "sensor.grid_power",
        battery_version="v3", max_charge_power=2500, max_discharge_power=2500, min_soc=12,
    )
    assert coordinator.capabilities.hardware_soc_cutoff is False
    soft = [n for n in await _numbers_for(coordinator) if isinstance(n, SoftSocLimitNumber)]
    assert sorted(n._kind for n in soft) == ["max", "min"]
