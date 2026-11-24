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
    the user-action "Predictive Charging: disabled" confirmation alone,
  * the switch is registered wherever the predictive master switch is.

Run without the Home Assistant runtime: stub hass/entry/controller objects.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from collections import namedtuple
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    CONF_ENABLE_PREDICTIVE_CHARGING,
    CONF_NOTIFY_PLAN_INFO_ENABLED,
    CONF_PREDICTIVE_CHARGING_MODE,
    CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED,
    DOMAIN,
    NOTIFICATION_ID_PREFIX,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_TIME_SLOT,
)
from custom_components.omnibattery.notifications import (
    EXECUTION,
    MODES,
    PLAN_CHARGE,
    PLAN_INFO,
    async_notify,
    load_categories,
)
from custom_components.omnibattery.pricing import engine as engine_module
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.switch import (
    NotificationCategorySwitch,
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
        data={},
        config_entries=SimpleNamespace(async_update_entry=_update_entry),
    )
    return hass, calls


# ---------------------------------------------------------------------------
# Pricing engine gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry_data", "expected_creates"),
    [
        (None, 1),  # categories never loaded (stubs) -> legacy behaviour
        ({CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED: True}, 1),
        ({CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED: False}, 0),
    ],
)
def test_engine_gate_honours_category_state(entry_data, expected_creates):
    hass, calls = _recording_hass()
    if entry_data is not None:
        load_categories(hass, entry_data)
    manager = PricingManager(hass, SimpleNamespace())

    sent = asyncio.run(manager._async_create_predictive_notification("T", "M", EVAL_ID, PLAN_CHARGE))

    assert sent is bool(expected_creates)

    creates = [c for c in calls if c[:2] == ("persistent_notification", "create")]
    assert len(creates) == expected_creates
    if expected_creates:
        assert creates[0][2] == {"title": "T", "message": "M", "notification_id": EVAL_ID}


def test_engine_creates_predictive_notifications_only_through_the_gate():
    """No evaluation notification may bypass the mute switch.

    The engine no longer talks to ``persistent_notification`` at all: its only
    ``async_notify(`` call is the one inside the gate helper.
    """
    src = ENGINE_SRC.read_text(encoding="utf-8")
    assert "persistent_notification" not in src
    assert src.count("async_notify(") == 1
    helper = src.index("async def _async_create_predictive_notification(")
    next_def = re.search(r"\n    (?:async )?def ", src[helper + 1 :]).start() + helper + 1
    assert helper < src.index("async_notify(") < next_def


