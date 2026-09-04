"""Focused unit tests for the runtime daily-operation diary."""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.pricing.daily_timeline import (
    BatteryProjectionInput,
    ProjectionIntervalInput,
)
from custom_components.omnibattery.tracking.daily_projection import (
    DailyOperationProjectionRequest,
    build_daily_operation_projection,
)
from custom_components.omnibattery.tracking.daily_timeline import (
    ACTION_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_SOLAR_CHARGE,
    CONTEXT_CHARGE_DELAY,
    CONTEXT_DYNAMIC_PRICE,
    CONTEXT_HOURLY_BALANCE,
    CONTEXT_SETPOINT,
    GRID_CHARGE_DECISIONS,
    GRID_CHARGE_SCHEDULED,
    DailyOperationTimelineManager,
    _normalize_grid_decision,
)

MADRID = ZoneInfo("Europe/Madrid")


class FakeStore:
    """Async Store double retaining exactly what the manager writes."""

    def __init__(self, data=None):
        self.data = copy.deepcopy(data)
        self.writes: list[dict] = []

    async def async_load(self):
        return copy.deepcopy(self.data)

    async def async_save(self, data):
        self.data = copy.deepcopy(data)
        self.writes.append(copy.deepcopy(data))


class BlockingStore(FakeStore):
    """Store double that exposes a mutation while a write is in flight."""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def async_save(self, data):
        self.started.set()
        await self.release.wait()
        await super().async_save(data)


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _manager(clock: MutableClock, store: FakeStore | None = None, *, mode="dynamic_pricing"):
    hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Madrid"))
    entry = SimpleNamespace(
        entry_id="entry-one",
        data={"pricing_mode": mode, "household_consumption_sensor": "sensor.house"},
        options={},
    )
    return DailyOperationTimelineManager(
        hass,
        entry,
        SimpleNamespace(pricing_mode=mode),
        store=store or FakeStore(),
        now_provider=clock,
        debounce_seconds=0.01,
    )


def _capture(value: float, coverage: float = 900.0):
    values = [None] * 96
    coverages = [0.0] * 96
    values[40] = value
    coverages[40] = coverage
    return {"interval_energy_kwh": values, "interval_coverage_s": coverages}


def _runtime_controller(coordinators, *, grid_active=False, solar_power_kw=None):
    tracker = None
    if solar_power_kw is not None:
        tracker = SimpleNamespace(
            _read_total_solar_power_kw=lambda: solar_power_kw
        )
    controller = SimpleNamespace(
        coordinators=coordinators,
        _consumption_tracker=tracker,
        grid_charging_active=grid_active,
        predictive_charging_enabled=False,
        charge_delay_enabled=False,
        previous_power=0.0,
        _daily_operation_mode=lambda: "normal",
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _coordinator_delivered_power=(
            ChargeDischargeController._coordinator_delivered_power
        ),
        _is_battery_manual_owned=lambda _coordinator: False,
        _charge_delay_unlocked=False,
        _charge_delay_status={"state": "Disabled"},
    )
    controller._daily_operation_delay_active = lambda: (
        ChargeDischargeController._daily_operation_delay_active(controller)
    )
    return controller


def test_runtime_diary_detects_dc_coupled_solar_charge_while_ac_is_exporting():
    coordinator = SimpleNamespace(
        capabilities=SimpleNamespace(has_mppt_pv=True, has_solar_telemetry=True),
        data={
            "ac_power": 265,
            "mppt1_power": 405,
            "mppt2_power": 402,
            "mppt3_power": 139,
            "mppt4_power": 0,
        },
    )
    # Even if grid charging is globally active for another unit, this battery
    # is exporting on AC and must only be classified as solar charging.
    controller = _runtime_controller([coordinator], grid_active=True)

    decision = ChargeDischargeController._daily_operation_runtime_decision(
        controller, datetime(2026, 8, 23, 11, 30, tzinfo=MADRID)
    )

    assert decision["action_mask"] == ACTION_SOLAR_CHARGE
    assert decision["charge_power_w"] == pytest.approx(681)


def test_runtime_diary_composes_grid_and_dc_solar_charge():
    coordinator = SimpleNamespace(
        capabilities=SimpleNamespace(has_mppt_pv=True, has_solar_telemetry=True),
        data={"ac_power": -200, "mppt1_power": 800},
    )
    controller = _runtime_controller([coordinator], grid_active=True)

    decision = ChargeDischargeController._daily_operation_runtime_decision(
        controller, datetime(2026, 8, 23, 11, 30, tzinfo=MADRID)
    )

    assert decision["action_mask"] == ACTION_SOLAR_CHARGE | ACTION_GRID_CHARGE
    assert decision["simultaneous"] is True


def test_runtime_diary_ignores_max_ac_derived_solar_for_charge_source():
    coordinator = SimpleNamespace(
        capabilities=SimpleNamespace(
            has_mppt_pv=False, has_solar_telemetry=False
        ),
        data={"ac_power": -600, "solar_power": 1400},
    )
    controller = _runtime_controller([coordinator], grid_active=False)

    decision = ChargeDischargeController._daily_operation_runtime_decision(
        controller, datetime(2026, 8, 23, 11, 30, tzinfo=MADRID)
    )

    assert decision["action_mask"] == ACTION_GRID_CHARGE


