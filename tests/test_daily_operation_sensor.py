"""HA-free contract tests for the daily operation timeline entity."""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from custom_components.omnibattery import diagnostics
from custom_components.omnibattery.sensor import DailyOperationTimelineSensor


class _TimelineManager:
    def __init__(self, snapshot: dict) -> None:
        self._snapshot = snapshot
        self.store = {"raw": ["must not be exported"] * 96}

    def snapshot(self) -> dict:
        return self._snapshot


def _snapshot() -> dict:
    solar_actual = [None] * 96
    solar_actual[0] = 0.12
    solar_actual[1] = float("nan")
    consumption_actual = [None] * 96
    consumption_actual[1] = 0.04
    coverage = [None] * 96
    coverage[0] = 900
    coverage[1] = 300
    solar_coverage = [None] * 96
    solar_coverage[0] = 300
    consumption_coverage = [None] * 96
    consumption_coverage[0] = 900

    actual_action = [0] * 96
    actual_action[0] = 1
    actual_action[2] = 4
    planned_action = [0] * 96
    planned_action[1] = 3
    planned_action[2] = 7

    return {
        "schema_version": 1,
        "local_date": "2026-08-22",
        "timezone": "Europe/Madrid",
        "generated_at": "2026-08-22T14:07:31+02:00",
        "plan_evaluated_at": "2026-08-22T13:55:00+02:00",
        "current_index": 56,
        "current_progress": 0.5,
        "mode": "dynamic_pricing",
        "stale": False,
        "series": {
            "solar_actual_kwh": solar_actual,
            "consumption_actual_kwh": consumption_actual,
            "actual_coverage_s": coverage,
            "solar_actual_coverage_s": solar_coverage,
            "consumption_actual_coverage_s": consumption_coverage,
            "solar_forecast_kwh": [0.2] * 96,
            "consumption_forecast_kwh": [0.1] * 96,
        },
        "operations": {
            "actual_action_mask": actual_action,
            "planned_action_mask": planned_action,
            "actual_source": ["runtime_command"] + [None] * 95,
            "planned_source": ["projection"] * 96,
            "actual_context_mask": [0] * 96,
            "planned_context_mask": [0] * 96,
            "grid_charge_decision": ["unknown"] * 96,
            "delay_until": [None] * 96,
            "charge_power_w": [0] * 96,
            "discharge_power_w": [0] * 96,
            "charge_to_battery_kwh": [0.1] * 96,
            "actual_charge_to_battery_kwh": [0.05] * 96,
            "planned_charge_to_battery_kwh": [0.1] * 96,
            "discharge_from_battery_kwh": [0.04] * 96,
            "actual_discharge_from_battery_kwh": [0.03] * 96,
            "planned_discharge_from_battery_kwh": [0.04] * 96,
            "soc_pct": [50.5] * 96,
            "actual_soc_pct": [49.5] * 96,
            "planned_soc_pct": [51.5] * 96,
            "observed_seconds_by_action": {"solar_charge": 900},
            "observed_seconds_by_action_by_interval": [
                {"solar_charge": 420},
                {},
            ],
        },
        "sources": {
            "solar_actual": "external_plus_mppt",
            "solar_forecast": "learned_profile",
            "solar_forecast_mature": True,
            "consumption_actual": "derived_home",
            "consumption_forecast": "profile",
            "consumption_forecast_mature": False,
            "operation_plan": "chronological",
        },
        "freshness": {"age_s": 12.0, "state": "fresh"},
        "restoration": {"status": "restored", "date": "2026-08-22"},
        "setpoint": {
            "estimated_completion_at": "2026-08-22T17:00:00+02:00",
            "target_soc": 80,
        },
        "delay": {
            "enabled": False,
            "status": "delayed",
            "estimated_unlock_time": "2026-08-22T16:15:00+02:00",
        },
        "extended_horizon": {
            "start": "2026-08-23T00:00:00+02:00",
            "end": "2026-08-23T12:00:00+02:00",
            "interval_minutes": 15,
            "interval_count": 48,
            "duration_s": [900] * 48,
            "dst_skipped": [False] * 8 + [True] * 4 + [False] * 36,
            "dst_repeated": [False] * 48,
        },
        "extended_projection": [
            {
                "extension_index": 0,
                "start": "2026-08-23T00:00:00+02:00",
                "end": "2026-08-23T00:15:00+02:00",
                "solar_kwh": 0.03,
                "action_mask": 4,
                "soc_end_pct": 47.25,
            }
        ],
    }


@pytest.mark.parametrize("attribute", ["daily_operation_timeline", "_daily_operation_timeline"])
def test_timeline_entity_has_stable_identity_and_local_date(attribute: str):
    controller = SimpleNamespace(**{attribute: _TimelineManager(_snapshot())})
    sensor = DailyOperationTimelineSensor(controller)

    assert sensor.entity_id == "sensor.omnibattery_daily_operation_timeline"
    assert sensor._attr_unique_id == "marstek_venus_system_daily_operation_timeline"
    assert sensor._attr_device_class.value == "date"
    assert sensor.native_value == date(2026, 8, 22)


