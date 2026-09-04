"""Solar-forecast health: Repairs issue for a configured but dead forecast sensor.

Every consumer degrades quietly on its own (charge delay unlocks, grid-charge
decisions switch to conservative mode, remaining-solar reads 0 kWh), so a broken
forecast sensor costs money without surfacing anywhere. These tests pin the
lifecycle of the issue that makes it visible.

``_check_solar_forecast_health`` only touches ``self.hass``, ``self.config_entry``,
``self.solar_forecast_sensor`` and its own counters, so it is exercised as an
unbound method against a lightweight stand-in rather than a real controller.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import custom_components.omnibattery as omnibattery_init
from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import FORECAST_DATA_ISSUE_DELAY_S


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


def _ctrl(
    forecast_state,
    sensor="sensor.solar_forecast_today",
    remaining_sensor=None,
):
    """Controller stand-in exposing only what the health check reads."""
    state = None if forecast_state is None else SimpleNamespace(state=forecast_state)
    return SimpleNamespace(
        hass=SimpleNamespace(states=SimpleNamespace(get=lambda _eid: state)),
        config_entry=SimpleNamespace(entry_id="abc123"),
        solar_forecast_sensor=sensor,
        solar_forecast_remaining_sensor=remaining_sensor,
        _solar_forecast_bad_since=None,
        _solar_forecast_issue_created=False,
        _solar_forecast_issue_cleared=False,
    )


def _check(ctrl):
    ChargeDischargeController._check_solar_forecast_health(ctrl)


def _check_migration(ctrl):
    ChargeDischargeController._check_solar_forecast_migration(ctrl)


def test_readable_forecast_raises_nothing(issues, clock):
    ctrl = _ctrl("18.4")

    _check(ctrl)

    assert issues.created == []
    assert ctrl._solar_forecast_bad_since is None


@pytest.mark.parametrize("bad_state", ["unavailable", "unknown", "not-a-number", None])
def test_sustained_unreadable_forecast_creates_one_issue(issues, clock, bad_state):
    ctrl = _ctrl(bad_state)

    _check(ctrl)  # first failure only starts the clock
    assert issues.created == []

    clock[0] += FORECAST_DATA_ISSUE_DELAY_S - 1
    _check(ctrl)
    assert issues.created == []

    clock[0] += 2
    _check(ctrl)
    clock[0] += FORECAST_DATA_ISSUE_DELAY_S
    _check(ctrl)

    assert len(issues.created) == 1
    issue_id, kwargs = issues.created[0]
    assert issue_id == "solar_forecast_unusable_abc123"
    assert kwargs["translation_key"] == "solar_forecast_unusable"
    assert kwargs["translation_placeholders"]["sensor"] == "sensor.solar_forecast_today"


def test_recovery_clears_the_issue(issues, clock):
    ctrl = _ctrl("unavailable")
    _check(ctrl)
    clock[0] += FORECAST_DATA_ISSUE_DELAY_S
    _check(ctrl)
    assert len(issues.created) == 1

    ctrl.hass.states.get = lambda _eid: SimpleNamespace(state="12.0")
    _check(ctrl)
    _check(ctrl)

    assert issues.deleted == ["solar_forecast_unusable_abc123"]
    assert ctrl._solar_forecast_issue_created is False
    assert ctrl._solar_forecast_bad_since is None


def test_unconfigured_sensor_is_not_a_defect(issues, clock):
    # Leaving the forecast sensor unset merely disables the features that use it.
    ctrl = _ctrl(None, sensor=None)

    _check(ctrl)
    clock[0] += FORECAST_DATA_ISSUE_DELAY_S * 2
    _check(ctrl)

    assert issues.created == []
    # A persistent issue from a run where the sensor WAS configured still clears.
    assert issues.deleted == ["solar_forecast_unusable_abc123"]


def test_legacy_forecast_creates_one_migration_repair(issues):
    ctrl = _ctrl("18.4")

    _check_migration(ctrl)
    _check_migration(ctrl)

    assert len(issues.created) == 1
    issue_id, kwargs = issues.created[0]
    assert issue_id == "solar_forecast_remaining_recommended_abc123"
    assert kwargs["translation_key"] == "solar_forecast_remaining_recommended"
    assert kwargs["translation_placeholders"] == {
        "sensor": "sensor.solar_forecast_today"
    }
    assert kwargs["is_fixable"] is False
    assert kwargs["is_persistent"] is True


def test_migration_repair_clears_when_remaining_forecast_is_configured(issues):
    ctrl = _ctrl("18.4")
    _check_migration(ctrl)

    ctrl.solar_forecast_remaining_sensor = "sensor.solar_forecast_remaining"
    _check_migration(ctrl)

    assert issues.deleted == ["solar_forecast_remaining_recommended_abc123"]
    assert ctrl._solar_forecast_migration_issue_created is False


def test_migration_repair_clears_when_legacy_forecast_is_removed(issues):
    ctrl = _ctrl("18.4")
    _check_migration(ctrl)

    ctrl.solar_forecast_sensor = None
    _check_migration(ctrl)

    assert issues.deleted == ["solar_forecast_remaining_recommended_abc123"]
