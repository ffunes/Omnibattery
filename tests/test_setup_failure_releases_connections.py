"""A failed or cancelled setup releases the battery connections it opened.

Home Assistant never unloads an entry whose setup raised or was cancelled. An
options-flow save reloads the entry inside the flow step; when the client gave
up on that request after 30 s, the setup was cancelled in the balance monitor,
after the batteries had connected. The Marstek v3 accepts one Modbus TCP
client, and the orphaned connection refused every later reload ("Failed to
connect to Modbus server") for two hours, until Home Assistant restarted.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.omnibattery as omnibattery
from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
)


def _coordinator():
    return SimpleNamespace(set_shutting_down=MagicMock(), disconnect=AsyncMock())


def _setup_that(fail_with, monkeypatch, count=2):
    """Stand in for the setup body: open ``count`` batteries, then fail."""
    created = [_coordinator() for _ in range(count)]

    async def fake_setup(hass, entry, opened):
        opened.extend(created)
        if fail_with is not None:
            raise fail_with
        return True

    monkeypatch.setattr(omnibattery, "_async_setup_entry", fake_setup)
    return created


@pytest.mark.parametrize(
    "error",
    [asyncio.CancelledError(), RuntimeError("balance monitor"), omnibattery.ConfigEntryNotReady("later")],
)
async def test_a_failed_setup_releases_every_battery_connection(monkeypatch, error):
    created = _setup_that(error, monkeypatch)

    with pytest.raises(type(error)):
        await omnibattery.async_setup_entry(MagicMock(), MagicMock())

    for coordinator in created:
        coordinator.set_shutting_down.assert_called_once_with(True)
        coordinator.disconnect.assert_awaited_once()


async def test_one_failing_release_does_not_keep_the_others_open(monkeypatch):
    created = _setup_that(asyncio.CancelledError(), monkeypatch)
    created[0].disconnect.side_effect = OSError("already gone")

    with pytest.raises(asyncio.CancelledError):
        await omnibattery.async_setup_entry(MagicMock(), MagicMock())

    created[1].disconnect.assert_awaited_once()


async def test_a_setup_that_works_keeps_its_connections(monkeypatch):
    created = _setup_that(None, monkeypatch)

    assert await omnibattery.async_setup_entry(MagicMock(), MagicMock()) is True

    for coordinator in created:
        coordinator.set_shutting_down.assert_not_called()
        coordinator.disconnect.assert_not_awaited()


async def test_a_coordinator_being_torn_down_does_not_reconnect():
    """The control loop's non-delivery repair reconnects fresh; on a released
    coordinator that would take the single slot again."""
    released = SimpleNamespace(
        _is_shutting_down=True,
        driver=SimpleNamespace(connect=AsyncMock(return_value=True)),
    )

    assert await MarstekVenusDataUpdateCoordinator.async_reconnect_fresh(released) is False
    released.driver.connect.assert_not_awaited()
