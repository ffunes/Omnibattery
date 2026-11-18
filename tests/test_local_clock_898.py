"""#898: "now" is Home Assistant local time, not the process (OS) clock.

Home Assistant never sets the process time zone, so a Docker/Core install
without TZ runs the OS clock on UTC while price slots are naive HA local time.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing import engine as engine_module
from custom_components.omnibattery.pricing.engine import PricingManager


@pytest.fixture
def brussels_with_utc_os_clock(monkeypatch):
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(ZoneInfo("Europe/Brussels"))
    # 15:10 in Brussels (UTC+1 in November) is 14:10 on the UTC OS clock.
    instant = datetime(2026, 11, 18, 14, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: instant)
    monkeypatch.setattr(
        dt_util, "now", lambda time_zone=None: instant.astimezone(time_zone or dt_util.DEFAULT_TIME_ZONE)
    )

    class _OsClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.replace(tzinfo=None) if tz is None else instant.astimezone(tz)

    monkeypatch.setattr(engine_module, "datetime", _OsClock)
    yield
    dt_util.set_default_time_zone(previous)


def test_current_slot_follows_the_home_assistant_zone(brussels_with_utc_os_clock):
    slot_14 = PriceSlot(datetime(2026, 11, 18, 14, 0), datetime(2026, 11, 18, 14, 15), 0.10)
    slot_15 = PriceSlot(datetime(2026, 11, 18, 15, 0), datetime(2026, 11, 18, 15, 15), 0.20)

    def manager_with(slot):
        ctrl = SimpleNamespace(
            _dynamic_pricing_schedule=SimpleNamespace(selected_slots=[slot])
        )
        return PricingManager(SimpleNamespace(), ctrl)

    manager = manager_with(slot_15)
    assert manager._now() == datetime(2026, 11, 18, 15, 10)
    assert manager.is_in_dynamic_pricing_slot() is True
    assert manager_with(slot_14).is_in_dynamic_pricing_slot() is False
