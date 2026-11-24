"""Planners book future slots at the fleet's configured charge power.

The live per-cycle limit (temperature derate, top-of-charge taper, driver
telemetry) describes now: a 2400 W + 2500 W fleet was planned at 1614 W.
"""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.pricing.engine import PricingManager


def _controller(live_limits: dict, *, system_cap: int = 0):
    controller = ChargeDischargeController.__new__(ChargeDischargeController)
    controller.enable_system_power_limits = system_cap > 0
    controller.system_max_charge_power = system_cap
    controller._battery_power_limit = lambda c, _charging: live_limits[c.name]
    return controller


_FLEET = [
    SimpleNamespace(name="zendure", battery_manual_mode_enabled=False,
                    effective_max_charge_power=2400, max_charge_power=2400),
    SimpleNamespace(name="venus", battery_manual_mode_enabled=False,
                    effective_max_charge_power=2500, max_charge_power=2500),
    SimpleNamespace(name="manual", battery_manual_mode_enabled=True,
                    effective_max_charge_power=2500, max_charge_power=2500),
]


def test_planning_capacity_ignores_live_derate_and_taper():
    # Zendure hot (derated), Venus in the top-of-charge taper.
    controller = _controller({"zendure": 1414, "venus": 200})

    live = controller._effective_system_capacity(_FLEET, True)
    planning = controller._effective_system_capacity(_FLEET, True, planning=True)

    assert live == 1614
    assert planning == 4900


def test_planning_capacity_survives_a_live_zero():
    # A driver reporting 0 W right now must not plan "no charge possible".
    controller = _controller({"zendure": 0, "venus": 0})

    assert controller._effective_system_capacity(_FLEET, True, planning=True) == 4900


def test_planning_capacity_keeps_the_system_cap():
    controller = _controller({"zendure": 0, "venus": 0}, system_cap=3000)

    assert controller._effective_system_capacity(_FLEET, True, planning=True) == 3000


def test_planning_capacity_leaves_out_an_observed_battery():
    observed = SimpleNamespace(
        name="observed", battery_manual_mode_enabled=False,
        effective_max_charge_power=3000, max_charge_power=3000,
        capabilities=SimpleNamespace(supports_power_setpoint=False),
    )
    controller = _controller({"zendure": 0, "venus": 0, "observed": 0})

    assert controller._effective_system_capacity(
        [*_FLEET, observed], True, planning=True
    ) == 4900


def test_engine_reads_planning_capacity_with_live_fallback():
    engine = PricingManager.__new__(PricingManager)
    engine._controller = SimpleNamespace(
        planning_charge_capacity=4900, max_charge_capacity=1614
    )
    assert engine._planning_charge_capacity() == 4900

    engine._controller = SimpleNamespace(max_charge_capacity=1614)
    assert engine._planning_charge_capacity() == 1614
