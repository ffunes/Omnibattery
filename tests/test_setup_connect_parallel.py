"""Tests for the per-battery connect step of async_setup_entry.

The batteries are connected concurrently, so the start-up time follows the
slowest battery instead of the sum over all of them.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.omnibattery import _async_connect_and_configure

BATTERY_CONFIG = {
    "name": "Battery",
    "max_soc": 100,
    "min_soc": 12,
    "max_charge_power": 2500,
    "max_discharge_power": 2500,
}


class FakeCoordinator:
    """Just enough of the coordinator for the connect step."""

    def __init__(self, delay: float = 0.0, connects: bool = True, apply_error=None):
        self.host = "127.0.0.1"
        self.port = 502
        self.brand = "marstek"
        self.battery_version = "vD"
        self.rs485_user_disabled = True
        self.capabilities = SimpleNamespace(has_rs485_control=False)
        self.reload_entry_when_reachable = False
        self._consecutive_failures = 0
        self.data = None
        self.refreshed = False
        self.disconnected = False
        self._delay = delay
        self._connects = connects
        self._apply_error = apply_error

    async def connect(self) -> bool:
        await asyncio.sleep(self._delay)
        return self._connects

    async def disconnect(self) -> None:
        self.disconnected = True

    async def apply_config(self, **kwargs) -> None:
        if self._apply_error is not None:
            raise self._apply_error

    async def async_request_refresh(self) -> None:
        self.refreshed = True


async def test_batteries_connect_concurrently():
    """Four batteries that take 0.3 s each finish in about 0.3 s, not 1.2 s."""
    coordinators = [FakeCoordinator(delay=0.3) for _ in range(4)]

    started = time.monotonic()
    await asyncio.gather(
        *(_async_connect_and_configure(c, BATTERY_CONFIG) for c in coordinators)
    )
    elapsed = time.monotonic() - started

    assert all(c.refreshed for c in coordinators)
    # Each battery also sleeps 0.5 s after its first refresh.
    assert elapsed < 1.0


async def test_unreachable_battery_starts_unreachable(monkeypatch):
    """A battery that never answers is flagged for a reload and does not raise."""
    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    coordinator = FakeCoordinator(connects=False)

    await _async_connect_and_configure(coordinator, BATTERY_CONFIG)

    assert coordinator.reload_entry_when_reachable is True
    assert coordinator._consecutive_failures == 1
    assert coordinator.data == {}
    assert coordinator.refreshed is False


async def test_failed_initial_configuration_raises_not_ready():
    """A battery that connects but rejects its configuration fails the setup."""
    coordinator = FakeCoordinator(apply_error=RuntimeError("write refused"))

    with pytest.raises(ConfigEntryNotReady):
        await _async_connect_and_configure(coordinator, BATTERY_CONFIG)

    assert coordinator.disconnected is True
