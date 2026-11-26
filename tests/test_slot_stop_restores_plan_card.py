"""#955: a price slot that stops gives the plan's card back.

The slot-start notice replaces the plan in the same card. Without this, a slot
that met its 0.1 kWh target in three minutes left "STARTED" up through the
hold that followed.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.omnibattery.const import (
    CONF_NOTIFY_EXECUTION_ENABLED,
    CONF_NOTIFY_PLAN_INFO_ENABLED,
    CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED as CONF_PLAN_CHARGE,
    NOTIFICATION_ID_PREFIX,
)
from custom_components.omnibattery.notifications import EXECUTION, PLAN_CHARGE, load_categories
from custom_components.omnibattery.pricing import engine as engine_module
from custom_components.omnibattery.pricing.engine import PricingManager

EVAL_ID = f"{NOTIFICATION_ID_PREFIX}predictive_charging_evaluation"


def _run(settings: dict, *, start: bool = True) -> list[tuple[str, str | None]]:
    """Plan, slot start, slot stop; returns (service, title) for the card."""
    calls: list[tuple[str, str | None]] = []

    async def _async_call(_domain, service, data=None, **_kw):
        if data["notification_id"] == EVAL_ID:
            calls.append((service, data.get("title")))

    hass = SimpleNamespace(services=SimpleNamespace(async_call=_async_call), data={})
    load_categories(hass, settings)
    controller = SimpleNamespace(
        max_price_threshold=None,
        discharge_price_threshold=None,
        _dp_arbitrage_ceiling=None,
        max_contracted_power=2500,
        max_charge_capacity=2500,
        capacity_protection_enabled=False,
        _dynamic_pricing_schedule=object(),
        _last_decision_data={"planner": "optimizer"},
        coordinators=[],
    )
    manager = PricingManager(hass, controller)
    manager._get_price_unit = lambda: "EUR/kWh"

    async def scenario():
        with (
            patch.object(
                engine_module.notifications,
                "format_dynamic_pricing_notification",
                lambda *a, **k: ("PLAN", "m", PLAN_CHARGE),
            ),
            patch.object(
                engine_module.notifications,
                "format_slot_start_notification",
                lambda *a, **k: ("STARTED", "m", EXECUTION),
            ),
        ):
            await manager._send_dynamic_pricing_notification(
                decision_data=controller._last_decision_data,
                schedule=controller._dynamic_pricing_schedule,
            )
            if start:
                await manager._send_dynamic_pricing_slot_start_notification(object())
            await manager._stop_dynamic_price_slot("soc_target_reached", write_idle=False)

    asyncio.run(scenario())
    return calls


def _visible(calls) -> str | None:
    shown = None
    for service, title in calls:
        shown = title if service == "create" else None
    return shown


def test_stop_puts_the_plan_back_after_the_start_notice():
    calls = _run({})
    assert [t for s, t in calls if s == "create"] == ["PLAN", "STARTED", "PLAN"]
    assert _visible(calls) == "PLAN"


def test_stop_with_the_plan_muted_leaves_no_started_card():
    calls = _run({
        CONF_PLAN_CHARGE: False,
        CONF_NOTIFY_PLAN_INFO_ENABLED: False,
        CONF_NOTIFY_EXECUTION_ENABLED: True,
    })
    assert [t for s, t in calls if s == "create"] == ["STARTED"]
    assert _visible(calls) is None


def test_stop_without_a_start_notice_leaves_the_plan_alone():
    calls = _run({CONF_NOTIFY_EXECUTION_ENABLED: False})
    assert calls == [("create", "PLAN")]
    assert _run({}, start=False) == [("create", "PLAN")]
