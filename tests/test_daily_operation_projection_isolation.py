"""Contract tests: the dashboard projection must be a read-only consumer.

These tests intentionally exercise the controller-facing entry point rather
than the pure simulation helpers.  They protect the boundary that matters to
the integration: rendering (or refreshing) Daily Operation must never alter a
predictive charge decision, a saved calendar, or a delayed-charge runtime
state.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.chronological import SlotAllocation
from custom_components.omnibattery.pricing.daily_timeline import (
    BatteryProjectionInput,
    ProjectionIntervalInput,
)


MADRID = ZoneInfo("Europe/Madrid")


class _ActuatorTrap:
    """Coordinator-shaped double that fails on any control-side access."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def apply_power(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))
        raise AssertionError("Daily Operation must not call battery actuators")

    def write_control(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))
        raise AssertionError("Daily Operation must not call battery actuators")


class _ProjectionPlanner:
    """Read-only projection adapter spy for the dashboard boundary."""

    def __init__(self, plan: SimpleNamespace) -> None:
        self.plan = plan
        self.calls: list[dict] = []
        self.extended_calls: list[dict] = []
        self.preview_slots_calls: list[tuple[datetime, datetime]] = []

    def build_extended_chronological_projection(self, **kwargs):
        self.extended_calls.append(kwargs)
        diagnostics = dict(kwargs["base_decision_data"] or {})
        # Deliberately change the returned view diagnostics.  The controller's
        # state must still retain its original control/diagnostic snapshots.
        diagnostics["solar_timeline_effective_kwh"] = 99.0
        diagnostics["chronological_source"] = "dashboard_only"
        return SimpleNamespace(plan=self.plan, diagnostics=diagnostics)

    def _build_chronological_plan(self, **kwargs):
        self.calls.append(kwargs)
        # Retained only to detect an accidental regression back to the legacy
        # stateful adapter.
        kwargs["decision_data"]["solar_timeline_effective_kwh"] = 99.0
        kwargs["decision_data"]["chronological_source"] = "dashboard_only"
        return self.plan

    def _time_slot_price_slots_for_horizon(
        self, now: datetime, horizon_end: datetime
    ) -> list[PriceSlot]:
        self.preview_slots_calls.append((now, horizon_end))
        return [PriceSlot(horizon_end - timedelta(hours=1), horizon_end, 0.0)]

    def _time_slot_price_slots(self, _now: datetime) -> list[PriceSlot]:
        raise AssertionError("Daily Operation must not invoke Time Slot control slots")


def _state(controller: SimpleNamespace) -> dict[str, object]:
    """Capture all control-owned values Daily Operation is forbidden to change."""
    schedule = controller._dynamic_pricing_schedule
    return {
        "decision": copy.deepcopy(controller._last_decision_data),
        "diagnostics": copy.deepcopy(controller._last_chronological_diagnostics),
        "schedule_identity": schedule,
        "schedule": copy.deepcopy(vars(schedule)) if schedule is not None else None,
        "charge_delay_unlocked": controller._charge_delay_unlocked,
        "delay_setpoint_reached": controller._delay_setpoint_reached,
        "charge_delay_status": copy.deepcopy(controller._charge_delay_status),
        "last_projection_signature": controller._daily_operation_last_projection_signature,
        "last_projection_monotonic": controller._daily_operation_last_projection_monotonic,
        "grid_charging_active": controller.grid_charging_active,
        "predictive_targets": copy.deepcopy(controller._predictive_charge_target_soc),
    }


