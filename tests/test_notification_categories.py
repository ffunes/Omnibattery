"""Each notification category mutes and clears its own notices (task 2.6).

Runs the real emitters (pricing engine, weekly full charge, alarm and Indevolt
notifiers) against a fake hass whose notification services record every
create/dismiss, so what the user would see is "created minus dismissed".
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.omnibattery.const import (
    CONF_NOTIFY_EXECUTION_ENABLED,
    CONF_NOTIFY_MODES_ENABLED,
    CONF_NOTIFY_PLAN_INFO_ENABLED,
    CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED as CONF_PLAN_CHARGE,
    NOTIFICATION_ID_PREFIX,
)
from custom_components.omnibattery.control import weekly_full_charge as wfc
from custom_components.omnibattery.control.weekly_full_charge import WeeklyFullChargeManager
from custom_components.omnibattery.drivers.indevolt_notify import IndevoltNotifier
from custom_components.omnibattery.drivers.marstek_alarms import decode_alarms
from custom_components.omnibattery.infra.alarm_notifier import AlarmNotifier
from custom_components.omnibattery.notifications import (
    EXECUTION,
    HEALTH,
    MODES,
    PLAN_CHARGE,
    PLAN_INFO,
    async_set_category_enabled,
    load_categories,
)
from custom_components.omnibattery.pricing import engine as engine_module
from custom_components.omnibattery.pricing.engine import PricingManager

EVAL_ID = f"{NOTIFICATION_ID_PREFIX}predictive_charging_evaluation"
ALARM_ID = f"{NOTIFICATION_ID_PREFIX}battery_alarm_Battery 1"
OUTLOOK_ID = wfc._OUTLOOK_NOTIFICATION_ID
CATEGORY_KEY = {
    PLAN_CHARGE: CONF_PLAN_CHARGE,
    PLAN_INFO: CONF_NOTIFY_PLAN_INFO_ENABLED,
    EXECUTION: CONF_NOTIFY_EXECUTION_ENABLED,
    MODES: CONF_NOTIFY_MODES_ENABLED,
}
ALL_OFF = {key: False for key in CATEGORY_KEY.values()}
GATEWAY_PN = "custom_components.omnibattery.notifications.gateway.persistent_notification"


@pytest.fixture
def fake():
    """Fake hass; `fake.calls` holds (service, notification_id) in order."""
    calls: list[tuple[str, str]] = []

    async def _async_call(domain, service, data=None, **_kw):
        calls.append((service, data["notification_id"]))

    gateway_pn = SimpleNamespace(
        async_create=lambda _h, _msg, title=None, notification_id=None: calls.append(
            ("create", notification_id)
        ),
        async_dismiss=lambda _h, notification_id: calls.append(("dismiss", notification_id)),
    )
    fake = SimpleNamespace(
        services=SimpleNamespace(async_call=_async_call),
        data={},
        config_entries=SimpleNamespace(
            async_update_entry=lambda target, *, data: setattr(target, "data", data)
        ),
        calls=calls,
    )
    with patch(GATEWAY_PN, gateway_pn):
        yield fake


def visible(fake) -> set[str]:
    shown: set[str] = set()
    for service, notification_id in fake.calls:
        (shown.add if service == "create" else shown.discard)(notification_id)
    return shown


def _entry(data=None):
    return SimpleNamespace(data=dict(data or {}))


# One real emitter per mutable category: emit(fake) posts its notice.


def _engine_emitter(formatter, category, send):
    """Real PricingManager sender; only the formatter's category is forced."""

    def emit(fake):
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
        manager = PricingManager(fake, controller)
        manager._get_price_unit = lambda: "EUR/kWh"
        with patch.object(
            engine_module.notifications, formatter, lambda *a, **k: ("t", "m", category)
        ):
            asyncio.run(send(manager))

    return emit


