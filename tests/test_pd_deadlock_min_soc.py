"""Regression tests for issue #117: PD deadlock at min SoC on a slow grid sensor.

Reported field case: main sensor = HA ``enphase_envoy`` (hard-capped at a 60 s
scan interval), 2x Marstek Venus at min SoC, sustained solar surplus. Result was
19 h with 0.00 kWh charged while exporting and 3.81 kWh imported.

The chain:

1. At min SoC the "no available batteries" bailout ends the cycle before the
   end-of-cycle PD state update, so ``last_output_sign`` stays latched at -1
   (discharge). That latch is intentional: the battery may still be ramping down.
2. Each fresh sensor sample therefore reads as a discharge->charge flip and
   ``_apply_zero_cross_hold`` clamps it to 0 until the settle window elapses.
3. The stale safety recalc in between freezes the command at 0 W, and a 0 W
   request cleared ``_zero_cross_since``. On a sensor slower than the stale
   window (~30 s) the timer was always cleared before the next fresh sample, so
   the hold re-armed at 0.0 s forever and the flip could never pass. The
   reporter's logs show exactly that: one suppression per Envoy refresh, always
   ``0.0s/5.0s``.

So the fix is in step 3, not in the latch: the settle timer must survive the
stale freeze. The last zero-cross test reproduces that state sequence.

Helpers are exercised unbound with a ``SimpleNamespace`` stub, per repo
convention (see ``test_pd_zero_cross.py``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from homeassistant.core import State
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import (
    MAX_SENSOR_STALE_S,
    PD_ZERO_CROSS_MIN_HOLD_S,
    SLOW_SENSOR_RECOVERY_INTERVALS,
    SLOW_SENSOR_WARN_INTERVALS,
)


def _coord(latency_s=0.8):
    return SimpleNamespace(capabilities=SimpleNamespace(actuator_latency_s=latency_s))


def _hold_ctrl(last_output_sign, *, zero_cross_since=None, latencies=(0.8,)):
    return SimpleNamespace(
        last_output_sign=last_output_sign,
        _zero_cross_since=zero_cross_since,
        coordinators=[_coord(lat) for lat in latencies],
    )


def _hold(ctrl, new_power, error=0.0, stale_recalc=False):
    return ChargeDischargeController._apply_zero_cross_hold(
        ctrl, new_power, error, stale_recalc=stale_recalc
    )


# --- the settle window must survive the stale freeze -----------------------


def test_stale_recalc_zero_keeps_armed_timer():
    """The stale freeze reissues the previous 0 W command; it is not an idle decision."""
    since = dt_util.utcnow() - timedelta(seconds=2)
    ctrl = _hold_ctrl(last_output_sign=-1, zero_cross_since=since)

    out = _hold(ctrl, new_power=0, error=-904, stale_recalc=True)

    assert out == 0
    assert ctrl._zero_cross_since == since


def test_real_zero_request_still_clears_timer():
    """Guard against over-reach: a fresh 0 W decision must still re-arm the window."""
    ctrl = _hold_ctrl(last_output_sign=-1, zero_cross_since=dt_util.utcnow())

    assert _hold(ctrl, new_power=0, error=10, stale_recalc=False) == 0
    assert ctrl._zero_cross_since is None


def test_stale_recalc_with_no_armed_timer_is_untouched():
    ctrl = _hold_ctrl(last_output_sign=-1, zero_cross_since=None)

    assert _hold(ctrl, new_power=0, error=-904, stale_recalc=True) == 0
    assert ctrl._zero_cross_since is None


def test_stale_recalc_with_nonzero_frozen_command_is_unaffected():
    """A frozen command in the previous direction takes the ordinary pass-through."""
    ctrl = _hold_ctrl(last_output_sign=-1, zero_cross_since=dt_util.utcnow())

    assert _hold(ctrl, new_power=-300, error=400, stale_recalc=True) == -300
    assert ctrl._zero_cross_since is None


def test_flip_accumulates_across_stale_cycles_on_slow_sensor():
    """The reported zero-cross sequence: 60 s sensor, 2 s control cycles.

    Sample 1 arms the timer. The stale recalcs in between hold it. By the time the
    next fresh sample arrives the window is long satisfied, so the charge order
    goes through instead of re-arming at 0.0 s forever (19 h at 0.00 kWh charged).
    """
    ctrl = _hold_ctrl(last_output_sign=-1)

    assert _hold(ctrl, new_power=912, error=-904) == 0
    armed_at = ctrl._zero_cross_since
    assert armed_at is not None

    # ~29 stale cycles at 2 s each; the command is frozen at the previous 0 W.
    for _ in range(29):
        assert _hold(ctrl, new_power=0, error=-904, stale_recalc=True) == 0
    assert ctrl._zero_cross_since == armed_at

    # Fresh sample 60 s later: rewind the arm time to stand in for the elapsed wait.
    ctrl._zero_cross_since = armed_at - timedelta(seconds=PD_ZERO_CROSS_MIN_HOLD_S + 1)
    assert _hold(ctrl, new_power=973, error=-964) == 973
    assert ctrl._zero_cross_since is None


# --- slow-sensor cadence warning ------------------------------------------


def _cadence_ctrl():
    return SimpleNamespace(
        _last_sensor_report_time=None,
        _last_sensor_cadence_time=None,
        _last_control_sample_value=None,
        _control_sample_is_new=True,
        _slow_sensor_issue_created=False,
        _slow_sensor_intervals=0,
        _fast_sensor_intervals=0,
        consumption_sensor="sensor.grid_power",
        config_entry=SimpleNamespace(entry_id="test-entry"),
        hass=object(),
    )


def _cadence(ctrl, elapsed_s):
    ChargeDischargeController._check_sensor_cadence(ctrl, elapsed_s)


def _track_report(ctrl, state):
    return ChargeDischargeController._track_sensor_report(ctrl, state)


def _track_control_report(ctrl, state, value):
    return ChargeDischargeController._track_sensor_report(ctrl, state, value)


def _observe_report(ctrl, report_time):
    return ChargeDischargeController._observe_sensor_cadence(ctrl, report_time)


def _capture_repairs(monkeypatch):
    created = []
    deleted = []
    monkeypatch.setattr(
        ir,
        "async_create_issue",
        lambda *args, **kwargs: created.append((args, kwargs)),
    )
    monkeypatch.setattr(
        ir,
        "async_delete_issue",
        lambda *args, **kwargs: deleted.append((args, kwargs)),
    )
    return created, deleted


def test_unchanged_four_second_reports_do_not_create_slow_sensor_repair(monkeypatch):
    """P1 reports remain fresh even when several consecutive values are identical."""
    ctrl = _cadence_ctrl()
    created, deleted = _capture_repairs(monkeypatch)
    first_report = datetime(2026, 1, 1, tzinfo=timezone.utc)

    for report_number in range(SLOW_SENSOR_RECOVERY_INTERVALS + 1):
        report_time = first_report + timedelta(seconds=4 * report_number)
        state = State(
            "sensor.grid_power",
            "123",
            last_changed=first_report,
            last_reported=report_time,
            last_updated=first_report,
        )

        tracked_time, _elapsed_s, is_stale = _track_report(ctrl, state)
        assert tracked_time == report_time
        assert is_stale is False

    assert created == []
    assert len(deleted) == 1
    assert ctrl._slow_sensor_intervals == 0


def test_sensor_report_tracking_falls_back_to_last_updated():
    """Retain compatibility with State-like objects without last_reported."""
    ctrl = _cadence_ctrl()
    update_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    legacy_state = SimpleNamespace(last_updated=update_time)

    tracked_time, elapsed_s, is_stale = _track_report(ctrl, legacy_state)

    assert tracked_time == update_time
    assert elapsed_s is None
    assert is_stale is False

    _track_control_report(ctrl, legacy_state, 123.0)
    assert ctrl._control_sample_is_new is True
    _track_control_report(ctrl, legacy_state, 123.0)
    assert ctrl._control_sample_is_new is False


def test_identical_publications_are_fresh_health_but_not_new_control_samples(monkeypatch):
    """P1 cadence advances without reapplying the incremental controller."""
    ctrl = _cadence_ctrl()
    created, deleted = _capture_repairs(monkeypatch)
    first_report = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ctrl.previous_power = -700.0

    for report_number in range(SLOW_SENSOR_RECOVERY_INTERVALS + 1):
        report_time = first_report + timedelta(seconds=4 * report_number)
        state = State(
            "sensor.grid_power",
            "123",
            last_changed=first_report,
            last_reported=report_time,
            last_updated=first_report,
        )
        _track_control_report(ctrl, state, 123.0)

        assert ctrl._last_sensor_report_time == report_time
        assert ctrl._control_sample_is_new is (report_number == 0)
        assert ctrl.previous_power == -700.0

    assert created == []
    assert len(deleted) == 1
    assert ctrl._slow_sensor_intervals == 0


def test_transformed_value_change_is_a_new_control_sample_once():
    ctrl = _cadence_ctrl()
    first_report = datetime(2026, 1, 1, tzinfo=timezone.utc)

    first = State(
        "sensor.grid_power",
        "123",
        last_reported=first_report,
        last_updated=first_report,
    )
    same_value_different_attribute = State(
        "sensor.grid_power",
        "123",
        attributes={"friendly_name": "renamed"},
        last_reported=first_report + timedelta(seconds=4),
        last_updated=first_report + timedelta(seconds=4),
    )
    changed = State(
        "sensor.grid_power",
        "456",
        last_reported=first_report + timedelta(seconds=8),
        last_updated=first_report + timedelta(seconds=8),
    )

    _track_control_report(ctrl, first, 123.0)
    assert ctrl._control_sample_is_new is True
    _track_control_report(ctrl, same_value_different_attribute, 123.0)
    assert ctrl._control_sample_is_new is False
    _track_control_report(ctrl, changed, 456.0)
    assert ctrl._control_sample_is_new is True


def test_report_event_observation_does_not_schedule_control():
    ctrl = _cadence_ctrl()
    observed = []
    scheduled = []
    ctrl._observe_sensor_cadence = lambda report_time: observed.append(report_time)
    ctrl.schedule_control_cycle = lambda: scheduled.append(True)
    report_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    event = SimpleNamespace(
        data={
            "new_state": State(
                "sensor.grid_power",
                "123",
                last_reported=report_time,
                last_updated=report_time,
            )
        }
    )

    ChargeDischargeController._observe_consumption_report(ctrl, event)

    assert observed == [report_time]
    assert scheduled == []


def test_sustained_slow_cadence_creates_one_repair_without_log_spam(caplog, monkeypatch):
    ctrl = _cadence_ctrl()
    created, _ = _capture_repairs(monkeypatch)

    with caplog.at_level(logging.WARNING):
        for _ in range(SLOW_SENSOR_WARN_INTERVALS + 3):
            _cadence(ctrl, 60.0)

    assert caplog.text == ""
    assert len(created) == 1
    args, kwargs = created[0]
    assert args[2] == "slow_main_sensor_test-entry"
    assert kwargs["severity"] is ir.IssueSeverity.WARNING
    assert kwargs["translation_key"] == "slow_main_sensor"
    assert kwargs["translation_placeholders"] == {
        "sensor": "sensor.grid_power",
        "observed_interval": "60",
        "warning_interval": "10",
        "stale_limit": "65",
    }


def test_single_outage_gap_does_not_warn(caplog, monkeypatch):
    """A sensor unavailable for minutes leaves one huge gap; that is not a slow sensor.

    ``_last_sensor_report_time`` is not advanced while the sensor reads unavailable,
    so the first sample after any downtime measures the whole outage.
    """
    ctrl = _cadence_ctrl()
    created, _ = _capture_repairs(monkeypatch)

    with caplog.at_level(logging.WARNING):
        _cadence(ctrl, 1.0)
        _cadence(ctrl, 180.0)  # outage gap
        _cadence(ctrl, 1.0)
        _cadence(ctrl, 1.0)

    assert "unsupported" not in caplog.text
    assert ctrl._slow_sensor_intervals == 0
    assert created == []


def test_fast_interval_resets_the_streak(caplog, monkeypatch):
    ctrl = _cadence_ctrl()
    created, _ = _capture_repairs(monkeypatch)

    with caplog.at_level(logging.WARNING):
        for _ in range(SLOW_SENSOR_WARN_INTERVALS - 1):
            _cadence(ctrl, 45.0)
        _cadence(ctrl, 2.0)
        for _ in range(SLOW_SENSOR_WARN_INTERVALS - 1):
            _cadence(ctrl, 45.0)

    assert "unsupported" not in caplog.text
    assert created == []


def test_first_sample_without_a_previous_timestamp_is_ignored():
    ctrl = _cadence_ctrl()

    _cadence(ctrl, None)

    assert ctrl._slow_sensor_intervals == 0
    assert ctrl._slow_sensor_issue_created is False


def test_watchdog_zero_intervals_do_not_reset_slow_streak(caplog, monkeypatch):
    """Real 60 s samples are separated by many elapsed=0 watchdog ticks."""
    ctrl = _cadence_ctrl()
    created, _ = _capture_repairs(monkeypatch)

    with caplog.at_level(logging.WARNING):
        for _ in range(SLOW_SENSOR_WARN_INTERVALS):
            _cadence(ctrl, 60.0)
            for _ in range(29):
                _cadence(ctrl, 0.0)

    assert len(created) == 1
    assert caplog.text == ""


def test_slow_warning_threshold_is_independent_of_stale_tolerance(caplog, monkeypatch):
    """A 12 s sensor is supported but still receives control-quality guidance."""
    ctrl = _cadence_ctrl()
    created, _ = _capture_repairs(monkeypatch)

    with caplog.at_level(logging.WARNING):
        for _ in range(SLOW_SENSOR_WARN_INTERVALS):
            _cadence(ctrl, 12.0)

    assert len(created) == 1
    assert caplog.text == ""


def test_repair_clears_when_cadence_recovers_in_the_same_run(monkeypatch):
    """A transient stall must not leave a permanent warning about a fast sensor."""
    ctrl = _cadence_ctrl()
    created, deleted = _capture_repairs(monkeypatch)

    for _ in range(SLOW_SENSOR_WARN_INTERVALS):
        _cadence(ctrl, 12.0)
    assert len(created) == 1

    for _ in range(SLOW_SENSOR_RECOVERY_INTERVALS):
        _cadence(ctrl, 3.0)

    assert len(deleted) == 1
    assert deleted[0][0][2] == "slow_main_sensor_test-entry"
    assert ctrl._slow_sensor_issue_created is False


def test_short_fast_streak_does_not_clear_the_repair(monkeypatch):
    """Clearing needs the full recovery streak; that asymmetry is the hysteresis."""
    ctrl = _cadence_ctrl()
    created, deleted = _capture_repairs(monkeypatch)

    for _ in range(SLOW_SENSOR_WARN_INTERVALS):
        _cadence(ctrl, 60.0)
    for _ in range(SLOW_SENSOR_RECOVERY_INTERVALS - 1):
        _cadence(ctrl, 2.0)

    assert len(created) == 1
    assert deleted == []
    assert ctrl._slow_sensor_issue_created is True


def test_repair_is_recreated_when_the_sensor_slows_down_again(monkeypatch):
    ctrl = _cadence_ctrl()
    created, deleted = _capture_repairs(monkeypatch)

    for _ in range(SLOW_SENSOR_WARN_INTERVALS):
        _cadence(ctrl, 60.0)
    for _ in range(SLOW_SENSOR_RECOVERY_INTERVALS):
        _cadence(ctrl, 2.0)
    for _ in range(SLOW_SENSOR_WARN_INTERVALS):
        _cadence(ctrl, 60.0)

    assert len(created) == 2
    assert len(deleted) == 1
    assert ctrl._slow_sensor_issue_created is True


def test_sustained_fast_cadence_clears_and_deletes_only_once(monkeypatch):
    ctrl = _cadence_ctrl()
    created, deleted = _capture_repairs(monkeypatch)

    for _ in range(SLOW_SENSOR_WARN_INTERVALS):
        _cadence(ctrl, 60.0)
    for _ in range(SLOW_SENSOR_RECOVERY_INTERVALS * 3):
        _cadence(ctrl, 2.0)

    assert len(created) == 1
    assert len(deleted) == 1


def test_publications_are_counted_while_control_loop_is_busy(monkeypatch):
    """A busy control loop must not turn fast P1 publications into a 65 s interval."""
    ctrl = _cadence_ctrl()
    created, _ = _capture_repairs(monkeypatch)
    first_report = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # State-publication callbacks see every report while the control task is busy.
    _observe_report(ctrl, first_report)
    for report_number in range(1, 14):
        _observe_report(
            ctrl,
            first_report + timedelta(seconds=5 * report_number)
        )

    # The control loop only gets to sample the latest state after a long-running
    # battery operation.  That observation must not create a second 65 s cadence
    # interval because the publication was already recorded by the callback.
    latest_state = State(
        "sensor.grid_power",
        "123",
        last_changed=first_report,
        last_reported=first_report + timedelta(seconds=65),
        last_updated=first_report + timedelta(seconds=65),
    )
    _track_report(ctrl, latest_state)

    assert created == []
    assert ctrl._slow_sensor_intervals == 0


def test_persisted_repair_clears_after_fast_startup_cadence(monkeypatch):
    ctrl = _cadence_ctrl()
    created, deleted = _capture_repairs(monkeypatch)

    for _ in range(SLOW_SENSOR_RECOVERY_INTERVALS):
        _cadence(ctrl, 2.0)

    assert created == []
    assert len(deleted) == 1
    assert deleted[0][0][2] == "slow_main_sensor_test-entry"


def test_grid_sample_is_authoritative_through_65_seconds():
    ctrl = SimpleNamespace(_max_sensor_stale_s=MAX_SENSOR_STALE_S)
    sample_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    is_authoritative = ChargeDischargeController._sensor_is_within_stale_tolerance(
        ctrl,
        sample_time,
        sample_time + timedelta(seconds=MAX_SENSOR_STALE_S),
    )

    assert is_authoritative is True


def test_grid_sample_becomes_stale_after_65_seconds():
    ctrl = SimpleNamespace(_max_sensor_stale_s=MAX_SENSOR_STALE_S)
    sample_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    is_authoritative = ChargeDischargeController._sensor_is_within_stale_tolerance(
        ctrl,
        sample_time,
        sample_time + timedelta(seconds=MAX_SENSOR_STALE_S + 0.001),
    )

    assert is_authoritative is False
