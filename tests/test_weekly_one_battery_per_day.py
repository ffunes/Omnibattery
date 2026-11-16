"""Weekly full charge, one battery per day (opt-in switch).

A fleet that cannot reach 100% on one winter day can still fill one battery.
With the switch on, the run lasts one day per automatic battery (at most a
week) from the full charge day, and each day charges only the battery that went
longest without a full charge.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.control.weekly_full_charge import (
    WeeklyFullChargeManager,
)
from custom_components.omnibattery.pricing.engine import PricingManager

_MODULE = "custom_components.omnibattery.control.weekly_full_charge.datetime"
# Sunday 15 Nov 2026 is the full charge day.
_SUNDAY = datetime(2026, 11, 15, 10, 0)


def _clock(now: datetime):
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 - same signature as datetime.now
            return now

    return patch(_MODULE, _Clock)


class _Battery(SimpleNamespace):
    """A coordinator stand-in, hashable like the real one (dicts key on it)."""

    __hash__ = object.__hash__


def _battery(name, soc=50, manual=False, hardware=False):
    return _Battery(
        name=name,
        data={"battery_soc": soc, "battery_total_energy": 10.0},
        battery_manual_mode_enabled=manual,
        max_soc=90,
        capabilities=SimpleNamespace(hardware_soc_cutoff=hardware),
        set_charge_cutoff=AsyncMock(return_value=True),
        enable_charge_hysteresis=False,
    )


def _controller(batteries, **overrides):
    values = dict(
        weekly_full_charge_enabled=True,
        weekly_full_charge_day="sun",
        weekly_full_charge_complete=False,
        weekly_full_charge_registers_written=False,
        last_checked_weekday=None,
        _force_full_charge=False,
        _weekly_charge_needs_restore=False,
        _weekly_charge_saved_max_soc={},
        _weekly_charge_status={"state": "Idle"},
        _weekly_full_charge_skip_recent=False,
        _weekly_full_charge_one_per_day=True,
        _consumption_tracker=SimpleNamespace(calculate_sunrise=lambda _day: 8.0),
        coordinators=list(batteries),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _manager(controller, last_full=None):
    manager = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    manager._controller = controller
    manager._already_complete_logged = False
    manager._cutoff_applied_names = set()
    manager._bms_cutoff_counts = {}
    manager._last_full_at = None
    manager._last_full_saved_at = None
    manager._last_full_by_battery = dict(last_full or {})
    manager._create_background_task = lambda coro, _name: coro.close()
    controller._weekly_charge_mgr = manager
    return manager


def _active(manager, now=_SUNDAY):
    with _clock(now):
        return manager.is_active()


def _three():
    return [_battery("A"), _battery("B"), _battery("C")]


# --- the window -------------------------------------------------------------


def test_the_run_lasts_one_day_per_automatic_battery():
    batteries = _three() + [_battery("M", manual=True)]
    manager = _manager(_controller(batteries))

    assert manager.rotation_days() == 3


def test_the_run_lasts_at_most_a_week():
    manager = _manager(_controller([_battery(str(i)) for i in range(9)]))

    assert manager.rotation_days() == 7


@pytest.mark.parametrize(
    ("now", "start"),
    [
        (datetime(2026, 11, 15, 0, 0), date(2026, 11, 15)),  # Sunday
        (datetime(2026, 11, 17, 23, 0), date(2026, 11, 15)),  # Tuesday, third day
        (datetime(2026, 11, 18, 7, 0), date(2026, 11, 15)),  # the night after it
        (datetime(2026, 11, 18, 9, 0), None),  # Wednesday after sunrise
        (datetime(2026, 11, 14, 12, 0), None),  # Saturday before
    ],
)
def test_the_window_runs_from_the_full_charge_day_through_the_last_night(now, start):
    manager = _manager(_controller(_three()))

    assert manager.rotation_start(now) == start


def test_the_window_ends_at_the_sunrise_after_the_last_day():
    manager = _manager(_controller(_three()))

    assert manager.window_end(datetime(2026, 11, 16, 12, 0)) == datetime(2026, 11, 18, 8, 0)
    assert manager.in_overnight_extension(datetime(2026, 11, 18, 3, 0)) is True
    assert manager.in_overnight_extension(datetime(2026, 11, 17, 3, 0)) is False


# --- whose turn -------------------------------------------------------------


def test_the_battery_longest_without_a_full_charge_goes_first():
    batteries = _three()
    manager = _manager(
        _controller(batteries),
        last_full={"A": datetime(2026, 11, 10), "B": datetime(2026, 11, 2)},
    )

    assert _active(manager) is True
    # C was never seen full, so it is the oldest.
    assert [manager.runs_for(b) for b in batteries] == [False, False, True]


def test_ties_go_in_configured_order():
    batteries = _three()
    manager = _manager(_controller(batteries))

    assert _active(manager) is True
    assert manager.runs_for(batteries[0]) is True
    assert manager.runs_for(batteries[1]) is False


def test_outside_the_mode_every_battery_is_in_the_run():
    batteries = _three()
    manager = _manager(_controller(batteries, _weekly_full_charge_one_per_day=False))

    assert all(manager.runs_for(b) for b in batteries)


@pytest.mark.asyncio
async def test_a_full_battery_ends_its_day_and_the_next_one_follows_tomorrow():
    batteries = _three()
    controller = _controller(batteries)
    manager = _manager(controller)
    manager._complete_weekly_charge = AsyncMock()
    manager.save_state = AsyncMock()
    assert _active(manager) is True  # A's turn

    with _clock(datetime(2026, 11, 15, 14, 0)):
        await manager._complete_rotation_day()

    assert manager._rot_done == {"A": "full"}
    assert controller.weekly_full_charge_complete is False
    assert controller.weekly_full_charge_registers_written is False
    # The rest of Sunday charges nothing more.
    assert _active(manager, datetime(2026, 11, 15, 18, 0)) is False
    assert not any(manager.runs_for(b) for b in batteries)
    # Monday is B's turn.
    assert _active(manager, datetime(2026, 11, 16, 10, 0)) is True
    assert manager.runs_for(batteries[1]) is True


@pytest.mark.asyncio
async def test_after_every_battery_had_its_turn_the_run_is_complete():
    batteries = [_battery("A"), _battery("B")]
    controller = _controller(batteries)
    manager = _manager(controller)
    manager._complete_weekly_charge = AsyncMock()
    manager.save_state = AsyncMock()

    for day in (15, 16):
        assert _active(manager, datetime(2026, 11, day, 10, 0)) is True
        with _clock(datetime(2026, 11, day, 14, 0)):
            await manager._complete_rotation_day()

    assert controller.weekly_full_charge_complete is True
    assert controller._weekly_charge_status["completion_reason"] == "every_battery_had_its_turn"
    # The night after the last day charges nothing.
    assert _active(manager, datetime(2026, 11, 17, 3, 0)) is False


def test_a_battery_that_did_not_get_full_stays_first_the_next_day():
    batteries = _three()
    manager = _manager(_controller(batteries))
    assert _active(manager) is True  # A, Sunday

    assert _active(manager, datetime(2026, 11, 16, 10, 0)) is True

    assert manager.runs_for(batteries[0]) is True
    assert manager._rot_done == {}


def test_a_battery_full_before_its_turn_is_chosen_was_already_charged():
    # A reached 100% on its own (sun) just before the run picked a battery:
    # it is the most recently full, so another battery gets the turn.
    batteries = [_battery("A", soc=100), _battery("B", soc=30)]
    manager = _manager(_controller(batteries))
    manager.note_full_charge(_SUNDAY - timedelta(minutes=5))

    assert _active(manager) is True

    assert manager.runs_for(batteries[1]) is True


def test_a_new_rotation_starts_fresh_next_week():
    batteries = _three()
    controller = _controller(batteries)
    manager = _manager(controller)
    assert _active(manager) is True
    manager._rot_done = {"A": "full"}

    # Wednesday: outside the window; the rotation is over.
    assert _active(manager, datetime(2026, 11, 18, 12, 0)) is False
    assert manager._rot_start is None and manager._rot_done == {}
    # Next Sunday starts again.
    assert _active(manager, datetime(2026, 11, 22, 10, 0)) is True


def test_leaving_the_window_mid_charge_restores_the_cutoff():
    controller = _controller(_three())
    manager = _manager(controller)
    assert _active(manager) is True
    controller.weekly_full_charge_registers_written = True

    assert _active(manager, datetime(2026, 11, 18, 12, 0)) is False

    assert controller._weekly_charge_needs_restore is True
    assert controller.weekly_full_charge_registers_written is False


# --- skip after a recent full charge, per battery ----------------------------


def test_a_battery_full_in_the_days_before_needs_no_turn():
    batteries = _three()
    manager = _manager(
        _controller(batteries, _weekly_full_charge_skip_recent=True),
        last_full={"A": datetime(2026, 11, 13, 12, 0)},  # Friday
    )

    assert _active(manager) is True

    assert manager._rot_done == {"A": "recent"}
    assert manager.runs_for(batteries[1]) is True


def test_when_every_battery_was_full_recently_the_run_is_skipped():
    batteries = [_battery("A"), _battery("B")]
    controller = _controller(batteries, _weekly_full_charge_skip_recent=True)
    manager = _manager(
        controller,
        last_full={"A": datetime(2026, 11, 13), "B": datetime(2026, 11, 14)},
    )

    assert _active(manager) is False

    assert controller.weekly_full_charge_complete is True
    assert controller._weekly_charge_status["state"] == "Skipped"


# --- the record of full charges ---------------------------------------------


def test_each_battery_records_its_own_full_charge():
    batteries = [_battery("A", soc=100), _battery("B", soc=60)]
    manager = _manager(_controller(batteries))
    now = datetime(2026, 11, 12, 14, 0)

    manager.note_full_charge(now)

    assert manager.last_full_by_battery == {"A": now}
    assert manager.last_full_charge_at is None  # the fleet was not full


@pytest.mark.asyncio
async def test_the_rotation_and_the_full_charges_survive_a_restart():
    batteries = _three()
    manager = _manager(_controller(batteries), last_full={"A": datetime(2026, 11, 15, 13, 0)})
    assert _active(manager) is True
    manager._rot_done = {"A": "full"}
    manager._rot_day_done = date(2026, 11, 15)
    store = SimpleNamespace(async_save=AsyncMock(), async_load=AsyncMock())
    manager._last_full_store = store

    await manager._save_last_full()
    store.async_load.return_value = store.async_save.await_args.args[0]
    restored = _manager(_controller(batteries))
    restored._last_full_store = store
    await restored.load_last_full()

    assert restored._rot_start == date(2026, 11, 15)
    assert restored._rot_done == {"A": "full"}
    assert restored._rot_day_done == date(2026, 11, 15)
    assert restored.last_full_by_battery == {"A": datetime(2026, 11, 15, 13, 0)}
    # Still Sunday after the restart: A's day is done, nothing charges.
    assert _active(restored, datetime(2026, 11, 15, 16, 0)) is False


# --- what the run charges -----------------------------------------------------


def test_only_the_battery_whose_turn_it_is_charges_to_100_percent():
    batteries = [_battery("A", soc=40), _battery("B", soc=70)]
    controller = _controller(batteries)
    manager = _manager(controller)
    assert _active(manager) is True  # A
    controller._weekly_full_charge_runs_for = manager.runs_for
    controller._weekly_full_charge_pending = lambda: True

    ceiling = ChargeDischargeController._charge_ceiling_soc
    assert ceiling(controller, batteries[0]) == 100.0
    assert ceiling(controller, batteries[1]) == 90.0
    # The gap to buy is A's alone: 60% of 10 kWh.
    assert ChargeDischargeController._weekly_full_charge_gap_kwh(
        controller, batteries
    ) == pytest.approx(6.0)


def test_the_evening_before_each_rotation_day_plans_a_full_charge():
    batteries = _three()
    controller = _controller(batteries)
    manager = _manager(controller)

    tomorrow = ChargeDischargeController._weekly_full_charge_tomorrow
    assert tomorrow(controller, datetime(2026, 11, 14, 20, 0)) is True  # Saturday
    assert _active(manager) is True
    assert tomorrow(controller, datetime(2026, 11, 16, 20, 0)) is True  # Monday -> Tuesday
    assert tomorrow(controller, datetime(2026, 11, 17, 20, 0)) is False  # Tuesday -> Wednesday


@pytest.mark.asyncio
async def test_the_hardware_cutoff_is_raised_only_for_the_battery_whose_turn_it_is():
    batteries = [_battery("A", hardware=True), _battery("B", hardware=True)]
    controller = _controller(
        batteries,
        charge_delay_enabled=False,
        _charge_delay_unlocked=True,
        _is_backup_function_active=lambda _c: False,
        _balance_monitor_overrides_delay=lambda: False,
    )
    manager = _manager(controller)
    manager.tick_bms_cutoff = lambda: None
    manager.save_state = AsyncMock()

    with _clock(_SUNDAY):
        await manager.handle_registers()

    batteries[0].set_charge_cutoff.assert_awaited_once_with(100)
    batteries[1].set_charge_cutoff.assert_not_awaited()
    assert controller._weekly_charge_saved_max_soc == {"A": 90}


@pytest.mark.asyncio
async def test_the_day_completes_when_its_battery_is_full_whatever_the_others():
    batteries = [_battery("A", soc=100), _battery("B", soc=30)]
    controller = _controller(
        batteries,
        weekly_full_charge_registers_written=True,
        _weekly_charge_saved_max_soc={"A": 90},
        charge_delay_enabled=False,
        _charge_delay_unlocked=True,
        _is_backup_function_active=lambda _c: False,
        _balance_monitor_overrides_delay=lambda: False,
    )
    manager = _manager(controller)
    manager._cutoff_applied_names = {"A"}
    manager.tick_bms_cutoff = lambda: None
    manager.save_state = AsyncMock()
    manager.is_battery_full = lambda c: c.data["battery_soc"] >= 100
    batteries[0].data["battery_soc"] = 60
    assert _active(manager) is True  # A's turn, chosen while it was filling
    batteries[0].data["battery_soc"] = 100

    with _clock(_SUNDAY + timedelta(hours=3)):
        await manager.handle_registers()

    assert manager._rot_done == {"A": "full"}
    assert manager._rot_day_done == date(2026, 11, 15)
    assert controller.weekly_full_charge_complete is False


def test_only_the_battery_being_filled_is_held_from_discharging():
    batteries = [_battery("A", soc=60), _battery("B", soc=80)]
    controller = _controller(batteries)
    manager = _manager(controller)
    assert _active(manager) is True  # A
    blocks = {}
    controller._weekly_full_charge_runs_for = manager.runs_for
    controller.set_discharge_block = lambda key, _src, _d, coordinator=None: blocks.__setitem__(
        (key, getattr(coordinator, "name", None)), True
    )
    controller.remove_discharge_block = lambda key, coordinator=None: blocks.pop(
        (key, getattr(coordinator, "name", None)), None
    )
    pricing = PricingManager.__new__(PricingManager)
    pricing._controller = controller
    pricing._weekly_refill_slots = lambda _now: [SimpleNamespace(start=datetime(2099, 1, 1))]
    pricing._weekly_refill_threshold = lambda _slots: 0.30
    pricing._get_current_price = lambda: 0.20  # cheaper than refilling: hold

    pricing.apply_weekly_refill_hold()

    assert blocks == {("weekly_refill_hold", "A"): True}


@pytest.mark.asyncio
async def test_the_switch_stores_the_setting():
    from custom_components.omnibattery.switch import WeeklyFullChargeOnePerDaySwitch

    controller = _controller(_three(), _weekly_full_charge_one_per_day=False)
    entry = SimpleNamespace(data={"weekly_full_charge_day": "sun"})
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=MagicMock()))
    switch = WeeklyFullChargeOnePerDaySwitch(hass, entry, controller)
    switch.async_write_ha_state = MagicMock()

    await switch.async_turn_on()

    assert controller._weekly_full_charge_one_per_day is True
    assert hass.config_entries.async_update_entry.call_args.kwargs["data"] == {
        "weekly_full_charge_day": "sun",
        "weekly_full_charge_one_battery_per_day": True,
    }
