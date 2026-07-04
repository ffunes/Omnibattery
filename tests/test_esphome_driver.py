"""Unit tests for the ESPHome-entity driver (Marstek behind a LilyGo RS485 bridge).

HA is faked with a minimal states/services stub, so no runtime is required.
Entity resolution is tested through the pure ``_match_entities`` helper.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.omnibattery.drivers.esphome import (
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
    ]
    resolved = EsphomeEntityDriver._match_entities(entries)
    # original_name match survives an entity_id rename
    assert resolved["battery_soc"] == "sensor.renamed_by_user"
    # punctuation in the name slugs away
    assert resolved["max_cell_voltage"] == "sensor.marstek_max_cell_voltage"
    assert resolved["force_mode"] == "select.marstek_forcible_charge_discharge"
    assert resolved["set_charge_power"] == "number.marstek_forcible_charge_power"
    # unmapped upstream entities are ignored
    assert "battery_runtime_estimate" not in resolved


def test_match_entities_entity_id_fallback():
    # original_name lost (None): the entity_id suffix still matches.
    entries = [
        ("sensor", None, "sensor.mydevice_battery_state_of_charge"),
        ("select", None, "select.mydevice_forcible_charge_discharge"),
    ]
    resolved = EsphomeEntityDriver._match_entities(entries)
    assert resolved["battery_soc"] == "sensor.mydevice_battery_state_of_charge"
    assert resolved["force_mode"] == "select.mydevice_forcible_charge_discharge"


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
        _ENTITIES["force_mode"]: "discharge",
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
        _ENTITIES["force_mode"]: "charge",
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
    assert calls[2] == ("select", "select_option", {"entity_id": _ENTITIES["force_mode"], "option": "charge"})


@pytest.mark.asyncio
async def test_apply_setpoint_discharge_clamps_to_capability():
    driver = _driver({})
    result = await driver.apply_setpoint(-9999, read_back=False)
    assert result.ok and not result.confirmed
    assert result.net_power_w == -2500
    calls = _calls(driver)
    assert calls[0][2]["value"] == 2500
    assert calls[2][2]["option"] == "discharge"


@pytest.mark.asyncio
async def test_apply_setpoint_idle_and_failure():
    driver = _driver({})
    result = await driver.apply_setpoint(0, read_back=False)
    assert result.ok
    assert _calls(driver)[2][2]["option"] == "stop"

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
    assert calls[0] == ("select", "select_option", {"entity_id": _ENTITIES["force_mode"], "option": "charge"})
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
    for d in SENSOR_DEFINITIONS + NUMBER_DEFINITIONS + SELECT_DEFINITIONS + SWITCH_DEFINITIONS:
        assert "register" not in d
        assert d.get("scale", 1) == 1  # HA states are already final values


def test_capabilities_force_mode_keeps_hardware_manual_path():
    driver = _driver({})
    assert driver.capabilities.has_force_mode
    # force_mode select + set_charge_power number exist → the coordinator's
    # needs_software_manual_control stays False (Marstek-style manual entities).
    assert any(d["key"] == "force_mode" for d in driver.select_definitions)
    assert any(d["key"] == "set_charge_power" for d in driver.number_definitions)
    assert driver.capabilities.has_energy_counters
    assert not driver.capabilities.has_alarm_registers
