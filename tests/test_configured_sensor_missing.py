"""Configured sensors that no longer exist: preservation + Repairs issue (#419).

Home Assistant's ``ha-form`` deletes a cleared field from ``user_input``, so the
options flow cannot tell "untouched" from "cleared". It can tell one thing: the
stored entity is gone, which is exactly when the entity picker renders an empty
box and commits it. Those submissions no longer clear the setting, and the
Repairs issue is what keeps the preserved reference visible and fixable.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import custom_components.omnibattery as omnibattery_init
from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.config_flow import _restore_unrenderable_sensors
from custom_components.omnibattery.const import (
    CONF_OFFGRID_POWER_SENSOR,
    CONF_SOLAR_FORECAST_REMAINING_SENSOR,
    CONF_SOLAR_PRODUCTION_SENSOR,
    MISSING_SENSOR_ISSUE_DELAY_S,
)


def _hass(existing: set[str]) -> SimpleNamespace:
    return SimpleNamespace(
        states=SimpleNamespace(
            get=lambda eid: SimpleNamespace(state="1.0") if eid in existing else None
        )
    )


# --- options flow ------------------------------------------------------------


def test_missing_stored_sensor_survives_an_empty_submission():
    stored = {
        CONF_SOLAR_PRODUCTION_SENSOR: "sensor.pv",
        CONF_SOLAR_FORECAST_REMAINING_SENSOR: "sensor.remaining",
    }

    restored = _restore_unrenderable_sensors(_hass(set()), {}, stored)

    assert restored[CONF_SOLAR_PRODUCTION_SENSOR] == "sensor.pv"
    assert restored[CONF_SOLAR_FORECAST_REMAINING_SENSOR] == "sensor.remaining"


def test_existing_stored_sensor_can_still_be_cleared():
    stored = {CONF_SOLAR_PRODUCTION_SENSOR: "sensor.pv"}

    restored = _restore_unrenderable_sensors(_hass({"sensor.pv"}), {}, stored)

    assert CONF_SOLAR_PRODUCTION_SENSOR not in restored


def test_submitted_replacement_always_wins():
    stored = {CONF_OFFGRID_POWER_SENSOR: "sensor.gone"}

    restored = _restore_unrenderable_sensors(
        _hass({"sensor.new"}), {CONF_OFFGRID_POWER_SENSOR: "sensor.new"}, stored
    )

    assert restored[CONF_OFFGRID_POWER_SENSOR] == "sensor.new"


# --- Repairs issue -----------------------------------------------------------


class _FakeIssueRegistry:
    def __init__(self):
        self.created: list[tuple] = []
        self.deleted: list[str] = []
        self.IssueSeverity = SimpleNamespace(WARNING="warning")

    def async_create_issue(self, hass, domain, issue_id, **kwargs):
        self.created.append((issue_id, kwargs))

    def async_delete_issue(self, hass, domain, issue_id):
        self.deleted.append(issue_id)


@pytest.fixture
def issues(monkeypatch):
    fake = _FakeIssueRegistry()
    monkeypatch.setattr(omnibattery_init, "ir", fake)
    return fake


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(omnibattery_init.time, "monotonic", lambda: now[0])
    return now


def _ctrl(data: dict, existing: set[str]) -> SimpleNamespace:
    """Controller stand-in exposing only what the check reads."""
    return SimpleNamespace(
        hass=_hass(existing),
        config_entry=SimpleNamespace(entry_id="abc123", data=data),
        _missing_sensors_since=None,
        _missing_sensors_reported=None,
    )


def _check(ctrl):
    ChargeDischargeController._check_missing_configured_sensors(ctrl)


def test_missing_sensor_raises_one_issue_after_the_delay(issues, clock):
    ctrl = _ctrl(
        {"consumption_sensor": "sensor.grid", CONF_SOLAR_PRODUCTION_SENSOR: "sensor.pv"},
        {"sensor.grid"},
    )

    _check(ctrl)
    assert issues.created == []  # first sighting only arms the timer

    clock[0] += MISSING_SENSOR_ISSUE_DELAY_S + 1
    _check(ctrl)
    _check(ctrl)

    assert len(issues.created) == 1
    issue_id, kwargs = issues.created[0]
    assert issue_id == "configured_sensor_missing_abc123"
    assert kwargs["translation_placeholders"] == {"sensors": "sensor.pv"}


def test_short_absence_never_raises(issues, clock):
    ctrl = _ctrl({"consumption_sensor": "sensor.grid"}, set())

    _check(ctrl)
    clock[0] += MISSING_SENSOR_ISSUE_DELAY_S - 1
    _check(ctrl)

    assert issues.created == []


def test_returning_entity_clears_the_issue(issues, clock):
    data = {"consumption_sensor": "sensor.grid"}
    ctrl = _ctrl(data, set())

    _check(ctrl)
    clock[0] += MISSING_SENSOR_ISSUE_DELAY_S + 1
    _check(ctrl)
    assert len(issues.created) == 1

    ctrl.hass = _hass({"sensor.grid"})
    _check(ctrl)
    _check(ctrl)

    assert issues.deleted == ["configured_sensor_missing_abc123"]


def test_all_sensors_present_raises_nothing(issues, clock):
    ctrl = _ctrl(
        {"consumption_sensor": "sensor.grid", CONF_SOLAR_PRODUCTION_SENSOR: "sensor.pv"},
        {"sensor.grid", "sensor.pv"},
    )

    clock[0] += MISSING_SENSOR_ISSUE_DELAY_S + 1
    _check(ctrl)
    _check(ctrl)

    assert issues.created == [] and issues.deleted == []
