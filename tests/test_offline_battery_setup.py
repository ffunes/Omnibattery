"""A battery that is switched off at setup must not take the system down.

Setup used to raise ``ConfigEntryNotReady`` when a battery did not answer, so
one battery on its side switch removed every other battery, the controller and
the dashboard, and the entry stayed in a setup-retry loop for as long as the
device stayed off. The battery is now set up unreachable, and the entry is
reloaded once it answers — the only path that redoes the hardware configuration
write, the connect-time entity definitions and the first telemetry read.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from custom_components.omnibattery.const import DOMAIN
from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
    _schedule_setup_reload_if_deferred,
)


def _deferred_coordinator(
    *, deferred: bool, entry=SimpleNamespace(entry_id="abc"), hass=None
):
    """Only the attributes the deferred-reload helper touches."""
    return SimpleNamespace(
        name="Battery 1",
        reload_entry_when_reachable=deferred,
        _config_entry=entry,
        hass=hass if hass is not None else _hass(),
    )


def _hass(runtime=None):
    """A hass stub with the entry runtime dict the dedupe marker lives in."""
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_schedule_reload=Mock()),
        data={DOMAIN: {"abc": runtime}} if runtime is not None else {},
    )


def _schedule(coordinator):
    _schedule_setup_reload_if_deferred(coordinator)


def test_a_battery_that_started_unreachable_reloads_the_entry_when_it_answers():
    coordinator = _deferred_coordinator(deferred=True)

    _schedule(coordinator)

    coordinator.hass.config_entries.async_schedule_reload.assert_called_once_with("abc")
    assert coordinator.reload_entry_when_reachable is False


def test_the_reload_fires_once_and_not_on_every_recovery():
    coordinator = _deferred_coordinator(deferred=True)

    _schedule(coordinator)
    _schedule(coordinator)

    assert coordinator.hass.config_entries.async_schedule_reload.call_count == 1


def test_a_battery_that_was_present_at_setup_never_reloads_the_entry():
    coordinator = _deferred_coordinator(deferred=False)

    _schedule(coordinator)

    coordinator.hass.config_entries.async_schedule_reload.assert_not_called()


def _reconnecting_coordinator(*, deferred: bool):
    """A coordinator whose driver reconnects, with RS485 out of the way."""
    return SimpleNamespace(
        name="Battery 1",
        host="192.0.2.10",
        port=502,
        lock=asyncio.Lock(),
        _consecutive_failures=5,
        _is_connected=False,
        _suspension_reset_time=object(),
        _last_rs485_reenable_success=None,
        _last_update_times={},
        _critical_group_failures={},
        rs485_user_disabled=True,
        capabilities=SimpleNamespace(has_rs485_control=False),
        reload_entry_when_reachable=deferred,
        _config_entry=SimpleNamespace(entry_id="abc"),
        hass=_hass(),
        driver=SimpleNamespace(connect=AsyncMock(return_value=True)),
    )


def test_a_successful_reconnection_carries_the_deferred_reload():
    coordinator = _reconnecting_coordinator(deferred=True)

    reconnected = asyncio.run(
        MarstekVenusDataUpdateCoordinator.async_reconnect_fresh(coordinator)
    )

    assert reconnected is True
    coordinator.hass.config_entries.async_schedule_reload.assert_called_once_with("abc")


def test_a_failed_reconnection_leaves_the_deferred_reload_armed():
    coordinator = _reconnecting_coordinator(deferred=True)
    coordinator.driver.connect = AsyncMock(return_value=False)

    reconnected = asyncio.run(
        MarstekVenusDataUpdateCoordinator.async_reconnect_fresh(coordinator)
    )

    assert reconnected is False
    coordinator.hass.config_entries.async_schedule_reload.assert_not_called()
    assert coordinator.reload_entry_when_reachable is True


def test_batteries_that_come_back_together_share_one_entry_reload():
    """The flag is per battery; the reload it asks for is per entry."""
    runtime: dict = {}
    hass = _hass(runtime)
    first = _deferred_coordinator(deferred=True, hass=hass)
    second = _deferred_coordinator(deferred=True, hass=hass)

    _schedule(first)
    _schedule(second)

    hass.config_entries.async_schedule_reload.assert_called_once_with("abc")
    assert second.reload_entry_when_reachable is False


def test_the_reload_marker_does_not_survive_into_the_reloaded_runtime():
    """Setup rebuilds the runtime dict, so a second outage reloads again."""
    hass = _hass({})
    _schedule(_deferred_coordinator(deferred=True, hass=hass))
    assert hass.data[DOMAIN]["abc"]["setup_reload_scheduled"] is True

    hass.data[DOMAIN]["abc"] = {}
    _schedule(_deferred_coordinator(deferred=True, hass=hass))

    assert hass.config_entries.async_schedule_reload.call_count == 2
