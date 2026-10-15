"""The Indevolt notification adapter: one notification per topic and battery."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.omnibattery.infra import indevolt_notify
from custom_components.omnibattery.infra.indevolt_notify import IndevoltNotifier


@pytest.fixture
def calls(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        indevolt_notify.persistent_notification, "async_create",
        lambda hass, message, title=None, notification_id=None: recorded.append(
            ("create", notification_id, title, message)),
    )
    monkeypatch.setattr(
        indevolt_notify.persistent_notification, "async_dismiss",
        lambda hass, notification_id: recorded.append(("dismiss", notification_id)),
    )
    return recorded


def _hass(*states):
    entries = [SimpleNamespace(state=state) for state in states]
    return SimpleNamespace(config_entries=SimpleNamespace(
        async_entries=lambda domain: entries if domain == "indevolt" else []))


def test_pause_and_resume(calls):
    notifier = IndevoltNotifier(_hass(), "Garage", "192.0.2.1_8080")
    notifier.paused("realtime_mode_required")
    kind, notification_id, title, message = calls[-1]
    assert kind == "create" and notification_id.endswith("indevolt_192.0.2.1_8080_realtime_mode_required")
    assert "Realtime" in message
    notifier.paused("handover_failed")
    assert ("dismiss", notification_id) in calls  # one pause reason at a time
    notifier.resumed()
    assert calls[-1][0] == "dismiss" and calls[-1][1].endswith("handover_failed")
    notifier.paused("telemetry_stale")  # no notification for this one
    assert calls[-1][0] == "dismiss"


def test_throttle_names_the_official_integration_only_when_loaded(calls):
    IndevoltNotifier(_hass(ConfigEntryState.NOT_LOADED), "Garage", "k").throttled(20)
    assert "official Indevolt integration" not in calls[-1][3]
    IndevoltNotifier(_hass(ConfigEntryState.LOADED), "Garage", "k").throttled(20)
    assert "official Indevolt integration" in calls[-1][3]
    assert "20s" in calls[-1][3]


def test_setting_texts_name_the_setting_and_value(calls):
    notifier = IndevoltNotifier(_hass(), "Garage", "k")
    notifier.setting_failed("discharging_cutoff_capacity", 20)
    kind, notification_id, _title, message = calls[-1]
    assert notification_id.endswith("setting_discharging_cutoff_capacity")
    assert "minimum SOC" in message and "20%" in message
    notifier.setting_budget_exhausted("max_charge_power", 10)
    assert "maximum charge power" in calls[-1][3] and "10 times" in calls[-1][3]
    notifier.setting_ok("max_charge_power")
    assert calls[-1][0] == "dismiss"
    notifier.setting_failed("max_discharge_power", 1800)
    assert "1800 W" in calls[-1][3]


@pytest.mark.parametrize("language, needle", [("nl", "Realtime"), ("nl-BE", "werkmodus"), ("de", "working mode")])
def test_dutch_texts_and_english_fallback(calls, language, needle):
    hass = _hass()
    hass.config = SimpleNamespace(language=language)
    notifier = IndevoltNotifier(hass, "Garage", "k")
    notifier.paused("realtime_mode_required")
    assert needle in calls[-1][3]
    notifier.setting_failed("discharging_cutoff_capacity", 15)
    assert "15%" in calls[-1][3]
    notifier.throttled(20)
    notifier.firmware_outdated("V1", "V2")
    notifier.paused("unknown_model", model="CMS-X")
    assert "CMS-X" in calls[-1][3]


def test_every_language_has_every_text():
    keys = set(indevolt_notify._TEXTS["en"])
    for language, texts in indevolt_notify._TEXTS.items():
        assert set(texts) == keys, language


def test_notifications_use_the_renamed_device_name(calls, monkeypatch):
    device = SimpleNamespace(name_by_user="Indevolt SolidFlex 2000", name="Indevolt Monitoring")
    registry = SimpleNamespace(async_get_device=lambda identifiers: device)
    monkeypatch.setattr(indevolt_notify.dr, "async_get", lambda hass: registry)
    notifier = IndevoltNotifier(_hass(), "Indevolt Monitoring", "192.0.2.1_8080")
    notifier.paused("realtime_mode_required")
    assert "Indevolt SolidFlex 2000" in calls[-1][2] and "Indevolt SolidFlex 2000" in calls[-1][3]
    device.name_by_user = None
    notifier.paused("handover_failed")
    assert "Indevolt Monitoring" in calls[-1][2]


def test_the_name_lookup_uses_the_current_registry_api(calls, monkeypatch):
    device = SimpleNamespace(name_by_user="Indevolt SolidFlex 2000", name="Indevolt Monitoring")
    seen = []

    def deprecated(**kwargs):
        raise AssertionError("async_get_device is deprecated")

    def get_devices(identifiers):
        seen.append(identifiers)
        return [device]

    registry = SimpleNamespace(async_get_device=deprecated, async_get_devices=get_devices)
    monkeypatch.setattr(indevolt_notify.dr, "async_get", lambda hass: registry)
    notifier = IndevoltNotifier(_hass(), "Indevolt Monitoring", "192.0.2.1_8080")
    assert notifier.display_name == "Indevolt SolidFlex 2000"
    assert seen == [{("omnibattery", "192.0.2.1_8080")}]
    registry.async_get_devices = lambda identifiers: []
    assert notifier.display_name == "Indevolt Monitoring"
