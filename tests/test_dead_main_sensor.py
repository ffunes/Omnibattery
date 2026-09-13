"""Dead main sensor: Repairs issue for a grid meter that stopped publishing.

The slow-sensor repair next door is publication-driven, so it only describes a
meter that still speaks. A meter that goes completely silent produces no further
publications, never reaches it, and the loop just holds its last command for as
long as the silence lasts (issue #452). These tests pin the lifecycle of the
issue that makes that visible, including the frozen-on-a-valid-value flavour
that logs nothing at all today.

``_check_main_sensor_liveness`` only touches ``self.hass``, ``self.config_entry``,
``self.consumption_sensor``, ``self.previous_power`` and its own flag, so it is
exercised as an unbound method against a lightweight stand-in.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

import custom_components.omnibattery as omnibattery_init
from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import MAIN_SENSOR_DEAD_S

NOW = omnibattery_init.dt_util.utcnow()


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


def _ctrl(silent_for_s, sensor="sensor.grid_power", previous_power=-1500.0):
    """Controller stand-in exposing only what the liveness check reads."""
    return SimpleNamespace(
        hass=object(),
        config_entry=SimpleNamespace(entry_id="abc123"),
        consumption_sensor=sensor,
        previous_power=previous_power,
        _last_sensor_report_time=(
            None if silent_for_s is None else NOW - timedelta(seconds=silent_for_s)
        ),
        _dead_sensor_issue_created=False,
    )


def _check(ctrl):
    # Bind the helper the check calls on itself, then run the check unbound.
    ctrl._sensor_age_seconds = lambda t, now=None: (
        ChargeDischargeController._sensor_age_seconds(ctrl, t, now)
    )
    ChargeDischargeController._check_main_sensor_liveness(ctrl, NOW)


def test_silent_meter_raises_the_issue(issues):
    ctrl = _ctrl(MAIN_SENSOR_DEAD_S + 60)
    _check(ctrl)

    assert len(issues.created) == 1
    issue_id, kwargs = issues.created[0]
    assert issue_id == "dead_main_sensor_abc123"
    assert kwargs["translation_key"] == "dead_main_sensor"
    # The held command is the whole point of the warning: name it.
    assert kwargs["translation_placeholders"]["power"] == "1500"
    assert kwargs["translation_placeholders"]["sensor"] == "sensor.grid_power"
    assert ctrl._dead_sensor_issue_created is True


def test_a_meter_within_tolerance_raises_nothing(issues):
    _check(_ctrl(MAIN_SENSOR_DEAD_S - 60))
    assert issues.created == []


def test_no_reading_yet_is_a_restart_not_a_fault(issues):
    """_last_sensor_report_time is None until the first successful read."""
    _check(_ctrl(None))
    assert issues.created == []


def test_the_issue_is_raised_once_per_episode(issues):
    ctrl = _ctrl(MAIN_SENSOR_DEAD_S + 60)
    for _ in range(5):
        _check(ctrl)
    assert len(issues.created) == 1


def test_a_recovered_meter_clears_the_issue(issues):
    ctrl = _ctrl(MAIN_SENSOR_DEAD_S + 60)
    _check(ctrl)
    # The meter publishes again: age collapses back under the threshold.
    ctrl._last_sensor_report_time = NOW
    _check(ctrl)

    assert issues.deleted == ["dead_main_sensor_abc123"]
    assert ctrl._dead_sensor_issue_created is False

    # ...and a second episode can raise it again.
    ctrl._last_sensor_report_time = NOW - timedelta(seconds=MAIN_SENSOR_DEAD_S + 60)
    _check(ctrl)
    assert len(issues.created) == 2


def test_no_configured_meter_is_not_a_fault(issues):
    _check(_ctrl(MAIN_SENSOR_DEAD_S + 60, sensor=None))
    assert issues.created == []