def test_runtime_diary_keeps_charge_history_without_inventing_solar():
    coordinators = [
        SimpleNamespace(
            capabilities=SimpleNamespace(
                has_mppt_pv=False, has_solar_telemetry=False
            ),
            data={"battery_power": 1000},
        ),
        SimpleNamespace(
            capabilities=SimpleNamespace(
                has_mppt_pv=False, has_solar_telemetry=False
            ),
            data={"battery_power": -400},
        ),
    ]
    controller = _runtime_controller(coordinators)

    decision = ChargeDischargeController._daily_operation_runtime_decision(
        controller, datetime(2026, 8, 23, 11, 30, tzinfo=MADRID)
    )

    assert decision["action_mask"] == ACTION_GRID_CHARGE | ACTION_DISCHARGE
    assert decision["charge_power_w"] == pytest.approx(1000)
    assert decision["discharge_power_w"] == pytest.approx(400)


def test_runtime_diary_classifies_ac_charge_from_live_external_solar():
    coordinator = SimpleNamespace(
        capabilities=SimpleNamespace(
            has_mppt_pv=False, has_solar_telemetry=False
        ),
        data={"ac_power": -600},
    )
    controller = _runtime_controller(
        [coordinator], grid_active=False, solar_power_kw=1.4
    )

    decision = ChargeDischargeController._daily_operation_runtime_decision(
        controller, datetime(2026, 8, 23, 11, 30, tzinfo=MADRID)
    )

    assert decision["action_mask"] == ACTION_SOLAR_CHARGE


def test_runtime_diary_keeps_grid_label_during_post_window_ac_tail_at_night():
    coordinator = SimpleNamespace(
        capabilities=SimpleNamespace(
            has_mppt_pv=False, has_solar_telemetry=False
        ),
        data={"ac_power": -600},
    )
    controller = _runtime_controller(
        [coordinator], grid_active=False, solar_power_kw=0.0
    )

    decision = ChargeDischargeController._daily_operation_runtime_decision(
        controller, datetime(2026, 8, 24, 0, 15, tzinfo=MADRID)
    )

    assert decision["action_mask"] == ACTION_GRID_CHARGE


def test_runtime_diary_classifies_grid_charge_from_hourly_balance():
    coordinator = SimpleNamespace(
        capabilities=SimpleNamespace(has_mppt_pv=False, has_solar_telemetry=False),
        data={"ac_power": -600},
    )
    controller = _runtime_controller([coordinator])
    controller.hourly_balance_enabled = True
    controller._hourly_balance_mgr = SimpleNamespace(
        get_status_dict=lambda: {"offset_w": 600.0, "in_active_slot": True}
    )

    decision = ChargeDischargeController._daily_operation_runtime_decision(
        controller, datetime(2026, 8, 23, 11, 30, tzinfo=MADRID)
    )

    assert decision["action_mask"] == ACTION_GRID_CHARGE
    assert decision["context_mask"] & CONTEXT_HOURLY_BALANCE
    assert decision["hourly_balance_active"] is True


def test_runtime_diary_classifies_discharge_from_hourly_balance():
    coordinator = SimpleNamespace(
        capabilities=SimpleNamespace(has_mppt_pv=False, has_solar_telemetry=False),
        data={"ac_power": 600},
    )
    controller = _runtime_controller([coordinator])
    controller.hourly_balance_enabled = True
    controller._hourly_balance_mgr = SimpleNamespace(
        get_status_dict=lambda: {"offset_w": -600.0, "in_active_slot": True}
    )

    decision = ChargeDischargeController._daily_operation_runtime_decision(
        controller, datetime(2026, 8, 23, 11, 30, tzinfo=MADRID)
    )

    assert decision["action_mask"] == ACTION_DISCHARGE
    assert decision["context_mask"] & CONTEXT_HOURLY_BALANCE
    assert decision["hourly_balance_active"] is True


@pytest.mark.parametrize(
    ("state", "unlocked", "expected"),
    [
        ("Idle", False, False),
        ("Charging to setpoint", False, False),
        ("Charging allowed", False, False),
        ("Delayed (10:45 est.)", False, True),
        ("Waiting for solar", False, True),
        ("Waiting for forecast", False, True),
        ("Delayed (10:45 est.)", True, False),
    ],
)
def test_runtime_diary_marks_only_real_charge_delay_states(
    state, unlocked, expected
):
    controller = SimpleNamespace(
        charge_delay_enabled=True,
        _charge_delay_unlocked=unlocked,
        _charge_delay_status={"state": state},
    )

    assert (
        ChargeDischargeController._daily_operation_delay_active(controller)
        is expected
    )


def test_runtime_diary_ignores_stale_delay_on_weekly_full_charge_day():
    controller = SimpleNamespace(
        charge_delay_enabled=True,
        _charge_delay_unlocked=False,
        _charge_delay_status={"state": "Delayed (10:45 est.)"},
        _balance_monitor_overrides_delay=lambda: True,
    )

    assert ChargeDischargeController._daily_operation_delay_active(controller) is False


def test_runtime_diary_omits_delay_and_setpoint_context_on_weekly_full_charge_day():
    coordinator = SimpleNamespace(
        capabilities=SimpleNamespace(has_mppt_pv=False, has_solar_telemetry=False),
        data={"battery_power": 1000},
    )
    controller = _runtime_controller([coordinator])
    controller.charge_delay_enabled = True
    controller._delay_soc_setpoint_enabled = True
    controller._delay_setpoint_reached = False
    controller._charge_delay_status = {"state": "Delayed (10:45 est.)"}
    controller._balance_monitor_overrides_delay = lambda: True

    decision = ChargeDischargeController._daily_operation_runtime_decision(
        controller, datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    )

    assert not decision["context_mask"] & (CONTEXT_CHARGE_DELAY | CONTEXT_SETPOINT)
    assert decision["delay_until"] is None


