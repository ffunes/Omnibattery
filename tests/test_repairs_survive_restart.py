"""A Repair raised before a restart is cleared by the first healthy check after it.

The Repairs are persistent, so one raised in an earlier run is restored as active
while every in-memory flag that says "I raised it" starts empty. Each of these
used that flag to decide whether a delete was needed, so after a restart a
battery that delivered again, a sensor that came back, or a phase that recovered
kept its warning for good.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController
import custom_components.omnibattery as omnibattery_init
from custom_components.omnibattery.tracking import non_responsive_tracker as nrt
from custom_components.omnibattery.tracking.non_responsive_tracker import (
    NON_DELIVERY_REPAIR_AFTER_S,
    NonResponsiveTracker,
)
from tests.conftest import FakeCoordinator


class _Repairs:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.IssueSeverity = SimpleNamespace(WARNING="warning")

    def async_create_issue(self, _hass, _domain, issue_id, **_kwargs):
        self.created.append(issue_id)

    def async_delete_issue(self, _hass, _domain, issue_id):
        self.deleted.append(issue_id)


@pytest.fixture(name="repairs")
def _repairs(monkeypatch):
    recorder = _Repairs()
    monkeypatch.setattr(nrt, "ir", recorder)
    monkeypatch.setattr(omnibattery_init, "ir", recorder)
    return recorder


# ---------------------------------------------------- battery not delivering
def test_a_restart_clears_the_repair_of_a_battery_that_never_failed_this_run(repairs):
    """The tracker is empty after a restart: no info, so no one ever deleted it."""
    battery = FakeCoordinator(name="BAT1")
    tracker = NonResponsiveTracker()

    tracker.update_repairs(object(), "entry", [battery])

    assert repairs.deleted == [f"battery_not_delivering_entry_{battery.device_key}"]
    assert repairs.created == []


def test_the_restart_cleanup_runs_once_per_battery(repairs):
    battery = FakeCoordinator(name="BAT1")
    tracker = NonResponsiveTracker()

    for _ in range(3):
        tracker.update_repairs(object(), "entry", [battery])

    assert len(repairs.deleted) == 1


def test_a_battery_failing_again_is_not_cleared_but_judged_on_its_own_spell(repairs):
    battery = FakeCoordinator(name="BAT1")
    tracker = NonResponsiveTracker()
    tracker.record_non_delivery(battery, 1000, 0)

    tracker.update_repairs(object(), "entry", [battery])
    # Unknown this run, not stuck yet: the persisted issue is removed once.
    assert len(repairs.deleted) == 1
    assert repairs.created == []

    tracker.batteries[battery]["degraded_since"] = dt_util.utcnow() - timedelta(
        seconds=NON_DELIVERY_REPAIR_AFTER_S + 1
    )
    tracker.update_repairs(object(), "entry", [battery])
    assert len(repairs.created) == 1


def test_a_stuck_battery_keeps_its_repair_without_recreating_it_each_cycle(repairs):
    battery = FakeCoordinator(name="BAT1")
    tracker = NonResponsiveTracker()
    tracker.record_non_delivery(battery, 1000, 0)
    tracker.batteries[battery]["degraded_since"] = dt_util.utcnow() - timedelta(
        seconds=NON_DELIVERY_REPAIR_AFTER_S + 1
    )

    for _ in range(3):
        tracker.update_repairs(object(), "entry", [battery])

    assert len(repairs.created) == 1


def test_update_repairs_without_the_coordinator_list_still_works(repairs):
    tracker = NonResponsiveTracker()
    tracker.update_repairs(object(), "entry")
    assert repairs.deleted == [] and repairs.created == []


# ---------------------------------------------------- configured sensor missing
def _ctrl(existing: set[str]) -> SimpleNamespace:
    hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda eid: object() if eid in existing else None)
    )
    return SimpleNamespace(
        hass=hass,
        config_entry=SimpleNamespace(
            entry_id="abc123", data={"consumption_sensor": "sensor.house"}
        ),
        _missing_sensors_since=None,
        _missing_sensors_reported=None,
        _missing_sensors_issue_cleared=False,
    )


def test_a_restart_clears_a_missing_sensor_repair_once_the_sensor_is_back(repairs):
    ctrl = _ctrl({"sensor.house"})

    ChargeDischargeController._check_missing_configured_sensors(ctrl)
    ChargeDischargeController._check_missing_configured_sensors(ctrl)

    assert repairs.deleted == ["configured_sensor_missing_abc123"]


# ---------------------------------------------------- phase sensor degraded
def test_a_restart_clears_a_degraded_phase_repair_once_the_phase_recovered(repairs, monkeypatch):
    from custom_components.omnibattery.control import phase_power_limit as module

    monkeypatch.setattr(module, "ir", repairs)
    limiter = SimpleNamespace(
        config_entry=SimpleNamespace(entry_id="entry"),
        hass=object(),
        enabled=True,
        _degraded_since={},
        _snapshots={},
        _degraded_warning_phases=set(),
        _degraded_warning_cleared=set(),
    )

    module.PhasePowerLimiter.update_degraded_warning(limiter)
    first = len(repairs.deleted)
    module.PhasePowerLimiter.update_degraded_warning(limiter)

    assert first == len(module.PHASE_VALUES)
    assert len(repairs.deleted) == first
