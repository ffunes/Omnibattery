"""Tests for the Sessy local HTTP driver."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
import voluptuous as vol
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.omnibattery.config_flow import (
    MarstekVenusConfigFlow,
    OptionsFlowHandler,
)
from custom_components.omnibattery.drivers.sessy import SessyLocalDriver
from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
)
from custom_components.omnibattery.sensors.calculated_sensors import (
    MarstekVenusCycleSensor,
    MarstekVenusStoredEnergySensor,
)


class _Response:
    def __init__(self, status=200, data=None):
        self.status = status
        self._data = data or {}

    async def json(self, content_type=None):
        return self._data

    async def read(self):
        return b"{}"


class _Context:
    def __init__(self, response): self.response = response
    async def __aenter__(self): return self.response
    async def __aexit__(self, *args): return False


def _session(status=None, energy=None, network=None, ota=None):
    power = status or {"sessy": {"state_of_charge": 0.6, "power": 420, "pack_voltage": 51200, "power_setpoint": 500}, "renewable_energy_phase1": {"power": 100}, "renewable_energy_phase2": {"power": 0}, "renewable_energy_phase3": {"power": -20}}
    session = MagicMock()
    session.closed = False
    session.get.side_effect = [
        _Context(_Response(200, power)),
        _Context(_Response(200, energy or {"sessy_energy": {"import_wh": 1200, "export_wh": 800}})),
        _Context(_Response(200, network or {"wifi_sta": {"rssi": -61}})),
        _Context(_Response(200, ota or {"self": {"installed_firmware": {"version": "1.9.2"}}})),
    ]
    session.post.return_value = _Context(_Response(200))
    return session


def _connect_session(auth_status=200):
    session = MagicMock()
    session.closed = False
    session.get.side_effect = [
        _Context(_Response(auth_status, {"strategy": "POWER_STRATEGY_NOM"})),
        _Context(_Response(200, {
            "sessy": {
                "state_of_charge": 0.6,
                "power": 0,
                "power_setpoint": 0,
            },
            "renewable_energy_phase1": {"power": 0},
            "renewable_energy_phase2": {"power": 0},
            "renewable_energy_phase3": {"power": 0},
        })),
    ]
    return session


@pytest.mark.asyncio
async def test_telemetry_maps_sessy_sign_and_units():
    snapshot = await SessyLocalDriver("sessy.local", session=_session()).read_telemetry()
    assert snapshot["battery_soc"] == 60
    assert snapshot["battery_power"] == -420
    assert snapshot["battery_voltage"] == 51200
    assert snapshot["pv_power"] == 80
    assert snapshot["total_charging_energy"] == 1200
    assert snapshot["wifi_signal_strength"] == -61
    assert snapshot["software_version"] == "1.9.2"


def test_sessy_uses_dongle_credentials_for_http_basic_auth():
    driver = SessyLocalDriver(
        "sessy.local", username="SESSY1234", password="secret"
    )

    assert driver._headers == {
        "Authorization": "Basic U0VTU1kxMjM0OnNlY3JldA=="
    }
    assert driver.model_label == "Sessy"


@pytest.mark.asyncio
async def test_connect_validates_authenticated_strategy_endpoint(caplog):
    session = _connect_session(auth_status=401)
    driver = SessyLocalDriver(
        "sessy.local",
        username="SESSY1234",
        password="secret",
        session=session,
    )

    assert await driver.connect() is False
    assert session.get.call_args_list[0].args[0].endswith(
        "/api/v1/power/active_strategy"
    )
    assert "Reconfigure the battery" in caplog.text


@pytest.mark.asyncio
async def test_connect_checks_telemetry_after_authentication():
    session = _connect_session()
    driver = SessyLocalDriver(
        "sessy.local",
        username="SESSY1234",
        password="secret",
        session=session,
    )

    assert await driver.connect() is True
    assert [call.args[0].rsplit("/api", 1)[-1] for call in session.get.call_args_list] == [
        "/v1/power/active_strategy",
        "/v1/power/status",
    ]


@pytest.mark.asyncio
async def test_repeated_auth_failures_emit_one_actionable_warning(caplog):
    session = MagicMock()
    session.closed = False
    session.get.side_effect = [
        _Context(_Response(401)),
        _Context(_Response(401)),
    ]
    driver = SessyLocalDriver(
        "sessy.local",
        username="SESSY1234",
        password="secret",
        session=session,
    )

    await driver._get_json("/api/v1/energy/status")
    await driver._get_json("/api/v1/network/status")

    assert sum(
        "Sessy local API authentication failed" in record.message
        for record in caplog.records
    ) == 1


def test_sessy_device_info_exposes_local_ui_and_firmware():
    coordinator = SimpleNamespace(
        device_key="sessy.local_80",
        name="Garage Sessy",
        brand="sessy",
        driver=SessyLocalDriver("sessy.local"),
        host="sessy.local",
        port=80,
        data={"software_version": "1.9.2"},
    )

    info = MarstekVenusDataUpdateCoordinator.battery_device_info.fget(
        coordinator
    )

    assert info["manufacturer"] == "Sessy"
    assert info["model"] == "Sessy"
    assert info["configuration_url"] == "http://sessy.local/"
    assert info["sw_version"] == "1.9.2"


@pytest.mark.asyncio
async def test_setpoint_selects_api_strategy_and_inverts_sign():
    session = _session()
    result = await SessyLocalDriver("sessy.local", session=session).apply_setpoint(700, read_back=False)
    assert result.ok and result.net_power_w == 700
    assert session.post.call_args_list[0].kwargs["json"] == {"strategy": "POWER_STRATEGY_API"}
    assert session.post.call_args_list[1].kwargs["json"] == {"setpoint": -700}


@pytest.mark.asyncio
async def test_setpoint_retries_after_sessy_closes_http_connection():
    session = _session()
    session.post.side_effect = [
        aiohttp.ServerDisconnectedError(),
        _Context(_Response(200)),
        _Context(_Response(200)),
    ]

    result = await SessyLocalDriver("sessy.local", session=session).apply_setpoint(
        700, read_back=False
    )

    assert result.ok
    assert session.post.call_count == 3
    assert session.post.call_args_list[0].kwargs["json"] == {
        "strategy": "POWER_STRATEGY_API"
    }
    assert session.post.call_args_list[1].kwargs["json"] == {
        "strategy": "POWER_STRATEGY_API"
    }
    assert session.post.call_args_list[2].kwargs["json"] == {"setpoint": -700}


@pytest.mark.asyncio
async def test_setpoint_clamps_legacy_limits_to_published_hardware_envelope():
    session = _session()
    driver = SessyLocalDriver(
        "sessy.local",
        session=session,
        max_charge_power_w=5000,
        max_discharge_power_w=5000,
    )

    assert driver.capabilities.max_charge_power_w == 2200
    assert driver.capabilities.max_discharge_power_w == 1700

    result = await driver.apply_setpoint(-2200, read_back=False)

    assert result.ok and result.net_power_w == -1700
    assert session.post.call_args_list[1].kwargs["json"] == {"setpoint": 1700}


@pytest.mark.asyncio
async def test_setpoint_confirms_against_sessy_power_setpoint():
    status = {"sessy": {"power_setpoint": -400, "power": -350}}
    session = _session(status=status)
    result = await SessyLocalDriver("sessy.local", session=session).apply_setpoint(400)
    assert result.confirmed is True
    assert result.battery_power_w == 350


def test_control_readback_uses_inverse_sessy_sign():
    driver = SessyLocalDriver("sessy.local", session=MagicMock(closed=False))
    assert driver.net_power_from_data({"power_setpoint": 600}) == -600


def test_sessy_uses_configured_capacity_for_stored_energy_and_cycles():
    """Sessy has energy counters but needs its nominal capacity from configuration."""
    data = {
        "battery_soc": 60,
        "battery_total_energy": 5.0,
        "total_charging_energy": 14.0,
        "total_discharging_energy": 12.4,
    }
    stored_energy = object.__new__(MarstekVenusStoredEnergySensor)
    stored_energy.coordinator = SimpleNamespace(data=data)
    stored_energy._dependency_keys = {"soc": "battery_soc", "capacity": "battery_total_energy"}
    cycles = object.__new__(MarstekVenusCycleSensor)
    cycles.coordinator = SimpleNamespace(
        data=data,
        capabilities=SimpleNamespace(cycles_from_discharge_only=True),
    )
    cycles._dependency_keys = {
        "charge": "total_charging_energy",
        "discharge": "total_discharging_energy",
        "capacity": "battery_total_energy",
    }

    assert stored_energy.native_value == 3.0
    assert cycles.native_value == 2.5


def test_sessy_reports_counters_but_not_nominal_capacity():
    capabilities = SessyLocalDriver("sessy.local", session=MagicMock(closed=False)).capabilities
    assert capabilities.has_energy_counters is True
    assert capabilities.has_nominal_capacity is False
    assert capabilities.cycles_from_discharge_only is True
    assert capabilities.readback_latency_s == 65.0
    assert capabilities.engage_grace_s == 65.0


def test_coordinator_clamps_legacy_sessy_power_limits_before_allocation(monkeypatch):
    """The clamp is coordinator logic; no Home Assistant runtime is needed."""
    monkeypatch.setattr(DataUpdateCoordinator, "__init__", lambda *_args, **_kwargs: None)

    coordinator = MarstekVenusDataUpdateCoordinator(
        MagicMock(),
        {
            "name": "Garage Sessy",
            "host": "sessy.local",
            "port": 80,
            "brand": "sessy",
            "max_charge_power": 5000,
            "max_discharge_power": 5000,
            "max_soc": 100,
            "min_soc": 12,
        },
        "sensor.grid_power",
    )

    assert coordinator.max_charge_power == 2200
    assert coordinator.max_discharge_power == 1700
    assert coordinator.user_max_charge_power == 2200
    assert coordinator.user_max_discharge_power == 1700


@pytest.mark.asyncio
async def test_sessy_configuration_requests_and_persists_nominal_capacity():
    flow = MarstekVenusConfigFlow()
    flow.config_data = {"num_batteries": 1}
    flow._current_battery_data = {"brand": "sessy"}

    form = await flow.async_step_battery_limits()
    fields = {
        marker.schema: selector for marker, selector in form["data_schema"].schema.items()
    }
    markers = {
        marker.schema: marker for marker in form["data_schema"].schema
    }
    assert "battery_capacity_kwh" in fields
    assert fields["max_charge_power"].config["max"] == 2200
    assert fields["max_discharge_power"].config["max"] == 1700
    assert fields["min_soc"].config["min"] == 0
    assert fields["min_soc"].config["max"] == 30
    assert markers["min_soc"].default() == 5
    assert fields["max_soc"].config["min"] == 80
    assert fields["max_soc"].config["max"] == 100
    assert markers["max_soc"].default() == 100
    capacity_marker = next(
        marker for marker in form["data_schema"].schema if marker.schema == "battery_capacity_kwh"
    )
    assert isinstance(capacity_marker, vol.Required)
    assert form["data_schema"].schema[capacity_marker].config["min"] == 0.01

    await flow.async_step_battery_limits(
        {
            "max_charge_power": 2200,
            "max_discharge_power": 1700,
            "max_soc": 100,
            "min_soc": 0,
            "charge_hysteresis_percent": 2,
            "backup_offgrid_threshold": 50,
            "battery_capacity_kwh": 5.12,
        }
    )

    assert flow.battery_configs[0]["battery_capacity_kwh"] == 5.12
    assert flow.battery_configs[0]["min_soc"] == 0
    assert flow.battery_configs[0]["max_soc"] == 100


@pytest.mark.asyncio
async def test_options_flow_offers_and_configures_sessy(monkeypatch):
    entry = SimpleNamespace(entry_id="test-entry", data={"batteries": []})
    flow = OptionsFlowHandler(entry)
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda entry_id: entry if entry_id == entry.entry_id else None
        )
    )
    flow.handler = entry.entry_id

    form = await flow.async_step_battery_brand()
    selector = next(iter(form["data_schema"].schema.values()))
    assert {option["value"] for option in selector.config["options"]} >= {"sessy"}

    form = await flow.async_step_battery_brand({"brand": "sessy"})
    assert form["step_id"] == "battery_connection_sessy"
    assert {marker.schema for marker in form["data_schema"].schema} >= {
        CONF_USERNAME,
        CONF_PASSWORD,
    }

    flow._current_battery_data = {"brand": "sessy"}
    limits = await flow.async_step_battery_limits()
    fields = {
        marker.schema: selector for marker, selector in limits["data_schema"].schema.items()
    }
    assert fields["max_charge_power"].config["max"] == 2200
    assert fields["max_discharge_power"].config["max"] == 1700
    markers = {
        marker.schema: marker for marker in limits["data_schema"].schema
    }
    assert fields["min_soc"].config["min"] == 0
    assert fields["min_soc"].config["max"] == 30
    assert markers["min_soc"].default() == 5
    assert fields["max_soc"].config["min"] == 80
    assert fields["max_soc"].config["max"] == 100
    assert markers["max_soc"].default() == 100
    capacity_marker = next(
        marker for marker in limits["data_schema"].schema if marker.schema == "battery_capacity_kwh"
    )
    assert isinstance(capacity_marker, vol.Required)
    assert not callable(capacity_marker.default)
    assert fields["battery_capacity_kwh"].config["min"] == 0.01

    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(SessyLocalDriver, "probe", probe)
    flow.async_step_battery_limits = AsyncMock(return_value={"type": "form"})
    result = await flow.async_step_battery_connection_sessy(
        {
            "name": "Garage Sessy",
            "host": "sessy.local",
            "port": 80,
            CONF_USERNAME: "SESSY1234",
            CONF_PASSWORD: "secret",
        }
    )

    assert result == {"type": "form"}
    probe.assert_awaited_once_with("sessy.local", 80, "SESSY1234", "secret")
    assert flow._current_battery_data == {
        "brand": "sessy",
        "name": "Garage Sessy",
        "host": "sessy.local",
        "port": 80,
        CONF_USERNAME: "SESSY1234",
        CONF_PASSWORD: "secret",
    }


@pytest.mark.asyncio
async def test_reconfigure_routes_sessy_to_its_http_form(monkeypatch):
    entry = SimpleNamespace(
        entry_id="test-entry",
        data={
            "batteries": [
                {
                    "brand": "sessy",
                    "name": "Garage Sessy",
                    "host": "old.local",
                    "port": 80,
                    CONF_USERNAME: "SESSY1234",
                    CONF_PASSWORD: "old-secret",
                }
            ]
        },
    )
    flow = MarstekVenusConfigFlow()
    flow.battery_index = 0
    flow._reconfigure_batteries = []
    flow._get_reconfigure_entry = lambda: entry
    flow._migrate_battery_registry_ids = MagicMock()

    form = await flow.async_step_reconfigure_battery()
    assert form["step_id"] == "reconfigure_battery_sessy"
    assert {marker.schema for marker in form["data_schema"].schema} == {
        "name",
        "host",
        "port",
        CONF_USERNAME,
        CONF_PASSWORD,
    }

    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(SessyLocalDriver, "probe", probe)
    flow.async_update_reload_and_abort = MagicMock(return_value={"type": "abort"})
    result = await flow.async_step_reconfigure_battery_sessy(
        {
            "name": "Garage Sessy",
            "host": "new.local",
            "port": 80,
            CONF_USERNAME: "SESSY1234",
            CONF_PASSWORD: "new-secret",
        }
    )

    assert result == {"type": "abort"}
    probe.assert_awaited_once_with(
        "new.local", 80, "SESSY1234", "new-secret"
    )
    assert flow._reconfigure_batteries == [
        {
            "brand": "sessy",
            "name": "Garage Sessy",
            "host": "new.local",
            "port": 80,
            CONF_USERNAME: "SESSY1234",
            CONF_PASSWORD: "new-secret",
        }
    ]
