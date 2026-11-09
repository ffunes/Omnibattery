"""Mute switch for predictive-charging evaluation notifications (#141).

Predictive charging posts a persistent notification for every evaluation
(daily / dynamic-pricing evaluation, price-slot start, pre-slot and evening
re-evaluation). ``switch.omnibattery_predictive_charging_notifications`` lets
users silence those at the source while the evaluations themselves keep
running. These tests cover:

  * the pricing engine routes every evaluation notification through one gate
    that honours the controller flag (default ON for existing installs),
  * the switch persists the flag in the entry, updates the controller live,
    dismisses currently shown evaluation notifications when muted, and leaves
    the user-action "Predictive Charging Disabled" confirmation alone,
  * the switch is registered wherever the predictive master switch is.

Run without the Home Assistant runtime: stub hass/entry/controller objects.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    CONF_ENABLE_PREDICTIVE_CHARGING,
    CONF_PREDICTIVE_CHARGING_MODE,
    CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED,
    DOMAIN,
    NOTIFICATION_ID_PREFIX,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_TIME_SLOT,
)
from custom_components.omnibattery.pricing import engine as engine_module
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.switch import (
    PredictiveChargingNotificationsSwitch,
    async_setup_entry,
)

ENGINE_SRC = Path("custom_components/omnibattery/pricing/engine.py")
EVAL_ID = f"{NOTIFICATION_ID_PREFIX}predictive_charging_evaluation"
EVENING_ID = f"{NOTIFICATION_ID_PREFIX}predictive_charging_evening_reeval"


def _recording_hass():
    calls: list[tuple] = []

    async def _async_call(domain, service, data=None, **_kwargs):
        calls.append((domain, service, data))

    def _update_entry(target, *, data):
        target.data = data

    hass = SimpleNamespace(
        services=SimpleNamespace(async_call=_async_call),
        config_entries=SimpleNamespace(async_update_entry=_update_entry),
    )
    return hass, calls


# ---------------------------------------------------------------------------
# Pricing engine gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("controller_kwargs", "expected_creates"),
    [
        ({}, 1),  # attribute missing (older controller/stubs) -> legacy behaviour
        ({"predictive_charging_notifications_enabled": True}, 1),
        ({"predictive_charging_notifications_enabled": False}, 0),
    ],
)
def test_engine_gate_honours_controller_flag(controller_kwargs, expected_creates):
    hass, calls = _recording_hass()
    manager = PricingManager(hass, SimpleNamespace(**controller_kwargs))

    asyncio.run(manager._async_create_predictive_notification("T", "M", EVAL_ID))

    creates = [c for c in calls if c[:2] == ("persistent_notification", "create")]
    assert len(creates) == expected_creates
    if expected_creates:
        assert creates[0][2] == {"title": "T", "message": "M", "notification_id": EVAL_ID}


def test_engine_creates_predictive_notifications_only_through_the_gate():
    """No evaluation notification may bypass the mute switch.

    The only ``persistent_notification`` ``create`` call left in the engine is
    the one inside the gate helper; the remaining service calls are dismissals.
    """
    src = ENGINE_SRC.read_text(encoding="utf-8")
    creates = [m.start() for m in re.finditer(r'"persistent_notification",\s*\n\s*"create"', src)]
    assert len(creates) == 1
    helper = src.index("async def _async_create_predictive_notification(")
    next_def = src.index("\n    def ", helper + 1)
    assert helper < creates[0] < next_def


def _muted_manager(monkeypatch):
    """Engine with formatters stubbed so each sender can run in isolation."""
    for name in (
        "format_dynamic_pricing_notification",
        "format_slot_start_notification",
        "format_dp_pre_slot_reevaluation_notification",
        "format_evening_recharge_notification",
        "format_predictive_notification_message",
    ):
        monkeypatch.setattr(
            engine_module.notifications, name, lambda *a, **k: ("title", "message")
        )
    hass, calls = _recording_hass()
    controller = SimpleNamespace(
        predictive_charging_notifications_enabled=False,
        max_price_threshold=None,
        discharge_price_threshold=None,
        _dp_arbitrage_ceiling=None,
        max_contracted_power=5000,
        max_charge_capacity=2500,
        capacity_protection_enabled=False,
        _dynamic_pricing_schedule=object(),
        coordinators=[],
        _active_charging_slot=lambda: None,
    )
    manager = PricingManager(hass, controller)
    manager._get_price_unit = lambda: "EUR/kWh"
    return manager, controller, calls


def test_every_evaluation_sender_is_muted(monkeypatch):
    manager, controller, calls = _muted_manager(monkeypatch)

    async def _send_all():
        await manager._send_dynamic_pricing_notification(decision_data={}, schedule=None)
        await manager._send_dynamic_pricing_slot_start_notification(object())
        await manager._send_dp_pre_slot_reevaluation_notification(object(), {})
        await manager._send_evening_recharge_notification(1.0, [])
        await manager._send_predictive_charging_notification({})

    asyncio.run(_send_all())
    assert calls == []

    # Unmuted, the same five senders post exactly five notifications with
    # their existing IDs (blueprints and user automations depend on them).
    controller.predictive_charging_notifications_enabled = True
    asyncio.run(_send_all())
    ids = [c[2]["notification_id"] for c in calls if c[1] == "create"]
    assert ids == [EVAL_ID, EVAL_ID, EVAL_ID, EVENING_ID, EVAL_ID]


# ---------------------------------------------------------------------------
# Switch
# ---------------------------------------------------------------------------


def _make_switch(entry_data=None, **controller_kwargs):
    hass, calls = _recording_hass()
    controller = SimpleNamespace(**controller_kwargs)
    entry = SimpleNamespace(entry_id="test-entry", data=dict(entry_data or {}))
    sw = PredictiveChargingNotificationsSwitch(hass, entry, controller)
    sw.async_write_ha_state = lambda: None  # not registered with HA
    return sw, controller, entry, calls


def test_switch_defaults_on_for_existing_installs():
    sw, *_ = _make_switch()
    assert sw.is_on is True
    assert sw.unique_id.endswith("predictive_charging_notifications")
    assert sw.entity_id == "switch.omnibattery_predictive_charging_notifications"


def test_turn_off_persists_mutes_and_dismisses_shown_notifications():
    sw, controller, entry, calls = _make_switch(
        entry_data={"unrelated": 42},
        predictive_charging_notifications_enabled=True,
    )
    asyncio.run(sw.async_turn_off())

    assert sw.is_on is False
    assert controller.predictive_charging_notifications_enabled is False
    assert entry.data[CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED] is False
    assert entry.data["unrelated"] == 42
    dismissed = {c[2]["notification_id"] for c in calls if c[1] == "dismiss"}
    assert dismissed == {EVAL_ID, EVENING_ID}
    # The "Predictive Charging Disabled" confirmation answers a user action
    # and is intentionally never dismissed by this switch.
    assert f"{NOTIFICATION_ID_PREFIX}predictive_charging_override" not in dismissed
    assert not any(c[1] == "create" for c in calls)


def test_turn_on_persists_and_does_not_touch_notifications():
    sw, controller, entry, calls = _make_switch(
        entry_data={CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED: False},
        predictive_charging_notifications_enabled=False,
    )
    asyncio.run(sw.async_turn_on())

    assert sw.is_on is True
    assert controller.predictive_charging_notifications_enabled is True
    assert entry.data[CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED] is True
    assert calls == []


def test_muting_through_the_switch_silences_the_engine(monkeypatch):
    """End to end on the shared controller object the two components use."""
    manager, controller, engine_calls = _muted_manager(monkeypatch)
    controller.predictive_charging_notifications_enabled = True
    hass, switch_calls = _recording_hass()
    entry = SimpleNamespace(entry_id="test-entry", data={})
    sw = PredictiveChargingNotificationsSwitch(hass, entry, controller)
    sw.async_write_ha_state = lambda: None

    asyncio.run(sw.async_turn_off())
    asyncio.run(manager._send_predictive_charging_notification({}))
    assert engine_calls == []

    asyncio.run(sw.async_turn_on())
    asyncio.run(manager._send_predictive_charging_notification({}))
    assert [c[1] for c in engine_calls] == ["create"]


@pytest.mark.parametrize(
    "mode", [PREDICTIVE_MODE_TIME_SLOT, PREDICTIVE_MODE_DYNAMIC_PRICING]
)
@pytest.mark.parametrize("enabled", [True, False])
def test_switch_registered_with_the_predictive_master_switch(mode, enabled):
    controller = SimpleNamespace(
        weekly_full_charge_enabled=False,
        predictive_charging_enabled=enabled,
        predictive_charging_mode=mode,
    )
    entry = SimpleNamespace(
        entry_id="test-entry",
        data={CONF_ENABLE_PREDICTIVE_CHARGING: enabled, CONF_PREDICTIVE_CHARGING_MODE: mode},
    )
    hass = SimpleNamespace(
        data={DOMAIN: {"test-entry": {"coordinators": [], "controller": controller}}}
    )
    added: list = []
    asyncio.run(async_setup_entry(hass, entry, lambda ents: added.extend(ents)))
    assert sum(isinstance(e, PredictiveChargingNotificationsSwitch) for e in added) == 1


def test_switch_absent_when_predictive_charging_never_configured():
    controller = SimpleNamespace(weekly_full_charge_enabled=False)
    entry = SimpleNamespace(entry_id="test-entry", data={})
    hass = SimpleNamespace(
        data={DOMAIN: {"test-entry": {"coordinators": [], "controller": controller}}}
    )
    added: list = []
    asyncio.run(async_setup_entry(hass, entry, lambda ents: added.extend(ents)))
    assert not any(isinstance(e, PredictiveChargingNotificationsSwitch) for e in added)
