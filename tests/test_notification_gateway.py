"""Tests for the notification gateway (both async and @callback styles)."""
from __future__ import annotations

import asyncio

from custom_components.omnibattery.notifications import gateway


class _FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data):
        self.calls.append((domain, service, data))


class _FakeHass:
    def __init__(self):
        self.services = _FakeServices()


def _off(monkeypatch):
    monkeypatch.setattr(gateway, "_category_enabled", lambda h, c: False)


def test_async_notify_creates_and_returns_true():
    hass = _FakeHass()
    ok = asyncio.run(gateway.async_notify(hass, gateway.PLAN_INFO, "id1", "T", "M"))
    assert ok is True
    assert hass.services.calls == [
        (
            "persistent_notification",
            "create",
            {"title": "T", "message": "M", "notification_id": "id1"},
        )
    ]


def test_async_dismiss():
    hass = _FakeHass()
    asyncio.run(gateway.async_dismiss(hass, "id1"))
    assert hass.services.calls == [
        ("persistent_notification", "dismiss", {"notification_id": "id1"})
    ]


def test_async_notify_category_off(monkeypatch):
    _off(monkeypatch)
    hass = _FakeHass()
    assert asyncio.run(gateway.async_notify(hass, gateway.HEALTH, "id", "T", "M")) is False
    assert hass.services.calls == []


def test_sync_notify_and_dismiss(monkeypatch):
    calls = []
    monkeypatch.setattr(
        gateway.persistent_notification,
        "async_create",
        lambda hass, message, **kw: calls.append(("create", message, kw)),
    )
    monkeypatch.setattr(
        gateway.persistent_notification,
        "async_dismiss",
        lambda hass, nid: calls.append(("dismiss", nid)),
    )
    hass = _FakeHass()
    assert gateway.notify(hass, gateway.MODES, "id", "T", "M") is True
    gateway.dismiss(hass, "id")
    assert calls == [
        ("create", "M", {"title": "T", "notification_id": "id"}),
        ("dismiss", "id"),
    ]
    assert hass.services.calls == []


def test_sync_notify_category_off(monkeypatch):
    _off(monkeypatch)
    calls = []
    monkeypatch.setattr(
        gateway.persistent_notification, "async_create", lambda *a, **k: calls.append(1)
    )
    assert gateway.notify(_FakeHass(), gateway.EXECUTION, "id", "T", "M") is False
    assert calls == []
