"""Contracts for bounded, serialized Recorder backfill work."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.tracking.backfill import (
    RecorderBackfillCoordinator,
    local_day_bounds,
)


def test_local_day_bounds_preserve_dst_day_duration():
    madrid = ZoneInfo("Europe/Madrid")

    spring_start, spring_end = local_day_bounds(date(2026, 3, 29), madrid)
    autumn_start, autumn_end = local_day_bounds(date(2026, 10, 25), madrid)

    assert spring_end.timestamp() - spring_start.timestamp() == 23 * 3600
    assert autumn_end.timestamp() - autumn_start.timestamp() == 25 * 3600


@pytest.mark.asyncio
async def test_recorder_queries_are_serialized_and_day_bounded(monkeypatch):
    from homeassistant.components import recorder
    from homeassistant.components.recorder import history

    class _FakeRecorder:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.ranges = []

        async def async_add_executor_job(self, target, *args):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                start, end = args[1:3]
                self.ranges.append((start, end))
                if end.timestamp() - start.timestamp() > 25 * 3600:
                    raise AssertionError("Recorder query exceeded one local day")
                await asyncio.sleep(0)
                return target(*args)
            finally:
                self.active -= 1

    fake = _FakeRecorder()
    monkeypatch.setattr(recorder, "get_instance", lambda _hass: fake)
    monkeypatch.setattr(
        history,
        "state_changes_during_period",
        lambda _hass, _start, _end, entity_id: {entity_id: []},
    )

    hass = SimpleNamespace()
    entry = SimpleNamespace(
        async_create_background_task=lambda _hass, coroutine, name: asyncio.create_task(
            coroutine, name=name
        )
    )
    coordinator = RecorderBackfillCoordinator(hass, entry)
    token = coordinator.new_token()
    start = datetime(2026, 8, 23, tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    results = await asyncio.gather(
        coordinator.async_query(token, "sensor.one", start, end, block="one"),
        coordinator.async_query(token, "sensor.two", start, end, block="two"),
    )

    assert results == [[], []]
    assert fake.max_active == 1
    assert all(
        0 < end.timestamp() - start.timestamp() <= 25 * 3600
        for start, end in fake.ranges
    )
    await coordinator.async_cancel()
