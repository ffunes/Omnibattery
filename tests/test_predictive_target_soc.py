"""Regression tests for ``_compute_predictive_target_soc`` (issue #409).

In Dynamic Pricing mode the scheduler sizes the cheap charging slots on
``energy_deficit_kwh`` (hours_needed = deficit / power). The enforcer that sets
the stop-SOC must charge the *same* energy, otherwise it overshoots and fills
the battery for the whole slot. The bug: the enforcer sized the target off the
raw gap-to-max minus solar surplus, which collapses to the full gap (→ max_soc)
whenever there is no solar surplus (consumption ≥ solar). These tests pin the
deficit-based target.

The method only reads ``_last_decision_data``, ``coordinators`` and per-coord
``data``/``max_soc``/``name``, so it is exercised unbound on a stub controller.
"""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.omnibattery import ChargeDischargeController


class _Coord:
    """Identity-hashable coordinator stub (used as dict keys)."""

    def __init__(self, name, soc, capacity_kwh, max_soc=100):
        self.name = name
        self.max_soc = max_soc
        self.data = {"battery_soc": soc, "battery_total_energy": capacity_kwh}


def _ctrl(coords, decision):
    return SimpleNamespace(
        coordinators=list(coords),
        _last_decision_data=decision,
        _predictive_grid_charge_margin_pct=0.0,
    )


def _compute(ctrl):
    return ChargeDischargeController._compute_predictive_target_soc(ctrl)


def test_no_solar_surplus_targets_deficit_not_max_soc():
    # Issue #409 scenario: solar (12.1) < consumption (17.1) → no surplus.
    # Two 5.12 kWh batteries at 36.5%, deficit 3.20 kWh. Total gap = 6.5 kWh,
    # so the old gap-based target was ~100%; deficit-based is ~67.8%.
    a = _Coord("a", 36.5, 5.12)
    b = _Coord("b", 36.5, 5.12)
    decision = {
        "energy_deficit_kwh": 3.1974,
        "solar_forecast_kwh": 12.113,
        "avg_consumption_kwh": 17.12,
    }
    targets = _compute(_ctrl([a, b], decision))

    # deficit 3.1974 kWh split over 10.24 kWh ≈ +31.2% → 36.5 + 31.2 ≈ 67.7%
    assert targets[a] == targets[b]
    assert 67.0 < targets[a] < 68.5
    # Crucially: well below max_soc (the pre-fix behavior).
    assert targets[a] < 95.0


def test_deficit_capped_at_gap_to_max():
    # Deficit larger than the room to max_soc must not exceed max_soc.
    c = _Coord("c", 90.0, 5.0)  # only 0.5 kWh of room
    decision = {"energy_deficit_kwh": 10.0}
    targets = _compute(_ctrl([c], decision))
    assert targets[c] == 100.0


def test_precomputed_planned_charge_keeps_target_in_sync_with_scheduler():
    c = _Coord("c", 19.0, 2.08, max_soc=95)
    decision = {
        "energy_deficit_kwh": 4.552857,
        "planned_grid_charge_kwh": 1.5808,
    }
    targets = _compute(_ctrl([c], decision))
    assert targets[c] == 95.0


def test_grid_charge_margin_applies_to_fallback_target_calculation():
    c = _Coord("c", 20.0, 5.0, max_soc=100)
    ctrl = _ctrl([c], {"energy_deficit_kwh": 2.0})
    ctrl._predictive_grid_charge_margin_pct = 50.0
    targets = _compute(ctrl)
    assert targets[c] == 80.0


def test_target_never_below_current_soc():
    # No deficit (covered by solar/storage) → target stays at current SOC,
    # not driven down.
    c = _Coord("c", 60.0, 5.0)
    decision = {"energy_deficit_kwh": 0.0}
    targets = _compute(_ctrl([c], decision))
    assert targets[c] == 60.0


def test_proportional_split_favors_larger_gap():
    # Battery with the larger gap gets the larger share of the grid charge.
    low = _Coord("low", 20.0, 5.0)   # gap 4.0 kWh
    high = _Coord("high", 80.0, 5.0)  # gap 1.0 kWh
    decision = {"energy_deficit_kwh": 2.5}
    targets = _compute(_ctrl([low, high], decision))
    low_added = targets[low] - 20.0
    high_added = targets[high] - 80.0
    assert low_added > high_added > 0


def test_returns_none_without_decision_data():
    assert _compute(_ctrl([_Coord("c", 50.0, 5.0)], None)) is None
