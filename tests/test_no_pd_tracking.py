"""Tests for ``_compute_no_pd_new_power`` — the No-PD direct-tracking law.

No-PD mode is a deadbeat 1:1 load tracker: ``new = measured - error``. The key
property is that it anchors to the MEASURED battery AC power, not the last
command. That is what keeps it stable across the inverter ramp + meter latency:
a ``previous_power`` anchor double-counts the still-uncovered error on every
mid-ramp sample and the loop oscillates rail-to-rail (the bug this law fixes).

The law is exercised unbound with light stubs, so no full controller is built.
Power convention is + charge / - discharge.
"""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.omnibattery import ChargeDischargeController


def _law(error, *, measured, previous_power=0.0):
    return ChargeDischargeController._compute_no_pd_new_power(
        SimpleNamespace(
            previous_power=previous_power,
            _measured_battery_power=lambda: measured,
        ),
        error,
    )


def test_law_anchors_to_measured():
    # Mid-ramp: battery delivering -400W (discharge), error 500W still on the grid.
    # new = measured - error = -400 - 500 = -900 (the true load), NOT previous-1400.
    assert _law(500.0, measured=-400.0, previous_power=-900.0) == -900.0


def test_law_falls_back_to_previous_when_measured_unknown():
    # No battery reports yet (just after a restart) → anchor to the last command.
    assert _law(200.0, measured=None, previous_power=-700.0) == -900.0


def _simulate(anchor, *, load=900.0, target=0.0, ramp=500.0, cycles=8):
    """Run the closed loop against a plant that ramps toward the command.

    ``anchor`` picks the deadbeat base: "measured" (the fixed law) or "previous"
    (the old, unstable one). Returns the list of issued commands. The grid meter
    reading includes the battery's current output: grid = load + battery_power.
    """
    battery = 0.0       # measured AC output, signed (+charge / -discharge)
    previous = 0.0
    commands = []
    for _ in range(cycles):
        grid = load + battery
        error = grid - target
        base = battery if anchor == "measured" else previous
        cmd = ChargeDischargeController._compute_no_pd_new_power(
            SimpleNamespace(previous_power=base, _measured_battery_power=lambda: None),
            error,
        )
        commands.append(cmd)
        battery += max(-ramp, min(ramp, cmd - battery))  # plant ramps toward cmd
        previous = cmd
    return commands, load + battery  # final grid


def test_measured_anchor_converges_without_overshoot():
    commands, final_grid = _simulate("measured")
    # Load is 900W → steady command is -900W (discharge 900). The measured anchor
    # never commands beyond the true load and parks exactly there.
    assert min(commands) >= -900.0 - 1e-6      # no overshoot past the real load
    assert abs(commands[-1] - (-900.0)) < 1e-6  # settled on the load
    assert abs(final_grid - 0.0) < 1e-6         # grid driven to target


def test_previous_anchor_overshoots_the_load():
    # Regression guard: the old previous_power anchor overshoots past the true
    # -900W load while the battery is still ramping. This is the rail-to-rail
    # oscillation the measured anchor removes.
    commands, _ = _simulate("previous")
    assert min(commands) < -900.0
