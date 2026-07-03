"""Tests for the directional hysteresis in ``_compute_pd_new_power``.

Regression for the one-shot hysteresis bug: suppressing a charge<->discharge
flip zeroed ``last_output_sign``, so the very next cycle allowed any tiny
opposite-direction command through — the battery flipped direction at a few
watts despite the configured threshold. The fix keeps the sign memory across
idle cycles (loop side) and gates the flip on the grid error as well as the
output (function side): after a suppressed flip the increment base is 0, so
the kp-scaled output understates demand, while the error is the physical
demand signal and does not decay.

The method is exercised unbound with a light stub (same pattern as
test_relay_dwell.py). Convention: + charge / - discharge; error > 0 = grid
import (needs discharge), error < 0 = export (needs charge).
"""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.omnibattery import ChargeDischargeController


def _ctrl(*, last_output_sign, previous_power=0, kp=0.35, hysteresis=60):
    return SimpleNamespace(
        previous_power=previous_power,
        _measured_battery_power=lambda: None,  # skip anti-windup re-anchor
        ki=0,
        kp=kp,
        kd=0,  # keep the numbers pure-P for the tests
        dt=2.0,
        derivative_tau=3.0,
        derivative_filtered=0.0,
        previous_error=0.0,
        error_integral=0.0,
        _stale_cycles=0,
        _max_stale_cycles=5,
        max_power_change_per_cycle=800,
        _should_log_rate_limiter=lambda change: False,
        _clear_rate_limiter_state=lambda: None,
        last_output_sign=last_output_sign,
        direction_hysteresis=hysteresis,
    )


def _run(ctrl, error):
    return ChargeDischargeController._compute_pd_new_power(
        ctrl, error, sensor_elapsed_s=2.0, stale_safety_recalc=False
    )


def test_small_opposite_flip_suppressed():
    # Was discharging (sign -1), now a 50W export -> PD asks +17.5W charge.
    # Both output and error are under the 60W threshold -> stay at 0.
    ctrl = _ctrl(last_output_sign=-1)
    assert _run(ctrl, error=-50) == 0


def test_repeated_small_flips_stay_suppressed():
    # The one-shot regression: with the loop now preserving last_output_sign
    # across suppressed cycles, a persistent sub-threshold export must stay
    # suppressed every cycle, not just the first.
    ctrl = _ctrl(last_output_sign=-1)
    for _ in range(5):
        assert _run(ctrl, error=-50) == 0
        ctrl.previous_power = 0  # loop state after a suppressed cycle


def test_large_error_allows_flip_despite_small_output():
    # Export 120W: kp-scaled output (42W) is under the threshold, but the
    # demand itself exceeds it -> the flip must be allowed (no dead zone).
    ctrl = _ctrl(last_output_sign=-1)
    out = _run(ctrl, error=-120)
    assert out > 0
    assert abs(out - 42.0) < 0.1


def test_large_output_allows_flip():
    ctrl = _ctrl(last_output_sign=-1)
    out = _run(ctrl, error=-250)
    assert out > 0
    assert out >= ctrl.direction_hysteresis


def test_same_direction_not_gated():
    # More import while already discharging: no direction change, no gating.
    ctrl = _ctrl(last_output_sign=-1, previous_power=-100)
    out = _run(ctrl, error=50)
    assert out < -100


def test_no_previous_direction_not_gated():
    # From a true cold start (sign 0) any direction may engage.
    ctrl = _ctrl(last_output_sign=0)
    out = _run(ctrl, error=-50)
    assert out > 0


if __name__ == "__main__":
    test_small_opposite_flip_suppressed()
    test_repeated_small_flips_stay_suppressed()
    test_large_error_allows_flip_despite_small_output()
    test_large_output_allows_flip()
    test_same_direction_not_gated()
    test_no_previous_direction_not_gated()
    print("ok")