def test_runtime_diary_omits_setpoint_context_when_charge_delay_is_off():
    coordinator = SimpleNamespace(
        capabilities=SimpleNamespace(has_mppt_pv=False, has_solar_telemetry=False),
        data={"battery_power": 1000},
    )
    controller = _runtime_controller([coordinator])
    controller.charge_delay_enabled = False
    controller._delay_soc_setpoint_enabled = True
    controller._delay_setpoint_reached = False
    controller._balance_monitor_overrides_delay = lambda: False

    decision = ChargeDischargeController._daily_operation_runtime_decision(
        controller, datetime(2026, 8, 24, 10, 0, tzinfo=MADRID)
    )

    assert not decision["context_mask"] & CONTEXT_SETPOINT


def test_runtime_delay_boundary_is_removed_when_delay_stops():
    clock = MutableClock(datetime(2026, 8, 24, 10, 0, tzinfo=MADRID))
    manager = _manager(clock, mode="normal")

    manager.record_runtime_decision(
        action_mask=ACTION_GRID_CHARGE,
        context_mask=CONTEXT_CHARGE_DELAY,
        delay_until="10:45",
    )
    assert manager.build_public_snapshot()["operations"]["delay_until"][40] == "10:45"

    manager.record_runtime_decision(
        action_mask=ACTION_GRID_CHARGE,
        context_mask=0,
    )

    assert manager.build_public_snapshot()["operations"]["delay_until"][40] is None


def test_runtime_diary_reports_capacity_weighted_total_soc():
    coordinators = [
        SimpleNamespace(
            capabilities=SimpleNamespace(
                has_mppt_pv=False, has_solar_telemetry=False
            ),
            data={
                "battery_power": 0,
                "battery_total_energy": 10,
                "battery_soc": 20,
            },
        ),
        SimpleNamespace(
            capabilities=SimpleNamespace(
                has_mppt_pv=False, has_solar_telemetry=False
            ),
            data={
                "battery_power": 0,
                "battery_total_energy": 30,
                "battery_soc": 60,
            },
        ),
    ]
    controller = _runtime_controller(coordinators)

    decision = ChargeDischargeController._daily_operation_runtime_decision(
        controller, datetime(2026, 8, 23, 11, 30, tzinfo=MADRID)
    )

    assert decision["soc_pct"] == pytest.approx(50)


def test_projection_keeps_manual_battery_as_fixed_soc_capacity():
    coordinators = [
        SimpleNamespace(
            name="automatic",
            manual=False,
            min_soc=10,
            max_soc=90,
            max_charge_power=2000,
            max_discharge_power=2000,
            data={"battery_total_energy": 10, "battery_soc": 40},
        ),
        SimpleNamespace(
            name="manual",
            manual=True,
            min_soc=10,
            max_soc=90,
            max_charge_power=2000,
            max_discharge_power=2000,
            data={"battery_total_energy": 5, "battery_soc": 80},
        ),
    ]
    controller = SimpleNamespace(
        coordinators=coordinators,
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _is_battery_manual_owned=lambda coordinator: coordinator.manual,
    )

    inputs = ChargeDischargeController._daily_operation_battery_inputs(controller)

    assert [item.key for item in inputs] == ["automatic", "manual"]
    assert inputs[0].can_charge is True
    assert inputs[1].can_charge is False
    assert inputs[1].can_discharge is False
    assert inputs[1].stored_kwh == pytest.approx(4)


def test_projection_starts_from_measured_soc_now_instead_of_replaying_the_day():
    now = datetime(2026, 8, 23, 12, 7, tzinfo=MADRID)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    plan = SimpleNamespace(
        allocations=[],
        intervals=[
            ProjectionIntervalInput(
                start=midnight,
                end=midnight + timedelta(minutes=15),
                consumption_kwh=0,
                solar_kwh=10,
            ),
            ProjectionIntervalInput(
                start=now.replace(minute=0),
                end=now.replace(minute=0) + timedelta(minutes=15),
                consumption_kwh=0,
                solar_kwh=1,
            ),
            ProjectionIntervalInput(
                start=now.replace(minute=15),
                end=now.replace(minute=15) + timedelta(minutes=15),
                consumption_kwh=0,
                solar_kwh=0,
            ),
        ],
    )
    controller = SimpleNamespace(
        _daily_operation_mode=lambda: "normal",
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _daily_operation_battery_inputs=lambda: [
            BatteryProjectionInput(
                key="battery-a",
                stored_kwh=6.4,
                capacity_kwh=10,
                min_soc_pct=10,
                max_soc_pct=100,
                charge_power_w=10_000,
                discharge_power_w=10_000,
            )
        ],
        _consumption_tracker=object(),
        _pricing_mgr=SimpleNamespace(
            build_extended_chronological_projection=lambda **_kwargs: SimpleNamespace(
                plan=plan, diagnostics={}
            )
        ),
        _last_decision_data={},
        _last_chronological_diagnostics={},
        _dynamic_pricing_schedule=None,
        predictive_charging_enabled=False,
        charge_delay_enabled=False,
        _daily_operation_delay_active=lambda: False,
        _delay_soc_setpoint_enabled=False,
        _delay_setpoint_reached=False,
        _charge_delay_unlocked=False,
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )

    assert result is not None
    intervals = result["intervals"]
    assert [item["index"] for item in intervals] == [48, 49]
    assert intervals[0]["solar_kwh"] == pytest.approx(8 / 15)
    assert intervals[0]["soc_end_pct"] == pytest.approx(68.5333333333)
    assert intervals[1]["soc_end_pct"] == pytest.approx(68.5333333333)


