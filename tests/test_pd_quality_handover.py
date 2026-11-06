"""Tests for the PD control-quality metric when the PD takes over again.

A grid-charge slot, the weekly full charge or manual mode runs the batteries
without the PD, so the metric is not updated meanwhile. When the PD takes over,
its stored numbers describe the situation before the gap, and the batteries are
still running the previous command for a few seconds, so the grid reads far off
target. The metric starts over: it skips the hand-over, averages one window of
fresh readings while the sensor shows "collecting_data", and then reports a
verdict on those readings only.

Convention: error > 0 = grid import, error < 0 = export.
"""
from __future__ import annotations

import math
from unittest.mock import patch

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import (
    MAX_SENSOR_STALE_S,
    PD_QUALITY_RESTART_GAP_S,
    PD_QUALITY_RESTART_SETTLE_S,
)
from custom_components.omnibattery.sensors.aggregate_sensors import PdControlQualitySensor
from tests.test_pd_quality_blocked import _MetricCtrl


class _Ctrl(_MetricCtrl):
    """Metric state plus the properties the sensor reads."""

    pd_blocked = False
    pd_limited = False

    @property
    def pd_quality_rms_error(self):
        return self.rms

    @property
    def pd_quality_age_s(self):
        return self.age

    @property
    def pd_quality_oscillation_per_min(self):
        return ChargeDischargeController.pd_quality_oscillation_per_min.fget(self)


class _Clock:
    """Virtual monotonic clock for the metric and the sensor."""

    def __init__(self):
        self.now = 1000.0

    def __enter__(self):
        self._patch = patch(
            "custom_components.omnibattery.time.monotonic", lambda: self.now
        )
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


def _sensor_state(ctrl):
    sensor = PdControlQualitySensor.__new__(PdControlQualitySensor)
    sensor._controller = ctrl
    return sensor.native_value


def _feed(ctrl, clock, errors, interval=1.0, *, blocked=False):
    """Feed one reading per interval and return the sensor state after each."""
    states = []
    for error in errors:
        ctrl.update(error, blocked=blocked)
        states.append(_sensor_state(ctrl))
        clock.now += interval
    return states


# A 1 Hz meter with the grid held at a few watts.
_ON_TARGET = [8, -5, 12, -3, 6]
# The first seconds after a grid-charge slot ends: the batteries still charge at
# the slot rate while the PD ramps them over to discharge.
_HAND_OVER = [4400, 3600, 2700, 1800, 1000, 450, 160, 60]
_SLOT_S = 5400.0
_SETTLE = int(PD_QUALITY_RESTART_SETTLE_S)
_WINDOW = 60  # the metric window (_pd_quality_tau)


def _settled_loop(clock):
    ctrl = _Ctrl()
    _feed(ctrl, clock, _ON_TARGET * 24)  # two minutes on target
    assert _sensor_state(ctrl) == "stable"
    return ctrl


def test_hand_over_after_a_grid_charge_slot_does_not_read_as_sluggish():
    with _Clock() as clock:
        ctrl = _settled_loop(clock)
        clock.now += _SLOT_S  # the slot runs the batteries; no metric updates

        states = _feed(ctrl, clock, _HAND_OVER + _ON_TARGET * 60)

        assert "sluggish" not in states
        assert states[-1] == "stable"
        assert ctrl.rms < 20


def test_numbers_from_before_the_slot_do_not_return():
    # The loop trailed the load until the battery ran empty, then sat
    # battery-limited, which freezes the metric at that high RMS.
    with _Clock() as clock:
        ctrl = _Ctrl()
        _feed(ctrl, clock, [900] * 60)
        assert _sensor_state(ctrl) == "sluggish"
        _feed(ctrl, clock, [2000] * 600, blocked=True)
        clock.now += _SLOT_S

        states = _feed(ctrl, clock, _HAND_OVER + _ON_TARGET * 60)

        assert "sluggish" not in states
        assert states[-1] == "stable"
        assert ctrl.rms < 20


def test_restart_reports_after_the_settle_window_and_one_metric_window():
    with _Clock() as clock:
        ctrl = _settled_loop(clock)
        clock.now += _SLOT_S

        # The loop now really trails the load by 400W.
        states = _feed(ctrl, clock, [400] * 120)

        assert states[: _SETTLE + _WINDOW] == ["collecting_data"] * (_SETTLE + _WINDOW)
        assert states[_SETTLE + _WINDOW] == "sluggish"
        assert states[-1] == "sluggish"
        assert ctrl.rms == pytest.approx(400.0)
        assert ctrl.age is not None and ctrl.age < 2.0


def test_warm_up_weighs_every_fresh_reading_the_same():
    with _Clock() as clock:
        ctrl = _settled_loop(clock)
        clock.now += _SLOT_S
        _feed(ctrl, clock, [0] * _SETTLE)  # the settle window

        _feed(ctrl, clock, [0] * 30 + [300] * 29)
        assert ctrl.rms is None  # one reading short of a full window
        _feed(ctrl, clock, [300])

    # The plain mean of 30 readings at 0W and 30 at 300W.
    assert math.sqrt(ctrl._pd_quality_rms_ema) == pytest.approx(math.sqrt(300**2 / 2))


@pytest.mark.parametrize(
    "gap_s",
    [
        30.0,
        # The slowest supported meter, plus one 2s safety-timer tick.
        MAX_SENSOR_STALE_S + 2.0,
    ],
)
def test_gap_below_the_restart_limit_updates_the_metric_as_before(gap_s):
    assert gap_s < PD_QUALITY_RESTART_GAP_S
    with _Clock() as clock:
        ctrl = _settled_loop(clock)
        prev = ctrl._pd_quality_rms_ema
        clock.now += gap_s - 1.0  # _feed already advanced the clock by 1s

        ctrl.update(400, blocked=False)

        assert ctrl._pd_quality_settle_until < clock.now
        assert ctrl._pd_quality_warmup_start is None

    alpha = gap_s / (ctrl._pd_quality_tau + gap_s)
    assert ctrl._pd_quality_rms_ema == pytest.approx(prev + alpha * (400**2 - prev))


def test_slowest_supported_meter_keeps_the_verdict_live():
    with _Clock() as clock:
        ctrl = _settled_loop(clock)
        states = _feed(ctrl, clock, [400] * 10, interval=MAX_SENSOR_STALE_S + 2.0)

    assert "collecting_data" not in states
    assert states[-1] == "sluggish"
