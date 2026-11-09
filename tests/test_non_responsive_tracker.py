"""Tests for the wake-grace round in ``NonResponsiveTracker.record_non_delivery``.

Reaching the fail threshold no longer excludes on the spot: the first
threshold-cross returns "wake" and resets the counter instead, so the caller's
wake nudge gets one real PD cycle to prove itself before the battery pays a
5-minute cooldown. Only a second consecutive threshold-cross excludes.
Comm failures (``record_comm_failure``) keep the old immediate-exclude
behaviour -- a wake nudge doesn't fix a comms-level fault.
"""
from __future__ import annotations

import logging

from custom_components.omnibattery.tracking import (
    non_responsive_tracker as tracker_module,
)
from custom_components.omnibattery.tracking.non_responsive_tracker import (
    NonResponsiveTracker,
)
from tests.conftest import FakeCoordinator


def _coord():
    return FakeCoordinator(name="BAT1")


def test_first_threshold_cross_is_wake_not_exclude():
    tracker = NonResponsiveTracker(fail_threshold=3)
    coord = _coord()

    assert tracker.record_non_delivery(coord, 300, 0) is None
    assert tracker.record_non_delivery(coord, 300, 0) is None
    assert tracker.record_non_delivery(coord, 300, 0) == "wake"

    assert tracker.is_excluded(coord) is False


def test_second_threshold_cross_excludes():
    tracker = NonResponsiveTracker(fail_threshold=3)
    coord = _coord()

    for _ in range(3):
        tracker.record_non_delivery(coord, 300, 0)  # grace round, counter resets
    for _ in range(2):
        assert tracker.record_non_delivery(coord, 300, 0) is None
    assert tracker.record_non_delivery(coord, 300, 0) == "excluded"

    assert tracker.is_excluded(coord) is True


def test_recovery_between_grace_and_second_attempt_clears_state():
    """If the wake actually fixed it, delivering power clears everything,
    including the wake-used flag, so a later unrelated episode gets its own
    fresh grace round."""
    tracker = NonResponsiveTracker(fail_threshold=3)
    coord = _coord()

    for _ in range(3):
        tracker.record_non_delivery(coord, 300, 0)
    tracker.clear(coord)

    assert tracker.record_non_delivery(coord, 300, 0) is None
    assert tracker.record_non_delivery(coord, 300, 0) is None
    assert tracker.record_non_delivery(coord, 300, 0) == "wake"  # fresh grace budget


def test_last_reason_distinguishes_charge_from_discharge():
    """Callers that only want to act on a charge-side condition (e.g. a BMS
    full-charge exemption) key off this prefix -- it must not blur charge and
    discharge episodes together."""
    tracker = NonResponsiveTracker(fail_threshold=3)
    coord = _coord()

    assert tracker.last_reason(coord) == ""
    assert tracker.last_reason(coord, "default") == "default"

    tracker.record_non_delivery(coord, 1250, 5, reason="charge_non_delivery")
    assert tracker.last_reason(coord).startswith("charge_")

    tracker.record_non_delivery(coord, 600, 0, reason="standby_no_delivery")
    assert not tracker.last_reason(coord).startswith("charge_")

    tracker.record_comm_failure(coord, "modbus_write_failed")
    assert not tracker.last_reason(coord).startswith("charge_")


def test_cooldown_is_flat_across_episodes():
    """The exclusion cooldown must not grow across episodes — recovering fast is
    the goal, so a second exclusion waits the same as the first (no backoff)."""
    import custom_components.omnibattery.tracking.non_responsive_tracker as trk
    from datetime import timedelta

    tracker = NonResponsiveTracker(fail_threshold=3, cooldown_min=5)
    coord = _coord()
    real_utcnow = trk.dt_util.utcnow

    def exclude_and_time_cooldown() -> None:
        for _ in range(6):
            tracker.record_non_delivery(coord, 300, 0)  # grace, then exclude
        assert tracker.is_excluded(coord) is True
        # Just under 5 min: still excluded. Just over: released.
        trk.dt_util.utcnow = lambda: real_utcnow() + timedelta(minutes=4)
        assert tracker.is_excluded(coord) is True
        trk.dt_util.utcnow = lambda: real_utcnow() + timedelta(minutes=6)
        assert tracker.is_excluded(coord) is False

    try:
        exclude_and_time_cooldown()   # first episode
        trk.dt_util.utcnow = real_utcnow
        exclude_and_time_cooldown()   # second episode: same 5-min window, not 10
    finally:
        trk.dt_util.utcnow = real_utcnow


def test_comm_failure_excludes_immediately_no_grace():
    tracker = NonResponsiveTracker(fail_threshold=3)
    coord = _coord()

    assert tracker.record_comm_failure(coord, "modbus_write_failed") is False
    assert tracker.record_comm_failure(coord, "modbus_write_failed") is False
    assert tracker.record_comm_failure(coord, "modbus_write_failed") is True

    assert tracker.is_excluded(coord) is True


def test_cooldown_expiry_resets_wake_budget():
    import custom_components.omnibattery.tracking.non_responsive_tracker as trk

    tracker = NonResponsiveTracker(fail_threshold=3, cooldown_min=5)
    coord = _coord()
    for _ in range(6):
        tracker.record_non_delivery(coord, 300, 0)  # grace round, then excluded
    assert tracker.is_excluded(coord) is True

    # Monkeypatch dt_util.utcnow to jump past the cooldown.
    from datetime import timedelta
    real_utcnow = trk.dt_util.utcnow
    trk.dt_util.utcnow = lambda: real_utcnow() + timedelta(minutes=6)
    try:
        assert tracker.is_excluded(coord) is False  # cooldown expired
    finally:
        trk.dt_util.utcnow = real_utcnow

    assert tracker.record_non_delivery(coord, 300, 0) is None
    assert tracker.record_non_delivery(coord, 300, 0) is None
    assert tracker.record_non_delivery(coord, 300, 0) == "wake"  # fresh grace budget


def test_a_flip_on_an_excluded_battery_logs_a_retry(caplog):
    """A direction flip lifts the exclusion without any delivery seen, so the
    log line says the battery is retried, not that it delivers again."""
    tracker = NonResponsiveTracker(fail_threshold=3)
    coord = _coord()
    for _ in range(6):
        tracker.record_non_delivery(coord, 300, 0)
    assert tracker.is_excluded(coord) is True

    caplog.clear()
    with caplog.at_level("INFO", logger=tracker_module.__name__):
        tracker.clear(coord, delivering=False)

    assert tracker.is_excluded(coord) is False
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert messages == ["[BAT1] Commanded direction changed - retrying battery"]


def test_ending_an_exclusion_does_not_claim_delivery(caplog):
    """clear() also ends an exclusion for a battery that is full, tapering or
    at its low-SOC cutoff, so its log line says the battery is back in the
    pool, not that it delivers again."""
    tracker = NonResponsiveTracker(fail_threshold=3)
    coord = _coord()
    for _ in range(6):
        tracker.record_non_delivery(coord, 300, 0, reason="charge_non_delivery")
    assert tracker.is_excluded(coord) is True

    caplog.clear()
    with caplog.at_level("INFO", logger=tracker_module.__name__):
        tracker.clear(coord)

    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert messages == ["[BAT1] No longer non-responsive - returned to pool"]