def test_timeline_native_value_reads_manager_date_without_building_snapshot():
    class _O1Manager:
        local_date = date(2026, 8, 22)

        def snapshot(self):
            raise AssertionError("native_value must not build the timeline DTO")

    sensor = DailyOperationTimelineSensor(
        SimpleNamespace(daily_operation_timeline=_O1Manager())
    )

    assert sensor._attr_should_poll is False
    assert sensor.native_value == date(2026, 8, 22)


def test_timeline_entity_publishes_fixed_96_element_json_safe_dto():
    sensor = DailyOperationTimelineSensor(
        SimpleNamespace(daily_operation_timeline=_TimelineManager(_snapshot()))
    )
    attrs = sensor.extra_state_attributes

    assert attrs["schema_version"] == 1
    assert attrs["interval_count"] == 96
    assert attrs["timezone"] == "Europe/Madrid"
    for section in (attrs["series"], attrs["operations"]):
        for key, value in section.items():
            if isinstance(value, list):
                assert len(value) == 96, key
    for key in ("dst_skipped", "dst_repeated", "dst_flags"):
        assert len(attrs[key]) == 96

    assert attrs["series"]["solar_actual_kwh"][1] is None
    assert attrs["series"]["solar_actual_coverage_s"][0] == 300
    assert attrs["series"]["consumption_actual_coverage_s"][0] == 900
    assert attrs["operations"]["actual_source"][0] == "runtime_command"
    assert attrs["operations"]["planned_source"][0] == "projection"
    assert attrs["operations"]["observed_seconds_by_action_by_interval"][0] == {
        "solar_charge": 420.0
    }
    assert len(attrs["operations"]["observed_seconds_by_action_by_interval"]) == 96
    assert attrs["operations"]["actual_soc_pct"][0] == 49.5
    assert attrs["operations"]["planned_soc_pct"][0] == 51.5
    assert attrs["operations"]["charge_to_battery_kwh"][0] == 0.1
    assert attrs["operations"]["actual_discharge_from_battery_kwh"][0] == 0.03
    assert attrs["operations"]["discharge_from_battery_kwh"][0] == 0.04
    assert attrs["delay"]["enabled"] is False
    assert attrs["extended_horizon"]["interval_count"] == 48
    assert len(attrs["extended_horizon"]["duration_s"]) == 48
    assert attrs["extended_horizon"]["dst_skipped"][8:12] == [True] * 4
    assert attrs["extended_projection"][0]["extension_index"] == 0
    assert attrs["extended_projection"][0]["solar_kwh"] == pytest.approx(0.03)
    assert attrs["extended_projection"][0]["soc_end_pct"] == pytest.approx(47.25)
    json.dumps(attrs, allow_nan=False)


def test_timeline_entity_excludes_heavy_sections_from_recorder():
    sensor = DailyOperationTimelineSensor(SimpleNamespace())

    assert {"series", "operations"}.issubset(sensor._unrecorded_attributes)


def test_timeline_entity_tolerates_missing_manager_and_events():
    sensor = DailyOperationTimelineSensor(SimpleNamespace())

    assert sensor.native_value is None
    assert sensor.extra_state_attributes["interval_count"] == 96
    sensor._handle_timeline_update({"event": "before_manager"})


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def test_diagnostics_are_bounded_and_count_timeline_without_store_or_arrays():
    controller = SimpleNamespace(
        daily_operation_timeline=_TimelineManager(_snapshot())
    )

    result = diagnostics._daily_operation_timeline_summary(controller)

    assert result["local_date"] == "2026-08-22"
    assert result["mode"] == "dynamic_pricing"
    assert result["schema_version"] == 1
    assert result["current_index"] == 56
    assert result["sources"]["solar_forecast"] == "learned_profile"
    assert result["sources"]["solar_forecast_mature"] is True
    assert result["counts"]["partial_cells"] == 1
    assert result["counts"]["by_action"]["solar_charge"] == 3
    assert result["counts"]["double_overlaps"] == 1
    assert result["counts"]["triple_overlaps"] == 1
    assert result["setpoint"]["target_soc"] == 80.0
    assert result["delay"]["estimated_unlock_time"].startswith("2026-08-22T16:15")
    assert "series" not in result
    assert "operations" not in result
    assert not _contains_key(result, "store")
    json.dumps(result, allow_nan=False)


def test_diagnostics_tolerate_missing_manager():
    result = diagnostics._daily_operation_timeline_summary(SimpleNamespace())

    assert result["available"] is False
    assert result["counts"]["unknown_cells"] == 96
    json.dumps(result, allow_nan=False)
