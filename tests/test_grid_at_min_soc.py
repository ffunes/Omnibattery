"""Regression tests for the Grid at Min SOC timeslot scope."""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.omnibattery import ChargeDischargeController


def _controller(slots, *, active_slot=None):
    controller = SimpleNamespace(
        config_entry=SimpleNamespace(data={"no_discharge_time_slots": slots}),
        coordinators=[object()],
        _get_active_slot=lambda _coordinator, _direction: active_slot,
    )
    controller._is_grid_at_min_soc_discharge_window = (
        ChargeDischargeController._is_grid_at_min_soc_discharge_window.__get__(
            controller,
            ChargeDischargeController,
        )
    )
    return controller


def test_no_manual_slots_counts_all_day():
    assert _controller([])._is_grid_at_min_soc_discharge_window() is True


def test_disabled_discharge_slot_does_not_suppress_accumulator():
    slot = {"enabled": False, "allow_discharge": True}
    assert _controller([slot])._is_grid_at_min_soc_discharge_window() is True


def test_enabled_charge_only_slot_does_not_suppress_accumulator():
    slot = {"enabled": True, "allow_discharge": False}
    assert _controller([slot])._is_grid_at_min_soc_discharge_window() is True


def test_enabled_discharge_slot_requires_an_active_window():
    slot = {"enabled": True, "allow_discharge": True}
    assert _controller([slot])._is_grid_at_min_soc_discharge_window() is False
    assert (
        _controller([slot], active_slot=slot)
        ._is_grid_at_min_soc_discharge_window()
        is True
    )
