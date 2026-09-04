"""Contract tests for the extracted Marstek active-balance blueprint."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.util import yaml as yaml_util
from jinja2 import Environment

from custom_components.omnibattery import _async_migrate_legacy_active_balance
from custom_components.omnibattery.const.registers_v2 import SELECT_DEFINITIONS
from custom_components.omnibattery.const.registers_v3 import SELECT_DEFINITIONS_V3
from custom_components.omnibattery.const.registers_va import SELECT_DEFINITIONS_VA
from custom_components.omnibattery.const.registers_vd import SELECT_DEFINITIONS_VD
from custom_components.omnibattery.drivers.esphome import SELECT_DEFINITIONS as ESPHOME_SELECT_DEFINITIONS


BLUEPRINT = Path(__file__).parents[1] / "blueprints" / "marstek_active_balance_blueprint.yaml"


class _BlueprintLoader(yaml.SafeLoader):
    """Load Home Assistant's !input tags as ordinary scalar references."""


_BlueprintLoader.add_constructor(
    "!input", lambda loader, node: f"!input {loader.construct_scalar(node)}"
)


def _load_blueprint() -> tuple[dict, str]:
    raw = BLUEPRINT.read_text(encoding="utf-8")
    return yaml.load(raw, Loader=_BlueprintLoader), raw


def _service_actions(node):
    if isinstance(node, dict):
        if "service" in node:
            yield node
        for value in node.values():
            yield from _service_actions(value)
    elif isinstance(node, list):
        for value in node:
            yield from _service_actions(value)


def _event_actions(node):
    if isinstance(node, dict):
        if "event" in node:
            yield node
        for value in node.values():
            yield from _event_actions(value)
    elif isinstance(node, list):
        for value in node:
            yield from _event_actions(value)


