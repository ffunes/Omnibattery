"""Smart Pre-Discharge decided by the optimizer, executed by the module
(docs/plans/predescarga-en-el-optimizador.md, stage 4).

With ``_dp_plan_room`` (Dynamic Pricing, the optimizer's plan, export prices)
only the plan's room orders and risk act: the module's own plan is still built
for its diagnostics but neither picks discharge slots nor stops an order with
its own early exits. Every protection still releases the order.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import PREDICTIVE_MODE_DYNAMIC_PRICING, PREDICTIVE_MODE_TIME_SLOT
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.curtailment import (
    BatterySnapshot,
    CurtailmentPlan,
    PreDischargeSlot,
)
from custom_components.omnibattery.pricing.engine import PlanRoom, PricingManager

NOW = datetime(2026, 11, 10, 9, 5)
ORDER = PriceSlot(NOW - timedelta(minutes=5), NOW + timedelta(minutes=10), 0.30)
LATER = PriceSlot(NOW + timedelta(hours=1), NOW + timedelta(hours=1, minutes=15), 0.30)
RISK = PriceSlot(NOW + timedelta(hours=2), NOW + timedelta(hours=2, minutes=15), -0.05)


def _room(orders=None, risk=(RISK,), reserve=None):
    return PlanRoom(
        orders={ORDER: (2.5, -1.0)} if orders is None else orders,
        risk=frozenset(risk),
        reserve={RISK: 1.5} if reserve is None else reserve,
    )


def _manager(*, room="default", module_plan=None, soc=80.0, snapshot=None, price=0.30, meter=0.0,
             mode=PREDICTIVE_MODE_DYNAMIC_PRICING, **extra):
    calls = []
    overrides = {}
    blocks = {}
    coordinator = SimpleNamespace(data={"battery_soc": soc, "battery_total_energy": 10.0}, is_available=True,
                                  name="b1", battery_manual_mode_enabled=False)
    ctrl = SimpleNamespace(
        smart_predischarge_enabled=True,
        predictive_charging_enabled=True,
        predictive_charging_mode=mode,
        coordinators=[coordinator],
        consumption_sensor="sensor.grid",
        _curtailment_plan=module_plan,
        _dp_plan_room=_room() if room == "default" else room,
        # The plan was evaluated at 80 %: an order stopping 1 kWh lower stops at 70 %.
        _dp_last_eval_soc=80.0,
        _dp_eval_projection_capacity_kwh=10.0,
        _setpoint_overrides=overrides,
        set_setpoint_override=lambda source, value, priority=0: overrides.__setitem__(source, (priority, value)),
        remove_setpoint_override=lambda source: overrides.pop(source, None),
        set_discharge_block=lambda source, reason, details=None, coordinator=None: blocks.__setitem__(
            (source, getattr(coordinator, "name", None)), reason),
        remove_discharge_block=lambda source, coordinator=None: blocks.pop(
            (source, getattr(coordinator, "name", None)), None),
        _apply_meter_transform=lambda _state: meter,
        _curtailment_active=False,
        _curtailment_active_export_target_w=0.0,
        max_charge_capacity=2500.0,
    )
    for key, value in extra.items():
        setattr(ctrl, key, value)
    state = SimpleNamespace(state="0.0", attributes={})
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: state))
    manager = PricingManager(hass, ctrl)
    manager._now = lambda: NOW
    manager._get_current_price = lambda: price
    manager._curtailment_battery_snapshots = lambda: [
        snapshot or BatterySnapshot("b1", soc, 10.0, 100.0, 20.0, 2500.0)
    ]
    return manager, ctrl, overrides, blocks


def test_only_the_plan_s_order_acts_where_the_module_would_pick_another_slot():
    # The module's plan would discharge now; the plan orders LATER only.
    module = CurtailmentPlan(status="planned", reason="headroom_required", risk_slots=[RISK],
                             selected_discharge_slots=[PreDischargeSlot(ORDER.start, ORDER.end, 0.30)],
                             required_headroom_kwh=5.0)
    manager, ctrl, overrides, _b = _manager(room=_room(orders={LATER: (2.5, -1.0)}), module_plan=module)
    manager.refresh_curtailment_runtime()
    assert "curtailment_predischarge" not in overrides
    manager._now = lambda: LATER.start + timedelta(minutes=1)
    manager.refresh_curtailment_runtime()
    assert overrides["curtailment_predischarge"] == (5, -2500.0)
    assert ctrl._curtailment_runtime_status == "predischarging"


@pytest.mark.parametrize(
    "module_plan",
    [
        None,
        CurtailmentPlan(status="fail_safe", reason="missing_forecast_or_solar_model"),
        CurtailmentPlan(status="no_risk", reason="no_negative_injection_window"),
        # Its headroom suffices for today: tomorrow's risk is the plan's.
        CurtailmentPlan(status="protected", reason="headroom_sufficient", risk_slots=[RISK],
                        required_headroom_kwh=1.0, current_headroom_kwh=2.0),
    ],
    ids=["no-plan", "fail-safe", "no-risk", "headroom"],
)
def test_the_module_s_own_exits_do_not_stop_a_plan_order(module_plan):
    manager, ctrl, overrides, _b = _manager(module_plan=module_plan)
    manager.refresh_curtailment_runtime()
    assert overrides["curtailment_predischarge"] == (5, -2500.0)
    assert ctrl._curtailment_active is True


def test_inside_the_plan_s_risk_the_grid_is_held_at_zero_and_the_floor_guarded():
    manager, ctrl, overrides, blocks = _manager(
        soc=20.0, snapshot=BatterySnapshot("b1", 20.0, 10.0, 100.0, 20.0, 2500.0))
    manager._now = lambda: RISK.start + timedelta(minutes=1)
    manager.refresh_curtailment_runtime()
    assert overrides["curtailment_negative_window"] == (6, 0.0)
    assert "curtailment_predischarge" not in overrides
    assert any(source == "curtailment_floor" for source, _c in blocks)
    assert ctrl._curtailment_runtime_status == "protected_window"


def test_an_order_stops_at_its_level_and_guards_the_floor():
    # Level 70 %: at 70 it still exports (the reading is at the level), under
    # it the order is released.
    manager, ctrl, overrides, _b = _manager(soc=70.0)
    manager.refresh_curtailment_runtime()
    assert overrides["curtailment_predischarge"] == (5, -2500.0)
    manager, ctrl, overrides, _b = _manager(soc=69.0)
    manager.refresh_curtailment_runtime()
    assert "curtailment_predischarge" not in overrides
    # At the reserve SOC the module's floor block stops the battery as today.
    manager, _ctrl, _o, blocks = _manager(snapshot=BatterySnapshot("b1", 20.0, 10.0, 100.0, 20.0, 2500.0))
    manager.refresh_curtailment_runtime()
    assert any(source == "curtailment_floor" for source, _c in blocks)


@pytest.mark.parametrize(
    "kwargs",
    [
        # A no-discharge window or a manual / backup battery: not dischargeable.
        dict(snapshot=BatterySnapshot("b1", 80.0, 10.0, 100.0, 20.0, 2500.0, can_discharge=False)),
        dict(snapshot=BatterySnapshot("b1", 80.0, 10.0, 100.0, 20.0, 2500.0, eligible=False)),
        dict(_capacity_protection_active=True),
        dict(manual_mode_enabled=True),
        dict(meter=None),
        dict(price=None),
    ],
    ids=["window", "manual-or-backup", "capacity", "manual-mode", "meter", "price"],
)
def test_every_protection_releases_a_plan_order(kwargs):
    manager, ctrl, overrides, _b = _manager(**kwargs)
    manager.refresh_curtailment_runtime()
    assert "curtailment_predischarge" not in overrides
    assert ctrl._curtailment_active is False


def test_a_stale_plan_in_another_mode_leaves_the_module_deciding():
    manager, _ctrl, _o, _b = _manager(mode=PREDICTIVE_MODE_TIME_SLOT)
    assert manager._plan_room() is None
    manager, _ctrl, _o, _b = _manager()
    assert manager._plan_room() is not None


def test_the_risk_others_read_is_the_plan_s():
    module = CurtailmentPlan(status="planned", reason="x", risk_slots=[LATER])
    manager, ctrl, _o, _b = _manager(module_plan=module)
    assert manager._slot_overlaps_curtailment_risk(RISK)
    assert not manager._slot_overlaps_curtailment_risk(LATER)


def test_the_negative_price_limiter_protects_the_plan_s_reserve():
    # 80 % of 10 kWh with max 100 %: 2 kWh free, the plan's risk stores 1.5.
    module = CurtailmentPlan(status="planned", reason="x", risk_slots=[LATER],
                             solar_reserve_by_slot={LATER: 5.0})
    manager, ctrl, _o, _b = _manager(module_plan=module)
    reserve, space = manager._update_curtailment_opportunistic_diagnostics(
        module, manager._curtailment_battery_snapshots(), NOW)
    assert reserve == pytest.approx(1.5)
    assert space == pytest.approx(0.5)


def test_a_planned_room_discharge_does_not_trigger_the_soc_drop_re_plan():
    """The projection includes the plan's room: executed as planned the SOC
    is on the line; the same drop without the order re-plans."""
    start = NOW - timedelta(minutes=5)
    projection = tuple((start + timedelta(minutes=15 * k), -0.5 * k) for k in range(1, 5))
    for proj, expected in ((projection, False), (tuple((t, 0.0) for t, _v in projection), True)):
        ctrl = SimpleNamespace(
            _dp_last_eval_soc=80.0, _dp_eval_projection=proj, _dp_eval_projection_capacity_kwh=10.0,
            coordinators=[SimpleNamespace(data={"battery_soc": 75.0}, battery_manual_mode_enabled=False)],
        )
        manager = PricingManager(SimpleNamespace(), ctrl)
        manager._now = lambda: start + timedelta(minutes=45)
        assert manager._is_dp_soc_drop_reeval() is expected
