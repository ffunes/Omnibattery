"""The optimizer publishes, per quarter, the kWh the battery delivers to the house."""
from __future__ import annotations

import dataclasses
import math

import pytest

from custom_components.omnibattery.pricing.optimizer import optimize_charge
from tests.test_optimizer import Q, T0, _request

EFF = math.sqrt(0.85)


def test_battery_covering_the_house_with_no_sun_and_no_charge():
    request = _request([0.2] * 4, house=0.1, solar=0.0, usable=0.5, threshold=0.0)
    plan = optimize_charge(request).plan
    assert not plan.allocations
    assert plan.house_delivery
    for end, kwh, price in plan.house_delivery:
        assert kwh > 0 and price == pytest.approx(0.2)
        assert kwh <= 0.1 + 1e-9  # house - solar of its quarter
    energies = [request.usable_initial_kwh] + [x for _t, x in plan.trajectory]
    drop = energies[0] - energies[-1]
    assert sum(k for _e, k, _p in plan.house_delivery) == pytest.approx(drop * EFF, abs=1e-6)


def test_delivery_never_exceeds_house_minus_solar_and_skips_idle_quarters():
    request = _request([0.3, 0.1, 0.3, 0.3], house=0.2, solar=[0.0, 0.0, 0.15, 0.3] + [0.0] * 12,
                       slot_quarters=4, usable=3.0)
    plan = optimize_charge(request).plan
    by_end = {i.end: i for i in request.intervals}
    for end, kwh, _price in plan.house_delivery:
        i = by_end[end]
        assert 0 < kwh <= max(0.0, i.grid_load_kwh - i.solar_kwh) + 1e-9


def test_replace_keeps_house_delivery():
    plan = optimize_charge(_request([0.2] * 4, house=0.1, usable=0.5, threshold=0.0)).plan
    assert plan.house_delivery
    assert dataclasses.replace(plan, allocations=[]).house_delivery == plan.house_delivery
