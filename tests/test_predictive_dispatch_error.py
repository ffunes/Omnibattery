"""An error in the predictive step no longer aborts the control cycle.

Without a guard, any exception in the mode handler skipped the rest of the
cycle - blocker enforcement included - so an active slot left the batteries
on their last charge command with nothing regulating it.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import PREDICTIVE_MODE_DYNAMIC_PRICING
from tests.test_dead_main_sensor import NOW, _cycle_ctrl, _meter


def _failing_ctrl():
    state_holder = {"state": _meter(900, NOW)}
    pd_calls, writes = [], []
    ctrl = _cycle_ctrl(state_holder, pd_calls, writes)
    ctrl.predictive_charging_enabled = True
    ctrl.predictive_charging_mode = PREDICTIVE_MODE_DYNAMIC_PRICING
    ctrl.grid_charging_active = True  # a slot was charging
    ctrl._predictive_dispatch_errors = 0
    cleared = []

    async def _boom():
        raise RuntimeError("planner bug")

    def _clear(reason):
        cleared.append(reason)
        ctrl.grid_charging_active = False

    ctrl._handle_dynamic_pricing_predictive_charging = _boom
    ctrl._clear_predictive_runtime = _clear
    ctrl._handle_predictive_dispatch_error = (
        ChargeDischargeController._handle_predictive_dispatch_error.__get__(
            ctrl, ChargeDischargeController
        )
    )
    return ctrl, pd_calls, cleared


def test_a_failing_predictive_step_falls_through_to_normal_control():
    ctrl, pd_calls, cleared = _failing_ctrl()

    asyncio.run(ctrl._run_control_cycle(now=NOW + timedelta(seconds=1)))

    assert cleared == ["predictive_error"]
    assert ctrl.grid_charging_active is False
    assert len(pd_calls) == 1  # normal PD control ran this cycle
    assert ctrl._predictive_dispatch_errors == 1


def test_repeated_failures_are_counted_and_reset_on_success():
    ctrl, pd_calls, _cleared = _failing_ctrl()

    asyncio.run(ctrl._run_control_cycle(now=NOW + timedelta(seconds=1)))
    asyncio.run(ctrl._run_control_cycle(now=NOW + timedelta(seconds=4)))
    assert ctrl._predictive_dispatch_errors == 2

    async def _ok():
        return None

    ctrl._handle_dynamic_pricing_predictive_charging = _ok
    asyncio.run(ctrl._run_control_cycle(now=NOW + timedelta(seconds=7)))
    assert ctrl._predictive_dispatch_errors == 0
