"""Dynamic Pricing evaluations no longer interleave (P5).

Evaluations started outside the control cycle (00:05 timer, startup, the
re-evaluate button, feature switches) ran without the control lock, and two
evaluations could interleave across their awaits so the last writer won the
schedule.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.omnibattery.pricing.engine import (
    DynamicPricingEvaluationHorizon,
    PricingManager,
)

REMAINING = DynamicPricingEvaluationHorizon.REMAINING


def _manager(controller=None):
    manager = PricingManager(SimpleNamespace(), controller or SimpleNamespace())
    log = []

    async def body(*, horizon, extended_horizon=False):
        log.append("start")
        await asyncio.sleep(0)  # an await inside the evaluation
        await asyncio.sleep(0)
        log.append("end")

    manager._evaluate_dynamic_pricing_unlocked = body
    return manager, log


def test_two_evaluations_run_one_after_the_other():
    manager, log = _manager()

    async def both():
        await asyncio.gather(
            manager._evaluate_dynamic_pricing(horizon=REMAINING),
            manager._evaluate_dynamic_pricing(horizon=REMAINING),
        )

    asyncio.run(both())

    assert log == ["start", "end", "start", "end"]


def test_external_evaluation_waits_for_the_control_cycle():
    async def scenario():
        lock = asyncio.Lock()
        manager, log = _manager(SimpleNamespace(_control_lock=lock))

        async def control_cycle():
            async with lock:
                log.append("cycle start")
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                log.append("cycle end")

        cycle = asyncio.create_task(control_cycle())
        await asyncio.sleep(0)  # the cycle holds the lock
        await manager.evaluate_dynamic_pricing_now(horizon=REMAINING)
        await cycle
        return log

    assert asyncio.run(scenario()) == ["cycle start", "cycle end", "start", "end"]
