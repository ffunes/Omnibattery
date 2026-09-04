"""Regression tests for config-entry background task shutdown."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.omnibattery import ChargeDischargeController


@pytest.mark.asyncio
async def test_background_shutdown_flushes_lifecycle_managers_after_cancellation():
    controller = ChargeDischargeController.__new__(ChargeDischargeController)
    pending = asyncio.create_task(asyncio.sleep(60))
    controller._background_tasks = {pending}
    controller._startup_dynamic_pricing_task = None
    controller._unloading = False
    controller._no_pd_debounce_unsub = None
    charge_delay = SimpleNamespace(async_flush_state=AsyncMock())
    weekly_charge = SimpleNamespace(async_flush_state=AsyncMock())
    controller._charge_delay_mgr = charge_delay
    controller._weekly_charge_mgr = weekly_charge

    await controller.async_stop_background_tasks()

    assert pending.cancelled()
    charge_delay.async_flush_state.assert_awaited_once_with()
    weekly_charge.async_flush_state.assert_awaited_once_with()
