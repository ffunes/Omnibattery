"""Batteries are set up concurrently, one link at a time (#560).

Setup used to connect, configure and first-refresh every battery in turn, so
four proxied batteries held HA startup ~70 s. Batteries on different links now
set up concurrently; batteries sharing a link (gateway slave ids, a single-slot
V3/EW11B port) stay sequential so they do not fight over the one TCP slot.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.omnibattery import _setup_batteries_per_link


def _battery(host, port=502, slave=1):
    return {"host": host, "port": port, "slave_id": slave}


def _recording_setup(fail_index=None):
    """A setup_battery stub that records how many run at once per link."""
    active: dict = {}
    peak: dict = {}
    created: list = []

    async def setup(index, config):
        link = (config["host"], config["port"])
        active[link] = active.get(link, 0) + 1
        peak[link] = max(peak.get(link, 0), active[link])
        peak["total"] = max(peak.get("total", 0), sum(
            v for k, v in active.items() if k != "total"
        ))
        await asyncio.sleep(0.01)
        active[link] -= 1
        if index == fail_index:
            raise RuntimeError("boom")
        coordinator = SimpleNamespace(index=index, disconnect=AsyncMock())
        created.append(coordinator)
        return coordinator

    return setup, peak, created


def test_batteries_on_different_links_set_up_concurrently_in_config_order():
    batteries = [_battery("10.0.0.1"), _battery("10.0.0.2"), _battery("10.0.0.3")]
    setup, peak, _ = _recording_setup()

    coordinators = asyncio.run(_setup_batteries_per_link(batteries, setup))

    assert [c.index for c in coordinators] == [0, 1, 2]
    assert peak["total"] == 3


def test_batteries_sharing_a_link_set_up_one_at_a_time():
    batteries = [
        _battery("10.0.0.1", slave=1),
        _battery("10.0.0.2"),
        _battery("10.0.0.1", slave=2),
    ]
    setup, peak, _ = _recording_setup()

    coordinators = asyncio.run(_setup_batteries_per_link(batteries, setup))

    assert [c.index for c in coordinators] == [0, 1, 2]
    assert peak[("10.0.0.1", 502)] == 1
    assert peak["total"] == 2


def test_a_failed_battery_releases_the_ones_already_set_up():
    batteries = [_battery("10.0.0.1"), _battery("10.0.0.2")]
    setup, _, created = _recording_setup(fail_index=1)

    with pytest.raises(RuntimeError):
        asyncio.run(_setup_batteries_per_link(batteries, setup))

    assert [c.index for c in created] == [0]
    created[0].disconnect.assert_awaited_once()