def _dict_nodes(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _dict_nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _dict_nodes(value)


def _force_mode_options(definitions: list[dict]) -> dict:
    return next(item["options"] for item in definitions if item["key"] == "force_mode")


def test_all_marstek_force_mode_selects_use_canonical_option_names():
    expected = {"None": 0, "Charge": 1, "Discharge": 2}
    for definitions in (
        SELECT_DEFINITIONS,
        SELECT_DEFINITIONS_V3,
        SELECT_DEFINITIONS_VA,
        SELECT_DEFINITIONS_VD,
        ESPHOME_SELECT_DEFINITIONS,
    ):
        assert _force_mode_options(definitions) == expected


def test_blueprint_has_one_battery_contract_and_plan_defaults():
    blueprint, raw = _load_blueprint()
    assert blueprint["blueprint"]["domain"] == "automation"
    inputs = blueprint["blueprint"]["input"]
    assert {
        "run_request",
        "battery_device",
        "battery_manual_mode_override",
        "battery_soc_override",
        "battery_power_override",
        "max_cell_voltage_override",
        "min_cell_voltage_override",
        "cell_delta_override",
        "force_mode_override",
        "set_charge_power_override",
        "set_discharge_power_override",
        "max_charge_power_override",
        "max_soc_override",
    } <= set(inputs)
    assert inputs["battery_device"]["selector"]["device"]["filter"] == [{
        "integration": "omnibattery",
        "manufacturer": "Marstek",
    }]
    for key in (
        "battery_manual_mode_override",
        "battery_soc_override",
        "battery_power_override",
        "max_cell_voltage_override",
        "min_cell_voltage_override",
        "cell_delta_override",
        "force_mode_override",
        "set_charge_power_override",
        "set_discharge_power_override",
        "max_charge_power_override",
        "max_soc_override",
    ):
        assert inputs[key]["default"] == ""
        assert "text" in inputs[key]["selector"]
    assert inputs["top_zone_voltage_v"]["default"] == 3.49
    assert inputs["charge_stop_voltage_v"]["default"] == 3.60
    assert inputs["final_discharge_voltage_v"]["default"] == 3.48
    assert inputs["target_delta_v"]["default"] == 0.03
    assert inputs["adaptive_min_resume_voltage_v"]["default"] == 3.40
    assert inputs["adaptive_resume_step_v"]["default"] == 0.01
    assert inputs["top_charge_power_w"]["default"] == 95
    assert inputs["discharge_power_w"]["default"] == 200
    assert inputs["measurement_wait_s"]["default"] == 60
    assert inputs["charge_engage_grace_s"]["default"] == 10
    assert inputs["rejection_samples"]["default"] == 3
    assert inputs["soc_maximo_normal"]["default"] == 100
    assert "modbus." not in raw.lower()
    assert "switch.turn_on" in raw
    assert "switch.turn_off" in raw
    assert "device_entities(battery_device)" in raw
    assert "sensor\\\\..*_cell_delta" in raw
    assert "states(cell_delta)" in raw
    assert "Initial delta: {{ initial_delta_text }}" in raw
    assert "unknown\n        {%- endif -%}" in raw
    assert "charging_cutoff_capacity" in raw
    assert "omnibattery_balance_measurement_ready" in raw
    assert "entity_id: !input battery_" not in raw
    assert 'option: "{{ force_mode_idle }}"' not in raw
    assert 'option: "None"' in raw
    assert "state_attr(set_charge_power, 'min')" in raw
    assert "state_attr(max_charge_power, 'min')" not in raw


def test_blueprint_restores_the_emoji_balance_notifications():
    blueprint, raw = _load_blueprint()

    assert blueprint["mode"] == "queued"
    assert "device_attr(battery_device, 'name_by_user')" in raw
    assert 'title: "🔋 Active balancing started - {{ battery_name }}"' in raw
    assert "📊 Initial delta:" in raw
    assert "🎯 Runs until delta" in raw
    assert "🚫 Battery paused from normal control while balancing." in raw
    assert 'title: "{{ cleanup_title }}"' in raw
    assert "✅ Active balancing finished - {{ battery_name }}" in raw
    assert "✅ Stopped by user" in raw
    assert "📊 Delta:" in raw
    assert "(improvement" in raw
    assert "⏱️ Duration:" in raw


def test_blueprint_waits_for_manual_release_without_duplicate_cancel_notice():
    blueprint, raw = _load_blueprint()
    recovery_sequence = blueprint["action"][0]["choose"][1]["sequence"]

    assert not any(
        action.get("service") == "persistent_notification.create"
        for action in _service_actions(recovery_sequence)
    )
    assert raw.count('wait_template: "{{ is_state(battery_manual_mode, \'off\') }}"') == 2
    assert raw.count('timeout: "00:00:15"') >= 6


def test_blueprint_matches_home_assistant_schema():
    """Keep the device selector and optional overrides importable by HA."""
    data = yaml_util.load_yaml_dict(BLUEPRINT)
    BLUEPRINT_SCHEMA(data)


def test_blueprint_writes_setpoints_before_force_mode_and_cleans_up_in_order():
    blueprint, _ = _load_blueprint()
    actions = list(_service_actions(blueprint["action"]))
    assert actions
    assert {action["service"] for action in actions} <= {
        "number.set_value",
        "select.select_option",
        "switch.turn_on",
        "switch.turn_off",
        "persistent_notification.create",
        "input_boolean.turn_off",
    }

    # The initial idle handoff and the final cleanup both stop both directions
    # before selecting the idle force mode.
    setpoint_pairs = [
        (index, action["target"]["entity_id"])
        for index, action in enumerate(actions)
        if action["service"] == "number.set_value"
    ]
    first_idle = next(
        index for index, action in enumerate(actions)
        if action["service"] == "select.select_option"
        and "force_mode_idle" in str(action)
    )
    assert [entity for index, entity in setpoint_pairs if index < first_idle][-2:] == [
        "{{ set_charge_power }}",
        "{{ set_discharge_power }}",
    ]


def test_blueprint_publishes_each_settled_measurement_to_omnibattery():
    blueprint, raw = _load_blueprint()
    events = list(_event_actions(blueprint["action"]))

    assert events == [{
        "event": "omnibattery_balance_measurement_ready",
        "event_data": {
            "device_id": "{{ battery_device }}",
            "phase": "WAIT_MEASURE",
            "measurement_id": "{{ started_at }}:{{ measurement_index }}",
        },
    }]
    assert "measurement_index: 0" in raw


def test_blueprint_measures_confirmed_bms_cutoff_in_the_top_window():
    blueprint, _ = _load_blueprint()
    rejection_branch = next(
        node
        for node in _dict_nodes(blueprint["action"])
        if "rejection_count | int(0) >= rejection_samples"
        in str(node.get("conditions"))
    )

    transition = rejection_branch["sequence"][0]["variables"]
    assert "retry_voltage" in transition
    assert "'WAIT_MEASURE'" in transition["phase"]
    assert "top_zone_voltage_v" in transition["phase"]
    assert "'DISCHARGE'" in transition["phase"]

    phase_template = Environment().from_string(transition["phase"])

    def render_phase(vmax: float) -> str:
        return phase_template.render(
            is_number=lambda value: isinstance(value, (int, float)),
            states=lambda _entity: vmax,
            max_cell_voltage="sensor.max_cell_voltage",
            top_zone_voltage_v=3.49,
        ).strip()

    assert render_phase(3.58) == "WAIT_MEASURE"
    assert render_phase(3.49) == "WAIT_MEASURE"
    assert render_phase(3.48) == "DISCHARGE"


def _migration_hass():
    return SimpleNamespace(
        services=SimpleNamespace(async_call=AsyncMock()),
        config_entries=SimpleNamespace(async_update_entry=Mock()),
    )


def _coordinator(*, hardware_cutoff=True):
    return SimpleNamespace(
        name="Marstek Venus",
        host="192.0.2.10",
        port=502,
        slave_id=1,
        device_key="192.0.2.10_502",
        battery_manual_mode_enabled=False,
        max_soc=80,
        capabilities=SimpleNamespace(hardware_soc_cutoff=hardware_cutoff),
        set_charge_cutoff=AsyncMock(return_value=True),
    )


@pytest.mark.asyncio
async def test_legacy_inactive_state_is_removed_without_hardware_writes(monkeypatch):
    from homeassistant.helpers import entity_registry as er

    registry = SimpleNamespace(
        async_get_entity_id=Mock(return_value="switch.marstek_active_balance_mode"),
        async_remove=Mock(),
    )
    monkeypatch.setattr(er, "async_get", lambda _hass: registry)
    hass = _migration_hass()
    coordinator = _coordinator()
    entry = SimpleNamespace(data={"batteries": [{
        "host": coordinator.host,
        "port": coordinator.port,
        "slave_id": coordinator.slave_id,
        "max_soc": 80,
        "active_balance_mode_enabled": False,
        "active_balance_mode_phase": None,
    }]})
    controller = SimpleNamespace(_set_battery_manual_mode=AsyncMock())

    await _async_migrate_legacy_active_balance(hass, entry, controller, [coordinator])

    controller._set_battery_manual_mode.assert_not_awaited()
    coordinator.set_charge_cutoff.assert_not_awaited()
    registry.async_remove.assert_called_once_with("switch.marstek_active_balance_mode")
    updated = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert not any(key.startswith("active_balance_mode_") for key in updated["batteries"][0])


@pytest.mark.asyncio
async def test_legacy_interrupted_state_restores_soc_inside_manual_handoff(monkeypatch):
    monkeypatch.setattr(
        "custom_components.omnibattery._legacy_active_balance_entity_id",
        lambda _hass, _coordinator: None,
    )
    hass = _migration_hass()
    coordinator = _coordinator()
    entry = SimpleNamespace(data={"batteries": [{
        "host": coordinator.host,
        "port": coordinator.port,
        "slave_id": coordinator.slave_id,
        "max_soc": 80,
        "active_balance_mode_enabled": True,
        "active_balance_mode_started_ts": "2026-08-08T10:00:00+00:00",
        "active_balance_mode_saved_max_soc": 80,
    }]})

    async def set_manual(coord, enabled):
        coord.battery_manual_mode_enabled = enabled

    controller = SimpleNamespace(_set_battery_manual_mode=AsyncMock(side_effect=set_manual))

    await _async_migrate_legacy_active_balance(hass, entry, controller, [coordinator])

    assert controller._set_battery_manual_mode.await_args_list[0].args == (coordinator, True)
    assert controller._set_battery_manual_mode.await_args_list[1].args == (coordinator, False)
    coordinator.set_charge_cutoff.assert_awaited_once_with(80)
    assert coordinator.max_soc == 80
    updated = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert not any(key.startswith("active_balance_mode_") for key in updated["batteries"][0])
    assert updated["batteries"][0]["battery_manual_mode_enabled"] is False


@pytest.mark.asyncio
async def test_legacy_migration_failure_keeps_manual_ownership_and_state(monkeypatch):
    monkeypatch.setattr(
        "custom_components.omnibattery._legacy_active_balance_entity_id",
        lambda _hass, _coordinator: None,
    )
    hass = _migration_hass()
    coordinator = _coordinator()
    entry = SimpleNamespace(data={"batteries": [{
        "host": coordinator.host,
        "port": coordinator.port,
        "slave_id": coordinator.slave_id,
        "max_soc": 80,
        "active_balance_mode_enabled": True,
        "active_balance_mode_saved_max_soc": 80,
    }]})
    controller = SimpleNamespace(
        _set_battery_manual_mode=AsyncMock(side_effect=RuntimeError("write failed"))
    )

    await _async_migrate_legacy_active_balance(hass, entry, controller, [coordinator])

    assert coordinator.battery_manual_mode_enabled is True
    updated = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert updated["batteries"][0]["battery_manual_mode_enabled"] is True
    assert updated["batteries"][0]["active_balance_mode_enabled"] is True
    assert any(
        call.args[:2] == ("persistent_notification", "create")
        for call in hass.services.async_call.await_args_list
    )


@pytest.mark.asyncio
async def test_legacy_migration_also_idles_an_existing_manual_owner(monkeypatch):
    monkeypatch.setattr(
        "custom_components.omnibattery._legacy_active_balance_entity_id",
        lambda _hass, _coordinator: None,
    )
    hass = _migration_hass()
    coordinator = _coordinator()
    coordinator.battery_manual_mode_enabled = True
    entry = SimpleNamespace(data={"batteries": [{
        "host": coordinator.host,
        "port": coordinator.port,
        "slave_id": coordinator.slave_id,
        "max_soc": 80,
        "active_balance_mode_enabled": True,
        "active_balance_mode_saved_max_soc": 80,
        "manual_force_mode": "Charge",
        "manual_set_charge_power": 95,
    }]})
    controller = SimpleNamespace(_set_battery_manual_mode=AsyncMock())

    await _async_migrate_legacy_active_balance(hass, entry, controller, [coordinator])

    controller._set_battery_manual_mode.assert_awaited_once_with(coordinator, True)
    updated = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    battery = updated["batteries"][0]
    assert battery["battery_manual_mode_enabled"] is True
    assert battery["manual_force_mode"] == "None"
    assert battery["manual_set_charge_power"] == 0