def _controller(
    *, now: datetime, mode: str, diagnostics: dict | None = None
) -> tuple[SimpleNamespace, _ProjectionPlanner, _ActuatorTrap]:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )
    plan = SimpleNamespace(
        intervals=[
            ProjectionIntervalInput(now, midnight, consumption_kwh=0.1),
            ProjectionIntervalInput(
                midnight, midnight + timedelta(minutes=15), consumption_kwh=0.1
            ),
        ],
        allocations=[],
    )
    planner = _ProjectionPlanner(plan)
    slot = PriceSlot(now, midnight, 0.05)
    schedule = SimpleNamespace(
        selected_slots=[slot],
        slot_energy_targets_kwh={},
        slot_deadlines={},
        slot_plan_kinds={},
        evaluation_time=now - timedelta(minutes=5),
    )
    actuator = _ActuatorTrap()
    controller = SimpleNamespace(
        _daily_operation_mode=lambda: mode,
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _daily_operation_battery_inputs=lambda: [
            BatteryProjectionInput("battery", 5.0, 10.0, 10.0, 100.0, 4000.0, 4000.0)
        ],
        _consumption_tracker=object(),
        _pricing_mgr=planner,
        _last_decision_data={
            "should_charge": True,
            "planned_grid_charge_kwh": 1.5,
            "chronological_source": "authoritative_control",
        },
        _last_chronological_diagnostics=(
            {
                "chronological_source": "profile",
                "solar_timeline_source": "provider",
                "solar_timeline_effective_kwh": 2.4,
            }
            if diagnostics is None
            else diagnostics
        ),
        _dynamic_pricing_schedule=schedule if mode == "dynamic_pricing" else None,
        _charge_delay_unlocked=False,
        _delay_setpoint_reached=False,
        _charge_delay_status={"state": "Delayed", "estimated_unlock_time": "06:00"},
        _daily_operation_last_projection_signature=("control",),
        _daily_operation_last_projection_monotonic=123.0,
        grid_charging_active=True,
        _predictive_charge_target_soc={"battery": 88.0},
        predictive_charging_enabled=True,
        charge_delay_enabled=True,
        _daily_operation_delay_active=lambda: False,
        _daily_operation_delay_unlock=lambda _now: None,
        _delay_soc_setpoint_enabled=False,
        _delay_soc_setpoint=0.0,
        _balance_monitor_overrides_delay=lambda: False,
        max_price_threshold=None,
        manual_mode_enabled=False,
        enable_system_power_limits=False,
        coordinators=[actuator],
    )
    return controller, planner, actuator


@pytest.mark.parametrize("mode", ["normal", "time_slot", "dynamic_pricing"])
def test_projection_is_read_only_for_control_state_in_every_predictive_mode(mode: str):
    """A render must not mutate the plan that runtime control will execute."""
    now = datetime(2026, 8, 24, 23, 45, tzinfo=MADRID)
    controller, planner, actuator = _controller(now=now, mode=mode)
    before = _state(controller)

    result = ChargeDischargeController._daily_operation_build_projection(controller, now)

    assert result is not None
    assert len(result["extended_intervals"]) == 1
    assert _state(controller) == before
    assert actuator.calls == []
    assert planner.calls == []
    assert planner.extended_calls[0]["horizon_end"] == now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1, hours=12)
    assert planner.extended_calls[0]["base_decision_data"] is not controller._last_decision_data
    if mode == "time_slot":
        assert planner.preview_slots_calls


def test_reload_without_diagnostics_does_not_make_dashboard_restore_them():
    """Canonical diagnostics belong to predictive startup/control, never the view.

    A reload may start without a persisted in-memory snapshot, but rebuilding
    it is a predictive-control responsibility rather than a rendering side
    effect.
    """
    now = datetime(2026, 8, 24, 23, 45, tzinfo=MADRID)
    controller, planner, actuator = _controller(
        now=now, mode="time_slot", diagnostics={}
    )
    before = _state(controller)

    result = ChargeDischargeController._daily_operation_build_projection(controller, now)

    assert result is not None
    assert _state(controller) == before
    assert actuator.calls == []
    assert len(planner.extended_calls) == 1
    assert planner.calls == []


def test_dashboard_refresh_keeps_the_dynamic_calendar_equivalent():
    """The stored Dynamic Pricing calendar remains authoritative and unchanged."""
    now = datetime(2026, 8, 24, 23, 45, tzinfo=MADRID)
    controller, planner, actuator = _controller(now=now, mode="dynamic_pricing")
    schedule = controller._dynamic_pricing_schedule
    # Give the saved calendar a real allocation to cover the projection's
    # schedule adaptation branch. Runtime price parsers retain local wall-clock
    # datetimes without tzinfo, while Home Assistant supplies an aware clock to
    # the dashboard refresh; the view must align its private copies only.
    slot = PriceSlot(
        now.replace(tzinfo=None),
        (now + timedelta(minutes=15)).replace(tzinfo=None),
        0.05,
    )
    schedule.selected_slots = [slot]
    schedule.slot_energy_targets_kwh[slot] = 0.25
    schedule.slot_deadlines[slot] = (now + timedelta(minutes=15)).replace(tzinfo=None)
    schedule.slot_plan_kinds[slot] = "deadline"
    before = _state(controller)

    result = ChargeDischargeController._daily_operation_build_projection(controller, now)

    assert result is not None
    assert result["sources"]["operation_plan"] == "dynamic_schedule"
    assert _state(controller) == before
    assert actuator.calls == []
    projected_slot = planner.extended_calls[0]["slots"][0]
    assert projected_slot.start == now
    assert projected_slot.end == now + timedelta(minutes=15)
    assert projected_slot.start.tzinfo is MADRID