def test_projection_limits_charge_delay_clock_to_unlock_boundary():
    now = datetime(2026, 8, 24, 10, 7, tzinfo=MADRID)
    hour = now.replace(minute=0, second=0, microsecond=0)
    plan = SimpleNamespace(
        allocations=[],
        intervals=[
            ProjectionIntervalInput(
                start=hour + timedelta(minutes=15 * index),
                end=hour + timedelta(minutes=15 * (index + 1)),
                consumption_kwh=0.1,
                solar_kwh=0.0,
            )
            for index in range(5)
        ],
    )
    controller = SimpleNamespace(
        _daily_operation_mode=lambda: "time_slot",
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _daily_operation_delay_unlock=(
            lambda current: ChargeDischargeController._daily_operation_delay_unlock(
                controller, current
            )
        ),
        _daily_operation_delay_active=(
            lambda: ChargeDischargeController._daily_operation_delay_active(
                controller
            )
        ),
        _daily_operation_battery_inputs=lambda: [
            BatteryProjectionInput(
                key="battery-a",
                stored_kwh=5.0,
                capacity_kwh=10.0,
                min_soc_pct=10.0,
                max_soc_pct=100.0,
                charge_power_w=2000.0,
                discharge_power_w=2000.0,
            )
        ],
        _consumption_tracker=object(),
        _pricing_mgr=SimpleNamespace(
            build_extended_chronological_projection=lambda **_kwargs: SimpleNamespace(
                plan=plan, diagnostics={}
            ),
            _time_slot_price_slots=lambda _now: [],
        ),
        _last_decision_data={},
        _last_chronological_diagnostics={},
        _dynamic_pricing_schedule=None,
        predictive_charging_enabled=True,
        charge_delay_enabled=True,
        _charge_delay_status={
            "state": "Delayed (10:45 est.)",
            "estimated_unlock_time": "10:45",
        },
        _delay_soc_setpoint_enabled=False,
        _delay_setpoint_reached=False,
        _charge_delay_unlocked=False,
        max_price_threshold=None,
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )

    assert result is not None
    contexts = {
        item["index"]: item["context_mask"] for item in result["intervals"]
    }
    assert contexts[40] & CONTEXT_CHARGE_DELAY
    assert contexts[41] & CONTEXT_CHARGE_DELAY
    assert contexts[42] & CONTEXT_CHARGE_DELAY
    assert not contexts[43] & CONTEXT_CHARGE_DELAY
    assert not contexts[44] & CONTEXT_CHARGE_DELAY
    delayed = [
        item for item in result["intervals"]
        if item["context_mask"] & CONTEXT_CHARGE_DELAY
    ]
    assert {item["delay_until"] for item in delayed} == {
        datetime(2026, 8, 24, 10, 45, tzinfo=MADRID)
    }


def test_midnight_refresh_normalizes_utc_and_publishes_one_complete_snapshot():
    clock = MutableClock(datetime(2026, 8, 23, 23, 59, tzinfo=MADRID))
    manager = _manager(clock, mode="normal")
    manager.rebuild_future_projection(
        [{"index": 95, "action_mask": ACTION_DISCHARGE}], mode="normal"
    )
    published = []
    manager.async_add_listener(lambda *_args: published.append(manager.snapshot()))

    clock.value = datetime(2026, 8, 24, 0, 0, tzinfo=MADRID)
    # Polling the sensor before the control refresh must keep the last coherent
    # day instead of publishing a transient empty new-day payload.
    before_refresh = manager.snapshot()
    assert before_refresh["local_date"] == "2026-08-23"
    assert before_refresh["operations"]["planned_action_mask"][95] == ACTION_DISCHARGE

    projection_times = []

    def build_projection(current):
        projection_times.append(current)
        return {
            "intervals": [{"index": 0, "action_mask": ACTION_GRID_CHARGE}],
            "mode": "normal",
            "stale": False,
            "sources": {"operation_plan": "projection"},
        }

    controller = SimpleNamespace(
        _daily_operation_timeline=manager,
        _consumption_tracker=SimpleNamespace(
            consumption_profile=None, solar_profile=None
        ),
        _daily_operation_capture=ChargeDischargeController._daily_operation_capture,
        _daily_operation_runtime_decision=lambda _current, **_kwargs: {
            "action_mask": 0,
            "context_mask": 0,
            "simultaneous": False,
        },
        _daily_operation_last_runtime_at=None,
        _dynamic_pricing_schedule=None,
        _daily_operation_mode=lambda: "normal",
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _daily_operation_last_projection_signature=None,
        _daily_operation_last_projection_monotonic=0.0,
        _daily_operation_build_projection=build_projection,
        _delay_soc_setpoint_enabled=False,
        _delay_soc_setpoint=0.0,
        _delay_setpoint_reached=False,
        charge_delay_enabled=False,
        _charge_delay_unlocked=False,
        _charge_delay_status={},
    )

    ChargeDischargeController._refresh_daily_operation_timeline(
        controller,
        now=datetime(2026, 8, 23, 22, 0, tzinfo=timezone.utc),
        force_projection=True,
    )

    assert projection_times == [datetime(2026, 8, 24, 0, 0, tzinfo=MADRID)]
    assert len(published) == 1
    assert published[0]["local_date"] == "2026-08-24"
    assert published[0]["operations"]["planned_action_mask"][0] == ACTION_GRID_CHARGE