def _muted_manager(monkeypatch):
    """Engine with formatters stubbed so each sender can run in isolation."""
    for name in (
        "format_dynamic_pricing_notification",
        "format_slot_start_notification",
        "format_dp_pre_slot_reevaluation_notification",
        "format_predictive_notification_message",
    ):
        monkeypatch.setattr(
            engine_module.notifications, name, lambda *a, **k: ("title", "message", PLAN_CHARGE)
        )
    hass, calls = _recording_hass()
    controller = SimpleNamespace(
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
    load_categories(hass, {CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED: False})
    manager = PricingManager(hass, controller)
    manager._get_price_unit = lambda: "EUR/kWh"
    return manager, controller, calls


def test_every_evaluation_sender_is_muted(monkeypatch):
    manager, controller, calls = _muted_manager(monkeypatch)

    async def _send_all():
        await manager._send_dynamic_pricing_notification(decision_data={}, schedule=None)
        await manager._send_dynamic_pricing_slot_start_notification(object())
        await manager._send_dp_pre_slot_reevaluation_notification(object(), {})
        await manager._send_predictive_charging_notification({})

    asyncio.run(_send_all())
    assert calls == []

    # Unmuted, the same four senders post exactly four notifications with
    # their existing IDs (blueprints and user automations depend on them).
    load_categories(manager._hass, {})
    asyncio.run(_send_all())
    ids = [c[2]["notification_id"] for c in calls if c[1] == "create"]
    assert ids == [EVAL_ID, EVAL_ID, EVAL_ID, EVAL_ID]


def test_engine_passes_the_formatter_category_to_the_gateway(monkeypatch):
    """The category the formatter returns decides whether the send is muted."""
    manager, _controller, calls = _muted_manager(monkeypatch)
    monkeypatch.setattr(
        engine_module.notifications,
        "format_dynamic_pricing_notification",
        lambda *a, **k: ("title", "message", PLAN_INFO),
    )

    async def _send():
        await manager._send_dynamic_pricing_notification(decision_data={}, schedule=None)

    # PLAN_INFO muted: nothing is created and the plan is not marked as announced.
    load_categories(manager._hass, {CONF_NOTIFY_PLAN_INFO_ENABLED: False})
    asyncio.run(_send())
    assert _creates(calls) == []
    assert manager._last_dp_notification_key is None

    # PLAN_CHARGE muted but PLAN_INFO on: the same message goes out.
    load_categories(manager._hass, {
        CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED: False,
        CONF_NOTIFY_PLAN_INFO_ENABLED: True,
    })
    asyncio.run(_send())
    assert len(_creates(calls)) == 1
    assert manager._last_dp_notification_key is not None


# Re-send only when the plan changes (#690)


def _dedupe_manager(monkeypatch, title=lambda: "needed"):
    manager, controller, calls = _muted_manager(monkeypatch)
    monkeypatch.setattr(
        engine_module.notifications,
        "format_dynamic_pricing_notification",
        lambda *a, **k: (title(), "message", PLAN_CHARGE),
    )
    load_categories(manager._hass, {})
    return manager, calls


_Slot = namedtuple("_Slot", "start end")


def _plan(*slots):
    """Schedule stub: slots is ((start, end), kwh) pairs."""
    sl = [_Slot(s, e) for (s, e), _ in slots]
    return SimpleNamespace(
        selected_slots=sl,
        slot_energy_targets_kwh={s: kwh for s, (_, kwh) in zip(sl, slots)},
    )


def _creates(calls):
    return [c for c in calls if c[1] == "create"]


def test_same_plan_new_live_values_notifies_once(monkeypatch):
    manager, calls = _dedupe_manager(monkeypatch)

    async def _run():
        for soc in (40, 55):
            await manager._send_dynamic_pricing_notification(
                {"avg_soc": soc, "usable_energy_kwh": soc / 10}, _plan(((1, 2), 3.0))
            )

    asyncio.run(_run())
    assert len(_creates(calls)) == 1


@pytest.mark.parametrize(
    "second",
    [
        lambda: _plan(((1, 2), 3.01)),
        lambda: _plan(((1, 2), 3.0), ((5, 6), 1.0)),
    ],
)
def test_changed_plan_notifies_again(monkeypatch, second):
    manager, calls = _dedupe_manager(monkeypatch)

    async def _run():
        await manager._send_dynamic_pricing_notification({}, _plan(((1, 2), 3.0)))
        await manager._send_dynamic_pricing_notification({}, second())

    asyncio.run(_run())
    assert len(_creates(calls)) == 2


def test_changed_title_notifies_again(monkeypatch):
    titles = iter(["needed", "not needed"])
    manager, calls = _dedupe_manager(monkeypatch, title=lambda: next(titles))

    async def _run():
        await manager._send_dynamic_pricing_notification({}, None)
        await manager._send_dynamic_pricing_notification({}, None)

    asyncio.run(_run())
    assert len(_creates(calls)) == 2


def test_no_schedule_twice_notifies_once(monkeypatch):
    manager, calls = _dedupe_manager(monkeypatch)

    async def _run():
        await manager._send_dynamic_pricing_notification({}, None)
        await manager._send_dynamic_pricing_notification({}, None)

    asyncio.run(_run())
    assert len(_creates(calls)) == 1


# ---------------------------------------------------------------------------
# Switch
# ---------------------------------------------------------------------------


def _count(added, category):
    return sum(
        isinstance(e, NotificationCategorySwitch) and e._category == category
        for e in added
    )


def _make_switch(entry_data=None):
    hass, calls = _recording_hass()
    controller = SimpleNamespace()
    entry = SimpleNamespace(entry_id="test-entry", data=dict(entry_data or {}))
    sw = NotificationCategorySwitch(hass, entry, controller, PLAN_CHARGE)
    sw.async_write_ha_state = lambda: None  # not registered with HA
    return sw, controller, entry, calls


def test_switch_defaults_on_for_existing_installs():
    sw, *_ = _make_switch()
    assert sw.is_on is True
    assert sw.unique_id.endswith("predictive_charging_notifications")
    assert sw.entity_id == "switch.omnibattery_predictive_charging_notifications"


def test_turn_off_persists_mutes_and_dismisses_shown_notifications():
    sw, controller, entry, calls = _make_switch(entry_data={"unrelated": 42})
    for category, nid in (
        (PLAN_CHARGE, EVAL_ID),
        (PLAN_CHARGE, EVENING_ID),
        (MODES, f"{NOTIFICATION_ID_PREFIX}predictive_charging_override"),
    ):
        asyncio.run(async_notify(sw.hass, category, nid, "T", "M"))
    calls.clear()
    asyncio.run(sw.async_turn_off())

    assert sw.is_on is False
    assert entry.data[CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED] is False
    assert entry.data["unrelated"] == 42
    dismissed = {c[2]["notification_id"] for c in calls if c[1] == "dismiss"}
    assert dismissed == {EVAL_ID, EVENING_ID}
    # The "Predictive Charging: disabled" confirmation answers a user action
    # and is intentionally never dismissed by this switch.
    assert f"{NOTIFICATION_ID_PREFIX}predictive_charging_override" not in dismissed
    assert not any(c[1] == "create" for c in calls)


def test_turn_on_persists_and_does_not_touch_notifications():
    sw, controller, entry, calls = _make_switch(
        entry_data={CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED: False},
    )
    asyncio.run(sw.async_turn_on())

    assert sw.is_on is True
    assert entry.data[CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED] is True
    assert calls == []


def test_muting_through_the_switch_silences_the_engine(monkeypatch):
    """End to end on the shared hass state the two components use."""
    manager, controller, engine_calls = _muted_manager(monkeypatch)
    load_categories(manager._hass, {})
    entry = SimpleNamespace(entry_id="test-entry", data={})
    sw = NotificationCategorySwitch(manager._hass, entry, controller, PLAN_CHARGE)
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
    assert _count(added, PLAN_CHARGE) == 1


def test_switch_absent_when_predictive_charging_never_configured():
    controller = SimpleNamespace(weekly_full_charge_enabled=False)
    entry = SimpleNamespace(entry_id="test-entry", data={})
    hass = SimpleNamespace(
        data={DOMAIN: {"test-entry": {"coordinators": [], "controller": controller}}}
    )
    added: list = []
    asyncio.run(async_setup_entry(hass, entry, lambda ents: added.extend(ents)))
    assert _count(added, PLAN_CHARGE) == 0

# ---------------------------------------------------------------------------
# One switch per category (task 2.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category, key",
    [
        (PLAN_CHARGE, "predictive_charging_notifications"),
        (PLAN_INFO, "notify_plan_info"),
        (EXECUTION, "notify_execution"),
        (MODES, "notify_modes"),
    ],
)
def test_category_switch_identity(category, key):
    hass, _ = _recording_hass()
    entry = SimpleNamespace(entry_id="test-entry", data={})
    sw = NotificationCategorySwitch(hass, entry, SimpleNamespace(), category)
    assert sw.translation_key == key
    assert sw.unique_id == f"marstek_venus_system_{key}"
    assert sw.entity_id == f"switch.omnibattery_{key}"


