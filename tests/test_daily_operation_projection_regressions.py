"""Regression coverage for controller-backed daily-operation projections."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.chronological import SlotAllocation
from custom_components.omnibattery.pricing.daily_timeline import (
    ACTION_GRID_CHARGE,
    ACTION_SOLAR_CHARGE,
    CONTEXT_CHARGE_DELAY,
    CONTEXT_SETPOINT,
    BatteryProjectionInput,
    ProjectionIntervalInput,
)
from custom_components.omnibattery.tracking.daily_projection import (
    DailyOperationProjectionRequest,
    build_daily_operation_projection,
)


MADRID = ZoneInfo("Europe/Madrid")


def _controller_for_projection(
    now: datetime,
    intervals: list[ProjectionIntervalInput],
    allocations: list[SlotAllocation],
    batteries: list[BatteryProjectionInput],
    **overrides,
):
    plan = SimpleNamespace(intervals=intervals, allocations=allocations)
    planner_calls = []

    def build_projection(**kwargs):
        planner_calls.append(kwargs)
        return SimpleNamespace(plan=plan, diagnostics={})

    values = {
        "_daily_operation_mode": lambda: "normal",
        "_daily_operation_float": ChargeDischargeController._daily_operation_float,
        "_daily_operation_battery_inputs": lambda: batteries,
        "_consumption_tracker": object(),
        "_pricing_mgr": SimpleNamespace(
            build_extended_chronological_projection=build_projection
        ),
        "_planner_calls": planner_calls,
        "_last_decision_data": {},
        "_last_chronological_diagnostics": {},
        "_dynamic_pricing_schedule": None,
        "predictive_charging_enabled": False,
        "charge_delay_enabled": False,
        "_daily_operation_delay_active": lambda: False,
        "_daily_operation_delay_unlock": lambda _now: None,
        "_delay_soc_setpoint_enabled": False,
        "_delay_setpoint_reached": False,
        "_charge_delay_unlocked": False,
        "_charge_delay_status": {},
        "max_price_threshold": None,
        "manual_mode_enabled": False,
        "enable_system_power_limits": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_projection_respects_combined_system_charge_limit():
    now = datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    end = now + timedelta(minutes=15)
    slot = PriceSlot(now, end, 0.1)
    controller = _controller_for_projection(
        now,
        [ProjectionIntervalInput(now, end)],
        [SlotAllocation(slot, 2.0, None, "scheduled")],
        [
            BatteryProjectionInput("a", 0, 10, 0, 100, 4000, 4000),
            BatteryProjectionInput("b", 0, 10, 0, 100, 4000, 4000),
        ],
        enable_system_power_limits=True,
        system_max_charge_power=2000,
        system_max_discharge_power=2000,
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )

    assert result is not None
    item = result["intervals"][0]
    assert item["grid_to_battery_kwh"] == pytest.approx(0.5)
    assert item["charge_power_w"] == pytest.approx(2000.0)


def test_projection_exposes_the_cross_midnight_extension():
    now = datetime(2026, 8, 24, 23, 45, tzinfo=MADRID)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    intervals = [
        ProjectionIntervalInput(now, midnight, consumption_kwh=0.1),
        ProjectionIntervalInput(
            midnight,
            midnight + timedelta(minutes=15),
            consumption_kwh=0.1,
        ),
    ]
    controller = _controller_for_projection(
        now,
        intervals,
        [],
        [BatteryProjectionInput("a", 0, 10, 50, 100, 4000, 4000)],
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )

    assert result is not None
    assert len(result["intervals"]) == 1
    assert len(result["extended_intervals"]) == 1
    assert result["extended_horizon"]["interval_count"] == 48
    assert result["extended_intervals"][0]["extension_index"] == 0
    assert result["extended_intervals"][0]["start"] == midnight
    assert controller._planner_calls[0]["horizon_end"] == midnight + timedelta(
        hours=12
    )


def test_pure_projection_matches_controller_and_does_not_mutate_inputs():
    now = datetime(2026, 8, 24, 23, 45, tzinfo=MADRID)
    midnight = now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    intervals = [
        ProjectionIntervalInput(now, midnight, consumption_kwh=0.1),
        ProjectionIntervalInput(
            midnight,
            midnight + timedelta(minutes=15),
            consumption_kwh=0.1,
        ),
    ]
    batteries = [BatteryProjectionInput("a", 5, 10, 0, 100, 4000, 4000)]
    decisions = {
        "chronological_source": "profile",
        "solar_timeline_source": "provider",
    }
    controller = _controller_for_projection(now, intervals, [], batteries)
    controller._last_decision_data = dict(decisions)

    controller_result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )
    pure_result = build_daily_operation_projection(
        DailyOperationProjectionRequest(
            now=now,
            plan_intervals=tuple(intervals),
            allocations=(),
            battery_inputs=tuple(batteries),
            mode="normal",
            decision_data=decisions,
        )
    )

    assert pure_result == controller_result
    assert decisions == {
        "chronological_source": "profile",
        "solar_timeline_source": "provider",
    }
    assert intervals[0].start == now
    assert batteries[0].stored_kwh == pytest.approx(5.0)


def test_time_slot_projection_uses_horizon_aware_windows_only_for_dashboard():
    now = datetime(2026, 8, 24, 23, 45, tzinfo=MADRID)
    midnight = now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    preview_calls = []
    preview_slot = PriceSlot(midnight, midnight + timedelta(hours=2), 0.0)
    controller = _controller_for_projection(
        now,
        [
            ProjectionIntervalInput(now, midnight),
            ProjectionIntervalInput(
                midnight,
                midnight + timedelta(minutes=15),
            ),
        ],
        [],
        [BatteryProjectionInput("a", 5, 10, 0, 100, 4000, 4000)],
        _daily_operation_mode=lambda: "time_slot",
    )

    def dashboard_slots(current: datetime, horizon_end: datetime):
        preview_calls.append((current, horizon_end))
        return [preview_slot]

    def control_slots(_now: datetime):
        raise AssertionError("dashboard must not use the control-only slot horizon")

    controller._pricing_mgr._time_slot_price_slots_for_horizon = dashboard_slots
    controller._pricing_mgr._time_slot_price_slots = control_slots

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )

    assert result is not None
    assert preview_calls == [(now, midnight + timedelta(hours=12))]
    assert controller._planner_calls[0]["slots"] == (preview_slot,)


def test_projection_uses_live_sources_without_restoring_daily_diagnostics():
    now = datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    end = now + timedelta(minutes=15)
    intervals = [
        ProjectionIntervalInput(
            now,
            end,
            consumption_kwh=0.1,
            solar_kwh=0.2,
        )
    ]
    plan = SimpleNamespace(intervals=intervals, allocations=[])
    controller = _controller_for_projection(
        now,
        intervals,
        [],
        [BatteryProjectionInput("a", 5, 10, 0, 100, 4000, 4000)],
    )
    calls = []

    def build_projection(**kwargs):
        calls.append(kwargs)
        diagnostics = {
            "chronological_source": "profile",
            "solar_timeline_source": "learned_profile",
            "solar_timeline_fallback_reason": None,
            "solar_timeline_effective_kwh": 4.5,
        }
        return SimpleNamespace(plan=plan, diagnostics=diagnostics)

    controller._pricing_mgr = SimpleNamespace(
        build_extended_chronological_projection=build_projection
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )

    assert result is not None
    assert result["sources"] == {
        "solar_forecast": "learned_profile",
        "solar_fallback_reason": None,
        "consumption_forecast": "profile",
        "operation_plan": "profile_projection",
    }
    assert calls[0]["horizon_end"] == now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1, hours=12)
    assert len(calls) == 1
    assert controller._last_chronological_diagnostics == {}


def test_projection_does_not_replace_existing_daily_diagnostics():
    now = datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    end = now + timedelta(minutes=15)
    controller = _controller_for_projection(
        now,
        [ProjectionIntervalInput(now, end)],
        [],
        [BatteryProjectionInput("a", 5, 10, 0, 100, 4000, 4000)],
        _last_chronological_diagnostics={
            "chronological_source": "legacy_daily",
            "solar_timeline_source": "sinusoidal",
            "solar_timeline_effective_kwh": 1.25,
        },
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )

    assert result is not None
    assert len(controller._planner_calls) == 1
    assert controller._last_chronological_diagnostics[
        "solar_timeline_effective_kwh"
    ] == pytest.approx(1.25)


def test_global_manual_mode_omits_automatic_future_projection():
    controller = SimpleNamespace(
        _daily_operation_mode=lambda: "dynamic_pricing",
        manual_mode_enabled=True,
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    )

    assert result == {
        "intervals": [],
        "mode": "dynamic_pricing",
        "stale": False,
        "sources": {"operation_plan": "manual_mode"},
    }


def test_setpoint_context_stops_after_the_interval_that_reaches_it():
    now = datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    ends = [now + timedelta(minutes=15 * step) for step in range(1, 4)]
    intervals = [
        ProjectionIntervalInput(
            now + timedelta(minutes=15 * index), end,
        )
        for index, end in enumerate(ends)
    ]
    slot = PriceSlot(now, ends[0], 0.1)
    controller = _controller_for_projection(
        now,
        intervals,
        [SlotAllocation(slot, 1.0, None, "scheduled")],
        [BatteryProjectionInput("a", 0, 4, 0, 100, 4000, 4000)],
        charge_delay_enabled=True,
        _delay_soc_setpoint_enabled=True,
        _delay_soc_setpoint=20.0,
        _charge_delay_status={"state": "Charging to setpoint"},
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )

    assert result is not None
    masks = [item["context_mask"] for item in result["intervals"]]
    assert masks[0] & CONTEXT_SETPOINT
    assert not masks[1] & CONTEXT_SETPOINT
    assert not masks[2] & CONTEXT_SETPOINT


def test_setpoint_unlock_projection_uses_final_target_and_safety_margin():
    now = datetime(2026, 8, 29, 10, 45, tzinfo=MADRID)
    intervals = []
    for index in range(44):
        start = now + timedelta(minutes=15 * index)
        intervals.append(
            ProjectionIntervalInput(
                start,
                start + timedelta(minutes=15),
                consumption_kwh=0.1,
                solar_kwh=1.0 if start.hour < 19 else 0.0,
            )
        )
    tracker = SimpleNamespace(
        get_today_target_soc=lambda: 95,
        estimate_t_end=lambda: 21.45,
    )
    controller = _controller_for_projection(
        now,
        intervals,
        [],
        [BatteryProjectionInput("a", 5.8125, 11.625, 0, 100, 4500, 4500)],
        _consumption_tracker=tracker,
        charge_delay_enabled=True,
        _delay_soc_setpoint_enabled=True,
        _delay_soc_setpoint=75.0,
        _delay_setpoint_reached=False,
        _delay_safety_margin_h=6.0,
        _charge_delay_status={"state": "Charging to setpoint"},
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )

    assert result is not None
    delay_projection = result["_delay_projection"]
    assert delay_projection["setpoint_reached_at"].startswith(
        "2026-08-29T11:45:"
    )
    # T_end 21:27 - 0.61 h of charging from 75% to 95% - 6 h margin.
    # The first projected solar deficit is at 19:00, so the time-backup edge
    # must win and match the later runtime estimate of 14:50.
    assert delay_projection["estimated_unlock_at"].startswith(
        "2026-08-29T14:50:"
    )


def test_active_delay_after_setpoint_blocks_projected_charge_until_unlock():
    now = datetime(2026, 8, 24, 13, 45, tzinfo=MADRID)
    unlock = now.replace(hour=14, minute=49)
    intervals = [
        ProjectionIntervalInput(
            start,
            start + timedelta(minutes=15),
            consumption_kwh=0.025,
            solar_kwh=0.25,
        )
        for start in (now + timedelta(minutes=15 * index) for index in range(6))
    ]
    controller = _controller_for_projection(
        now,
        intervals,
        [],
        [BatteryProjectionInput("a", 5, 10, 0, 100, 4000, 4000)],
        charge_delay_enabled=True,
        _delay_soc_setpoint_enabled=True,
        _delay_setpoint_reached=True,
        _daily_operation_delay_active=lambda: True,
        _daily_operation_delay_unlock=lambda _now: unlock,
        _charge_delay_status={
            "state": "Delayed (14:49 est.)",
            "estimated_unlock_time": "14:49",
        },
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )

    assert result is not None
    fully_blocked = result["intervals"][:4]
    assert all(
        item["context_mask"] & CONTEXT_CHARGE_DELAY
        and item["delay_until"] == unlock
        and item["solar_to_battery_kwh"] == 0.0
        and item["action_mask"] & ACTION_SOLAR_CHARGE == 0
        for item in fully_blocked
    )
    boundary = result["intervals"][4]
    assert boundary["context_mask"] & CONTEXT_CHARGE_DELAY
    assert boundary["delay_until"] == unlock
    assert boundary["solar_to_battery_kwh"] == pytest.approx(0.165)
    assert boundary["action_mask"] & ACTION_SOLAR_CHARGE
    after_unlock = result["intervals"][5]
    assert not after_unlock["context_mask"] & CONTEXT_CHARGE_DELAY
    assert after_unlock.get("delay_until") is None
    assert after_unlock["solar_to_battery_kwh"] == pytest.approx(0.225)


def test_weekly_full_charge_bypasses_delay_and_setpoint_projection_markers():
    now = datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    end = now + timedelta(minutes=15)
    slot = PriceSlot(now, end, 0.1)
    controller = _controller_for_projection(
        now,
        [ProjectionIntervalInput(now, end, consumption_kwh=0.0, solar_kwh=0.0)],
        [SlotAllocation(slot, 1.0, None, "scheduled")],
        [BatteryProjectionInput("a", 0, 10, 0, 100, 4000, 4000)],
        charge_delay_enabled=True,
        _delay_soc_setpoint_enabled=True,
        _delay_soc_setpoint=50.0,
        _delay_setpoint_reached=False,
        _charge_delay_status={
            "state": "Delayed (10:45 est.)",
            "estimated_unlock_time": "10:45",
        },
        _balance_monitor_overrides_delay=lambda: True,
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )

    assert result is not None
    assert all(
        not item["context_mask"] & (CONTEXT_CHARGE_DELAY | CONTEXT_SETPOINT)
        and item.get("delay_until") is None
        for item in result["intervals"]
    )


def test_external_solar_does_not_hide_grid_charge_during_net_import():
    coordinator = SimpleNamespace(
        capabilities=SimpleNamespace(has_mppt_pv=False, has_solar_telemetry=False),
        data={"ac_power": -600},
    )
    controller = SimpleNamespace(
        coordinators=[coordinator],
        _consumption_tracker=SimpleNamespace(
            _read_total_solar_power_kw=lambda: 1.4
        ),
        grid_charging_active=False,
        previous_sensor=700.0,
        predictive_charging_enabled=False,
        charge_delay_enabled=False,
        previous_power=0.0,
        _daily_operation_mode=lambda: "normal",
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _coordinator_delivered_power=(
            ChargeDischargeController._coordinator_delivered_power
        ),
        _is_battery_manual_owned=lambda _coordinator: False,
        _daily_operation_delay_active=lambda: False,
        _charge_delay_unlocked=False,
        _charge_delay_status={"state": "Disabled"},
    )

    now = datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    for _ in range(5):
        decision = ChargeDischargeController._daily_operation_runtime_decision(
            controller, now, sample_duration_s=60.0
        )

    assert decision["action_mask"] == ACTION_GRID_CHARGE


def test_external_solar_charge_uses_net_interval_energy_not_power_samples():
    coordinator = SimpleNamespace(
        capabilities=SimpleNamespace(has_mppt_pv=False, has_solar_telemetry=False),
        data={"ac_power": -600},
    )
    tracker = SimpleNamespace(_read_total_solar_power_kw=lambda: 1.4)
    controller = SimpleNamespace(
        coordinators=[coordinator],
        _consumption_tracker=tracker,
        grid_charging_active=False,
        previous_sensor=0.0,
        predictive_charging_enabled=False,
        charge_delay_enabled=False,
        previous_power=0.0,
        _daily_operation_mode=lambda: "normal",
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _coordinator_delivered_power=(
            ChargeDischargeController._coordinator_delivered_power
        ),
        _is_battery_manual_owned=lambda _coordinator: False,
        _daily_operation_delay_active=lambda: False,
        _charge_delay_unlocked=False,
        _charge_delay_status={"state": "Disabled"},
    )
    now = datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)

    # Every positive sample would have crossed the old 10 W threshold. Their
    # import/export energy cancels within the displayed quarter-hour instead.
    for grid_power_w in (200.0, -200.0, 200.0, -200.0):
        controller.previous_sensor = grid_power_w
        decision = ChargeDischargeController._daily_operation_runtime_decision(
            controller, now, sample_duration_s=60.0
        )
        assert decision["action_mask"] == ACTION_SOLAR_CHARGE


def test_external_solar_charge_becomes_grid_after_material_import_energy():
    coordinator = SimpleNamespace(
        capabilities=SimpleNamespace(has_mppt_pv=False, has_solar_telemetry=False),
        data={"ac_power": -600},
    )
    controller = SimpleNamespace(
        coordinators=[coordinator],
        _consumption_tracker=SimpleNamespace(
            _read_total_solar_power_kw=lambda: 1.4
        ),
        grid_charging_active=False,
        previous_sensor=300.0,
        predictive_charging_enabled=False,
        charge_delay_enabled=False,
        previous_power=0.0,
        _daily_operation_mode=lambda: "normal",
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _coordinator_delivered_power=(
            ChargeDischargeController._coordinator_delivered_power
        ),
        _is_battery_manual_owned=lambda _coordinator: False,
        _daily_operation_delay_active=lambda: False,
        _charge_delay_unlocked=False,
        _charge_delay_status={"state": "Disabled"},
    )
    now = datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)

    decisions = [
        ChargeDischargeController._daily_operation_runtime_decision(
            controller, now, sample_duration_s=60.0
        )
        for _ in range(11)
    ]

    assert all(
        decision["action_mask"] == ACTION_SOLAR_CHARGE
        for decision in decisions[:10]
    )
    assert decisions[10]["action_mask"] == ACTION_GRID_CHARGE

    next_interval = ChargeDischargeController._daily_operation_runtime_decision(
        controller,
        now.replace(minute=15),
        sample_duration_s=60.0,
    )
    assert next_interval["action_mask"] == ACTION_SOLAR_CHARGE


def test_direct_solar_and_ac_draw_are_both_reported_during_net_import():
    coordinator = SimpleNamespace(
        capabilities=SimpleNamespace(has_mppt_pv=True, has_solar_telemetry=True),
        data={"ac_power": -600, "mppt1_power": 800},
    )
    controller = SimpleNamespace(
        coordinators=[coordinator],
        _consumption_tracker=None,
        grid_charging_active=False,
        previous_sensor=700.0,
        predictive_charging_enabled=False,
        charge_delay_enabled=False,
        previous_power=0.0,
        _daily_operation_mode=lambda: "normal",
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _coordinator_delivered_power=(
            ChargeDischargeController._coordinator_delivered_power
        ),
        _is_battery_manual_owned=lambda _coordinator: False,
        _daily_operation_delay_active=lambda: False,
        _charge_delay_unlocked=False,
        _charge_delay_status={"state": "Disabled"},
    )

    now = datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    for _ in range(5):
        decision = ChargeDischargeController._daily_operation_runtime_decision(
            controller, now, sample_duration_s=60.0
        )

    assert decision["action_mask"] == ACTION_SOLAR_CHARGE | ACTION_GRID_CHARGE


def test_previous_command_is_not_reported_as_measured_operation():
    controller = SimpleNamespace(
        coordinators=[],
        _consumption_tracker=None,
        grid_charging_active=False,
        previous_sensor=None,
        previous_power=750.0,
        predictive_charging_enabled=False,
        charge_delay_enabled=False,
        _daily_operation_mode=lambda: "normal",
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _is_battery_manual_owned=lambda _coordinator: False,
        _daily_operation_delay_active=lambda: False,
        _charge_delay_unlocked=False,
        _charge_delay_status={"state": "Disabled"},
    )

    decision = ChargeDischargeController._daily_operation_runtime_decision(
        controller, datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    )

    assert decision["action_mask"] == ACTION_GRID_CHARGE
    assert decision["charge_power_w"] == pytest.approx(750.0)
    assert decision["source"] == "runtime_command"


def test_informational_schedule_is_not_painted_as_grid_charge():
    """A no-deficit 00:05 cheap-hour calendar must not become planned grid charge.

    ``charging_needed=False`` means the runtime slot resolver never activates
    these slots.  Without a chronological plan they also carry no energy
    target, so the projection used to fall back to a full-power grid quota and
    paint "grid charge" over an interval that solar covers on its own.
    """
    now = datetime(2026, 9, 1, 12, 0, tzinfo=MADRID)
    end = now + timedelta(minutes=15)
    slot = PriceSlot(now, end, 0.216)
    controller = _controller_for_projection(
        now,
        [ProjectionIntervalInput(now, end, consumption_kwh=0.136, solar_kwh=0.945)],
        [],
        [BatteryProjectionInput("a", 7.0, 10.0, 0, 100, 4000, 4000)],
        _daily_operation_mode=lambda: "dynamic_pricing",
        predictive_charging_enabled=True,
        _last_decision_data={"should_charge": False},
        _dynamic_pricing_schedule=SimpleNamespace(
            selected_slots=[slot],
            slot_energy_targets_kwh={},
            slot_deadlines={},
            slot_plan_kinds={},
            charging_needed=False,
            evaluation_time=now,
        ),
        max_contracted_power=6000,
        max_charge_capacity=4000,
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )

    assert result is not None
    item = result["intervals"][0]
    assert item["grid_to_battery_kwh"] == pytest.approx(0.0)
    assert not item["action_mask"] & ACTION_GRID_CHARGE
    assert item["action_mask"] & ACTION_SOLAR_CHARGE
    assert item["grid_charge_decision"] == "not_needed"


def test_executable_schedule_still_plans_grid_charge():
    """The same calendar with ``charging_needed`` set stays visible."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=MADRID)
    end = now + timedelta(minutes=15)
    slot = PriceSlot(now, end, 0.216)
    controller = _controller_for_projection(
        now,
        [ProjectionIntervalInput(now, end, consumption_kwh=0.136, solar_kwh=0.945)],
        [],
        [BatteryProjectionInput("a", 7.0, 10.0, 0, 100, 4000, 4000)],
        _daily_operation_mode=lambda: "dynamic_pricing",
        predictive_charging_enabled=True,
        _last_decision_data={"should_charge": True},
        _dynamic_pricing_schedule=SimpleNamespace(
            selected_slots=[slot],
            slot_energy_targets_kwh={slot: 0.5},
            slot_deadlines={},
            slot_plan_kinds={},
            charging_needed=True,
            evaluation_time=now,
        ),
        max_contracted_power=6000,
        max_charge_capacity=4000,
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )

    assert result is not None
    item = result["intervals"][0]
    assert item["grid_to_battery_kwh"] > 0.0
    assert item["action_mask"] & ACTION_GRID_CHARGE