def test_projection_replaces_null_delay_clock_and_marks_setpoint_milestones():
    now = datetime(2026, 8, 24, 0, 0, tzinfo=MADRID)
    clock = MutableClock(now)
    manager = _manager(clock, mode="normal")
    projected_setpoint = now + timedelta(hours=1)
    projected_unlock = now + timedelta(hours=11)

    controller = SimpleNamespace(
        _daily_operation_timeline=manager,
        _consumption_tracker=SimpleNamespace(
            consumption_profile=None, solar_profile=None
        ),
        _daily_operation_capture=ChargeDischargeController._daily_operation_capture,
        _daily_operation_runtime_decision=lambda _current, **_kwargs: {
            "action_mask": 0,
            "context_mask": 0,
            "simultaneous": False,
        },
        _daily_operation_last_runtime_at=None,
        _dynamic_pricing_schedule=None,
        _daily_operation_mode=lambda: "normal",
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _daily_operation_last_projection_signature=None,
        _daily_operation_last_projection_monotonic=0.0,
        _daily_operation_build_projection=lambda _now: {
            "intervals": [],
            "mode": "normal",
            "stale": False,
            "sources": {"operation_plan": "projection"},
            "_delay_projection": {
                "setpoint_reached_at": projected_setpoint,
                "estimated_unlock_at": projected_unlock,
            },
        },
        _delay_soc_setpoint_enabled=True,
        _delay_soc_setpoint=50.0,
        _delay_setpoint_reached=False,
        charge_delay_enabled=True,
        _charge_delay_unlocked=False,
        _charge_delay_status={
            "state": "Charging to setpoint",
            "estimated_unlock_time": None,
        },
        _balance_monitor_overrides_delay=lambda: False,
    )

    ChargeDischargeController._refresh_daily_operation_timeline(
        controller, now=now, force_projection=True
    )

    assert controller._charge_delay_status["projected_unlock_time"] == projected_unlock
    assert controller._charge_delay_status["estimated_setpoint_time"] == projected_setpoint
    assert manager._delay_info["estimated_unlock_time"] == projected_unlock.isoformat()
    assert manager._delay_info["projected_unlock_time"] == projected_unlock.isoformat()


def test_fall_back_hour_keeps_the_second_occurrence_writable():
    clock = MutableClock(datetime(2026, 10, 25, 2, 0, tzinfo=MADRID, fold=0))
    manager = _manager(clock, mode="normal")

    assert manager.record_runtime_decision(action_mask=ACTION_SOLAR_CHARGE) is True
    clock.value = datetime(2026, 10, 25, 2, 59, tzinfo=MADRID, fold=0)
    manager.refresh_actual_partial(consumption_kwh=0.1, coverage_s=899)
    assert manager.current_index == 11
    assert manager.closed_intervals[8] is False

    # Although the wall clock moves backwards, this is a later absolute
    # instant and belongs to the second occurrence of the 02:00 cell.
    clock.value = datetime(2026, 10, 25, 2, 0, tzinfo=MADRID, fold=1)
    assert manager.record_runtime_decision(action_mask=ACTION_GRID_CHARGE) is True
    assert manager.current_index == 8
    assert manager.closed_intervals[8] is False
    snapshot = manager.build_public_snapshot()
    assert snapshot["operations"]["actual_action_mask"][8] == (
        ACTION_SOLAR_CHARGE | ACTION_GRID_CHARGE
    )

    clock.value = datetime(2026, 10, 25, 2, 15, tzinfo=MADRID, fold=1)
    manager.refresh_actual_partial(consumption_kwh=0.2, coverage_s=0)
    assert manager.closed_intervals[8] is True
    assert manager.current_index == 9


def test_snapshot_poll_does_not_close_a_partial_previous_interval():
    clock = MutableClock(datetime(2026, 8, 23, 10, 14, 59, tzinfo=MADRID))
    manager = _manager(clock)
    manager.refresh_actual_partial(_capture(0.1, 899), _capture(0.2, 899))

    clock.value = datetime(2026, 8, 23, 10, 15, tzinfo=MADRID)
    snapshot = manager.build_public_snapshot()
    assert snapshot["operations"]["closed"][40] is False

    # The capture arrives after the entity polling boundary and must still
    # replace the cumulative values for the quarter that just finished.
    manager.refresh_actual_partial(_capture(0.3), _capture(0.4))
    after = manager.build_public_snapshot()
    assert after["series"]["consumption_actual_kwh"][40] == pytest.approx(0.3)
    assert after["series"]["solar_actual_kwh"][40] == pytest.approx(0.4)
    assert after["operations"]["closed"][40] is True


def test_snapshot_cache_reuses_same_revision_and_only_refreshes_progress():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)

    first = manager.build_public_snapshot()
    assert manager.snapshot_build_count == 1
    first_progress = first["current_progress"]
    first_revision = manager.revision

    clock.value = datetime(2026, 8, 23, 10, 10, tzinfo=MADRID)
    second = manager.build_public_snapshot()

    assert second is first
    assert manager.revision == first_revision
    assert manager.snapshot_build_count == 1
    assert second["current_progress"] > first_progress

    manager.record_runtime_decision(action_mask=ACTION_GRID_CHARGE)
    third = manager.build_public_snapshot()
    assert third is not second
    assert manager.snapshot_build_count == 2


def test_identical_projection_refresh_does_not_publish_only_new_evaluation_time():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock, mode="normal")
    projection = [{"index": 42, "action_mask": ACTION_GRID_CHARGE}]

    assert manager.rebuild_future_projection(
        projection, mode="normal", evaluated_at=clock.value
    ) is True
    revision = manager.revision

    clock.value = datetime(2026, 8, 23, 10, 8, tzinfo=MADRID)
    assert manager.rebuild_future_projection(
        projection, mode="normal", evaluated_at=clock.value
    ) is False
    assert manager.revision == revision


