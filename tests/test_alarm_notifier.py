"""Tests for AlarmNotifier: notify on new bits, dismiss on all-clear, severity.

First coverage for this module. A fake hass captures persistent_notification
service calls; no HA runtime needed.
"""
from __future__ import annotations

import asyncio

from custom_components.omnibattery.infra.alarm_notifier import AlarmNotifier


class _FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data):
        self.calls.append((domain, service, data))


class _FakeHass:
    def __init__(self):
        self.services = _FakeServices()


def _notifier():
    hass = _FakeHass()
    return hass, AlarmNotifier(hass, "Battery 1")


def test_new_fault_bit_creates_notification():
    hass, n = _notifier()
    asyncio.run(n.check(alarm_status=0, fault_status=0b1))
    assert len(hass.services.calls) == 1
    domain, service, data = hass.services.calls[0]
    assert (domain, service) == ("persistent_notification", "create")
    assert "Fault" in data["title"]


def test_new_alarm_bit_only_is_a_warning():
    hass, n = _notifier()
    asyncio.run(n.check(alarm_status=0b1, fault_status=0))
    domain, service, data = hass.services.calls[0]
    assert (domain, service) == ("persistent_notification", "create")
    assert "Warning" in data["title"]


def test_unchanged_bits_do_not_renotify():
    hass, n = _notifier()
    asyncio.run(n.check(0, 0b1))
    asyncio.run(n.check(0, 0b1))  # same bit still set -> no new bits
    assert len(hass.services.calls) == 1


def test_all_clear_dismisses_notification():
    hass, n = _notifier()
    asyncio.run(n.check(0, 0b1))          # raise
    asyncio.run(n.check(0, 0))            # clear
    assert len(hass.services.calls) == 2
    domain, service, data = hass.services.calls[1]
    assert (domain, service) == ("persistent_notification", "dismiss")


def test_no_bits_ever_set_is_silent():
    hass, n = _notifier()
    asyncio.run(n.check(0, 0))
    assert hass.services.calls == []


def test_additional_new_bit_renotifies():
    hass, n = _notifier()
    asyncio.run(n.check(0, 0b01))         # first fault
    asyncio.run(n.check(0, 0b11))         # a second fault bit appears
    assert len(hass.services.calls) == 2
    assert hass.services.calls[1][1] == "create"
