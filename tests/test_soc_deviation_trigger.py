"""The Dynamic Pricing SOC-drop trigger compares against the plan's projection.

The chronological plan already projects the overnight discharge; only a SOC
away from that projected line (a deviation, below or above) re-plans.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from tests import test_predictive_scenarios as scenarios
from tests.test_pricing_engine import _controller, _mgr

_D = datetime(2026, 1, 20)


def _at(hour, minute=0):
    return _D.replace(hour=hour, minute=minute)


def _coord(soc, kwh=10.0):
    return SimpleNamespace(data={"battery_soc": soc, "battery_total_energy": kwh})


def _trigger(*, ref, soc, now, projection=None, chronological=True, **extra):
    ctrl = _controller(
        _dp_last_eval_soc=ref,
        _dp_eval_projection=projection,
        _dp_eval_projection_capacity_kwh=10.0,
        _dynamic_pricing_schedule=SimpleNamespace(
            chronological_planning_active=chronological
        ),
        coordinators=[_coord(soc)],
        **extra,
    )
    manager = _mgr(ctrl)
    manager._now = lambda: now
    return manager._is_dp_soc_drop_reeval()


# 100 % at 20:00 draining to 10 % at 23:00 (10 kWh fleet): -30 points per hour.
_DISCHARGE = ((_at(20), 0.0), (_at(23), -9.0))


def test_discharging_as_projected_does_not_replan():
    for soc in (69.0, 70.0, 71.0):  # projected line at 21:00 = 100 - 30
        assert not _trigger(ref=100.0, soc=soc, now=_at(21), projection=_DISCHARGE)


def test_six_points_below_the_projected_line_replans():
    assert _trigger(ref=100.0, soc=64.0, now=_at(21), projection=_DISCHARGE)


def test_a_projected_charge_that_did_not_happen_replans():
    projection = ((_at(2), 0.0), (_at(3), 4.0))  # +40 points after the slot
    assert _trigger(ref=30.0, soc=30.0, now=_at(3, 30), projection=projection)
    assert not _trigger(ref=30.0, soc=68.0, now=_at(3, 30), projection=projection)


def test_without_a_projection_the_old_rule_applies():
    assert _trigger(ref=60.0, soc=55.0, now=_at(21))  # chronological: 5 points
    assert not _trigger(ref=60.0, soc=56.0, now=_at(21))
    assert not _trigger(ref=60.0, soc=40.0, now=_at(21), chronological=False)  # 20 < 30
    assert _trigger(ref=60.0, soc=24.0, now=_at(21), chronological=False)


def test_a_dynamic_pricing_evaluation_stores_the_plan_projection(monkeypatch):
    controllers = []
    real = scenarios._controller

    def _capture(scenario):
        controllers.append(real(scenario))
        return controllers[-1]

    monkeypatch.setattr(scenarios, "_controller", _capture)
    path = scenarios.SCENARIO_DIR / "000-baseline-cheap-block-engine.json"
    scenario = scenarios.load_scenario(path)
    scenarios.run_scenario(scenario, monkeypatch)

    ctrl = controllers[0]
    projection = ctrl._dp_eval_projection
    assert projection
    assert ctrl._dp_eval_projection_capacity_kwh > 0
    deltas = [delta for _t, delta in projection]
    # No solar in the scenario: the energy only falls between charge slots.
    assert deltas[0] == pytest.approx(0.0, abs=0.5)
    assert min(deltas) < deltas[0]
    assert all(isinstance(t, datetime) for t, _d in projection)
    assert [t for t, _d in projection] == sorted(t for t, _d in projection)


# Step 10: the limit is 3 points below the projected line (5 before).
def test_four_points_below_the_projected_line_replans_two_do_not():
    assert _trigger(ref=100.0, soc=66.0, now=_at(21), projection=_DISCHARGE)
    assert not _trigger(ref=100.0, soc=68.0, now=_at(21), projection=_DISCHARGE)


def test_a_fast_planned_discharge_right_after_the_evaluation_does_not_replan():
    # Evaluated at 20:00; the plan drains 8 points by 20:15 (a sale, a big
    # load). At 20:08 the battery is exactly on that path (100 - 8 x 8/15).
    projection = ((_at(20, 15), -0.8), (_at(20, 30), -0.8))
    assert not _trigger(ref=100.0, soc=95.7, now=_at(20, 8), projection=projection)


def test_reaching_the_quarter_s_planned_level_early_does_not_replan():
    # The quarter projects 16 % -> 10 % (the tail of a sale); the battery is
    # already at 10 % mid-quarter: on the plan, just early.
    projection = ((_at(20), 0.0), (_at(20, 15), -0.6), (_at(20, 30), -0.6))
    assert not _trigger(ref=16.0, soc=10.0, now=_at(20, 7), projection=projection)


# Above the line: an optimizer plan (its rows) that has spent less than planned.
_ROWS = ((_at(20), _at(23), 0.0, 0.0, -9.0, 0.0),)


def test_four_points_above_the_projected_line_replans_two_do_not():
    assert _trigger(ref=100.0, soc=74.0, now=_at(21), projection=_DISCHARGE, _dp_eval_rows=_ROWS)
    assert not _trigger(ref=100.0, soc=72.0, now=_at(21), projection=_DISCHARGE, _dp_eval_rows=_ROWS)


def test_above_the_line_without_optimizer_rows_does_not_replan():
    assert not _trigger(ref=100.0, soc=90.0, now=_at(21), projection=_DISCHARGE)


def test_the_sun_the_safety_margin_took_off_does_not_replan():
    # The margin took 0.5 kWh (5 points) of sun before 21:00: 74 % is on the
    # plan once that sun is back.
    rows = ((_at(20), _at(20, 30), 0.0, 0.0, -1.5, 0.5),) + _ROWS
    assert not _trigger(ref=100.0, soc=74.0, now=_at(21), projection=_DISCHARGE, _dp_eval_rows=rows)


def test_above_the_line_during_a_charge_slot_does_not_replan():
    assert not _trigger(
        ref=100.0, soc=80.0, now=_at(21), projection=_DISCHARGE,
        _dp_eval_rows=_ROWS, _current_price_slot_active=True,
    )


def test_above_the_line_within_the_plan_s_first_hour_does_not_replan():
    # 20:30: line at 85 %; the plan is half an hour old.
    assert not _trigger(ref=100.0, soc=95.0, now=_at(20, 30), projection=_DISCHARGE, _dp_eval_rows=_ROWS)


def test_a_planned_charge_reached_early_in_its_quarter_does_not_replan():
    # The quarter charges 50 % -> 56 %; at 12:07 the battery is at 58 %:
    # 2 points over the quarter's end, 5 over the line now.
    projection = ((_at(10), 0.0), (_at(12), 0.0), (_at(12, 15), 0.6))
    rows = ((_at(12), _at(12, 15), 0.0, 0.0, 0.6, 0.0),)
    assert not _trigger(ref=50.0, soc=58.0, now=_at(12, 7), projection=projection, _dp_eval_rows=rows)
    assert _trigger(ref=50.0, soc=60.0, now=_at(12, 7), projection=projection, _dp_eval_rows=rows)