def test_out_of_order_callback_cannot_reopen_or_move_the_clock_backwards():
    clock = MutableClock(datetime(2026, 8, 23, 10, 20, tzinfo=MADRID))
    manager = _manager(clock)
    manager.refresh_actual_partial(consumption_kwh=0.2, coverage_s=300)
    assert manager.current_index == 41
    assert manager.closed_intervals[40] is True

    late = datetime(2026, 8, 23, 10, 10, tzinfo=MADRID)
    assert manager.record_runtime_decision(action_mask=ACTION_GRID_CHARGE, at=late) is False
    assert manager.current_index == 41
    assert manager.closed_intervals[40] is True


@pytest.mark.asyncio
async def test_fake_persistence_restores_current_day_and_actions():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    store = FakeStore()
    manager = _manager(clock, store)

    manager.refresh_actual_partial(_capture(0.21), _capture(0.42, 600.0))
    manager.record_runtime_decision(
        {"source": "chronological", "slot": "slot-1", "soc_pct": 56.5},
        action_mask=ACTION_SOLAR_CHARGE | ACTION_GRID_CHARGE,
        context_mask=CONTEXT_DYNAMIC_PRICE | CONTEXT_HOURLY_BALANCE,
        charge_power_w=1200,
        duration_s=120,
        simultaneous=True,
    )
    await manager.async_save_all()

    assert len(store.writes) == 1
    restored = _manager(clock, store)
    assert await restored.async_load() is True

    snapshot = restored.build_public_snapshot()
    assert snapshot["local_date"] == "2026-08-23"
    assert snapshot["operations"]["actual_action_mask"][40] == 3
    assert snapshot["operations"]["actual_context_mask"][40] & CONTEXT_DYNAMIC_PRICE
    assert snapshot["operations"]["actual_context_mask"][40] & CONTEXT_HOURLY_BALANCE
    assert (
        snapshot["operations"]["observed_seconds_by_action_by_interval"][40]["solar_charge"]
        == 120
    )
    assert snapshot["operations"]["actual_charge_to_battery_kwh"][40] == pytest.approx(
        0.04
    )
    assert snapshot["operations"]["charge_to_battery_kwh"][40] == pytest.approx(
        0.04
    )
    assert snapshot["operations"]["actual_soc_pct"][40] == pytest.approx(56.5)
    assert snapshot["operations"]["soc_pct"][40] == pytest.approx(56.5)
    assert snapshot["series"]["solar_actual_kwh"][40] == pytest.approx(0.42)
    assert snapshot["series"]["consumption_actual_kwh"][40] == pytest.approx(0.21)


@pytest.mark.asyncio
async def test_config_change_restores_actual_activity_and_discards_old_projection():
    """A reload after changing options must not erase earlier operation evidence."""
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    store = FakeStore()
    manager = _manager(clock, store)
    manager.record_runtime_decision(
        action_mask=ACTION_DISCHARGE,
        discharge_power_w=400,
        duration_s=300,
        source="runtime_measured",
    )
    manager.rebuild_future_projection(
        [{"index": 41, "action_mask": ACTION_GRID_CHARGE, "solar_kwh": 0.4}],
        now=clock(),
    )
    await manager.async_save_all()

    restored = _manager(clock, store)
    restored._config_entry.options["offgrid_enabled"] = True

    assert await restored.async_load() is True
    snapshot = restored.build_public_snapshot()
    assert snapshot["operations"]["actual_action_mask"][40] == ACTION_DISCHARGE
    assert snapshot["operations"]["actual_discharge_power_w"][40] == pytest.approx(400)
    assert snapshot["operations"]["discharge_from_battery_kwh"][40] == pytest.approx(
        1 / 30
    )
    assert snapshot["operations"]["actual_source"][40] == "runtime_measured"
    assert snapshot["operations"]["planned_action_mask"][41] == 0
    assert snapshot["series"]["solar_forecast_kwh"][41] is None

    await restored.async_save_all()
    assert store.data["configuration_fingerprint"] == restored.configuration_fingerprint()


@pytest.mark.asyncio
async def test_persistence_retries_when_data_changes_during_an_inflight_save():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    store = BlockingStore()
    manager = _manager(clock, store)

    manager.update_runtime_metadata(stale=True)
    manager.request_save(immediate=True)
    await store.started.wait()

    manager.update_runtime_metadata(stale_reason="newer-runtime-state")
    store.release.set()
    assert await manager.async_save_all() is True

    assert len(store.writes) == 2
    assert store.writes[-1]["metadata"]["stale_reason"] == "newer-runtime-state"


def test_closed_intervals_are_immutable_and_reevaluation_starts_at_present():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)
    manager.refresh_actual_partial(consumption_kwh=1.0, solar_kwh=0.5, coverage_s=300)
    manager.record_runtime_decision(action_mask=ACTION_SOLAR_CHARGE, duration_s=300)
    manager.rebuild_future_projection(
        [{"index": 40, "action_mask": ACTION_GRID_CHARGE}, {"index": 41, "action_mask": ACTION_DISCHARGE}],
        mode="dynamic_pricing",
    )

    clock.value = datetime(2026, 8, 23, 10, 16, tzinfo=MADRID)
    manager.refresh_actual_partial(consumption_kwh=2.0, solar_kwh=0.1, coverage_s=100)
    before = manager.build_public_snapshot()
    closed_action = before["operations"]["actual_action_mask"][40]
    closed_solar = before["series"]["solar_actual_kwh"][40]
    assert before["operations"]["closed"][40] is True

    # The old current interval is now closed.  Both an old runtime callback and
    # a new projection are forbidden from rewriting it.
    assert manager.record_runtime_decision(
        {"action_mask": ACTION_DISCHARGE}, at=datetime(2026, 8, 23, 10, 5, tzinfo=MADRID)
    ) is False
    manager.rebuild_future_projection(
        [
            {"index": 40, "action_mask": ACTION_DISCHARGE},
            {"index": 41, "action_mask": ACTION_GRID_CHARGE},
        ],
        mode="dynamic_pricing",
    )
    after = manager.build_public_snapshot()
    assert after["operations"]["actual_action_mask"][40] == closed_action
    assert after["series"]["solar_actual_kwh"][40] == closed_solar
    assert after["operations"]["planned_action_mask"][41] == ACTION_GRID_CHARGE


