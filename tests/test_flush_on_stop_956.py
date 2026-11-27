"""#956: Home Assistant stopping must flush the daily accumulators.

HA does not unload the entries when it stops, so the throttled (~5 min) saves
were all a restart got back, and the daily TOTAL_INCREASING sensors stepped
backwards. The stop handler now freezes accumulation and flushes the tracker.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.omnibattery import ChargeDischargeController
from tests.test_control_sample_scheduling import _main_controller, _state


def _stop_rig(rest_fails=False):
    calls = []

    async def _rest():
        calls.append("rest")
        if rest_fails:
            raise RuntimeError("battery unreachable")

    async def _save():
        calls.append("save")

    controller = SimpleNamespace(
        _accumulation_frozen=False,
        _autonomous_mode_mgr=SimpleNamespace(async_rest_all=_rest),
        _consumption_tracker=SimpleNamespace(async_save_all=_save),
    )
    return controller, calls


@pytest.mark.parametrize("rest_fails", [False, True])
def test_stopping_rests_the_batteries_then_flushes_the_tracker(rest_fails):
    controller, calls = _stop_rig(rest_fails)
    stop = ChargeDischargeController.async_on_homeassistant_stop(controller)
    if rest_fails:
        with pytest.raises(RuntimeError):
            asyncio.run(stop)
    else:
        asyncio.run(stop)
    assert calls == ["rest", "save"], "a failed rest must not skip the flush"
    assert controller._accumulation_frozen is True


@pytest.mark.parametrize("frozen", [False, True])
def test_a_frozen_cycle_accumulates_nothing_after_the_flush(frozen):
    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    controller = _main_controller({"state": _state(1500, first)}, [])
    controller._compute_pd_new_power = lambda *_a: 1000.0
    controller.previous_power = 0.0
    controller.last_output_sign = 0
    controller._accumulation_frozen = frozen
    accumulated = []

    async def _accumulate(*_a, **_k):
        accumulated.append(True)

    controller._consumption_tracker = SimpleNamespace(
        handle_accumulator_daily_reset=lambda: None,
        accumulate_household_consumption=_accumulate,
        handle_daily_energy_reset=lambda: None,
        accumulate_daily_solar_energy=_accumulate,
        accumulate_daily_home_energy=_accumulate,
        accumulate_daily_grid_energy=_accumulate,
        maybe_save_accumulators=lambda: None,
    )
    asyncio.run(controller._run_control_cycle(now=first + timedelta(seconds=1)))
    assert bool(accumulated) is not frozen
