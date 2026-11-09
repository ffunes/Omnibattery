"""Regression tests for weekly full-charge lifecycle persistence and cleanup."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.omnibattery.control.weekly_full_charge import (
    WeeklyFullChargeManager,
)


def _controller(**overrides):
    values = dict(
        weekly_full_charge_enabled=True,
        weekly_full_charge_day="mon",
        weekly_full_charge_complete=False,
        weekly_full_charge_registers_written=True,
        last_checked_weekday=0,
        _force_full_charge=False,
        _weekly_charge_needs_restore=False,
        _weekly_charge_saved_max_soc={"Zendure": 95},
        _weekly_charge_status={"state": "Charging to 100%"},
        _charge_delay_unlocked=False,
        _solar_t_start=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _manager(controller):
    manager = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    manager._controller = controller
    manager._already_complete_logged = False
    manager._cutoff_applied_names = set()
    return manager


def test_exiting_weekly_day_marks_unfinished_run_for_restore():
    controller = _controller()
    manager = _manager(controller)
    manager.save_state = MagicMock()

    # Tuesday immediately after a Monday weekly run.
    with (
        patch("custom_components.omnibattery.control.weekly_full_charge.datetime") as clock,
        patch("custom_components.omnibattery.control.weekly_full_charge.asyncio.create_task"),
    ):
        clock.now.return_value = datetime(2026, 8, 18)
        assert manager.is_active() is False

    assert controller._weekly_charge_needs_restore is True
    assert controller.weekly_full_charge_complete is False
    assert controller.weekly_full_charge_registers_written is False


@pytest.mark.asyncio
async def test_save_state_persists_original_soc_and_restore_pending():
    controller = _controller(_weekly_charge_needs_restore=True)
    store = SimpleNamespace(async_save=AsyncMock())
    manager = _manager(controller)
    manager._store = store

    await manager.save_state()

    payload = store.async_save.await_args.args[0]
    assert payload["saved_max_soc"] == {"Zendure": 95}
    assert payload["restore_pending"] is True


@pytest.mark.asyncio
async def test_load_state_restores_saved_soc_and_pending_restore():
    controller = _controller(_weekly_charge_saved_max_soc={})
    store = SimpleNamespace(
        async_load=AsyncMock(
            return_value={
                "date": date.today().isoformat(),
                "complete": False,
                "registers_written": True,
                "state": "Charging to 100%",
                "restore_pending": True,
                "saved_max_soc": {"Zendure": 95},
                "delay_unlocked": False,
                "solar_t_start": None,
            }
        )
    )
    manager = _manager(controller)
    manager._store = store

    await manager.load_state()

    assert controller._weekly_charge_saved_max_soc == {"Zendure": 95}
    assert controller._weekly_charge_needs_restore is True
    assert controller.weekly_full_charge_registers_written is True


@pytest.mark.asyncio
async def test_load_state_queues_restore_for_unfinished_previous_day():
    controller = _controller(_weekly_charge_saved_max_soc={})
    store = SimpleNamespace(
        async_load=AsyncMock(
            return_value={
                "date": (date.today() - timedelta(days=1)).isoformat(),
                "complete": False,
                "registers_written": True,
                "saved_max_soc": {"Zendure": 95},
            }
        )
    )
    manager = _manager(controller)
    manager._store = store

    await manager.load_state()

    assert controller._weekly_charge_saved_max_soc == {"Zendure": 95}
    assert controller._weekly_charge_needs_restore is True
    assert controller.weekly_full_charge_registers_written is False


@pytest.mark.asyncio
async def test_failed_cutoff_restore_is_reported_for_retry():
    coordinator = SimpleNamespace(
        name="Zendure",
        max_soc=100,
        battery_manual_mode_enabled=False,
        capabilities=SimpleNamespace(hardware_soc_cutoff=True),
        set_charge_cutoff=AsyncMock(return_value=False),
    )
    controller = _controller()
    controller.coordinators = [coordinator]
    controller._is_backup_function_active = lambda _coordinator: False
    manager = _manager(controller)

    restored = await manager._restore_hardware_cutoffs("test")

    assert restored is False
    coordinator.set_charge_cutoff.assert_awaited_once_with(95)


@pytest.mark.asyncio
async def test_restore_uses_persisted_config_when_saved_soc_is_missing():
    coordinator = SimpleNamespace(
        name="Zendure",
        max_soc=100,
        _config_entry=SimpleNamespace(
            data={"batteries": [{"name": "Zendure", "max_soc": 95}]}
        ),
        battery_manual_mode_enabled=False,
        capabilities=SimpleNamespace(hardware_soc_cutoff=True),
        set_charge_cutoff=AsyncMock(return_value=True),
    )
    controller = _controller(_weekly_charge_saved_max_soc={})
    controller.coordinators = [coordinator]
    controller._is_backup_function_active = lambda _coordinator: False
    manager = _manager(controller)

    restored = await manager._restore_hardware_cutoffs("legacy state")

    assert restored is True
    coordinator.set_charge_cutoff.assert_awaited_once_with(95)


@pytest.mark.asyncio
async def test_pending_restore_runs_before_weekly_state_is_evaluated():
    coordinator = SimpleNamespace(
        name="Zendure",
        max_soc=100,
        battery_manual_mode_enabled=False,
        capabilities=SimpleNamespace(hardware_soc_cutoff=True),
        set_charge_cutoff=AsyncMock(return_value=True),
    )
    controller = _controller(_weekly_charge_needs_restore=True)
    controller.coordinators = [coordinator]
    controller._is_backup_function_active = lambda _coordinator: False
    manager = _manager(controller)
    manager.tick_bms_cutoff = MagicMock()
    manager.is_active = MagicMock(return_value=False)
    manager.save_state = AsyncMock()

    await manager.handle_registers()

    coordinator.set_charge_cutoff.assert_awaited_once_with(95)
    assert controller._weekly_charge_needs_restore is False
    assert controller._weekly_charge_saved_max_soc == {}


# ----------------------------------------------------------------------
# The night after the target day: the run ends at sunrise, not at midnight
# ----------------------------------------------------------------------

from custom_components.omnibattery.control import weekly_full_charge as weekly_module  # noqa: E402

MONDAY = datetime(2026, 8, 17)  # the target day in _controller()
SUNRISE_H = 7.5


def _clock(monkeypatch, moment):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr(weekly_module, "datetime", FixedDateTime)


def _night_manager(**overrides):
    overrides.setdefault(
        "_consumption_tracker",
        SimpleNamespace(calculate_sunrise=lambda _day: SUNRISE_H),
    )
    controller = _controller(**overrides)
    manager = _manager(controller)
    manager.save_state = MagicMock()
    manager._create_background_task = MagicMock()
    return controller, manager


def test_an_unfinished_run_carries_on_in_the_night_after_the_target_day(monkeypatch):
    """The gap is bought after the sun, so the cheap night belongs to the run."""
    controller, manager = _night_manager()
    _clock(monkeypatch, MONDAY + timedelta(days=1, hours=2))

    assert manager.is_active() is True
    assert controller.weekly_full_charge_registers_written is True
    assert controller._weekly_charge_needs_restore is False


def test_the_run_ends_at_the_next_sunrise(monkeypatch):
    controller, manager = _night_manager()
    _clock(monkeypatch, MONDAY + timedelta(days=1, hours=2))
    assert manager.is_active() is True

    _clock(monkeypatch, MONDAY + timedelta(days=1, hours=8))
    assert manager.is_active() is False
    assert controller._weekly_charge_needs_restore is True
    assert controller.weekly_full_charge_registers_written is False


def test_a_finished_run_is_not_reopened_in_the_night(monkeypatch):
    controller, manager = _night_manager(weekly_full_charge_complete=True)
    _clock(monkeypatch, MONDAY + timedelta(days=1, hours=2))

    assert manager.is_active() is False
    assert controller._weekly_charge_needs_restore is False


@pytest.mark.parametrize(
    "moment, expected",
    [
        (MONDAY + timedelta(hours=15), MONDAY + timedelta(days=1, hours=SUNRISE_H)),
        (MONDAY + timedelta(days=1, hours=2), MONDAY + timedelta(days=1, hours=SUNRISE_H)),
        (MONDAY + timedelta(days=1, hours=9), None),
        (MONDAY + timedelta(days=2, hours=2), None),
    ],
)
def test_the_window_ends_at_the_sunrise_after_the_target_day(moment, expected):
    _controller_, manager = _night_manager()

    assert manager.window_end(moment) == expected


def test_without_a_known_sunrise_the_run_still_ends_at_midnight(monkeypatch):
    controller, manager = _night_manager(
        _consumption_tracker=SimpleNamespace(calculate_sunrise=lambda _day: None)
    )
    _clock(monkeypatch, MONDAY + timedelta(days=1, hours=2))

    assert manager.is_active() is False
    assert controller._weekly_charge_needs_restore is True


@pytest.mark.asyncio
async def test_a_restart_in_the_night_keeps_the_run(monkeypatch):
    today = date.today()
    yesterday = today - timedelta(days=1)
    day_names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    controller, manager = _night_manager(
        weekly_full_charge_day=day_names[yesterday.weekday()],
        weekly_full_charge_registers_written=False,
        _weekly_charge_saved_max_soc={},
    )
    _clock(monkeypatch, datetime.combine(today, datetime.min.time()) + timedelta(hours=2))
    manager._store = SimpleNamespace(
        async_load=AsyncMock(
            return_value={
                "date": yesterday.isoformat(),
                "complete": False,
                "registers_written": True,
                "saved_max_soc": {"Zendure": 95},
            }
        )
    )

    await manager.load_state()

    assert controller.weekly_full_charge_registers_written is True
    assert controller._weekly_charge_needs_restore is False
