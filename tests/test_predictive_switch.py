"""Unit tests for ``PredictiveChargingSwitch`` (#68).

The switch is the dashboard enable toggle for predictive grid charging. It must:
  * be created whenever predictive charging is configured, not only while
    currently enabled (otherwise the sliders show with no toggle), and
  * move the ``enabled`` and ``overridden`` flags together so every consumer
    stays consistent regardless of which flag it reads.

Exercised without the full Home Assistant runtime: the entity is built on stub
hass/entry/controller objects and ``async_write_ha_state`` is neutralised.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.omnibattery.const import (
    CONF_ENABLE_PREDICTIVE_CHARGING,
    CONF_PREDICTIVE_CHARGING_OVERRIDDEN,
)
from custom_components.omnibattery.switch import PredictiveChargingSwitch


def _make_switch(*, enabled, overridden, entry_data=None):
    controller = SimpleNamespace(
        predictive_charging_enabled=enabled,
        predictive_charging_overridden=overridden,
        grid_charging_active=False,
    )
    entry = SimpleNamespace(data=dict(entry_data or {}))

    async def _async_call(*_a, **_k):
        return None

    def _update_entry(target, *, data):
        target.data = data

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=_update_entry),
        services=SimpleNamespace(async_call=_async_call),
    )
    sw = PredictiveChargingSwitch(hass, entry, controller)
    sw.async_write_ha_state = lambda: None  # not registered with HA
    return sw, controller, entry


def test_is_on_requires_enabled_and_not_overridden():
    assert _make_switch(enabled=True, overridden=False)[0].is_on is True
    # Enabled but paused (legacy override state) reads OFF, matching the pricing
    # engine which pauses on ``overridden``.
    assert _make_switch(enabled=True, overridden=True)[0].is_on is False
    # Configured-but-disabled (issue #68 reporter's state) reads OFF.
    assert _make_switch(enabled=False, overridden=False)[0].is_on is False


def test_turn_on_enables_and_clears_override():
    sw, controller, entry = _make_switch(enabled=False, overridden=True)
    asyncio.run(sw.async_turn_on())
    assert controller.predictive_charging_enabled is True
    assert controller.predictive_charging_overridden is False
    assert entry.data[CONF_ENABLE_PREDICTIVE_CHARGING] is True
    assert entry.data[CONF_PREDICTIVE_CHARGING_OVERRIDDEN] is False
    assert sw.is_on is True


def test_turn_off_disables_and_sets_override():
    sw, controller, entry = _make_switch(enabled=True, overridden=False)
    asyncio.run(sw.async_turn_off())
    assert controller.predictive_charging_enabled is False
    assert controller.predictive_charging_overridden is True
    assert entry.data[CONF_ENABLE_PREDICTIVE_CHARGING] is False
    assert entry.data[CONF_PREDICTIVE_CHARGING_OVERRIDDEN] is True
    assert sw.is_on is False


def test_toggle_preserves_other_entry_data():
    sw, _controller, entry = _make_switch(
        enabled=True, overridden=False, entry_data={"unrelated": 42}
    )
    asyncio.run(sw.async_turn_off())
    assert entry.data["unrelated"] == 42
