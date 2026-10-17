"""Tests for the PD control-quality metric while the grid is inside the deadband.

The deadband shortcut skips the PD step but still feeds the quality metric.
Without that, the metric only averages the cycles outside the deadband, mostly
the first seconds of a load step: large, one-sided errors that read as
"sluggish" even when the loop holds the grid on target most of the time.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.sensors.aggregate_sensors import PdControlQualitySensor
from tests.test_control_sample_scheduling import _main_controller, _state
from tests.test_pd_quality_blocked import _MetricCtrl


async def _no_rebalance(**_kwargs):
    return False


def test_deadband_cycle_feeds_the_quality_metric():
    reported_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state_holder = {"state": _state(12, reported_at)}
    pd_calls = []
    controller = _main_controller(state_holder, pd_calls)
    controller.ki = 0.0
    controller._zero_cross_since = None
    controller._power_distribution._rebalance_expired_load_sharing_hold = _no_rebalance
    metric_calls = []
    controller._update_pd_quality_metrics = (
        lambda error, sign_changed, target, skip: metric_calls.append(
            (error, sign_changed, target, skip)
        )
    )

    asyncio.run(controller._run_control_cycle(now=reported_at + timedelta(seconds=1)))

    assert pd_calls == []  # inside the deadband: no PD step
    assert metric_calls == [(12.0, False, 0.0, False)]


def _run_minute(ctrl, errors):
    """Feed one sample per second on a virtual clock."""
    clock = [1000.0]
    with patch("custom_components.omnibattery.time.monotonic", lambda: clock[0]):
        for error in errors:
            ctrl.update(error, blocked=False)
            clock[0] += 1.0


def _verdict(ctrl):
    return PdControlQualitySensor._verdict(
        ctrl.rms, ChargeDischargeController.pd_quality_oscillation_per_min.fget(ctrl)
    )


# A 1 Hz meter with the grid held at a few watts, then a 450 W load step the
# loop corrects in a few seconds.
_STEP = [450, 330, 210, 120, 60]
_ON_TARGET = [8, -5, 12, -3, 6] * 11


def test_time_on_target_keeps_a_good_loop_stable():
    ctrl = _MetricCtrl()
    _run_minute(ctrl, _ON_TARGET + _STEP + _ON_TARGET)
    assert ctrl.rms < PdControlQualitySensor._RMS_HIGH_W
    assert _verdict(ctrl) == "stable"


def test_excursions_alone_read_as_sluggish():
    # Only the step, without the time on target around it.
    ctrl = _MetricCtrl()
    _run_minute(ctrl, _STEP)
    assert _verdict(ctrl) == "sluggish"