def _setup(data):
    controller = SimpleNamespace(weekly_full_charge_enabled=False)
    entry = SimpleNamespace(entry_id="test-entry", data=data)
    hass = SimpleNamespace(
        data={DOMAIN: {"test-entry": {"coordinators": [], "controller": controller}}}
    )
    added: list = []
    asyncio.run(async_setup_entry(hass, entry, lambda ents: added.extend(ents)))
    return added


def test_setup_adds_four_switches_with_predictive_configured():
    added = _setup({CONF_ENABLE_PREDICTIVE_CHARGING: False})
    for category in (PLAN_CHARGE, PLAN_INFO, EXECUTION, MODES):
        assert _count(added, category) == 1


def test_setup_adds_only_modes_without_predictive():
    added = _setup({})
    assert _count(added, MODES) == 1
    for category in (PLAN_CHARGE, PLAN_INFO, EXECUTION):
        assert _count(added, category) == 0


def test_turning_off_execution_persists_all_keys_with_others_unchanged():
    hass, _ = _recording_hass()
    entry = SimpleNamespace(entry_id="test-entry", data={})
    load_categories(hass, entry.data)
    sw = NotificationCategorySwitch(hass, entry, SimpleNamespace(), EXECUTION)
    sw.async_write_ha_state = lambda: None
    asyncio.run(sw.async_turn_off())
    assert sw.is_on is False
    assert entry.data["notify_execution_enabled"] is False
    assert entry.data[CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED] is True
    plan_charge = NotificationCategorySwitch(hass, entry, SimpleNamespace(), PLAN_CHARGE)
    assert plan_charge.is_on is True


