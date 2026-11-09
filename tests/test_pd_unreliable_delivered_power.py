"""Regression for #522: lagging delivered-power telemetry (Anker) must not
re-anchor the PD base through the timed anti-windup fallback.

Anker reports 0 W / stale values for seconds after a setpoint change. The 15 s
fallback read that as unexplained shortfall and yanked the base from -1616 W to
the stale -510 W while the grid still imported ~1 kW, making the loop hunt.
The saturation fast path (a real limit) is unaffected.

Convention: + charge / - discharge; error > 0 = grid import.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController


def _coord(reliable):
    return SimpleNamespace(
        capabilities=SimpleNamespace(delivered_power_reliable=reliable),
        manual_mode_enabled=False,
    )


def _ctrl(reliable, *, saturated=False):
    ctrl = SimpleNamespace(
        coordinators=[_coord(reliable)],
        previous_power=-1616.0,
        _measured_battery_power=lambda: -510.0,  # stale Anker reading
        _backcalc_is_saturated=lambda is_charging: saturated,
        saturation_backcalc_threshold=150.0,
        saturation_backcalc_cycles=3,
        saturation_backcalc_fallback_s=15.0,
        _saturation_cycles=0,
        # Shortfall already sustained well past the 15 s fallback.
        _saturation_shortfall_since=dt_util.utcnow() - timedelta(seconds=20),
        ki=0, kp=0.35, kd=0, dt=2.0, derivative_tau=3.0,
        derivative_filtered=0.0, previous_error=0.0, error_integral=0.0,
        _stale_cycles=0, max_power_change_per_cycle=800,
        _should_log_rate_limiter=lambda change: False,
        _clear_rate_limiter_state=lambda: None,
        last_output_sign=-1, direction_hysteresis=60,
    )
    ctrl._delivered_power_reliable = (
        lambda: ChargeDischargeController._delivered_power_reliable(ctrl)
    )
    return ctrl


def _run(ctrl, error=0.0):
    return ChargeDischargeController._compute_pd_new_power(
        ctrl, error, sensor_elapsed_s=2.0, stale_safety_recalc=False
    )


def test_unreliable_telemetry_skips_timed_fallback():
    ctrl = _ctrl(reliable=False)
    _run(ctrl)
    assert ctrl.previous_power == -1616.0


def test_reliable_telemetry_still_uses_timed_fallback():
    ctrl = _ctrl(reliable=True)
    _run(ctrl)
    assert ctrl.previous_power == -510.0


def test_real_saturation_still_reanchors_unreliable():
    ctrl = _ctrl(reliable=False, saturated=True)
    ctrl._saturation_cycles = 2  # third saturated cycle hits the threshold
    _run(ctrl)
    assert ctrl.previous_power == -510.0


def test_mixed_fleet_with_one_unreliable_is_unreliable():
    ctrl = _ctrl(reliable=True)
    ctrl.coordinators.append(_coord(False))
    assert ChargeDischargeController._delivered_power_reliable(ctrl) is False
