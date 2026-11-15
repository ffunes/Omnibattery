"""Skip the weekly full charge after a recent full charge (opt-in switch).

A full charge to 100% balances the cells just as the weekly run does. With the
switch on, a run is skipped when every battery already stood full in the five
days before the run day.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.control.weekly_full_charge import (
    WeeklyFullChargeManager,
)

_MODULE = "custom_components.omnibattery.control.weekly_full_charge.datetime"
# Sunday 15 Nov 2026, the configured run day; the five days before it start
# on Tuesday 10 Nov 00:00.
_RUN_DAY = date(2026, 11, 15)
_SINCE = datetime(2026, 11, 10)


def _clock(now: datetime):
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 - same signature as datetime.now
            return now

    return patch(_MODULE, _Clock)


def _battery(name="A", soc=100, manual=False):
    return SimpleNamespace(
        name=name, data={"battery_soc": soc}, battery_manual_mode_enabled=manual
    )


def _controller(skip=True, **overrides):
    values = dict(
        weekly_full_charge_enabled=True,
        weekly_full_charge_day="sun",
        weekly_full_charge_complete=False,
        weekly_full_charge_registers_written=False,
        last_checked_weekday=6,
        _force_full_charge=False,
        _weekly_charge_needs_restore=False,
        _weekly_charge_saved_max_soc={},
        _weekly_charge_status={"state": "Idle"},
        _weekly_full_charge_skip_recent=skip,
        coordinators=[_battery()],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _manager(controller, last_full=None):
    manager = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    manager._controller = controller
    manager._already_complete_logged = False
    manager._cutoff_applied_names = set()
    manager._bms_cutoff_counts = {}
    manager._last_full_at = last_full
    manager._last_full_saved_at = last_full
    return manager


def _active(manager, now=datetime(2026, 11, 15, 10, 0)):
    with _clock(now):
        return manager.is_active()


def test_a_full_charge_in_the_five_days_before_skips_the_run():
    controller = _controller()
    full = datetime(2026, 11, 12, 14, 30)
    manager = _manager(controller, last_full=full)

    assert _active(manager) is False
    assert controller._weekly_charge_status["state"] == "Skipped"
    assert controller._weekly_charge_status["skipped_for_full_charge_at"] == (
        "2026-11-12T14:30"
    )
    # Nothing was written or completed: next week starts from scratch.
    assert controller.weekly_full_charge_complete is False
    assert controller.weekly_full_charge_registers_written is False


def test_with_the_switch_off_the_run_goes_ahead():
    manager = _manager(_controller(skip=False), last_full=datetime(2026, 11, 14, 12, 0))

    assert _active(manager) is True


@pytest.mark.parametrize(
    ("full", "skipped"),
    [
        (_SINCE, True),  # the first moment of the five days
        (_SINCE - timedelta(minutes=1), False),
        (datetime(2026, 11, 8, 15, 0), False),  # last weekend
        (None, False),  # never seen full
    ],
)
def test_only_the_five_days_before_the_run_day_count(full, skipped):
    manager = _manager(_controller(), last_full=full)

    assert _active(manager) is (not skipped)


def test_the_window_is_judged_against_the_run_day_not_the_clock():
    # Full on Tuesday 10 Nov 00:30: still within the five days at Sunday 23:59,
    # though by then more than five days ago.
    manager = _manager(_controller(), last_full=datetime(2026, 11, 10, 0, 30))

    assert _active(manager, datetime(2026, 11, 15, 0, 1)) is False
    assert _active(manager, datetime(2026, 11, 15, 23, 59)) is False


def test_the_night_after_the_run_day_is_skipped_too():
    # The window runs on to Monday's sunrise; it is still Sunday's run.
    controller = _controller(
        _consumption_tracker=SimpleNamespace(calculate_sunrise=lambda _day: 8.0),
        last_checked_weekday=6,
    )
    manager = _manager(controller, last_full=_SINCE + timedelta(hours=1))

    assert _active(manager, datetime(2026, 11, 16, 3, 0)) is False
    assert controller._weekly_charge_status["state"] == "Skipped"


def test_a_run_that_already_raised_the_cutoff_is_not_cut_short():
    # Turning the switch on mid-run leaves the run to finish and restore.
    controller = _controller(weekly_full_charge_registers_written=True)
    manager = _manager(controller, last_full=datetime(2026, 11, 13, 12, 0))

    assert _active(manager) is True


def test_turning_the_switch_off_during_the_window_starts_the_run():
    controller = _controller()
    manager = _manager(controller, last_full=datetime(2026, 11, 13, 12, 0))
    assert _active(manager) is False

    controller._weekly_full_charge_skip_recent = False

    assert _active(manager) is True
    assert controller._weekly_charge_status["state"] == "Idle"
    assert "skipped_for_full_charge_at" not in controller._weekly_charge_status


def test_leaving_the_window_clears_the_skip():
    controller = _controller()
    manager = _manager(controller, last_full=datetime(2026, 11, 13, 12, 0))
    manager.save_state = AsyncMock()
    assert _active(manager) is False

    assert _active(manager, datetime(2026, 11, 17, 10, 0)) is False

    assert controller._weekly_charge_status["state"] == "Idle"
    assert "skipped_for_full_charge_at" not in controller._weekly_charge_status


def test_the_evening_before_a_skipped_run_plans_no_full_charge():
    controller = _controller()
    manager = _manager(controller, last_full=datetime(2026, 11, 13, 12, 0))
    controller._weekly_charge_mgr = manager
    controller.weekly_full_charge_enabled = True
    saturday = datetime(2026, 11, 14, 20, 0)

    assert ChargeDischargeController._weekly_full_charge_tomorrow(controller, saturday) is False

    controller._weekly_full_charge_skip_recent = False
    assert ChargeDischargeController._weekly_full_charge_tomorrow(controller, saturday) is True


def test_the_last_full_charge_is_noted_only_when_every_battery_is_full():
    controller = _controller(coordinators=[_battery("A", 100), _battery("B", 99)])
    manager = _manager(controller)
    now = datetime(2026, 11, 12, 14, 0)

    manager.note_full_charge(now)
    assert manager.last_full_charge_at is None

    controller.coordinators[1].data["battery_soc"] = 100
    manager.note_full_charge(now)
    assert manager.last_full_charge_at == now


def test_a_battery_in_manual_mode_does_not_count():
    controller = _controller(
        coordinators=[_battery("A", 100), _battery("B", 40, manual=True)]
    )
    manager = _manager(controller)

    manager.note_full_charge(datetime(2026, 11, 12, 14, 0))

    assert manager.last_full_charge_at == datetime(2026, 11, 12, 14, 0)


def test_a_confirmed_bms_cutoff_counts_as_full():
    controller = _controller(coordinators=[_battery("A", 99)])
    manager = _manager(controller)
    manager._bms_cutoff_counts = {"A": 99}

    manager.note_full_charge(datetime(2026, 11, 12, 14, 0))

    assert manager.last_full_charge_at == datetime(2026, 11, 12, 14, 0)


def test_the_last_full_charge_is_saved_at_most_hourly():
    manager = _manager(_controller())
    saves = []
    manager._create_background_task = lambda coro, _name: (saves.append(1), coro.close())
    start = datetime(2026, 11, 12, 14, 0)

    manager.note_full_charge(start)
    manager.note_full_charge(start + timedelta(minutes=30))
    manager.note_full_charge(start + timedelta(minutes=61))

    assert len(saves) == 2
    assert manager.last_full_charge_at == start + timedelta(minutes=61)


@pytest.mark.asyncio
async def test_the_last_full_charge_survives_a_restart():
    manager = _manager(_controller())
    store = SimpleNamespace(async_save=AsyncMock(), async_load=AsyncMock())
    manager._last_full_store = store
    manager._last_full_at = datetime(2026, 11, 12, 14, 0)

    await manager._save_last_full()
    store.async_load.return_value = store.async_save.await_args.args[0]

    restored = _manager(_controller())
    restored._last_full_store = store
    await restored.load_last_full()

    assert restored.last_full_charge_at == datetime(2026, 11, 12, 14, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, {}, {"last_full_at": "garbage"}])
async def test_missing_or_broken_storage_leaves_no_full_charge(stored):
    manager = _manager(_controller())
    manager._last_full_store = SimpleNamespace(async_load=AsyncMock(return_value=stored))

    await manager.load_last_full()

    assert manager.last_full_charge_at is None


@pytest.mark.asyncio
async def test_the_switch_stores_the_setting_and_updates_the_controller():
    from unittest.mock import MagicMock

    from custom_components.omnibattery.switch import WeeklyFullChargeSkipRecentSwitch

    controller = _controller(skip=False)
    entry = SimpleNamespace(data={"weekly_full_charge_day": "sun"})
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=MagicMock()))
    switch = WeeklyFullChargeSkipRecentSwitch(hass, entry, controller)
    switch.async_write_ha_state = MagicMock()
    assert switch.is_on is False

    await switch.async_turn_on()

    assert controller._weekly_full_charge_skip_recent is True
    assert switch.is_on is True
    stored = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert stored == {"weekly_full_charge_day": "sun", "weekly_full_charge_skip_recent": True}

    await switch.async_turn_off()
    assert controller._weekly_full_charge_skip_recent is False