def test_inherited_state_visible_in_is_on():
    hass, _ = _recording_hass()
    load_categories(hass, {CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED: False})
    entry = SimpleNamespace(entry_id="test-entry", data={})
    states = {
        c: NotificationCategorySwitch(hass, entry, SimpleNamespace(), c).is_on
        for c in (PLAN_CHARGE, PLAN_INFO, EXECUTION, MODES)
    }
    assert states == {
        PLAN_CHARGE: False,
        PLAN_INFO: False,
        EXECUTION: False,
        MODES: True,
    }


def test_changed_plan_orders_notify_again(monkeypatch):
    """A plan that only changes its sales (or holds) is a new plan."""
    manager, calls = _dedupe_manager(monkeypatch)
    controller = manager._controller

    async def _run():
        for kw in (2.5, 2.5, 3.0):
            controller._dp_plan_sales = {_Slot(7, 8): kw}
            await manager._send_dynamic_pricing_notification({}, None)

    asyncio.run(_run())
    assert len(_creates(calls)) == 2



def test_changed_room_orders_notify_again(monkeypatch):
    """Smart Pre-Discharge's room orders are part of the plan, too."""
    from custom_components.omnibattery.pricing.engine import PlanRoom

    manager, calls = _dedupe_manager(monkeypatch)
    controller = manager._controller
    controller.predictive_charging_enabled = True
    controller.predictive_charging_mode = "dynamic_pricing"

    async def _run():
        for kw in (2.5, 2.5, 1.25):
            controller._dp_plan_room = PlanRoom({_Slot(7, 8): (kw, -1.0)}, frozenset(), {})
            await manager._send_dynamic_pricing_notification({}, None)

    asyncio.run(_run())
    assert len(_creates(calls)) == 2


def test_engine_hands_the_published_plan_saving_to_the_formatter(monkeypatch):
    manager, controller, _calls = _muted_manager(monkeypatch)
    seen = {}

    def _capture(*a, **k):
        seen.update(k)
        return ("title", "message", PLAN_CHARGE)

    monkeypatch.setattr(engine_module.notifications, "format_dynamic_pricing_notification", _capture)
    manager._surplus_hold_enabled = lambda: False
    manager._store_eval_projection = lambda plan: None
    manager._now = lambda: datetime(2999, 1, 1, 18, 0)
    quarters = ((datetime(2999, 1, 1, 19, 0), datetime(2999, 1, 1, 19, 15), "hold", 0.3, 0.2),)
    plan = SimpleNamespace(plan_saving=0.25, quarter_actions=quarters)

    manager._publish_plan_holds(plan, {"planner": "other"})
    assert controller._dp_plan_saving is None
    assert controller._dp_plan_quarters is None

    manager._publish_plan_holds(plan, {"planner": "optimizer"})
    assert controller._dp_plan_saving == 0.25
    asyncio.run(manager._send_dynamic_pricing_notification(decision_data={}, schedule=None))
    assert seen["plan_saving"] == 0.25
    assert seen["plan_orders"]["quarters"] == quarters

    manager._clear_plan_orders()
    assert controller._dp_plan_saving is None
    assert controller._dp_plan_quarters is None


@pytest.mark.parametrize("saving, decision, warns", [
    (-0.05, {}, True),
    (-0.05, {"floor_active": True}, False),
    (-0.004, {}, False),
    (0.30, {}, False),
], ids=["dearer", "floor-paid-for", "under-a-cent", "saves"])
def test_a_plan_dearer_than_no_plan_is_logged(monkeypatch, caplog, saving, decision, warns):
    manager, _controller, _calls = _muted_manager(monkeypatch)
    manager._surplus_hold_enabled = lambda: False
    manager._store_eval_projection = lambda plan: None
    with caplog.at_level("WARNING"):
        manager._publish_plan_holds(
            SimpleNamespace(plan_saving=saving), {"planner": "optimizer", **decision})
    assert ("more than the battery with no plan" in caplog.text) is warns

