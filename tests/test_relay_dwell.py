"""Tests for ``_apply_relay_dwell`` — the relay anti-chatter shut-off dwell.

When the controller decides to send the battery back to idle, the dwell keeps it
engaged at minimum power for ``_relay_cooldown_s`` seconds first, so the relay
doesn't click off the instant demand falls and on again when it returns.

The dwell is timed from the moment idle was FIRST requested, not from when the
battery engaged. The original implementation measured from engagement, so after
a long active run ``held_s`` was already far past the cooldown and the hold never
fired — the regression these tests pin down.

The method is exercised unbound with a light stub, so no full controller is built.
``previous_power`` convention is + charge / - discharge.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from custom_components.omnibattery import (
    ChargeDischargeController,
    RELAY_COOLDOWN_HOLD_POWER,
)


def _ctrl(
    previous_power,
    *,
    cooldown_s=30,
    deadband=40,
    min_charge=50,
    min_discharge=50,
    shutoff_since=None,
    zero_cross_since=None,
):
    return SimpleNamespace(
        _relay_cooldown_s=cooldown_s,
        previous_power=previous_power,
        deadband=deadband,
        min_charge_power=min_charge,
        min_discharge_power=min_discharge,
        _relay_shutoff_since=shutoff_since,
        _zero_cross_since=zero_cross_since,
    )


def _dwell(ctrl, new_power, error):
    return ChargeDischargeController._apply_relay_dwell(ctrl, new_power, error)


def test_shutoff_starts_hold_at_min_discharge():
    # Discharging 300W, demand vanishes -> controller asks for idle. Instead of
    # dropping to 0 the dwell holds at min discharge and arms the timer.
    ctrl = _ctrl(previous_power=-300)
    out = _dwell(ctrl, new_power=0, error=300)
    assert out == -50
    assert ctrl._relay_shutoff_since is not None


def test_shutoff_starts_hold_at_min_charge():
    ctrl = _ctrl(previous_power=200)
    out = _dwell(ctrl, new_power=0, error=-200)
    assert out == 50
    assert ctrl._relay_shutoff_since is not None


def test_min_power_zero_falls_back_to_hold_power():
    ctrl = _ctrl(previous_power=-300, min_charge=0, min_discharge=0)
    out = _dwell(ctrl, new_power=0, error=300)
    assert out == -RELAY_COOLDOWN_HOLD_POWER


def test_hold_continues_within_dwell():
    # 10s into a 30s dwell, still being asked for idle -> keep holding.
    since = dt_util.utcnow() - timedelta(seconds=10)
    ctrl = _ctrl(previous_power=-50, shutoff_since=since)
    out = _dwell(ctrl, new_power=0, error=50)
    assert out == -50
    assert ctrl._relay_shutoff_since == since


def test_hold_releases_after_dwell():
    # Past the 30s dwell -> allow idle and re-arm for next time.
    since = dt_util.utcnow() - timedelta(seconds=31)
    ctrl = _ctrl(previous_power=-50, shutoff_since=since)
    out = _dwell(ctrl, new_power=0, error=50)
    assert out == 0
    assert ctrl._relay_shutoff_since is None


def test_long_active_run_then_shutoff_still_holds():
    # Regression: the battery has been engaged for minutes (shutoff_since is None
    # because it never tried to idle). The FIRST idle request must start a fresh
    # dwell and hold -- the old engage-based timer would have skipped it.
    ctrl = _ctrl(previous_power=-300, shutoff_since=None)
    out = _dwell(ctrl, new_power=0, error=300)
    assert out == -50
    assert ctrl._relay_shutoff_since is not None


def test_large_imbalance_bypasses_hold():
    # A sudden real load (error far beyond what the battery was handling) must not
    # be left on the grid: skip the hold, go to the commanded idle, drop the timer.
    ctrl = _ctrl(previous_power=-100, shutoff_since=dt_util.utcnow())
    out = _dwell(ctrl, new_power=0, error=500)
    assert out == 0
    assert ctrl._relay_shutoff_since is None


def test_zero_cross_suppressed_flip_holds_despite_large_error():
    # The zero came from a zero-cross-suppressed flip (timer armed), not a real
    # idle decision: the PD wants the opposite direction and the relay will stay
    # engaged after the flip. The large-imbalance bypass must NOT trip here, or
    # every direction swing drops the relay for the settle window (chatter).
    ctrl = _ctrl(previous_power=-500, zero_cross_since=dt_util.utcnow())
    out = _dwell(ctrl, new_power=0, error=-900)
    assert out == -50
    assert ctrl._relay_shutoff_since is not None


def test_large_imbalance_still_bypasses_without_suppressed_flip():
    # Same large error but no zero-cross timer armed: genuine idle request with a
    # big real imbalance keeps the original bypass behaviour.
    ctrl = _ctrl(previous_power=-100, zero_cross_since=None)
    out = _dwell(ctrl, new_power=0, error=500)
    assert out == 0
    assert ctrl._relay_shutoff_since is None


def test_active_power_rearms_timer():
    # Controller commands real power (not idle) -> dwell does nothing, timer clears.
    ctrl = _ctrl(previous_power=-300, shutoff_since=dt_util.utcnow())
    out = _dwell(ctrl, new_power=-400, error=400)
    assert out == -400
    assert ctrl._relay_shutoff_since is None


def test_stale_recalc_keeps_armed_timer():
    # A pass without a new meter sample replays previous_power (the held 400W).
    # That is not a fresh "active" decision: the timer must survive it, or the
    # dwell restarts at 0s on every repeat and holds forever (Zendure, 2026-10-02).
    since = dt_util.utcnow() - timedelta(seconds=7)
    ctrl = _ctrl(previous_power=400, min_charge=400, shutoff_since=since)
    out = ChargeDischargeController._apply_relay_dwell(
        ctrl, 400, -200, stale_recalc=True
    )
    assert out == 400
    assert ctrl._relay_shutoff_since == since


def test_cooldown_disabled_no_hold():
    ctrl = _ctrl(previous_power=-300, cooldown_s=0)
    out = _dwell(ctrl, new_power=0, error=300)
    assert out == 0
    assert ctrl._relay_shutoff_since is None


def test_hold_never_raises_power_above_what_was_flowing():
    # Charging only 15W when idle is requested: the 100W fallback would raise the
    # charge, so the hold stays at the 15W already flowing.
    ctrl = _ctrl(previous_power=15, min_charge=0, min_discharge=0)
    out = _dwell(ctrl, new_power=0, error=-10)
    assert out == 15
    assert ctrl._relay_shutoff_since is not None


def test_hold_capped_below_configured_min_power():
    ctrl = _ctrl(previous_power=-30, min_discharge=50)
    out = _dwell(ctrl, new_power=0, error=30)
    assert out == -30


def test_small_charge_during_import_reaches_discharge_instead_of_looping(monkeypatch):
    # Field trace (SolarFlow 800 Plus): charging ~15W after sunset while the house
    # imports ~240W. The PD asks for discharge, the zero-cross hold clamps the flip
    # and the dwell held the old direction at the 100W fallback. Stepping down from
    # 100W gave a small charge again, which reset both timers: Set Charge Power
    # alternated 100W <-> 5..65W for hours and the battery never discharged.
    clock = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: clock[0])
    ctrl = SimpleNamespace(
        _relay_cooldown_s=30,
        deadband=40,
        min_charge_power=0,
        min_discharge_power=0,
        previous_power=15,
        last_output_sign=1,
        _flip_hyst_suppressed=False,
        _relay_shutoff_since=None,
        _zero_cross_since=None,
        coordinators=[
            SimpleNamespace(capabilities=SimpleNamespace(actuator_latency_s=0.8))
        ],
    )
    # Commands flip faster than the actuator settles, so the meter keeps
    # reading the household import.
    kp, error = 0.35, 240.0
    commands = []
    for _ in range(15):
        new_power = ctrl.previous_power - kp * error
        new_power = ChargeDischargeController._apply_zero_cross_hold(
            ctrl, new_power, error
        )
        new_power = _dwell(ctrl, new_power, error)
        commands.append(new_power)
        ctrl.previous_power = new_power
        if new_power:
            ctrl.last_output_sign = 1 if new_power > 0 else -1
        clock[0] += timedelta(seconds=1)

    assert max(commands) <= 15
    assert any(command < 0 for command in commands)
