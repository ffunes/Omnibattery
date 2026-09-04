"""Regressions for the daily-operation solar window."""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.control.charge_delay import ChargeDelayManager
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.switch import ChargeDelaySwitch
from custom_components.omnibattery.tracking import consumption_tracker as tracker_module
from custom_components.omnibattery.tracking.consumption_tracker import (
    ConsumptionTracker,
)


AMSTERDAM = ZoneInfo("Europe/Amsterdam")


@pytest.mark.asyncio
async def test_disabled_charge_delay_does_not_limit_solar_window_to_next_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled Charge Delay must not constrain the dashboard solar window."""
    now = datetime(2026, 8, 29, 16, 30, tzinfo=AMSTERDAM)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return now.replace(tzinfo=None)
            return now.astimezone(tz)

    monkeypatch.setattr(tracker_module, "datetime", FrozenDateTime)

    controller = SimpleNamespace(
        charge_delay_enabled=True,
        _charge_delay_status={"state": "Idle"},
        # Simulate a late marker retained from a previous Charge Delay run.
        _solar_t_start=17.0,
        coordinators=[SimpleNamespace(data={"battery_power": 1_000})],
    )
    switch = ChargeDelaySwitch.__new__(ChargeDelaySwitch)
    switch.controller = controller
    switch.entry = SimpleNamespace(data={})
    switch.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=lambda *_args, **_kwargs: None)
    )
    switch.async_write_ha_state = lambda: None

    await switch.async_turn_off()
    assert controller.charge_delay_enabled is False

    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._controller = controller
    tracker.calculate_sunrise = lambda: 7.0
    tracker.calculate_solar_noon = lambda: 13.0
    tracker.solar_profile = SimpleNamespace(_days={})
    controller._consumption_tracker = tracker

    manager = PricingManager(SimpleNamespace(), controller)
    solar_start, solar_end = manager._solar_timeline_window(now, tracker)

    assert solar_start == datetime(2026, 8, 29, 7, 0, tzinfo=AMSTERDAM)
    # The astronomical 07:00 start mirrored around 13:00 gives a 19:00 end. The
    # retained 17:00 Charge Delay marker currently produces 09:00 and then the
    # active-charge extension replaces it with 17:30 (``now + 1 hour``).
    assert solar_end == datetime(2026, 8, 29, 19, 0, tzinfo=AMSTERDAM)


def test_charge_delay_disabled_across_midnight_does_not_limit_solar_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale marker surviving a disabled daily reset must remain isolated."""
    now = datetime(2026, 8, 29, 16, 30, tzinfo=AMSTERDAM)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return now.replace(tzinfo=None)
            return now.astimezone(tz)

    monkeypatch.setattr(tracker_module, "datetime", FrozenDateTime)

    controller = SimpleNamespace(
        charge_delay_enabled=False,
        _charge_delay_last_date=date(2026, 8, 28),
        _solar_t_start=17.0,
        coordinators=[SimpleNamespace(data={"battery_power": 1_000})],
    )
    delay_manager = ChargeDelayManager.__new__(ChargeDelayManager)
    delay_manager._controller = controller
    delay_manager.handle_daily_reset_and_eval()

    assert controller._solar_t_start == 17.0

    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._controller = controller
    tracker.calculate_solar_noon = lambda: 13.0
    tracker.solar_profile = SimpleNamespace(
        _days={
            date(2026, 8, 29): SimpleNamespace(
                solar_start=datetime(2026, 8, 29, 7, 0, tzinfo=AMSTERDAM),
                solar_end=now,
                complete=False,
            )
        }
    )

    manager = PricingManager(SimpleNamespace(), controller)
    solar_start, solar_end = manager._solar_timeline_window(now, tracker)

    assert solar_start == datetime(2026, 8, 29, 7, 0, tzinfo=AMSTERDAM)
    assert solar_end == datetime(2026, 8, 29, 19, 0, tzinfo=AMSTERDAM)
