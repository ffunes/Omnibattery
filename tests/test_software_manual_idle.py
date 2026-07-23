"""Manual Mode idle must not reassert 0 W (Anker Third-Party release)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.omnibattery import ChargeDischargeController


@pytest.mark.asyncio
async def test_software_manual_idle_skips_zero_watt_reassert():
    """Idle force mode must not call _set_battery_power — that would force
    Anker Third-Party Control every control cycle while Manual Mode is on."""
    controller = ChargeDischargeController.__new__(ChargeDischargeController)
    coord = SimpleNamespace(
        needs_software_manual_control=True,
        manual_force_mode=None,
        manual_set_charge_power=0,
        manual_set_discharge_power=0,
    )
    controller.coordinators = [coord]
    controller._set_battery_power = AsyncMock()

    await ChargeDischargeController._apply_software_manual_setpoints(controller)

    controller._set_battery_power.assert_not_awaited()


@pytest.mark.asyncio
async def test_software_manual_charge_still_asserts_setpoint():
    controller = ChargeDischargeController.__new__(ChargeDischargeController)
    coord = SimpleNamespace(
        needs_software_manual_control=True,
        manual_force_mode="Charge",
        manual_set_charge_power=1200,
        manual_set_discharge_power=0,
    )
    controller.coordinators = [coord]
    controller._set_battery_power = AsyncMock()

    await ChargeDischargeController._apply_software_manual_setpoints(controller)

    controller._set_battery_power.assert_awaited_once_with(
        coord, 1200, 0, bypass_blockers=True
    )


@pytest.mark.asyncio
async def test_software_manual_discharge_still_asserts_setpoint():
    controller = ChargeDischargeController.__new__(ChargeDischargeController)
    coord = SimpleNamespace(
        needs_software_manual_control=True,
        manual_force_mode="Discharge",
        manual_set_charge_power=0,
        manual_set_discharge_power=800,
    )
    controller.coordinators = [coord]
    controller._set_battery_power = AsyncMock()

    await ChargeDischargeController._apply_software_manual_setpoints(controller)

    controller._set_battery_power.assert_awaited_once_with(
        coord, 0, 800, bypass_blockers=True
    )
