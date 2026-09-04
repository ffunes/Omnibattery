"""Consumption forecasting stays on a 24-hour basis across charge windows."""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.omnibattery.tracking.consumption_tracker import (
    ConsumptionTracker,
)


def _win(start, end, days=None):
    return {"start_time": start, "end_time": end, "days": days or ["mon"]}


def _tracker(slots):
    t = ConsumptionTracker.__new__(ConsumptionTracker)
    t._controller = SimpleNamespace(charging_time_slots=slots)
    return t


def test_charge_windows_do_not_shorten_daily_consumption_basis():
    t = _tracker([_win("02:00:00", "05:00:00"), _win("12:00:00", "14:00:00")])
    assert t.get_consumption_window_hours_per_day() == 24.0


def test_window_hours_per_day_no_slots():
    assert _tracker([]).get_consumption_window_hours_per_day() == 24.0


def test_charge_windows_remain_in_remaining_consumption_range():
    t = _tracker([_win("02:00:00", "05:00:00"), _win("12:00:00", "14:00:00")])
    assert t.consumption_window_hours_in_range(0.0, 12.0) == 12.0


def test_hours_in_range_no_slots_full_range():
    assert _tracker([]).consumption_window_hours_in_range(6.0, 10.0) == 4.0


if __name__ == "__main__":
    import sys

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
            else:
                print(f"ok   {name}")
    sys.exit(1 if failed else 0)
