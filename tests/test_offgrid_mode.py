"""Regression tests for the software-only off-grid meter selector."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.config_flow import (
    MarstekVenusConfigFlow,
    _validate_offgrid_power_sensor,
)
from custom_components.omnibattery.const import (
    CONF_METER_INVERTED,
    CONF_OFFGRID_METER_INVERTED,
    CONF_OFFGRID_MODE_ENABLED,
    CONF_OFFGRID_POWER_SENSOR,
)
from custom_components.omnibattery.sensors.aggregate_sensors import (
    MarstekVenusAggregateSensor,
)
from custom_components.omnibattery.switch import OffgridModeSwitch


def _controller(*, enabled: bool = False):
    ctrl = object.__new__(ChargeDischargeController)
    ctrl.primary_consumption_sensor = "sensor.grid"
    ctrl.offgrid_power_sensor = "sensor.backup_load"
    ctrl.meter_inverted = False
    ctrl.offgrid_meter_inverted = False
    ctrl.offgrid_mode_enabled = enabled
    return ctrl


def _state(value: str, unit: str = "W") -> SimpleNamespace:
    return SimpleNamespace(state=value, attributes={"unit_of_measurement": unit})


def test_effective_sensor_changes_without_replacing_primary_sensor():
    ctrl = _controller()

    assert ctrl.consumption_sensor == "sensor.grid"
    ctrl.offgrid_mode_enabled = True
    assert ctrl.consumption_sensor == "sensor.backup_load"
    assert ctrl.primary_consumption_sensor == "sensor.grid"
    assert ctrl.consumption_sensor_ids == ["sensor.grid", "sensor.backup_load"]


def test_each_meter_uses_its_own_sign_setting():
    ctrl = _controller()
    ctrl.meter_inverted = False
    ctrl.offgrid_meter_inverted = True
    state = _state("0.65", "kW")

    assert ctrl._apply_meter_transform(state) == 650
    ctrl.offgrid_mode_enabled = True
    assert ctrl._apply_meter_transform(state) == -650


def test_source_change_resets_control_and_statistics_sample_anchors():
    ctrl = _controller()
    ctrl._grid_filter_ema = 123.0
    ctrl._last_sensor_report_time = object()
    ctrl._last_sensor_cadence_time = object()
    ctrl._last_control_sample_value = 123.0
    ctrl._control_sample_is_new = False
    ctrl._stale_cycles = 4
    ctrl._consumption_sensor_issue = "sensor.grid"
    tracker = SimpleNamespace(
        _daily_home_last_time=1.0,
        _daily_home_last_power_kw=0.1,
        _daily_grid_last_time=1.0,
        _daily_grid_last_power_kw=0.2,
    )
    hourly = SimpleNamespace(_last_sample_monotonic=1.0, _last_grid_w=200.0)
    ctrl._consumption_tracker = tracker
    ctrl._hourly_balance_mgr = hourly

    ctrl.set_offgrid_mode(True)

    assert ctrl.consumption_sensor == "sensor.backup_load"
    assert ctrl._grid_filter_ema is None
    assert ctrl._last_sensor_report_time is None
    assert ctrl._last_sensor_cadence_time is None
    assert ctrl._last_control_sample_value is None
    assert ctrl._control_sample_is_new is True
    assert tracker._daily_home_last_time is None
    assert tracker._daily_grid_last_time is None
    assert hourly._last_sample_monotonic is None


def test_offgrid_sensor_validation_requires_distinct_available_power_sensor():
    hass = SimpleNamespace(
        states=SimpleNamespace(
            get={
                "sensor.backup_load": _state("450"),
                "sensor.energy": _state("2.3", "kWh"),
            }.get
        )
    )

    assert _validate_offgrid_power_sensor(
        hass,
        {
            "consumption_sensor": "sensor.grid",
            CONF_OFFGRID_POWER_SENSOR: "sensor.backup_load",
        },
    ) == {}
    assert _validate_offgrid_power_sensor(
        hass,
        {
            "consumption_sensor": "sensor.grid",
            CONF_OFFGRID_POWER_SENSOR: "sensor.grid",
        },
    ) == {CONF_OFFGRID_POWER_SENSOR: "offgrid_sensor_must_differ"}
    assert _validate_offgrid_power_sensor(
        hass,
        {
            "consumption_sensor": "sensor.grid",
            CONF_OFFGRID_POWER_SENSOR: "sensor.missing",
        },
    ) == {CONF_OFFGRID_POWER_SENSOR: "offgrid_sensor_not_found"}
    assert _validate_offgrid_power_sensor(
        hass,
        {
            "consumption_sensor": "sensor.grid",
            CONF_OFFGRID_POWER_SENSOR: "sensor.energy",
        },
    ) == {CONF_OFFGRID_POWER_SENSOR: "offgrid_sensor_invalid_unit"}


async def test_initial_flow_stores_the_offgrid_sign_independently():
    flow = MarstekVenusConfigFlow()
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(
            get={"sensor.backup_load": _state("-450")}.get
        ),
        config_entries=SimpleNamespace(async_entries=lambda _domain: []),
    )

    await flow.async_step_user(
        {
            "consumption_sensor": "sensor.grid",
            "max_contracted_power": 7000,
            CONF_METER_INVERTED: False,
            CONF_OFFGRID_POWER_SENSOR: "sensor.backup_load",
            CONF_OFFGRID_METER_INVERTED: True,
        }
    )

    assert flow.config_data[CONF_METER_INVERTED] is False
    assert flow.config_data[CONF_OFFGRID_METER_INVERTED] is True


def test_home_consumption_statistic_reads_the_selected_offgrid_meter():
    states = {
        "sensor.grid": _state("100"),
        "sensor.backup_load": _state("-650"),
    }
    aggregate = SimpleNamespace(
        entry=SimpleNamespace(
            data={
                "consumption_sensor": "sensor.grid",
                CONF_OFFGRID_POWER_SENSOR: "sensor.backup_load",
                CONF_METER_INVERTED: False,
                CONF_OFFGRID_METER_INVERTED: True,
                CONF_OFFGRID_MODE_ENABLED: True,
            }
        ),
        hass=SimpleNamespace(states=SimpleNamespace(get=states.get)),
        coordinators=[],
        definition={"precision": 0},
        _home_consumption_quality="measured",
        _last_valid_home_consumption=None,
    )
    aggregate._read_power_w = lambda entity_id: float(states[entity_id].state)

    value = MarstekVenusAggregateSensor._calculate_home_consumption(aggregate)

    assert value == 650


def test_dashboard_exposes_the_offgrid_mode_switch():
    panel = Path(
        "custom_components/omnibattery/frontend/marstek-panel.js"
    ).read_text(encoding="utf-8")

    assert 'key: "offgrid_mode", domain: "switch"' in panel
    assert 'tk: "secOffgridMeter"' in panel


def test_switch_only_persists_and_selects_the_meter_source():
    entry = SimpleNamespace(
        data={
            "consumption_sensor": "sensor.grid",
            CONF_OFFGRID_POWER_SENSOR: "sensor.backup_load",
            "unrelated": "kept",
        }
    )
    ctrl = _controller()
    ctrl._control_lock = asyncio.Lock()
    cycles: list[None] = []
    ctrl.schedule_control_cycle = lambda: cycles.append(None)

    def update_entry(target, *, data):
        target.data = data

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=update_entry)
    )
    switch = OffgridModeSwitch(hass, entry, ctrl)
    switch.async_write_ha_state = lambda: None

    asyncio.run(switch.async_turn_on())

    assert entry.data[CONF_OFFGRID_MODE_ENABLED] is True
    assert entry.data["unrelated"] == "kept"
    assert ctrl.consumption_sensor == "sensor.backup_load"
    assert cycles == [None]
