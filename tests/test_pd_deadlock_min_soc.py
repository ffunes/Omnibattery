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

from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import (
    MAX_SENSOR_STALE_S,
    PD_ZERO_CROSS_MIN_HOLD_S,
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
        _slow_sensor_issue_created=False,
        _slow_sensor_intervals=0,
        _fast_sensor_intervals=0,
        consumption_sensor="sensor.grid_power",
        config_entry=SimpleNamespace(entry_id="test-entry"),
        hass=object(),
    )


def _cadence(ctrl, elapsed_s):
    ChargeDischargeController._check_sensor_cadence(ctrl, elapsed_s)


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

    ``_last_sensor_update_time`` is not advanced while the sensor reads unavailable,
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


def test_created_repair_is_not_cleared_or_recreated_during_same_run(monkeypatch):
    ctrl = _cadence_ctrl()
    created, deleted = _capture_repairs(monkeypatch)

    for _ in range(SLOW_SENSOR_WARN_INTERVALS):
        _cadence(ctrl, 60.0)
    for _ in range(SLOW_SENSOR_WARN_INTERVALS + 3):
        _cadence(ctrl, 2.0)

    assert len(created) == 1
    for _ in range(SLOW_SENSOR_WARN_INTERVALS):
        _cadence(ctrl, 60.0)

    assert deleted == []
    assert len(created) == 1
    assert ctrl._slow_sensor_issue_created is True


def test_persisted_repair_clears_after_fast_startup_cadence(monkeypatch):
    ctrl = _cadence_ctrl()
    created, deleted = _capture_repairs(monkeypatch)

    for _ in range(SLOW_SENSOR_WARN_INTERVALS):
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
