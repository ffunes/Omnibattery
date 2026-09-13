"""Unit tests for the ESPHome-entity driver (Marstek behind a LilyGo RS485 bridge).

HA is faked with a minimal states/services stub, so no runtime is required.
Entity resolution is tested through the pure ``_match_entities`` helper.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.drivers.esphome import (
    BINARY_SENSOR_DEFINITIONS,
    EsphomeEntityDriver,
    NUMBER_DEFINITIONS,
    SELECT_DEFINITIONS,
    SENSOR_DEFINITIONS,
    SWITCH_DEFINITIONS,
)


# ---------------------------------------------------------------------------
# HA fakes
# ---------------------------------------------------------------------------

class _State:
    def __init__(self, state: str):
        self.state = state


class _States:
    def __init__(self, table: dict[str, str]):
        self._table = table

    def get(self, entity_id):
        raw = self._table.get(entity_id)
        return _State(raw) if raw is not None else None


def _hass(states: dict[str, str]) -> MagicMock:
    hass = MagicMock()
    hass.states = _States(states)
    hass.services.async_call = AsyncMock()
    return hass


# Entity ids as the upstream YAML creates them on a device named "marstek".
_ENTITIES = {
    "battery_soc":                 "sensor.marstek_battery_state_of_charge",
    "battery_power":               "sensor.marstek_battery_power",
    "ac_power":                    "sensor.marstek_ac_power",
    "battery_voltage":             "sensor.marstek_battery_voltage",
    "internal_temperature":        "sensor.marstek_internal_temperature",
    "software_version":            "sensor.marstek_software_version",
    "ems_version":                 "sensor.marstek_firmware_version",
    "bms_version":                 "sensor.marstek_bms_version",
    "battery_runtime_estimate":    "sensor.marstek_battery_runtime_estimate",
    "esp_ip":                       "sensor.marstek_esphome_ip",
    "esp_ssid":                     "sensor.marstek_esphome_ssid",
    "esp_version":                  "sensor.marstek_esp_version",
    "wifi_status":                  "sensor.marstek_battery_wifi_status",
    "bt_status":                    "sensor.marstek_bt_status",
    "cloud_status":                 "sensor.marstek_cloud_status",
    "power_restriction":            "sensor.marstek_power_restriction",
    "internal_mos1_temperature":    "sensor.marstek_internal_mos1_temperature",
    "internal_mos2_temperature":    "sensor.marstek_internal_mos2_temperature",
    "max_cell_temperature":         "sensor.marstek_max_cell_temperature",
    "min_cell_temperature":         "sensor.marstek_min_cell_temperature",
    "cell_voltage_delta":           "sensor.marstek_cell_voltage_delta",
    "esp_wifi_signal_strength":     "sensor.marstek_esp_wifi_signal_strength",
    "esp_wifi_status":              "binary_sensor.marstek_esp_wifi_status",
    "inverter_state":              "sensor.marstek_inverter_state",
    "force_mode":                  "select.marstek_forcible_charge_discharge",
    "user_work_mode":              "select.marstek_user_work_mode",
    "backup_function":             "select.marstek_backup_function",
    "rs485_control_mode":          "select.marstek_rs485_control_mode",
    "set_charge_power":            "number.marstek_forcible_charge_power",
    "set_discharge_power":         "number.marstek_forcible_discharge_power",
    "max_charge_power":            "number.marstek_max_charge_power",
    "max_discharge_power":         "number.marstek_max_discharge_power",
    "charging_cutoff_capacity":    "number.marstek_charging_cutoff_capacity",
    "discharging_cutoff_capacity": "number.marstek_discharging_cutoff_capacity",
}


def _driver(states: dict[str, str]) -> EsphomeEntityDriver:
    driver = EsphomeEntityDriver(_hass(states), "devid123")
    driver._entities = dict(_ENTITIES)
    return driver


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------

def test_match_entities_by_original_name():
    entries = [
        ("sensor", "Battery State Of Charge", "sensor.renamed_by_user"),
        ("sensor", "Max. Cell Voltage", "sensor.marstek_max_cell_voltage"),
        ("select", "Forcible Charge-Discharge", "select.marstek_forcible_charge_discharge"),
        ("number", "Forcible Charge Power", "number.marstek_forcible_charge_power"),
        ("sensor", "Battery Runtime Estimate", "sensor.marstek_battery_runtime_estimate"),
        ("sensor", "Unmapped Diagnostic", "sensor.marstek_unmapped_diagnostic"),
    ]
    resolved = EsphomeEntityDriver._match_entities(entries)
    # original_name match survives an entity_id rename
    assert resolved["battery_soc"] == "sensor.renamed_by_user"
    # punctuation in the name slugs away
    assert resolved["max_cell_voltage"] == "sensor.marstek_max_cell_voltage"
    assert resolved["force_mode"] == "select.marstek_forcible_charge_discharge"
    assert resolved["set_charge_power"] == "number.marstek_forcible_charge_power"
    assert resolved["battery_runtime_estimate"] == "sensor.marstek_battery_runtime_estimate"
    # unmapped upstream entities are ignored
    assert "unmapped_diagnostic" not in resolved


def test_match_entities_entity_id_fallback():
    # original_name lost (None): the entity_id suffix still matches.
    entries = [
        ("sensor", None, "sensor.mydevice_battery_state_of_charge"),
        ("select", None, "select.mydevice_forcible_charge_discharge"),
    ]
    resolved = EsphomeEntityDriver._match_entities(entries)
    assert resolved["battery_soc"] == "sensor.mydevice_battery_state_of_charge"
    assert resolved["force_mode"] == "select.mydevice_forcible_charge_discharge"


def test_match_entities_includes_lilygo_diagnostics():
    entries = [
        ("sensor", "Software Version", "sensor.marstek_software_version"),
        ("sensor", "Battery Wifi Status", "sensor.marstek_battery_wifi_status"),
        ("sensor", "Internal MOS1 Temperature", "sensor.marstek_internal_mos1_temperature"),
        ("binary_sensor", "BAT communication failure", "binary_sensor.marstek_bat_communication_failure"),
        ("binary_sensor", "ESP WiFi Status", "binary_sensor.marstek_esp_wifi_status"),
    ]
    resolved = EsphomeEntityDriver._match_entities(entries)
    assert resolved["software_version"] == "sensor.marstek_software_version"
    assert resolved["wifi_status"] == "sensor.marstek_battery_wifi_status"
    assert resolved["internal_mos1_temperature"] == "sensor.marstek_internal_mos1_temperature"
    assert resolved["bat_communication_failure"] == "binary_sensor.marstek_bat_communication_failure"
    assert resolved["esp_wifi_status"] == "binary_sensor.marstek_esp_wifi_status"


def test_legacy_force_mode_option_names_are_still_decoded():
    assert EsphomeEntityDriver._decode("force_mode", "stop") == 0
    assert EsphomeEntityDriver._decode("force_mode", "standby") == 0
    assert EsphomeEntityDriver._decode("force_mode", "charge") == 1
    assert EsphomeEntityDriver._decode("force_mode", "discharge") == 2


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_telemetry_decodes_states():
    driver = _driver({
        _ENTITIES["battery_soc"]: "57",
        _ENTITIES["battery_power"]: "-612.0",
        _ENTITIES["ac_power"]: "830",
        _ENTITIES["battery_voltage"]: "52.3",
        _ENTITIES["inverter_state"]: "Discharge",
        _ENTITIES["force_mode"]: "Discharge",
        _ENTITIES["user_work_mode"]: "manual",
        _ENTITIES["backup_function"]: "disable",
        _ENTITIES["rs485_control_mode"]: "enable",
        _ENTITIES["set_charge_power"]: "0",
        _ENTITIES["set_discharge_power"]: "650",
        _ENTITIES["charging_cutoff_capacity"]: "95",
        _ENTITIES["max_charge_power"]: "2500",
    })
    snap = await driver.read_telemetry()
    assert snap["battery_soc"] == 57
    assert snap["battery_power"] == -612
    assert snap["battery_voltage"] == 52.3
    # select states decode to Marstek wire values
    assert snap["force_mode"] == 2
    assert snap["user_work_mode"] == 0
    assert snap["backup_function"] == 1
    assert snap["rs485_control_mode"] == 21930
    # inverter state text → v2 numeric code
    assert snap["inverter_state"] == 3
    # cutoffs stay in percent (scale 1 end to end)
    assert snap["charging_cutoff_capacity"] == 95
    # unavailable / unmapped keys are omitted
    assert "min_cell_voltage" not in snap


@pytest.mark.asyncio
async def test_read_telemetry_offline_device_returns_empty():
    driver = _driver({
        _ENTITIES["battery_soc"]: "unavailable",
        _ENTITIES["battery_power"]: "unknown",
    })
    assert await driver.read_telemetry() == {}


@pytest.mark.asyncio
async def test_read_telemetry_key_filter():
    driver = _driver({
        _ENTITIES["battery_soc"]: "57",
        _ENTITIES["ac_power"]: "830",
    })
    snap = await driver.read_telemetry(["battery_soc"])
    assert snap == {"battery_soc": 57}


@pytest.mark.asyncio
async def test_read_telemetry_decodes_lilygo_diagnostics():
    driver = _driver({
        _ENTITIES["software_version"]: "V12",
        _ENTITIES["ems_version"]: "V34",
        _ENTITIES["bms_version"]: "V56",
        _ENTITIES["battery_runtime_estimate"]: "Voll in: 1h 20min",
        _ENTITIES["esp_ip"]: "192.168.1.42",
        _ENTITIES["esp_ssid"]: "Home WiFi",
        _ENTITIES["esp_version"]: "2026.7.0",
        _ENTITIES["wifi_status"]: "Connected",
        _ENTITIES["bt_status"]: "Active",
        _ENTITIES["cloud_status"]: "Disconnected",
        _ENTITIES["power_restriction"]: "800W limited",
        _ENTITIES["internal_mos1_temperature"]: "41.5",
        _ENTITIES["cell_voltage_delta"]: "0.012",
        _ENTITIES["esp_wifi_signal_strength"]: "-52",
        _ENTITIES["esp_wifi_status"]: "on",
    })
    snap = await driver.read_telemetry()
    assert snap["software_version"] == "V12"
    assert snap["ems_version"] == "V34"
    assert snap["battery_runtime_estimate"] == "Voll in: 1h 20min"
    assert snap["esp_ip"] == "192.168.1.42"
    assert snap["wifi_status"] is True
    assert snap["cloud_status"] is False
    assert snap["bt_status"] == "Active"
    assert snap["internal_mos1_temperature"] == 41.5
    assert snap["cell_voltage_delta"] == 0.012
    assert snap["esp_wifi_status"] is True


def test_inverter_state_labels_match_lilygo_firmware():
    definition = next(d for d in SENSOR_DEFINITIONS if d["key"] == "inverter_state")
    assert {key: definition["states"][key] for key in (4, 5, 6)} == {
        4: "Fault", 5: "Idle", 6: "AC Bypass"
    }
    assert EsphomeEntityDriver._decode("inverter_state", "Fault") == 4
    assert EsphomeEntityDriver._decode("inverter_state", "AC bypass") == 6


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------

def _calls(driver):
    return [
        (c.args[0], c.args[1], c.args[2])
        for c in driver.hass.services.async_call.call_args_list
    ]


@pytest.mark.asyncio
async def test_apply_setpoint_charge(monkeypatch):
    import asyncio as _asyncio
    monkeypatch.setattr(_asyncio, "sleep", AsyncMock())
    driver = _driver({
        _ENTITIES["force_mode"]: "Charge",
        _ENTITIES["set_charge_power"]: "800",
        _ENTITIES["set_discharge_power"]: "0",
        _ENTITIES["battery_power"]: "790",
    })
    result = await driver.apply_setpoint(800)
    assert result.ok and result.confirmed
    assert result.net_power_w == 800
    assert result.battery_power_w == 790
    calls = _calls(driver)
    # discharge number, charge number, then the force select — Marstek order
    assert calls[0] == ("number", "set_value", {"entity_id": _ENTITIES["set_discharge_power"], "value": 0})
    assert calls[1] == ("number", "set_value", {"entity_id": _ENTITIES["set_charge_power"], "value": 800})
    assert calls[2] == ("select", "select_option", {"entity_id": _ENTITIES["force_mode"], "option": "Charge"})


@pytest.mark.asyncio
async def test_apply_setpoint_discharge_clamps_to_capability():
    driver = _driver({})
    result = await driver.apply_setpoint(-9999, read_back=False)
    assert result.ok and not result.confirmed
    assert result.net_power_w == -2500
    calls = _calls(driver)
    assert calls[0][2]["value"] == 2500
    assert calls[2][2]["option"] == "Discharge"


@pytest.mark.asyncio
async def test_apply_setpoint_idle_and_failure():
    driver = _driver({})
    result = await driver.apply_setpoint(0, read_back=False)
    assert result.ok
    assert _calls(driver)[2][2]["option"] == "None"

    driver.hass.services.async_call = AsyncMock(side_effect=Exception("boom"))
    result = await driver.apply_setpoint(500, read_back=False)
    assert not result.ok
    assert result.failure_reason == "service_call_failed"


@pytest.mark.asyncio
async def test_write_control_maps_wire_values():
    driver = _driver({})
    assert await driver.write_control("force_mode", 1)
    assert await driver.write_control("rs485_control_mode", 21947)
    assert await driver.write_control("max_charge_power", 2000)
    assert not await driver.write_control("nonexistent_key", 1)
    calls = _calls(driver)
    assert calls[0] == ("select", "select_option", {"entity_id": _ENTITIES["force_mode"], "option": "Charge"})
    assert calls[1] == ("select", "select_option", {"entity_id": _ENTITIES["rs485_control_mode"], "option": "disable"})
    assert calls[2] == ("number", "set_value", {"entity_id": _ENTITIES["max_charge_power"], "value": 2000})


@pytest.mark.asyncio
async def test_net_power_from_data():
    driver = _driver({})
    assert driver.net_power_from_data({"force_mode": 1, "set_charge_power": 700, "set_discharge_power": 0}) == 700
    assert driver.net_power_from_data({"force_mode": 2, "set_charge_power": 0, "set_discharge_power": 400}) == -400
    assert driver.net_power_from_data({"force_mode": 0, "set_charge_power": 0, "set_discharge_power": 0}) == 0
    assert driver.net_power_from_data({}) is None


@pytest.mark.asyncio
async def test_concrete_config_methods():
    driver = _driver({_ENTITIES["rs485_control_mode"]: "enable"})
    assert await driver.apply_config(
        max_soc_pct=95, min_soc_pct=15, max_charge_power_w=2000, max_discharge_power_w=1800
    )
    assert await driver.set_rs485_control(True)
    assert await driver.get_rs485_control() is True
    assert await driver.standby()
    calls = _calls(driver)
    assert calls[0][2] == {"entity_id": _ENTITIES["charging_cutoff_capacity"], "value": 95}
    assert calls[1][2] == {"entity_id": _ENTITIES["discharging_cutoff_capacity"], "value": 15}
    assert calls[4][2] == {"entity_id": _ENTITIES["rs485_control_mode"], "option": "enable"}


# ---------------------------------------------------------------------------
# Definitions / capabilities sanity
# ---------------------------------------------------------------------------

def test_definitions_have_no_register_and_scale_one():
    for d in (
        SENSOR_DEFINITIONS
        + NUMBER_DEFINITIONS
        + SELECT_DEFINITIONS
        + SWITCH_DEFINITIONS
        + BINARY_SENSOR_DEFINITIONS
    ):
        assert "register" not in d
        assert d.get("scale", 1) == 1  # HA states are already final values


def test_lilygo_diagnostics_are_diagnostic_entities():
    sensor_keys = {
        "internal_temperature", "max_cell_voltage", "min_cell_voltage",
        "internal_mos1_temperature", "internal_mos2_temperature",
        "max_cell_temperature", "min_cell_temperature", "cell_voltage_delta",
        "esp_wifi_signal_strength", "software_version", "ems_version",
        "bms_version", "esp_version",
    }
    definitions = {
        d["key"]: d for d in SENSOR_DEFINITIONS + BINARY_SENSOR_DEFINITIONS
    }
    assert all(definitions[key].get("category") == "diagnostic" for key in sensor_keys)
    assert len(BINARY_SENSOR_DEFINITIONS) == 27  # 3 status + 24 warnings


def test_capabilities_force_mode_keeps_hardware_manual_path():
    driver = _driver({})
    assert driver.capabilities.has_force_mode
    # force_mode select + set_charge_power number exist → the coordinator's
    # needs_software_manual_control stays False (Marstek-style manual entities).
    assert any(d["key"] == "force_mode" for d in driver.select_definitions)
    assert any(d["key"] == "set_charge_power" for d in driver.number_definitions)
    assert driver.capabilities.has_energy_counters
    assert not driver.capabilities.has_alarm_registers


# ---------------------------------------------------------------------------
# Bus liveness guard (issue #452)
# ---------------------------------------------------------------------------

class _TimedState:
    def __init__(self, state: str, last_reported):
        self.state = state
        self.last_reported = last_reported


class _TimedStates:
    def __init__(self, table: dict[str, tuple[str, float]]):
        # entity_id -> (state, age in seconds)
        now = dt_util.utcnow()
        self._table = {
            entity_id: _TimedState(value, now - timedelta(seconds=age))
            for entity_id, (value, age) in table.items()
        }

    def get(self, entity_id):
        return self._table.get(entity_id)


def _timed_driver(table: dict[str, tuple[str, float]]) -> EsphomeEntityDriver:
    hass = MagicMock()
    hass.states = _TimedStates(table)
    hass.services.async_call = AsyncMock()
    driver = EsphomeEntityDriver(hass, "devid123")
    driver._entities = dict(_ENTITIES)
    return driver


@pytest.mark.asyncio
async def test_read_telemetry_passes_a_live_bus():
    # A single key may legitimately be silent for minutes (the firmware filters
    # Battery Power with `delta: 5.0`); only the newest report gates the read.
    driver = _timed_driver({
        _ENTITIES["battery_soc"]: ("57", 3),
        _ENTITIES["battery_power"]: ("-612.0", 900),
    })
    snapshot = await driver.read_telemetry(["battery_soc", "battery_power"])
    assert snapshot == {"battery_soc": 57, "battery_power": -612}


@pytest.mark.asyncio
async def test_read_telemetry_drops_a_stalled_bus():
    # Every state object still holds a readable value — this is exactly the
    # frozen-telemetry case the coordinator used to control on.
    driver = _timed_driver({
        _ENTITIES["battery_soc"]: ("54", 4000),
        _ENTITIES["battery_power"]: ("-16", 4000),
        _ENTITIES["inverter_state"]: ("Charge", 4000),
    })
    assert await driver.read_telemetry(["battery_soc", "battery_power"]) == {}
    # Recovers on its own once the ESP speaks again, no reconnect needed.
    driver.hass.states = _TimedStates({
        _ENTITIES["battery_soc"]: ("54", 2),
        _ENTITIES["battery_power"]: ("-16", 2),
    })
    assert await driver.read_telemetry(["battery_soc"]) == {"battery_soc": 54}


@pytest.mark.asyncio
async def test_esp_local_entities_do_not_mask_a_stalled_bus():
    # wifi_signal / wifi_info / version publish from the ESP itself and keep
    # updating while the Modbus bus is dead. They must not count as a heartbeat.
    driver = _timed_driver({
        _ENTITIES["battery_soc"]: ("54", 4000),
        _ENTITIES["battery_power"]: ("-16", 4000),
        _ENTITIES["esp_wifi_signal_strength"]: ("-64", 1),
        _ENTITIES["esp_ip"]: ("192.168.1.42", 1),
    })
    assert await driver.read_telemetry(["battery_soc"]) == {}


@pytest.mark.asyncio
async def test_guard_fails_open_without_timestamps():
    # A state object the heartbeat cannot date must never block the read.
    driver = _driver({_ENTITIES["battery_soc"]: "57"})
    assert await driver.read_telemetry(["battery_soc"]) == {"battery_soc": 57}


def test_liveness_capability_enables_the_reconnect_probe():
    # Without this the coordinator skips the post-reconnect probe for push
    # drivers, clears the failure counter on a wedged bus and never backs off.
    driver = _driver({})
    assert driver.capabilities.push_telemetry
    assert driver.capabilities.telemetry_liveness_checked
