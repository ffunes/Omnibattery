"""Step 4 of the optimizer design: the fixed rules.

The rules that stop the battery covering the house (a fleet no-discharge
window, price-based discharge control, the peak-shaving hold) already reach the
planner as intervals where the battery is idle (``battery_idle_intervals``). The
optimizer's plan must keep the battery from draining there.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime

import pytest

from tests import test_predictive_scenarios as h

SCENARIO = h.SCENARIO_DIR / "834-midnight-reevaluation.json"


def _run(monkeypatch, tweak=None, settings=None, holds=True):
    """``holds=False``: the plan may not hold by itself (#888), so a rule is
    seen keeping the battery where the plan alone would spend it."""
    scenario = h.load_scenario(SCENARIO)
    if settings:
        scenario = dataclasses.replace(scenario, settings={**scenario.settings, **settings})
    real = h._controller

    def build(s):
        controller = real(s)
        if tweak:
            tweak(controller)
        return controller

    monkeypatch.setattr(h, "_controller", build)
    if not holds:
        from custom_components.omnibattery.pricing import engine

        real_optimize = engine.optimize_charge
        monkeypatch.setattr(engine, "optimize_charge", lambda request: real_optimize(
            dataclasses.replace(request, hold_allowed=False)))
    outcome = h._run_engine(scenario, monkeypatch)
    assert outcome.planner == "optimizer"
    return outcome


def _drains(outcome, start: datetime, end: datetime) -> float:
    """Largest drop of the projected energy across one interval in [start, end)."""
    plan = outcome.plan
    energy = {outcome.request.intervals[0].start: outcome.request.usable_initial_kwh}
    energy.update(dict(plan.trajectory))
    drops = [
        energy[i.start] - energy[i.end]
        for i in outcome.request.intervals
        if start <= i.start < end and i.start in energy and i.end in energy
    ]
    assert drops, "no interval in the window"
    return max(drops)


def _window(hh_from, hh_to):
    day = datetime(2026, 11, 11)
    return day.replace(hour=hh_from), day.replace(hour=hh_to)


def test_a_no_discharge_window_keeps_the_battery(monkeypatch):
    start, end = _window(6, 8)
    assert _drains(_run(monkeypatch, holds=False), start, end) > 0.05  # drains without the rule

    def block(controller):
        controller.config_entry.data["no_discharge_time_slots"] = [
            {"start_time": "06:00", "end_time": "08:00", "days": [], "enabled": True}
        ]

    assert _drains(_run(monkeypatch, block, holds=False), start, end) <= 1e-9


def test_price_based_discharge_control_keeps_the_battery(monkeypatch):
    start, end = _window(8, 10)  # 0.55, 0.57: the battery covers the morning
    assert _drains(_run(monkeypatch), start, end) > 0.05

    def control(controller):
        controller.dp_price_discharge_control = True
        controller.discharge_price_threshold = 0.57

    assert _drains(_run(monkeypatch, control), start, end) <= 1e-9


def test_the_peak_shaving_hold_keeps_the_battery_until_the_sun(monkeypatch):
    start, end = _window(4, 8)
    assert _drains(_run(monkeypatch, holds=False), start, end) > 0.05
    held = _run(monkeypatch, settings={"peak_shaving": {"limit_w": 3000, "soc_threshold": 50}},
                holds=False)
    assert _drains(held, start, end) <= 1e-9


def test_the_guaranteed_floor_goes_to_the_optimizer_until_the_sun(monkeypatch):
    # 3 x 5.12 kWh at 35 % with a 40 % floor: under it, so the controller's own
    # trigger makes it active; the plan keeps it to the next sun.
    outcome = _run(monkeypatch, settings={"guaranteed_floor_soc": 40.0})
    request = outcome.request
    assert request.floor_kwh > 1.0  # above the planning floor, in stored kWh
    assert request.floor_until is not None and request.floor_until > request.now
    energy = dict(outcome.plan.trajectory)
    after_charge = [e for t, e in energy.items() if t >= datetime(2026, 11, 11, 6) and t <= request.floor_until]
    assert after_charge and min(after_charge) >= request.floor_kwh - 0.05


def test_negative_price_slots_come_from_the_optimizer(monkeypatch):
    """Switch on: the optimizer books the negative-price slots it finds worth it,
    with their energy; the opportunistic calendar adds none of its own."""
    from tests import test_predictive_sweep as sw

    monkeypatch.setitem(
        sw.PRICES, "neg-noon",
        lambda hr: 0.15 if hr < 7 else (-0.05 if 11 <= hr < 15 else (0.30 if 18 <= hr < 22 else 0.20)),
    )
    real = h._controller

    def build(s):
        controller = real(s)
        controller.negative_price_charging_enabled = True
        return controller

    monkeypatch.setattr(h, "_controller", build)
    r = sw.run(sw.scenario(("1x7k-800", 15, "flat1.2", "neg-noon", False, "dull", "03:05")))
    assert r.o.planner == "optimizer"
    assert not r.o.to_top
    negative = [k for slot, k in r.o.booked if slot.price < 0]
    assert negative and sum(negative) > 3.0


def test_the_import_limit_caps_every_planned_charge(monkeypatch):
    """Peak shaving's limit is the charge ceiling (the controller's too): each
    quarter stores at most (limit - house) x sqrt(rte)."""
    import math

    outcome = _run(monkeypatch, settings={"peak_shaving": {"limit_w": 1500, "soc_threshold": 0}})
    request = outcome.request
    assert request.grid_ceiling_kw == pytest.approx(1.5)
    by_slot = dict(outcome.booked)
    assert by_slot
    for slot, kwh in by_slot.items():
        room = sum(
            max(0.0, 1.5 * (i.end - i.start).total_seconds() / 3600 - i.grid_load_kwh)
            * math.sqrt(request.import_value_efficiency)
            for i in request.intervals
            if slot.start <= i.start < slot.end
        )
        assert kwh <= room + 1e-6, slot