def test_future_charge_energy_combines_solar_and_grid_flows():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)
    manager.rebuild_future_projection(
        [
            {
                "index": 41,
                "action_mask": ACTION_SOLAR_CHARGE | ACTION_GRID_CHARGE,
                "solar_to_battery_kwh": 0.2,
                "grid_to_battery_kwh": 0.1,
                "soc_end_pct": 72.5,
            }
        ],
        mode="dynamic_pricing",
    )

    snapshot = manager.build_public_snapshot()

    assert snapshot["operations"]["planned_charge_to_battery_kwh"][41] == pytest.approx(
        0.3
    )
    assert snapshot["operations"]["charge_to_battery_kwh"][41] == pytest.approx(
        0.3
    )
    assert snapshot["operations"]["planned_soc_pct"][41] == pytest.approx(72.5)
    assert snapshot["operations"]["soc_pct"][41] == pytest.approx(72.5)


def test_extended_projection_keeps_the_next_twelve_hours_separate_from_daily_arrays():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)
    manager.rebuild_future_projection(
        {
            "intervals": [
                {"index": 41, "solar_kwh": 0.1},
            ],
            "extended_intervals": [
                {
                    "extension_index": 0,
                    "start": "2026-08-24T00:00:00+02:00",
                    "end": "2026-08-24T00:15:00+02:00",
                    "consumption_kwh": 0.2,
                    "action_mask": ACTION_DISCHARGE,
                    "soc_end_pct": 48.5,
                },
                {
                    "extension_index": 48,
                    "start": "2026-08-24T12:00:00+02:00",
                    "end": "2026-08-24T12:15:00+02:00",
                    "consumption_kwh": 9.9,
                },
            ],
        },
        mode="dynamic_pricing",
    )

    snapshot = manager.build_public_snapshot()

    assert snapshot["interval_count"] == 96
    assert len(snapshot["series"]["consumption_forecast_kwh"]) == 96
    assert snapshot["extended_horizon"]["interval_count"] == 48
    assert snapshot["extended_horizon"]["start"].startswith("2026-08-24T00:00")
    assert len(snapshot["extended_projection"]) == 1
    assert snapshot["extended_projection"][0]["extension_index"] == 0
    assert snapshot["extended_projection"][0]["consumption_kwh"] == pytest.approx(0.2)
    assert snapshot["extended_projection"][0]["soc_end_pct"] == pytest.approx(48.5)


@pytest.mark.parametrize(
    ("current", "flag", "expected_duration"),
    [
        (
            datetime(2026, 3, 28, 10, 7, tzinfo=MADRID),
            "dst_skipped",
            0.0,
        ),
        (
            datetime(2026, 10, 24, 10, 7, tzinfo=MADRID),
            "dst_repeated",
            1800.0,
        ),
    ],
)
def test_extended_horizon_exposes_next_day_dst_cells(
    current: datetime,
    flag: str,
    expected_duration: float,
):
    manager = _manager(MutableClock(current))

    horizon = manager.build_public_snapshot()["extended_horizon"]

    assert len(horizon["duration_s"]) == 48
    assert len(horizon["dst_skipped"]) == 48
    assert len(horizon["dst_repeated"]) == 48
    assert horizon[flag][8:12] == [True] * 4
    assert horizon["duration_s"][8:12] == [expected_duration] * 4


@pytest.mark.asyncio
async def test_extended_projection_survives_store_round_trip():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    store = FakeStore()
    manager = _manager(clock, store)
    manager.rebuild_future_projection(
        {
            "extended_intervals": [
                {
                    "extension_index": 4,
                    "start": "2026-08-24T01:00:00+02:00",
                    "end": "2026-08-24T01:15:00+02:00",
                    "solar_kwh": 0.12,
                }
            ]
        },
        mode="dynamic_pricing",
    )
    await manager.async_save_all()

    restored = _manager(clock, store)
    assert await restored.async_load() is True
    projection = restored.build_public_snapshot()["extended_projection"]
    assert projection[0]["extension_index"] == 4
    assert projection[0]["solar_kwh"] == pytest.approx(0.12)


def test_actual_charge_and_discharge_energy_are_accumulated_separately():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)
    manager.record_runtime_decision(
        action_mask=ACTION_SOLAR_CHARGE | ACTION_DISCHARGE,
        charge_power_w=1200,
        discharge_power_w=600,
        duration_s=300,
        simultaneous=True,
    )

    snapshot = manager.build_public_snapshot()

    assert snapshot["operations"]["actual_charge_to_battery_kwh"][40] == pytest.approx(
        0.1
    )
    assert snapshot["operations"]["actual_discharge_from_battery_kwh"][40] == pytest.approx(
        0.05
    )
    assert snapshot["operations"]["charge_to_battery_kwh"][40] == pytest.approx(0.1)
    assert snapshot["operations"]["discharge_from_battery_kwh"][40] == pytest.approx(
        0.05
    )


