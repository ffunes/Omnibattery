"""A battery stuck non-delivering must reach the user, not only the log (#452).

The detection was already right -- "command accepted but power not delivered" --
but it only ever produced a warning line, so a battery that never recovered sat
dead for a day with nothing a user would see. The Repair is timed from the start
of an unbroken non-delivery spell: an ordinary episode (excluded, cooled down,
delivering again on retry) must never raise one, and a commanded direction flip,
which resets the episode bookkeeping without proving anything about delivery,
must not restart the clock either.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.tracking import non_responsive_tracker as nrt
from custom_components.omnibattery.tracking.non_responsive_tracker import (
    NON_DELIVERY_REPAIR_AFTER_S,
    NonResponsiveTracker,
)
from tests.conftest import FakeCoordinator


class _Repairs:
    """Record what the tracker asks the issue registry to do."""

    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []
        self.deleted: list[str] = []

    def async_create_issue(self, _hass, _domain, issue_id, **kwargs):
        self.created.append((issue_id, kwargs))

    def async_delete_issue(self, _hass, _domain, issue_id):
        self.deleted.append(issue_id)

    class IssueSeverity:
        WARNING = "warning"


@pytest.fixture(name="repairs")
def _repairs(monkeypatch):
    recorder = _Repairs()
    monkeypatch.setattr(nrt, "ir", recorder)
    return recorder


def _excluded_tracker(coord):
    """Drive a battery all the way to exclusion (grace round included)."""
    tracker = NonResponsiveTracker(fail_threshold=3)
    for _ in range(6):
        tracker.record_non_delivery(coord, 1625, 5)
    assert tracker.is_excluded(coord) is True
    return tracker


def _backdate(tracker, coord, seconds):
    info = tracker.batteries[coord]
    info["degraded_since"] = dt_util.utcnow() - timedelta(seconds=seconds)


def test_one_episode_raises_nothing(repairs):
    coord = FakeCoordinator(name="BAT1")
    tracker = _excluded_tracker(coord)

    tracker.update_repairs(object(), "entry1")

    assert repairs.created == []


def test_a_spell_past_the_threshold_raises_a_repair(repairs):
    coord = FakeCoordinator(name="BAT1")
    tracker = _excluded_tracker(coord)
    _backdate(tracker, coord, NON_DELIVERY_REPAIR_AFTER_S + 60)

    tracker.update_repairs(object(), "entry1")

    assert len(repairs.created) == 1
    issue_id, kwargs = repairs.created[0]
    assert issue_id == f"battery_not_delivering_entry1_{coord.device_key}"
    assert kwargs["translation_key"] == "battery_not_delivering"
    assert kwargs["translation_placeholders"]["battery"] == "BAT1"
    assert kwargs["translation_placeholders"]["reason"] == "non_delivery"

    # Idempotent: a second cycle must not re-raise it.
    tracker.update_repairs(object(), "entry1")
    assert len(repairs.created) == 1


def test_the_repair_text_fits_every_battery_in_every_language(repairs):
    """Each language fills in what the tracker passes and names no protocol.

    The Repair is raised for every brand, over Modbus, HTTP or MQTT alike, so
    its advice speaks of the battery's connection rather than of one protocol.
    """
    coord = FakeCoordinator(name="BAT1")
    tracker = _excluded_tracker(coord)
    _backdate(tracker, coord, NON_DELIVERY_REPAIR_AFTER_S + 60)
    tracker.update_repairs(object(), "entry1")
    passed = set(repairs.created[0][1]["translation_placeholders"])

    component = Path("custom_components/omnibattery")
    for path in [component / "strings.json"] + sorted(
        (component / "translations").glob("*.json")
    ):
        text = json.loads(path.read_text(encoding="utf-8"))
        description = text["issues"]["battery_not_delivering"]["description"]

        assert set(re.findall(r"\{(\w+)\}", description)) == passed, path.name
        for protocol in ("Modbus", "HTTP", "MQTT"):
            assert protocol not in description, (path.name, protocol)


def test_delivering_again_resolves_the_repair(repairs):
    coord = FakeCoordinator(name="BAT1")
    tracker = _excluded_tracker(coord)
    _backdate(tracker, coord, NON_DELIVERY_REPAIR_AFTER_S + 60)
    tracker.update_repairs(object(), "entry1")

    tracker.clear(coord)
    tracker.update_repairs(object(), "entry1")

    assert repairs.deleted == [f"battery_not_delivering_entry1_{coord.device_key}"]


def test_a_direction_flip_does_not_restart_the_spell(repairs):
    """clear(delivering=False) resets the episode, not the fault clock.

    PD flips the commanded direction on its own; if that reset the spell, the
    reported 26-hour outage would never have raised anything.
    """
    coord = FakeCoordinator(name="BAT1")
    tracker = _excluded_tracker(coord)
    _backdate(tracker, coord, NON_DELIVERY_REPAIR_AFTER_S + 60)

    tracker.clear(coord, delivering=False)
    tracker.update_repairs(object(), "entry1")

    assert len(repairs.created) == 1


def test_a_direction_flip_keeps_the_reason_of_the_spell(repairs):
    """The open spell keeps the reason it was excluded for, so the full-battery
    sweep can still tell a charge-side spell apart and the Repair names it."""
    coord = FakeCoordinator(name="BAT1")
    tracker = NonResponsiveTracker(fail_threshold=3)
    for _ in range(6):
        tracker.record_non_delivery(coord, 2400, 235, reason="charge_non_delivery")
    _backdate(tracker, coord, NON_DELIVERY_REPAIR_AFTER_S + 60)

    tracker.clear(coord, delivering=False)
    tracker.update_repairs(object(), "entry1")

    assert tracker.last_reason(coord) == "charge_non_delivery"
    assert tracker.batteries[coord]["fail_count"] == 0
    assert repairs.created[0][1]["translation_placeholders"]["reason"] == (
        "charge_non_delivery"
    )


def test_a_direction_flip_without_a_spell_forgets_the_reason():
    """Fails that never led to an exclusion leave no spell open, so a flip
    resets them completely, reason included."""
    coord = FakeCoordinator(name="BAT1")
    tracker = NonResponsiveTracker(fail_threshold=3)
    tracker.record_non_delivery(coord, 2400, 235, reason="charge_non_delivery")
    tracker.record_non_delivery(coord, 2400, 235, reason="charge_non_delivery")

    tracker.clear(coord, delivering=False)

    assert tracker.last_reason(coord) == ""
    assert tracker.batteries[coord]["fail_count"] == 0