def _modes_emitter(fake):
    """Weekly full charge warns that the sun will not reach 100% (sync callback)."""
    ctrl = SimpleNamespace(
        hass=fake,
        predictive_charging_enabled=False,
        weekly_full_charge_day="sun",
        _weekly_charge_status={"state": "Charging to 100%"},
        _solar_t_start=8.0,
        _charge_delay_mgr=SimpleNamespace(
            _excluded_claim_kwh=lambda _kwh: 0.0,
            _profile_forecast_between=lambda _a, _b: SimpleNamespace(energy_kwh=1.0),
        ),
        _consumption_tracker=SimpleNamespace(estimate_t_end=lambda: 17.0),
    )
    manager = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    manager._controller = ctrl
    battery = SimpleNamespace(data={"battery_soc": 60, "battery_total_energy": 5.0})
    forecast = SimpleNamespace(source="remaining", remaining_kwh=2.5)
    with (
        patch(f"{wfc.__name__}.dt_util.now", return_value=datetime(2026, 11, 15, 12, 0)),
        patch(f"{wfc.__name__}.read_remaining_solar_kwh", return_value=forecast),
    ):
        manager.update_solar_outlook([battery])


def _send_plan(manager):
    return manager._send_dynamic_pricing_notification(decision_data={}, schedule=None)


def _send_slot_start(manager):
    return manager._send_dynamic_pricing_slot_start_notification(object())


EMITTERS = {
    PLAN_CHARGE: (_engine_emitter("format_dynamic_pricing_notification", PLAN_CHARGE, _send_plan), EVAL_ID),
    PLAN_INFO: (_engine_emitter("format_dynamic_pricing_notification", PLAN_INFO, _send_plan), EVAL_ID),
    EXECUTION: (_engine_emitter("format_slot_start_notification", EXECUTION, _send_slot_start), EVAL_ID),
    MODES: (_modes_emitter, OUTLOOK_ID),
}


def _alarm(fake, name="Battery 1"):
    asyncio.run(AlarmNotifier(fake, name).check(decode_alarms({"alarm_status": 0, "fault_status": 1})))


@pytest.mark.parametrize("category", EMITTERS)
def test_category_on_creates_its_usual_id(fake, category):
    emit, notification_id = EMITTERS[category]
    load_categories(fake, {})
    emit(fake)
    assert visible(fake) == {notification_id}


@pytest.mark.parametrize("category", EMITTERS)
def test_category_off_creates_nothing(fake, category):
    emit, _ = EMITTERS[category]
    load_categories(fake, {CATEGORY_KEY[category]: False})
    emit(fake)
    assert fake.calls == []


@pytest.mark.parametrize("category", EMITTERS)
def test_switching_off_clears_that_category_only(fake, category):
    emit, notification_id = EMITTERS[category]
    load_categories(fake, {})
    emit(fake)
    _alarm(fake)  # a visible notice of another category, with its own ID
    assert visible(fake) == {notification_id, ALARM_ID}

    entry = _entry()
    asyncio.run(async_set_category_enabled(fake, entry, category, False))

    assert visible(fake) == {ALARM_ID}
    assert entry.data[CATEGORY_KEY[category]] is False


def test_switching_off_plan_charge_keeps_a_visible_modes_notice(fake):
    load_categories(fake, {})
    EMITTERS[PLAN_CHARGE][0](fake)
    _modes_emitter(fake)
    asyncio.run(async_set_category_enabled(fake, _entry(), PLAN_CHARGE, False))
    assert visible(fake) == {OUTLOOK_ID}


def test_health_cannot_be_muted(fake):
    load_categories(fake, ALL_OFF)
    _alarm(fake)
    IndevoltNotifier(fake, "Garage", "192.0.2.1_8080").paused("realtime_mode_required")
    assert ALARM_ID in visible(fake)
    assert any("indevolt_192.0.2.1_8080_realtime_mode_required" in i for i in visible(fake))

    with pytest.raises(ValueError):
        asyncio.run(async_set_category_enabled(fake, _entry(), HEALTH, False))
    _alarm(fake, "Battery 2")
    assert f"{NOTIFICATION_ID_PREFIX}battery_alarm_Battery 2" in visible(fake)


def test_old_key_alone_mutes_execution_but_not_modes(fake):
    """Only the legacy flag in the entry: execution inherits it, modes stay on."""
    load_categories(fake, {CONF_PLAN_CHARGE: False})
    EMITTERS[EXECUTION][0](fake)
    assert fake.calls == []
    _modes_emitter(fake)
    assert visible(fake) == {OUTLOOK_ID}
