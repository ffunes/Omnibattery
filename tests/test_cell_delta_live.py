"""Live cell delta vs the top-of-charge cell delta.

``cell_delta`` is a snapshot the balance monitor takes at the top of a full
charge; ``cell_delta_live`` is max minus min right now. Mid-SOC on LFP the two
disagree by design (243 mV stored vs 3 mV live was the report that started
this), so the live one must never borrow a fallback that looks like a reading.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import CELL_DELTA_LIVE_SENSOR_DEFINITIONS
from custom_components.omnibattery.infra.entity_naming import english_entity_id
from custom_components.omnibattery.sensors.balance_sensors import CellDeltaSensor
from custom_components.omnibattery.sensors.calculated_sensors import (
    MarstekVenusCellDeltaLiveSensor,
)

_DEFINITION = CELL_DELTA_LIVE_SENSOR_DEFINITIONS[0]


def _live(data, *, last_update_success=True):
    sensor = object.__new__(MarstekVenusCellDeltaLiveSensor)
    sensor.coordinator = SimpleNamespace(
        data=data, last_update_success=last_update_success
    )
    sensor._vmax_key = _DEFINITION["dependency_keys"]["vmax"]
    sensor._vmin_key = _DEFINITION["dependency_keys"]["vmin"]
    return sensor


def test_live_delta_is_max_minus_min_in_mv():
    # The reported L1 reading: 3.331 / 3.328 V at 80 % SOC.
    sensor = _live({"max_cell_voltage": 3.331, "min_cell_voltage": 3.328})
    assert sensor.available is True
    assert sensor.native_value == 3
    assert sensor.extra_state_attributes is None


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"max_cell_voltage": 3.331},
        {"min_cell_voltage": 3.328},
        {"max_cell_voltage": None, "min_cell_voltage": 3.328},
        {"max_cell_voltage": 3.331, "min_cell_voltage": None},
        {"max_cell_voltage": 0, "min_cell_voltage": 0},
        {"max_cell_voltage": 3.10, "min_cell_voltage": 3.40},
    ],
)
def test_missing_or_impossible_input_is_unavailable_not_zero(data):
    sensor = _live(data)
    assert sensor.available is False
    assert sensor.native_value is None


def test_failed_coordinator_update_is_unavailable():
    sensor = _live(
        {"max_cell_voltage": 3.331, "min_cell_voltage": 3.328},
        last_update_success=False,
    )
    assert sensor.available is False


def test_multi_pack_battery_reports_the_worst_pack():
    # Matches what the balance monitor stores for Venus A/D: the widest single
    # pack, not a spread across independent BMSs.
    sensor = _live({
        "max_cell_voltage": 3.517, "min_cell_voltage": 3.320,
        "max_cell_voltage_pack_1": 3.363, "min_cell_voltage_pack_1": 3.353,
        "max_cell_voltage_pack_2": 3.517, "min_cell_voltage_pack_2": 3.320,
    })
    assert sensor.native_value == 197
    assert sensor.extra_state_attributes == {"pack": 2}


def test_definition_is_a_whole_mv_measurement():
    assert _DEFINITION["key"] == "cell_delta_live"
    assert _DEFINITION["unit"] == "mV"
    assert _DEFINITION["state_class"] == "measurement"
    assert _DEFINITION["precision"] == 0


def test_blueprint_discovery_does_not_pick_up_the_live_sensor():
    # The active-balance blueprint (and every copy users already imported) finds
    # the stored delta by suffix and takes the first match; the live sensor must
    # not be a candidate.
    raw = (
        Path(__file__).resolve().parents[1]
        / "blueprints"
        / "marstek_active_balance_blueprint.yaml"
    ).read_text()
    assert "'^sensor\\\\..*_cell_delta$'" in raw
    pattern = re.compile(r"^sensor\..*_cell_delta$")
    assert pattern.match(english_entity_id("sensor", "L1", "cell_delta"))
    assert not pattern.match(english_entity_id("sensor", "L1", "cell_delta_live"))


# --- top-of-charge sensor attributes ----------------------------------------


def _top(readings):
    sensor = object.__new__(CellDeltaSensor)
    sensor._coordinator = SimpleNamespace(device_key="l1", data={})
    sensor._monitor = SimpleNamespace(
        get_recent_readings=lambda host, limit: list(readings)
    )
    return sensor


def test_top_of_charge_delta_says_when_and_at_what_soc():
    older = {"ts": "2026-09-17T10:40:00Z", "delta_mV": 231, "soc": 100}
    newest = {
        "ts": "2026-09-24T10:55:12Z", "delta_mV": 243, "vmax_V": 3.613,
        "vmin_V": 3.370, "soc": 100, "type": "top_balance_measurement",
        "phase": "top_charge_3_55v",
    }
    attrs = _top([older, newest]).extra_state_attributes
    assert attrs["measured_at"] == "2026-09-24T10:55:12Z"
    assert attrs["soc_at_measurement"] == 100
    # history is unchanged: newest first, entries passed through as stored
    assert attrs["history"] == [newest, older]


def test_top_of_charge_delta_without_history():
    attrs = _top([]).extra_state_attributes
    assert attrs["measured_at"] is None
    assert attrs["soc_at_measurement"] is None
    assert attrs["history"] == []
