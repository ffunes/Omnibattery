"""Unit tests for ``PredictiveChargingSwitch`` (#68).

The switch is the dashboard enable toggle for predictive grid charging. It must:
  * be created whenever predictive charging is configured, not only while
    currently enabled (otherwise the sliders show with no toggle),
  * move the ``enabled`` and ``overridden`` flags together so every consumer
    stays consistent regardless of which flag it reads, and
  * toggle entirely in place without reloading the config entry or making every
    integration entity temporarily unavailable.

Exercised without the full Home Assistant runtime: entities are built on stub
hass/entry/controller objects and ``async_write_ha_state`` is neutralised.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.omnibattery.const import (
    CONF_ENABLE_PREDICTIVE_CHARGING,
    CONF_PREDICTIVE_CHARGING_OVERRIDDEN,
    CONF_PREDICTIVE_CHARGING_MODE,
    DOMAIN,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_TIME_SLOT,
)
from custom_components.omnibattery.switch import (
    NegativePriceChargingSwitch,
    PriceDischargeControlSwitch,
    PredictiveChargingSwitch,
    SmartPredischargeSwitch,
    async_setup_entry,
)
from custom_components.omnibattery.binary_sensor import (
    CurtailmentStatusSensor,
    PredictiveChargingStatusSensor,
    async_setup_entry as async_setup_binary_sensors,
)
from custom_components.omnibattery.button import (
    ReevaluateDynamicPricingButton,
    async_setup_entry as async_setup_buttons,
)
from custom_components.omnibattery import ChargeDischargeController


def _make_switch(
    *, enabled, overridden, mode=PREDICTIVE_MODE_TIME_SLOT, entry_data=None
):
    runtime_clears: list[str] = []
    control_cycles: list[None] = []
    evaluations: list[tuple] = []

    async def _evaluate_dynamic_pricing(*, horizon, extended_horizon=False):
        evaluations.append((horizon, extended_horizon))

    controller = SimpleNamespace(
        predictive_charging_enabled=enabled,
        predictive_charging_overridden=overridden,
        predictive_charging_mode=mode,
        grid_charging_active=False,
        _pricing_mgr=SimpleNamespace(
            _evaluate_dynamic_pricing=_evaluate_dynamic_pricing,
        ),
        _clear_predictive_runtime=runtime_clears.append,
        schedule_control_cycle=lambda: control_cycles.append(None),
    )
    entry = SimpleNamespace(entry_id="test-entry", data=dict(entry_data or {}))
    scheduled: list[asyncio.Task] = []

    async def _async_call(*_a, **_k):
        return None

    def _update_entry(target, *, data):
        target.data = data

    def _async_create_task(coro, _name=None):
        task = asyncio.create_task(coro)
        scheduled.append(task)
        return task

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_update_entry=_update_entry,
        ),
        services=SimpleNamespace(async_call=_async_call),
        async_create_task=_async_create_task,
    )
    sw = PredictiveChargingSwitch(hass, entry, controller)
    sw.async_write_ha_state = lambda: None  # not registered with HA
    return (
        sw,
        controller,
        entry,
        runtime_clears,
        control_cycles,
        evaluations,
        scheduled,
    )


async def _run_toggle(toggle, scheduled):
    """Run a toggle and finish any explicitly scheduled live reevaluation."""
    await toggle()
    await asyncio.gather(*scheduled)


def test_is_on_requires_enabled_and_not_overridden():
    assert _make_switch(enabled=True, overridden=False)[0].is_on is True
    # Enabled but paused (legacy override state) reads OFF, matching the pricing
    # engine which pauses on ``overridden``.
    assert _make_switch(enabled=True, overridden=True)[0].is_on is False
    # Configured-but-disabled (issue #68 reporter's state) reads OFF.
    assert _make_switch(enabled=False, overridden=False)[0].is_on is False


def test_turn_on_from_disabled_enables_without_reload():
    sw, controller, entry, clears, cycles, evaluations, scheduled = _make_switch(
        enabled=False, overridden=True
    )
    asyncio.run(_run_toggle(sw.async_turn_on, scheduled))
    assert controller.predictive_charging_enabled is True
    assert controller.predictive_charging_overridden is False
    assert entry.data[CONF_ENABLE_PREDICTIVE_CHARGING] is True
    assert entry.data[CONF_PREDICTIVE_CHARGING_OVERRIDDEN] is False
    assert clears == []
    assert cycles == [None]
    assert evaluations == []
    assert scheduled == []


def test_turn_off_from_enabled_disables_without_reload():
    sw, controller, entry, clears, cycles, evaluations, scheduled = _make_switch(
        enabled=True, overridden=False
    )
    asyncio.run(_run_toggle(sw.async_turn_off, scheduled))
    assert controller.predictive_charging_enabled is False
    assert controller.predictive_charging_overridden is True
    assert entry.data[CONF_ENABLE_PREDICTIVE_CHARGING] is False
    assert entry.data[CONF_PREDICTIVE_CHARGING_OVERRIDDEN] is True
    assert clears == ["user_disabled"]
    assert cycles == [None]
    assert evaluations == []
    assert scheduled == []


def test_resume_from_legacy_pause_is_live():
    # Legacy paused entry: enabled stayed True, only overridden was set. Turning
    # the switch back on clears the override without flipping enabled, so no
    # reload is needed (the schedules were already armed at setup).
    sw, controller, entry, clears, cycles, evaluations, scheduled = _make_switch(
        enabled=True, overridden=True
    )
    asyncio.run(sw.async_turn_on())
    assert controller.predictive_charging_overridden is False
    assert clears == []
    assert cycles == [None]
    assert evaluations == []
    assert scheduled == []


def test_dynamic_enable_rebuilds_remaining_schedule_without_reload():
    sw, _controller, _entry, clears, cycles, evaluations, scheduled = _make_switch(
        enabled=False,
        overridden=True,
        mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
    )

    asyncio.run(_run_toggle(sw.async_turn_on, scheduled))

    assert clears == []
    assert cycles == [None]
    assert len(evaluations) == 1
    horizon, extended = evaluations[0]
    assert horizon.value == "remaining"
    assert extended is True


def test_disabling_clears_all_predictive_runtime_ownership():
    cleanup_calls: list[tuple[str, str]] = []
    controller = SimpleNamespace(
        _pricing_mgr=SimpleNamespace(
            clear_curtailment_runtime=lambda reason: cleanup_calls.append(
                ("curtailment", reason)
            ),
            clear_negative_price_runtime=lambda reason: cleanup_calls.append(
                ("negative_price", reason)
            ),
        ),
        _reset_predictive_demand_runtime=lambda: cleanup_calls.append(
            ("demand", "reset")
        ),
        grid_charging_active=True,
        _grid_charging_initialized=True,
        _current_price_slot_active=True,
        _realtime_price_charging=True,
        _active_dynamic_slot_purpose="deficit",
        _active_dynamic_price_slot=object(),
        _predictive_charge_target_soc={"battery": 80},
        _predictive_deficit_target_soc={"battery": 80},
        _curtailment_opportunistic_target_soc={"battery": 90},
        _curtailment_opportunity_limited=True,
        first_execution=False,
    )

    ChargeDischargeController._clear_predictive_runtime(controller, "test")

    assert cleanup_calls == [
        ("curtailment", "test"),
        ("negative_price", "test"),
        ("demand", "reset"),
    ]
    assert controller.grid_charging_active is False
    assert controller._grid_charging_initialized is False
    assert controller._current_price_slot_active is False
    assert controller._realtime_price_charging is False
    assert controller._active_dynamic_slot_purpose is None
    assert controller._active_dynamic_price_slot is None
    assert controller._predictive_charge_target_soc is None
    assert controller._predictive_deficit_target_soc is None
    assert controller._curtailment_opportunistic_target_soc is None
    assert controller._curtailment_opportunity_limited is False
    assert controller.first_execution is True


def test_toggle_preserves_other_entry_data():
    sw, _controller, entry, _clears, _cycles, _evaluations, scheduled = _make_switch(
        enabled=True, overridden=False, entry_data={"unrelated": 42}
    )
    asyncio.run(_run_toggle(sw.async_turn_off, scheduled))
    assert entry.data["unrelated"] == 42


def test_switch_created_when_configured_but_disabled():
    """#68: the enable toggle must be built even when predictive charging is
    currently disabled, as long as it has been through config (key present)."""
    controller = SimpleNamespace(weekly_full_charge_enabled=False)
    entry = SimpleNamespace(
        entry_id="test-entry",
        data={
            CONF_ENABLE_PREDICTIVE_CHARGING: False,
            CONF_PREDICTIVE_CHARGING_MODE: PREDICTIVE_MODE_TIME_SLOT,
        },
    )
    hass = SimpleNamespace(
        data={DOMAIN: {"test-entry": {"coordinators": [], "controller": controller}}}
    )
    added: list = []
    asyncio.run(async_setup_entry(hass, entry, lambda ents: added.extend(ents)))
    assert any(isinstance(e, PredictiveChargingSwitch) for e in added)


def test_dynamic_predictive_entities_exist_while_master_switch_is_disabled():
    """Every setup-gated entity remains available for a reload-free live enable."""
    entry = SimpleNamespace(
        entry_id="test-entry",
        data={
            CONF_ENABLE_PREDICTIVE_CHARGING: False,
            CONF_PREDICTIVE_CHARGING_MODE: PREDICTIVE_MODE_DYNAMIC_PRICING,
        },
    )
    controller = SimpleNamespace(
        predictive_charging_enabled=False,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        weekly_full_charge_enabled=False,
    )
    hass = SimpleNamespace(
        data={DOMAIN: {entry.entry_id: {"coordinators": [], "controller": controller}}}
    )

    switches: list = []
    binary_sensors: list = []
    buttons: list = []
    asyncio.run(async_setup_entry(hass, entry, lambda ents: switches.extend(ents)))
    asyncio.run(
        async_setup_binary_sensors(
            hass, entry, lambda ents: binary_sensors.extend(ents)
        )
    )
    asyncio.run(async_setup_buttons(hass, entry, lambda ents: buttons.extend(ents)))

    assert any(isinstance(e, PredictiveChargingSwitch) for e in switches)
    assert any(isinstance(e, PriceDischargeControlSwitch) for e in switches)
    assert any(isinstance(e, SmartPredischargeSwitch) for e in switches)
    assert any(isinstance(e, NegativePriceChargingSwitch) for e in switches)
    assert any(isinstance(e, PredictiveChargingStatusSensor) for e in binary_sensors)
    assert any(isinstance(e, CurtailmentStatusSensor) for e in binary_sensors)
    assert any(isinstance(e, ReevaluateDynamicPricingButton) for e in buttons)


def test_switch_absent_when_never_configured():
    """No predictive config key at all (legacy/undeclared) → no switch."""
    controller = SimpleNamespace(weekly_full_charge_enabled=False)
    entry = SimpleNamespace(entry_id="test-entry", data={})
    hass = SimpleNamespace(
        data={DOMAIN: {"test-entry": {"coordinators": [], "controller": controller}}}
    )
    added: list = []
    asyncio.run(async_setup_entry(hass, entry, lambda ents: added.extend(ents)))
    assert not any(isinstance(e, PredictiveChargingSwitch) for e in added)
