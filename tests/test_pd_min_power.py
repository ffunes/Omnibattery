"""Tests for ``_apply_min_power`` — the minimum charge/discharge power gate.

Regression for the dead-zone bug: zeroing a sub-minimum PD output also reset
``previous_power``, so the incremental loop restarted from 0 every cycle and a
steady sub-minimum demand (e.g. an 80W load with min_discharge=100W) could
never accumulate up to the minimum — it was simply never covered.

The fix engages AT the minimum when the grid error is large enough that the
over-correction lands inside the deadband (error >= minimum - deadband), a
stable point the deadband then holds. Smaller errors stay idle (also stable)
instead of bouncing on/off around the minimum.

The method is exercised unbound with a light stub (same pattern as
test_relay_dwell.py). Convention: + charge / - discharge; error > 0 = grid
import, error < 0 = export.
"""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.omnibattery import ChargeDischargeController


def _ctrl(*, min_charge=100, min_discharge=100, deadband=40):
    return SimpleNamespace(
        min_charge_power=min_charge,
        min_discharge_power=min_discharge,
        deadband=deadband,
    )


def _gate(ctrl, new_power, error):
    return ChargeDischargeController._apply_min_power(ctrl, new_power, error)


def test_dead_zone_load_engages_at_min_discharge():
    # 80W import, PD asks -28W (< min 100). Covering with 100W leaves a 20W
    # export, inside the 40W deadband -> engage at the minimum.
    ctrl = _ctrl()
    assert _gate(ctrl, new_power=-28, error=80) == -100


def test_small_load_stays_idle():
    # 50W import: covering with 100W would export 50W (> deadband), an
    # unstable point that would bounce on/off -> stay idle.
    ctrl = _ctrl()
    assert _gate(ctrl, new_power=-17, error=50) == 0


def test_export_engages_at_min_charge():
    # 80W export, PD asks +28W (< min 100). Charging 100W leaves 20W import,
    # inside the deadband -> engage at the minimum.
    ctrl = _ctrl()
    assert _gate(ctrl, new_power=28, error=-80) == 100


def test_small_export_stays_idle():
    ctrl = _ctrl()
    assert _gate(ctrl, new_power=17, error=-50) == 0


def test_output_above_min_passes_through():
    ctrl = _ctrl()
    assert _gate(ctrl, new_power=-150, error=200) == -150
    assert _gate(ctrl, new_power=150, error=-200) == 150


def test_min_disabled_passes_through():
    ctrl = _ctrl(min_charge=0, min_discharge=0)
    assert _gate(ctrl, new_power=-20, error=30) == -20
    assert _gate(ctrl, new_power=20, error=-30) == 20


def test_idle_passes_through():
    ctrl = _ctrl()
    assert _gate(ctrl, new_power=0, error=80) == 0


if __name__ == "__main__":
    test_dead_zone_load_engages_at_min_discharge()
    test_small_load_stays_idle()
    test_export_engages_at_min_charge()
    test_small_export_stays_idle()
    test_output_above_min_passes_through()
    test_min_disabled_passes_through()
    test_idle_passes_through()
    print("ok")
