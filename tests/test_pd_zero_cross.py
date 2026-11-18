"""Tests for ``_apply_zero_cross_hold`` — the charge<->discharge flip dwell.

Diagnosed 2026-07-13: on a downward load step the discharging battery keeps
delivering its old setpoint for the actuator settle time (~3-6 s), so the grid
shows a transient export of hundreds of watts. The incremental PD crosses zero
on that transient and emits a real charge command (min-power floored to ~200 W)
on another battery while the assignment loop zeroes the discharger — 0 W
discharge dips of 5-40 s and ping-pong every 1-3 min. The direction hysteresis
(magnitude-based, 60 W) cannot stop a -500/-1500 W transient and the relay
dwell only gates active->idle, not direction flips.

The hold clamps the FIRST opposite-direction request to 0 and arms a timer; the
flip only passes once the request has persisted past the settle window, so a
transient export never becomes a charge order but sustained solar surplus still
flips (a few seconds late).

The method is exercised unbound with a ``SimpleNamespace`` stub, matching
``test_relay_dwell.py``. Timestamps are shifted backwards instead of sleeping.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import PD_ZERO_CROSS_MIN_HOLD_S


def _coord(latency_s=0.8):
    return SimpleNamespace(capabilities=SimpleNamespace(actuator_latency_s=latency_s))


def _ctrl(
    last_output_sign,
    *,
    zero_cross_since=None,
    latencies=(0.8,),
):
    return SimpleNamespace(
        last_output_sign=last_output_sign,
        _zero_cross_since=zero_cross_since,
        _flip_hyst_suppressed=False,
        coordinators=[_coord(lat) for lat in latencies],
    )


def _hold(ctrl, new_power, error=0.0):
    return ChargeDischargeController._apply_zero_cross_hold(ctrl, new_power, error)


def test_first_flip_request_clamped_to_zero():
    # Discharging (sign -1), transient export makes the PD request +250W charge.
    # The flip is clamped to 0 (no charge order) and the settle timer is armed.
    ctrl = _ctrl(last_output_sign=-1)
    out = _hold(ctrl, new_power=250, error=-800)
    assert out == 0
    assert ctrl._zero_cross_since is not None


def test_flip_still_clamped_within_window():
    since = dt_util.utcnow() - timedelta(seconds=2)
    ctrl = _ctrl(last_output_sign=-1, zero_cross_since=since)
    out = _hold(ctrl, new_power=400, error=-900)
    assert out == 0
    assert ctrl._zero_cross_since == since


def test_sustained_flip_allowed_after_window():
    # Real sustained surplus: the charge request persisted past the window,
    # so the flip goes through and the timer re-arms for next time.
    since = dt_util.utcnow() - timedelta(seconds=PD_ZERO_CROSS_MIN_HOLD_S + 1)
    ctrl = _ctrl(last_output_sign=-1, zero_cross_since=since)
    out = _hold(ctrl, new_power=400, error=-900)
    assert out == 400
    assert ctrl._zero_cross_since is None


def test_return_to_previous_direction_resets_timer():
    # Import came back within the window: discharge resumes untouched and the
    # streak timer clears — the next transient starts its own window.
    ctrl = _ctrl(last_output_sign=-1, zero_cross_since=dt_util.utcnow())
    out = _hold(ctrl, new_power=-300, error=400)
    assert out == -300
    assert ctrl._zero_cross_since is None


def test_zero_request_resets_timer():
    ctrl = _ctrl(last_output_sign=-1, zero_cross_since=dt_util.utcnow())
    out = _hold(ctrl, new_power=0, error=10)
    assert out == 0
    assert ctrl._zero_cross_since is None


def test_idle_to_active_not_gated():
    # No previous direction (sign 0): engaging from idle is not a flip.
    ctrl = _ctrl(last_output_sign=0)
    out = _hold(ctrl, new_power=250, error=-300)
    assert out == 250
    assert ctrl._zero_cross_since is None


def test_charge_to_discharge_flip_also_gated():
    ctrl = _ctrl(last_output_sign=1)
    out = _hold(ctrl, new_power=-350, error=500)
    assert out == 0
    assert ctrl._zero_cross_since is not None


def test_window_stretches_for_slow_actuators():
    # Zendure/ESPHome fleet (latency 4.0s): window = 2*4 = 8s, so a request
    # 6s old is still clamped; the same age passes on a fast Marstek fleet.
    since = dt_util.utcnow() - timedelta(seconds=6)
    slow = _ctrl(last_output_sign=-1, zero_cross_since=since, latencies=(0.8, 4.0))
    assert _hold(slow, new_power=400, error=-900) == 0
    fast = _ctrl(last_output_sign=-1, zero_cross_since=since, latencies=(0.8,))
    assert _hold(fast, new_power=400, error=-900) == 400


def test_transient_export_never_emits_charge_order():
    """Diagnosed scenario end to end through the _run_control_cycle chain.

    Steady 300W discharge; an induction hob pulse drops the load and the grid
    exports for 3 cycles while the actuator ramps down. Chain the helpers in
    the same order as _run_control_cycle (zero-cross -> min power -> relay
    dwell) and assert the final command is NEVER positive: no charge order,
    so the assignment loop never selects another battery for charging.
    """
    ctrl = SimpleNamespace(
        last_output_sign=-1,
        _zero_cross_since=None,
        _flip_hyst_suppressed=False,
        coordinators=[_coord()],
        # _apply_min_power / _apply_relay_dwell fields
        min_charge_power=200,
        min_discharge_power=50,
        deadband=40,
        previous_power=-300,
        _relay_cooldown_s=30,
        _relay_shutoff_since=None,
    )
    # 3 cycles of transient export: PD requests a charge flip each time.
    for requested, error in ((250, -800), (400, -1200), (150, -500)):
        power = ChargeDischargeController._apply_zero_cross_hold(ctrl, requested, error)
        power = ChargeDischargeController._apply_min_power(ctrl, power, error)
        power = ChargeDischargeController._apply_relay_dwell(ctrl, power, error)
        assert power <= 0, f"charge order {power}W emitted during transient"
        ctrl.previous_power = power

    # Import returns: discharge resumes immediately, no window applies.
    power = ChargeDischargeController._apply_zero_cross_hold(ctrl, -350, 400)
    assert power == -350
    assert ctrl._zero_cross_since is None


def test_hysteresis_suppression_does_not_reset_armed_timer_849():
    """#849: idle battery, last_output_sign latched +1, grid import hovering
    around the 60 W Direction Hysteresis (errors 64/57 W, Kp 0.5). The ALLOWED
    cycle arms the zero-cross timer, the suppressed one used to reset it, so the
    window was never reached. Chain compute -> hold like _run_control_cycle,
    ageing the stored timestamp instead of sleeping."""
    ctrl = SimpleNamespace(
        previous_power=0,
        _measured_battery_power=lambda: None,
        ki=0, kp=0.5, kd=0, dt=2.0, derivative_tau=3.0, derivative_filtered=0.0,
        previous_error=0.0, error_integral=0.0, _stale_cycles=0,
        max_power_change_per_cycle=800,
        _should_log_rate_limiter=lambda change: False,
        _clear_rate_limiter_state=lambda: None,
        last_output_sign=1,
        direction_hysteresis=60,
        _zero_cross_since=None,
        _flip_hyst_suppressed=False,
        coordinators=[_coord(3.0)],  # window = 6 s
    )
    emitted = []
    for error in (64, 57) * 4:  # 8 cycles, alternating allowed / suppressed
        power = ChargeDischargeController._compute_pd_new_power(
            ctrl, error, sensor_elapsed_s=2.0, stale_safety_recalc=False
        )
        power = ChargeDischargeController._apply_zero_cross_hold(ctrl, power, error)
        emitted.append(power)
        if ctrl._zero_cross_since is not None:
            ctrl._zero_cross_since -= timedelta(seconds=2)  # one cycle elapsed
    assert any(p < 0 for p in emitted), f"no discharge emitted: {emitted}"


def test_export_transient_after_discharge_still_no_charge_849():
    # Hysteresis suppression of a flip must not weaken the original protection:
    # a flip armed by a real request is still clamped, and a genuine 0 resets.
    ctrl = _ctrl(last_output_sign=-1, zero_cross_since=dt_util.utcnow())
    assert _hold(ctrl, new_power=250, error=-800) == 0
    assert ctrl._zero_cross_since is not None
    assert _hold(ctrl, new_power=0, error=10) == 0  # genuine idle (flag False)
    assert ctrl._zero_cross_since is None


def test_hysteresis_flag_is_consumed_once_849():
    # A suppression flag left by one PD cycle must not shield a later genuine
    # 0 W (no-PD paths never clear it in _compute_pd_new_power).
    since = dt_util.utcnow()
    ctrl = _ctrl(last_output_sign=1, zero_cross_since=since)
    ctrl._flip_hyst_suppressed = True
    assert _hold(ctrl, new_power=0) == 0
    assert ctrl._zero_cross_since == since
    assert _hold(ctrl, new_power=0) == 0
    assert ctrl._zero_cross_since is None
