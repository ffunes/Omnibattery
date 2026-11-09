"""The weekly full charge restores a Zendure to its own Target SOC.

The run lifts ``socSet`` to 100 % and puts it back afterwards. The value it put
back came from the integration's stored config, which the device's own Target
SOC entity never updates, so a Target SOC changed on that entity was replaced by
an older one after the run. The run now saves what the device reported before
the raise.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_weekly_full_charge_lifecycle import _controller, _manager


def _zendure(soc_set=95, stored=100):
    return SimpleNamespace(
        name="Zendure",
        # The coordinator copies the reported socSet into max_soc every poll.
        max_soc=soc_set,
        data={"battery_soc": 50, "soc_set": soc_set},
        battery_manual_mode_enabled=False,
        capabilities=SimpleNamespace(hardware_soc_cutoff=True),
        set_charge_cutoff=AsyncMock(return_value=True),
        # The device's Target SOC entity does not update this.
        _config_entry=SimpleNamespace(
            data={"batteries": [{"name": "Zendure", "max_soc": stored}]}
        ),
    )


def _marstek_v2(live=90, stored=95):
    return SimpleNamespace(
        name="Venus v2",
        max_soc=live,
        data={"battery_soc": 50, "charging_cutoff_capacity": live},
        battery_manual_mode_enabled=False,
        capabilities=SimpleNamespace(hardware_soc_cutoff=True),
        set_charge_cutoff=AsyncMock(return_value=True),
        _config_entry=SimpleNamespace(
            data={"batteries": [{"name": "Venus v2", "max_soc": stored}]}
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
async def test_the_run_saves_the_zendures_own_target_soc():
    zendure = _zendure(soc_set=95, stored=100)
    controller, manager = _run(zendure)

    await manager.handle_registers()

    zendure.set_charge_cutoff.assert_awaited_once_with(100)
    # What the device held, not the stale 100 in the stored config.
    assert controller._weekly_charge_saved_max_soc == {"Zendure": 95}


@pytest.mark.asyncio
async def test_the_zendure_is_restored_to_that_value():
    zendure = _zendure(soc_set=100)
    controller, manager = _run(zendure)
    controller._weekly_charge_saved_max_soc = {"Zendure": 95}

    assert await manager._restore_hardware_cutoffs("test") is True

    zendure.set_charge_cutoff.assert_awaited_once_with(95)


@pytest.mark.asyncio
async def test_a_battery_without_its_own_target_soc_keeps_the_stored_config():
    venus = _marstek_v2(live=90, stored=95)
    controller, manager = _run(venus)

    await manager.handle_registers()
    assert controller._weekly_charge_saved_max_soc == {"Venus v2": 95}
    await manager._restore_hardware_cutoffs("test")

    assert [call.args for call in venus.set_charge_cutoff.await_args_list] == [(100,), (95,)]


@pytest.mark.asyncio
async def test_after_a_restart_mid_run_the_saved_target_soc_is_kept():
    """HA restarted while the Zendure held 100 %: the persisted value stays,
    it is not replaced by the 100 the device reports now."""
    zendure = _zendure(soc_set=100)
    controller, manager = _run(zendure)
    controller.weekly_full_charge_registers_written = True
    controller._weekly_charge_saved_max_soc = {"Zendure": 95}

    await manager.handle_registers()

    zendure.set_charge_cutoff.assert_awaited_once_with(100)
    assert controller._weekly_charge_saved_max_soc == {"Zendure": 95}


@pytest.mark.asyncio
async def test_a_zendure_that_reports_100_falls_back_to_the_stored_config():
    """Without a persisted value, a reported 100 % may be the raise itself."""
    zendure = _zendure(soc_set=100, stored=95)
    controller, manager = _run(zendure)

    await manager.handle_registers()

    assert controller._weekly_charge_saved_max_soc == {"Zendure": 95}
