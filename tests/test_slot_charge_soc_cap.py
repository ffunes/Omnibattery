"""Tests for the Time Slot charge SOC cap in ``_effective_charge_max_soc``.

Regression: the precedence chain was ``predictive_target -> weekly -> slot
override -> max_soc``, so a predictive grid charge returned on its own target
and the slot branch was never reached. A Time Slot that explicitly capped
charging (``soc_override_enabled`` + ``soc_max``) was silently ignored for the
whole window — exactly the configuration a user sets to bound that charge.
The cap now folds into the ceiling before the predictive branch; a weekly full
charge still overrides it, as it did when the slot branch sat after it.

Exercised unbound with the shared FakeCoordinator and a light controller stub
(same pattern as test_charge_hysteresis.py).
"""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.omnibattery import ChargeDischargeController

from tests.conftest import FakeCoordinator


def _coord(soc=50, *, max_soc=95):
    return FakeCoordinator(max_soc=max_soc, data={"battery_soc": soc})


def _ctrl(
    coord,
    *,
    slot_max=None,
    grid_charging=False,
    target=None,
    weekly_pending=False,
):
    slot = None
    if slot_max is not None:
        slot = {
            "enabled": True,
            "allow_charge": True,
            "soc_override_enabled": True,
            "battery_limits": {"battery_1": {"soc_max": slot_max}},
        }
    return SimpleNamespace(
        coordinators=[coord],
        grid_charging_active=grid_charging,
        _predictive_charge_target_soc=({coord: target} if target is not None else None),
        _weekly_charge_mgr=SimpleNamespace(is_active=lambda: weekly_pending),
        _get_active_slot=lambda c, direction="any": slot if direction == "charge" else None,
        _slot_battery_limits=lambda s, c: s.get("battery_limits", {})["battery_1"],
    )


def _ceiling(ctrl, coord, weekly_unlocked=False):
    return ChargeDischargeController._effective_charge_max_soc(
        ctrl, coord, weekly_unlocked
    )


def test_no_slot_falls_back_to_max_soc():
    c = _coord()
    assert _ceiling(_ctrl(c), c) == (95, "max_soc")


def test_slot_cap_applies_outside_grid_charging():
    c = _coord()
    assert _ceiling(_ctrl(c, slot_max=80), c) == (80, "slot_soc_override")


def test_grid_charge_target_is_capped_by_the_slot():
    # The regression: target 92 beat the slot cap of 80 and charged straight past it.
    c = _coord()
    ctrl = _ctrl(c, slot_max=80, grid_charging=True, target=92)
    assert _ceiling(ctrl, c) == (80, "predictive_target")


def test_grid_charge_target_below_the_cap_still_wins():
    c = _coord()
    ctrl = _ctrl(c, slot_max=80, grid_charging=True, target=60)
    assert _ceiling(ctrl, c) == (60, "predictive_target")


def test_weekly_full_charge_overrides_the_slot_cap():
    c = _coord()
    ctrl = _ctrl(c, slot_max=80, grid_charging=True, target=100, weekly_pending=True)
    assert _ceiling(ctrl, c) == (100, "predictive_target")


def test_unparsable_slot_cap_is_ignored():
    c = _coord()
    ctrl = _ctrl(c, slot_max="not-a-number", grid_charging=True, target=92)
    assert _ceiling(ctrl, c) == (92, "predictive_target")


def test_solar_surplus_ceiling_ignores_only_the_predictive_target():
    # Issue #470: surplus charging past the grid-charge target still honours the slot cap.
    c = _coord()
    ctrl = _ctrl(c, slot_max=80, grid_charging=True, target=37)
    assert ChargeDischargeController._effective_charge_max_soc(
        ctrl, c, False, ignore_predictive_target=True
    ) == (80, "slot_soc_override")
