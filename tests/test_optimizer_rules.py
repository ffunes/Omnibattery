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


def _run(monkeypatch, tweak=None, settings=None):
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
    assert _drains(_run(monkeypatch), start, end) > 0.05  # it drains without the rule

    def block(controller):
        controller.config_entry.data["no_discharge_time_slots"] = [
            {"start_time": "06:00", "end_time": "08:00", "days": [], "enabled": True}
        ]

    assert _drains(_run(monkeypatch, block), start, end) <= 1e-9


def test_price_based_discharge_control_keeps_the_battery(monkeypatch):
    start, end = _window(8, 10)  # 0.55, 0.57: the battery covers the morning
    assert _drains(_run(monkeypatch), start, end) > 0.05

    def control(controller):
        controller.dp_price_discharge_control = True
        controller.discharge_price_threshold = 0.57

    assert _drains(_run(monkeypatch, control), start, end) <= 1e-9


def test_the_peak_shaving_hold_keeps_the_battery_until_the_sun(monkeypatch):
    start, end = _window(4, 8)
    assert _drains(_run(monkeypatch), start, end) > 0.05
    held = _run(monkeypatch, settings={"peak_shaving": {"limit_w": 3000, "soc_threshold": 50}})
    assert _drains(held, start, end) <= 1e-9
