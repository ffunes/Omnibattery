"""Tests for the notification gateway (both async and @callback styles)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from custom_components.omnibattery.notifications import gateway


class _FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data):
        self.calls.append((domain, service, data))


class _FakeConfigEntries:
    def async_update_entry(self, entry, data):
        entry.data = data


class _FakeEntry:
    def __init__(self, data=None):
        self.data = data or {}


class _FakeHass:
    def __init__(self):
        self.services = _FakeServices()
        self.data = {}
        self.config_entries = _FakeConfigEntries()


def _off(monkeypatch):
    monkeypatch.setattr(gateway, "category_enabled", lambda h, c: False)


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
    assert asyncio.run(gateway.async_notify(hass, gateway.PLAN_INFO, "id", "T", "M")) is False
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


def test_only_gateway_mentions_persistent_notification():
    """Design rule: no file outside notifications/ touches persistent_notification."""
    pkg = Path(__file__).resolve().parent.parent / "custom_components" / "omnibattery"
    gateway_dir = pkg / "notifications"
    scanned = [p for p in pkg.rglob("*.py") if gateway_dir not in p.parents]
    assert pkg / "switch.py" in scanned, "scan walked nothing: guard would pass vacuously"
    offenders = [
        str(p.relative_to(pkg))
        for p in scanned
        if "persistent_notification" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"persistent_notification outside notifications/: {offenders}"


_PC = gateway.CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED


def test_inheritance_missing_keys_all_on():
    hass = _FakeHass()
    gateway.load_categories(hass, {})
    assert all(gateway.category_enabled(hass, c) for c in gateway.CATEGORY_KEYS)


def test_inheritance_old_key_off_mutes_info_and_execution_not_modes():
    hass = _FakeHass()
    gateway.load_categories(hass, {_PC: False})
    assert gateway.category_enabled(hass, gateway.PLAN_CHARGE) is False
    assert gateway.category_enabled(hass, gateway.PLAN_INFO) is False
    assert gateway.category_enabled(hass, gateway.EXECUTION) is False
    assert gateway.category_enabled(hass, gateway.MODES) is True


def test_explicit_keys_win():
    hass = _FakeHass()
    gateway.load_categories(
        hass,
        {
            _PC: False,
            gateway.CONF_NOTIFY_PLAN_INFO_ENABLED: True,
            gateway.CONF_NOTIFY_EXECUTION_ENABLED: True,
            gateway.CONF_NOTIFY_MODES_ENABLED: False,
        },
    )
    assert gateway.category_enabled(hass, gateway.PLAN_CHARGE) is False
    assert gateway.category_enabled(hass, gateway.PLAN_INFO) is True
    assert gateway.category_enabled(hass, gateway.EXECUTION) is True
    assert gateway.category_enabled(hass, gateway.MODES) is False


def test_health_always_sends_and_muted_returns_false():
    hass = _FakeHass()
    gateway.load_categories(
        hass, {_PC: False, gateway.CONF_NOTIFY_MODES_ENABLED: False}
    )
    assert asyncio.run(gateway.async_notify(hass, gateway.HEALTH, "h", "T", "M")) is True
    assert asyncio.run(gateway.async_notify(hass, gateway.MODES, "m", "T", "M")) is False
    assert asyncio.run(gateway.async_notify(hass, gateway.PLAN_CHARGE, "p", "T", "M")) is False
    assert [c[2]["notification_id"] for c in hass.services.calls] == ["h"]


def test_set_category_enabled_persists_and_dismisses_only_that_category():
    hass = _FakeHass()
    entry = _FakeEntry({"other": 1})
    for cat, nid in (
        (gateway.PLAN_INFO, "a"),
        (gateway.PLAN_INFO, "b"),
        (gateway.MODES, "c"),
        (gateway.HEALTH, "d"),
    ):
        assert asyncio.run(gateway.async_notify(hass, cat, nid, "T", "M"))
    hass.services.calls.clear()
    asyncio.run(gateway.async_set_category_enabled(hass, entry, gateway.PLAN_INFO, False))
    assert entry.data == {
        "other": 1,
        gateway.CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED: True,
        gateway.CONF_NOTIFY_PLAN_INFO_ENABLED: False,
        gateway.CONF_NOTIFY_EXECUTION_ENABLED: True,
        gateway.CONF_NOTIFY_MODES_ENABLED: True,
    }
    assert gateway.category_enabled(hass, gateway.PLAN_INFO) is False
    assert gateway.category_enabled(hass, gateway.MODES) is True
    dismissed = sorted(c[2]["notification_id"] for c in hass.services.calls if c[1] == "dismiss")
    assert dismissed == ["a", "b"]
    asyncio.run(gateway.async_set_category_enabled(hass, entry, gateway.PLAN_INFO, True))
    assert entry.data[gateway.CONF_NOTIFY_PLAN_INFO_ENABLED] is True
    assert gateway.category_enabled(hass, gateway.PLAN_INFO) is True


def test_switching_one_category_off_survives_reload_without_muting_others():
    hass = _FakeHass()
    entry = _FakeEntry()
    asyncio.run(gateway.async_set_category_enabled(hass, entry, gateway.PLAN_CHARGE, False))
    assert set(gateway.CATEGORY_KEYS.values()) <= set(entry.data)
    gateway.load_categories(hass, entry.data)
    assert gateway.category_enabled(hass, gateway.PLAN_CHARGE) is False
    for cat in (gateway.PLAN_INFO, gateway.EXECUTION, gateway.MODES):
        assert gateway.category_enabled(hass, cat) is True


def test_set_category_enabled_rejects_health():
    import pytest

    with pytest.raises(ValueError):
        asyncio.run(
            gateway.async_set_category_enabled(_FakeHass(), _FakeEntry(), gateway.HEALTH, False)
        )