@pytest.mark.asyncio
async def test_corrupt_store_degrades_to_empty_diary():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    store = FakeStore(["not", "a", "timeline"])
    manager = _manager(clock, store)

    assert await manager.async_load() is False
    assert manager.last_error == "load: invalid_store"
    snapshot = manager.build_public_snapshot()
    assert snapshot["interval_count"] == 96
    assert all(value == 0 for value in snapshot["operations"]["actual_action_mask"])


@pytest.mark.asyncio
async def test_debounce_coalesces_many_runtime_updates():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    store = FakeStore()
    manager = _manager(clock, store)

    for duration in (1, 2, 3, 4):
        manager.record_runtime_decision(action_mask=ACTION_GRID_CHARGE, duration_s=duration)
    await asyncio.sleep(0.03)

    assert len(store.writes) == 1
    assert store.writes[0]["cells"][40]["observed_seconds_by_action"]["grid_charge"] == 10


def test_snapshot_has_96_lists_and_is_strictly_json_safe():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)
    manager.refresh_actual_partial(
        consumption_kwh=float("nan"),
        solar_kwh=float("inf"),
        coverage_s=float("nan"),
    )
    manager.rebuild_future_projection(
        [{"index": 41, "solar_kwh": float("nan"), "consumption_kwh": 0.2}],
        mode="dynamic_pricing",
    )
    snapshot = manager.build_public_snapshot()

    assert snapshot["interval_count"] == 96
    for section in (snapshot["series"], snapshot["operations"]):
        for value in section.values():
            if isinstance(value, list):
                assert len(value) == 96
    json.dumps(snapshot, allow_nan=False)


def test_realtime_price_keeps_real_current_decision_but_no_future_plan():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock, mode="realtime_price")
    manager.record_runtime_decision(
        {"mode": "realtime_price", "grid_charge_decision": "scheduled"},
        action_mask=ACTION_GRID_CHARGE,
        duration_s=90,
    )
    manager.rebuild_future_projection(
        [
            {"index": 40, "action_mask": ACTION_GRID_CHARGE, "solar_kwh": 1.0},
            {"index": 41, "action_mask": ACTION_GRID_CHARGE, "solar_kwh": 1.0},
            {"index": 42, "action_mask": ACTION_DISCHARGE, "solar_kwh": 1.0},
        ],
        mode="realtime_price",
    )
    snapshot = manager.build_public_snapshot()

    assert snapshot["operations"]["actual_action_mask"][40] == ACTION_GRID_CHARGE
    assert snapshot["operations"]["planned_action_mask"][40] == ACTION_GRID_CHARGE
    assert snapshot["operations"]["planned_action_mask"][41] == 0
    assert snapshot["operations"]["planned_action_mask"][42] == 0
    assert snapshot["series"]["solar_forecast_kwh"][41] is None
    assert snapshot["series"]["solar_forecast_kwh"][42] is None


def test_canonical_grid_decisions_round_trip_through_the_normalizer():
    for decision in GRID_CHARGE_DECISIONS:
        assert _normalize_grid_decision(decision) == decision


def test_charge_power_infers_grid_charge_from_a_scheduled_decision():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)

    manager.record_runtime_decision(
        {"grid_charge_decision": GRID_CHARGE_SCHEDULED, "charge_power_w": 1200.0},
        duration_s=90,
    )
    snapshot = manager.build_public_snapshot()

    assert snapshot["operations"]["actual_action_mask"][40] == ACTION_GRID_CHARGE
    assert (
        snapshot["operations"]["actual_grid_charge_decision"][40]
        == GRID_CHARGE_SCHEDULED
    )
    assert snapshot["operations"]["actual_context_mask"][40] & CONTEXT_DYNAMIC_PRICE


def test_in_progress_interval_publishes_the_full_quarter_forecast():
    now = datetime(2026, 8, 23, 13, 7, tzinfo=MADRID)
    interval_start = now.replace(minute=0)
    intervals = [
        ProjectionIntervalInput(
            interval_start + timedelta(minutes=15 * offset),
            interval_start + timedelta(minutes=15 * (offset + 1)),
            consumption_kwh=0.2,
            solar_kwh=0.4,
        )
        for offset in range(2)
    ]
    projection = build_daily_operation_projection(
        DailyOperationProjectionRequest(
            now=now,
            plan_intervals=tuple(intervals),
            allocations=(),
            battery_inputs=(BatteryProjectionInput("a", 5, 10, 0, 100, 4000, 4000),),
            mode="normal",
            decision_data={},
        )
    )
    # The in-progress interval only keeps 8 of its 15 minutes, so the battery
    # simulation must still see the prorated remainder.
    current = next(item for item in projection["intervals"] if item["index"] == 52)
    assert current["consumption_kwh"] == pytest.approx(0.2 * 8 / 15)
    assert current["solar_kwh"] == pytest.approx(0.4 * 8 / 15)

    clock = MutableClock(now)
    manager = _manager(clock, mode="normal")
    manager.rebuild_future_projection(projection, mode="normal")
    series = manager.build_public_snapshot()["series"]

    assert series["consumption_forecast_kwh"][52] == pytest.approx(0.2)
    assert series["solar_forecast_kwh"][52] == pytest.approx(0.4)
    assert series["consumption_forecast_kwh"][53] == pytest.approx(0.2)
    assert series["solar_forecast_kwh"][53] == pytest.approx(0.4)

    # Closing the interval must not freeze a prorated remnant into the past.
    clock.value = now.replace(minute=20)
    manager.rebuild_future_projection(projection, mode="normal")
    closed = manager.build_public_snapshot()["series"]
    assert closed["consumption_forecast_kwh"][52] == pytest.approx(0.2)
    assert closed["solar_forecast_kwh"][52] == pytest.approx(0.4)
