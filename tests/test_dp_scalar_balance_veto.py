"""The scalar energy balance must not veto the Dynamic Pricing plan.

A sunny day nets today's solar against tonight: the balance sees no deficit,
yet the battery (15 %) runs empty before the sun and the plan books the cheap
night slots. Found by the profitability check (I8) on the sweep grid.
"""
import pytest

from tests.test_predictive_sweep import run, scenario, total

CASE = ("1x5k-2500", 15, "flat0.4", "night-cheap", False, "sunny", "00:05")


@pytest.mark.parametrize("daily", [False, True], ids=["remaining", "daily"])
def test_a_sufficient_balance_does_not_veto_the_plan(daily):
    r = run(scenario(CASE), daily=daily)
    planned = sum(a.planned_battery_kwh for a in r.plan.allocations)
    assert planned > 0.4  # the plan's need, ~0.52 kWh
    assert total(r) == pytest.approx(planned, abs=0.01)
