"""The weekly full charge raises a Zendure's ceiling in RAM, not in flash.

The run lifts ``socSet`` to 100 % and puts it back afterwards. Written with
``smartMode=0`` that was two flash writes a week, and the restore used the
integration's stored config, which the device's own Target SOC entity never
updates. In RAM the device keeps its Target SOC in flash, falls back to it on a
reboot, and the run restores what the device reported before the raise.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.omnibattery.control import weekly_full_charge as weekly_module

from tests.test_weekly_full_charge_lifecycle import _controller, _manager


def _zendure(max_soc=95, stored=100):
    return SimpleNamespace(
        name="Zendure",
        max_soc=max_soc,
        data={"battery_soc": 50},
        battery_manual_mode_enabled=False,
        capabilities=SimpleNamespace(hardware_soc_cutoff=True),
        charge_cutoff_is_temporary=True,
        set_temporary_charge_cutoff=AsyncMock(return_value=True),
        set_charge_cutoff=AsyncMock(return_value=True),
        # The device's Target SOC entity does not update this.
        _config_entry=SimpleNamespace(
            data={"batteries": [{"name": "Zendure", "max_soc": stored}]}
        ),
    )


def _marstek_v2():
    return SimpleNamespace(
        name="Venus v2",
        max_soc=95,
        data={"battery_soc": 50},
        battery_manual_mode_enabled=False,
        capabilities=SimpleNamespace(hardware_soc_cutoff=True),
        set_charge_cutoff=AsyncMock(return_value=True),
        _config_entry=SimpleNamespace(
            data={"batteries": [{"name": "Venus v2", "max_soc": 95}]}
        ),
    )


def _run(*coordinators):
    controller = _controller(
        weekly_full_charge_registers_written=False,
        _weekly_charge_saved_max_soc={},
        charge_delay_enabled=False,
    )
    controller.coordinators = list(coordinators)
    controller._is_backup_function_active = lambda _coordinator: False
    manager = _manager(controller)
    manager.tick_bms_cutoff = MagicMock()
    manager._bms_cutoff_counts = {}
    manager.is_battery_full = MagicMock(return_value=False)
    manager.is_active = MagicMock(return_value=True)
    manager.save_state = AsyncMock()
    return controller, manager


@pytest.mark.asyncio
async def test_the_run_raises_a_zendure_in_ram_and_saves_its_own_target_soc():
    zendure = _zendure(max_soc=95, stored=100)
    controller, manager = _run(zendure)

    await manager.handle_registers()

    zendure.set_temporary_charge_cutoff.assert_awaited_once_with(100)
    zendure.set_charge_cutoff.assert_not_awaited()
    # What the device held, not the stale 100 in the stored config.
    assert controller._weekly_charge_saved_max_soc == {"Zendure": 95}


@pytest.mark.asyncio
async def test_the_restore_goes_to_ram_too():
    zendure = _zendure(max_soc=100)
    controller, manager = _run(zendure)
    controller._weekly_charge_saved_max_soc = {"Zendure": 95}

    assert await manager._restore_hardware_cutoffs("test") is True

    zendure.set_temporary_charge_cutoff.assert_awaited_once_with(95)
    zendure.set_charge_cutoff.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_battery_without_a_ram_ceiling_keeps_its_register_write():
    venus = _marstek_v2()
    controller, manager = _run(venus)

    await manager.handle_registers()
    controller._weekly_charge_saved_max_soc = {"Venus v2": 95}
    await manager._restore_hardware_cutoffs("test")

    assert [call.args for call in venus.set_charge_cutoff.await_args_list] == [(100,), (95,)]


@pytest.mark.asyncio
async def test_a_rebooted_zendure_is_raised_again_once_a_minute_at_most(monkeypatch):
    zendure = _zendure(max_soc=95)
    venus = _marstek_v2()
    controller, manager = _run(zendure, venus)
    clock = [1000.0]
    monkeypatch.setattr(weekly_module, "monotonic", lambda: clock[0])

    await manager.handle_registers()
    zendure.max_soc = venus.max_soc = 100

    # The poll shows the fallback 30 s later: too early to write again.
    zendure.max_soc = 95
    clock[0] += 30.0
    await manager.handle_registers()
    assert zendure.set_temporary_charge_cutoff.await_count == 1

    clock[0] += 40.0
    await manager.handle_registers()
    assert zendure.set_temporary_charge_cutoff.await_count == 2
    # The Marstek's register write is unchanged: it is written again along
    # with the other battery, as before.
    assert [call.args for call in venus.set_charge_cutoff.await_args_list] == [(100,), (100,)]
    # The saved ceiling is still the one from before the first raise.
    assert controller._weekly_charge_saved_max_soc["Zendure"] == 95


@pytest.mark.asyncio
async def test_after_a_restart_mid_run_the_saved_ceiling_is_kept():
    """HA restarted while the Zendure held 100 % in RAM: the persisted value
    stays, it is not replaced by the 100 the device reports now."""
    zendure = _zendure(max_soc=100)
    controller, manager = _run(zendure)
    controller.weekly_full_charge_registers_written = True
    controller._weekly_charge_saved_max_soc = {"Zendure": 95}

    await manager.handle_registers()

    zendure.set_temporary_charge_cutoff.assert_awaited_once_with(100)
    assert controller._weekly_charge_saved_max_soc == {"Zendure": 95}
