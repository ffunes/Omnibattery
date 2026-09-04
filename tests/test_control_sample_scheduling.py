"""Regression tests for separating meter health from control samples.

The controller is exercised with small stubs so these tests cover the real
``_run_control_cycle`` and predictive-control branches without requiring a live
Home Assistant event loop or a battery connection.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from homeassistant.core import State

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import PREDICTIVE_MODE_DYNAMIC_PRICING


class _HashableNamespace(SimpleNamespace):
    __hash__ = object.__hash__
    __eq__ = object.__eq__


async def _async_noop(*_args, **_kwargs):
    return None


async def _async_false(*_args, **_kwargs):
    return False


def _state(value, reported_at, *, updated_at=None, attributes=None):
    return State(
        "sensor.grid_power",
        str(value),
        attributes=attributes or {},
        last_changed=updated_at or reported_at,
        last_reported=reported_at,
        last_updated=updated_at or reported_at,
    )


def _main_controller(state_holder, pd_calls):
    class _Coordinator(SimpleNamespace):
        __hash__ = object.__hash__

    coordinator = _Coordinator(
        _is_shutting_down=False,
        is_available=True,
        data={"battery_soc": 50},
        max_soc=90,
        min_soc=10,
        name="battery",
        commanded_charge_power=0,
        commanded_discharge_power=0,
    )

    async def _max_soc_measurement(*_args, **_kwargs):
        return False

    class _States:
        def get(self, _entity_id):
            return state_holder["state"]

    def _pd(error, sensor_elapsed_s, stale_safety_recalc):
        pd_calls.append((error, sensor_elapsed_s, stale_safety_recalc))
        return -500.0

    controller = SimpleNamespace(
        coordinators=[coordinator],
        _phase_power_limiter=SimpleNamespace(
            enabled=False,
            begin_cycle=lambda: None,
        ),
        _consumption_tracker=None,
        _balance_monitor=None,
        _pricing_mgr=SimpleNamespace(maybe_check_price_data_health=lambda: None),
        manual_mode_enabled=False,
        _weekly_charge_mgr=SimpleNamespace(handle_registers=_async_noop),
        _charge_delay_mgr=SimpleNamespace(handle_daily_reset_and_eval=lambda: None),
        _refresh_operation_blockers=lambda: None,
        _try_apply_manual_slot=_async_noop,
        _phase_safety_pending=False,
        _max_soc_mgr=SimpleNamespace(handle_measurement=_max_soc_measurement),
        predictive_charging_enabled=False,
        previous_power=-400.0,
        previous_sensor=None,
        previous_error=0.0,
        first_execution=False,
        last_output_sign=-1,
        last_error_sign=0,
        sign_changes=0,
        error_integral=0.0,
        derivative_filtered=0.0,
        _stale_cycles=0,
        _last_sensor_report_time=None,
        _last_sensor_cadence_time=None,
        _last_control_sample_value=None,
        _control_sample_is_new=True,
        _slow_sensor_issue_created=False,
        _slow_sensor_intervals=0,
        _fast_sensor_intervals=0,
        _max_sensor_stale_s=65.0,
        consumption_sensor="sensor.grid_power",
        config_entry=SimpleNamespace(entry_id="control-sample", data={}),
        hass=SimpleNamespace(states=_States()),
        _apply_meter_transform=lambda state: float(state.state),
        _check_solar_forecast_health=lambda: None,
        _is_capacity_protection_soc_limited=lambda: False,
        _filter_grid_sample=lambda raw, _elapsed: raw,
        compute_active_target=lambda: 0.0,
        _resolve_home_consumption_sensor=lambda: None,
        _external_loads=SimpleNamespace(
            calculate_adjustment=lambda: 0.0,
            check_ev_charger_state=lambda: (False, False),
        ),
        _hourly_balance_mgr=None,
        _apply_capacity_protection=lambda sensor, target: (target, sensor),
        _capacity_protection_force_idle=False,
        deadband=40.0,
        _is_charge_blocked=lambda *_args, **_kwargs: False,
        _is_discharge_blocked=lambda *_args, **_kwargs: False,
        is_charge_blocked=lambda *_args, **_kwargs: False,
        is_discharge_blocked=lambda *_args, **_kwargs: False,
        _stop_blocked_active_batteries=_async_false,
        _stop_all_batteries_for_block=_async_noop,
        _refresh_effective_system_capacities=lambda: None,
        no_pd_mode_enabled=False,
        _check_feedforward_step=lambda _error: False,
        _compute_pd_new_power=_pd,
        _apply_zero_cross_hold=lambda power, _error, stale_recalc=False: power,
        _apply_min_power=lambda power, _error: power,
        _apply_relay_dwell=lambda power, _error: power,
        _is_operation_allowed=lambda _is_charging: True,
        _price_based_discharge_blocked=False,
        _solar_surplus_discharge_blocked=False,
        _get_available_batteries=lambda is_charging, include_operation_blocks=True: [coordinator],
        _effective_system_capacity=lambda _batteries, is_charging: 2500.0,
        max_contracted_power=0,
        grid_charging_active=False,
        _daily_grid_at_min_soc_kwh=0.0,
        _grid_at_min_soc_last_ts=None,
        _grid_at_min_soc_sensor=None,
        _power_distribution=SimpleNamespace(
            _select_batteries_for_operation=lambda _power, batteries, is_charging=None: batteries,
            _distribute_power_by_limits=lambda power, batteries, is_charging=None: {
                battery: power / len(batteries) for battery in batteries
            },
        ),
        _log_power_command_plan=lambda **_kwargs: None,
        _set_battery_power=_async_noop,
        _update_pd_quality_metrics=lambda *_args, **_kwargs: None,
        _pd_demand_blocked=lambda _error, _commanded_power: False,
        _set_pd_limited=lambda _value: None,
        _set_pd_blocked=lambda _value: None,
        _pd_limited=False,
        _pd_blocked=False,
        _active_discharge_batteries=[],
        _active_charge_batteries=[],
    )
    controller._run_control_cycle = ChargeDischargeController._run_control_cycle.__get__(
        controller, ChargeDischargeController
    )
    controller._track_sensor_report = ChargeDischargeController._track_sensor_report.__get__(
        controller, ChargeDischargeController
    )
    controller._sensor_age_seconds = ChargeDischargeController._sensor_age_seconds.__get__(
        controller, ChargeDischargeController
    )
    controller._sensor_is_within_stale_tolerance = ChargeDischargeController._sensor_is_within_stale_tolerance.__get__(
        controller, ChargeDischargeController
    )
    return controller


def test_repeated_publication_does_not_reapply_pd_but_real_change_runs_once():
    first_report = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state_holder = {"state": _state(100, first_report)}
    pd_calls = []
    controller = _main_controller(state_holder, pd_calls)

    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=1)))
    assert len(pd_calls) == 1
    previous_power = controller.previous_power

    # Same transformed value, newer last_reported, unchanged last_updated.
    state_holder["state"] = _state(
        100,
        first_report + timedelta(seconds=4),
        updated_at=first_report,
    )
    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=4)))
    assert len(pd_calls) == 1
    assert controller.previous_power == previous_power

    # A real value change is consumed exactly once by the incremental PD path.
    state_holder["state"] = _state(
        200,
        first_report + timedelta(seconds=8),
        updated_at=first_report + timedelta(seconds=8),
    )
    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=8)))
    assert len(pd_calls) == 2

    # The watchdog sees the already-consumed state but must not apply P/D again.
    previous_power = controller.previous_power
    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=10)))
    assert len(pd_calls) == 2
    assert controller.previous_power == previous_power


def test_unavailable_consumption_sensor_does_not_spam_warnings(caplog):
    reported_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state_holder = {"state": _state("unavailable", reported_at)}
    controller = _main_controller(state_holder, [])
    controller.meter_inverted = False
    controller._apply_meter_transform = (
        ChargeDischargeController._apply_meter_transform.__get__(
            controller, ChargeDischargeController
        )
    )
    controller._log_consumption_sensor_issue = (
        ChargeDischargeController._log_consumption_sensor_issue.__get__(
            controller, ChargeDischargeController
        )
    )
    caplog.set_level(logging.DEBUG, logger="custom_components.omnibattery")

    asyncio.run(controller._run_control_cycle(now=reported_at))
    asyncio.run(controller._run_control_cycle(now=reported_at + timedelta(seconds=2)))

    matching = [
        record
        for record in caplog.records
        if "Consumption sensor sensor.grid_power" in record.getMessage()
    ]
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert [record.levelno for record in matching] == [logging.DEBUG]
    assert "is unavailable" in matching[0].getMessage()

    # A numeric sample ends the episode, so a later outage is reported once again.
    state_holder["state"] = _state(100, reported_at + timedelta(seconds=4))
    asyncio.run(controller._run_control_cycle(now=reported_at + timedelta(seconds=4)))
    state_holder["state"] = _state("unknown", reported_at + timedelta(seconds=6))
    asyncio.run(controller._run_control_cycle(now=reported_at + timedelta(seconds=6)))

    matching = [
        record
        for record in caplog.records
        if "Consumption sensor sensor.grid_power" in record.getMessage()
    ]
    assert [record.levelno for record in matching] == [logging.DEBUG, logging.DEBUG]
    assert "is unknown" in matching[1].getMessage()


def test_manual_grid_charge_does_not_induce_automatic_discharge_when_idle():
    first_report = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state_holder = {"state": _state(700, first_report)}
    pd_calls = []
    controller = _main_controller(state_holder, pd_calls)
    controller.previous_power = 0.0
    controller.last_output_sign = 0
    controller.ki = 0.0
    controller._power_distribution._rebalance_expired_load_sharing_hold = _async_false

    manual = _HashableNamespace(
        _is_shutting_down=False,
        battery_manual_mode_enabled=True,
        data={"battery_power": 700},
        name="manual battery",
    )
    controller.coordinators.append(manual)

    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=1)))

    # With no automatic charge active, the intentional manual import is not a
    # reason to discharge an automatic battery.
    assert pd_calls == []
    assert controller.previous_power == 0.0


def test_manual_grid_charge_reduces_automatic_charge_without_discharge():
    first_report = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state_holder = {"state": _state(1000, first_report)}
    pd_calls = []
    controller = _main_controller(state_holder, pd_calls)
    controller.previous_power = 2000.0
    controller.last_output_sign = 1
    controller.ki = 0.0
    controller._power_distribution._rebalance_expired_load_sharing_hold = _async_false
    controller.coordinators[0].data = {
        "battery_soc": 50,
        "battery_power": 2000,
    }

    def _reduce_automatic_charge(error, sensor_elapsed_s, stale_safety_recalc):
        pd_calls.append((error, sensor_elapsed_s, stale_safety_recalc))
        return 1000.0

    controller._compute_pd_new_power = _reduce_automatic_charge
    manual = _HashableNamespace(
        _is_shutting_down=False,
        battery_manual_mode_enabled=True,
        data={"battery_power": 1000},
        name="manual battery",
    )
    controller.coordinators.append(manual)

    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=1)))

    # The 1 kW import is used to reduce the automatic 2 kW charge to 1 kW;
    # it must not be turned into an automatic discharge command.
    assert pd_calls and pd_calls[0][0] == 1000
    assert controller.previous_power == 1000.0


def test_busy_loop_cadence_does_not_consume_pending_real_change():
    first_report = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state_holder = {"state": _state(100, first_report)}
    pd_calls = []
    controller = _main_controller(state_holder, pd_calls)

    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=1)))
    assert len(pd_calls) == 1

    # Publication callbacks continue to feed cadence while the previous cycle is
    # busy. They intentionally do not update the last control fingerprint.
    for seconds in (4, 8):
        ChargeDischargeController._observe_sensor_cadence(
            controller, first_report + timedelta(seconds=seconds)
        )

    state_holder["state"] = _state(
        250,
        first_report + timedelta(seconds=8),
        updated_at=first_report + timedelta(seconds=8),
    )
    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=9)))

    assert len(pd_calls) == 2
    assert controller._last_sensor_cadence_time == first_report + timedelta(seconds=8)


def test_silent_sensor_uses_stale_safety_without_reapplying_pd():
    first_report = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state_holder = {"state": _state(100, first_report)}
    pd_calls = []
    controller = _main_controller(state_holder, pd_calls)

    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=1)))
    previous_power = controller.previous_power

    asyncio.run(controller._run_control_cycle(now=first_report + timedelta(seconds=70)))

    assert len(pd_calls) == 1
    assert controller.previous_power == previous_power


def _predictive_controller(state_holder, writes):
    class _Coordinator(SimpleNamespace):
        __hash__ = object.__hash__

    coordinator = _Coordinator(name="battery", data={"battery_soc": 50}, max_soc=90)

    class _States:
        def get(self, _entity_id):
            return state_holder["state"]

    controller = SimpleNamespace(
        is_charge_blocked=lambda: False,
        get_charge_blockers=dict,
        hass=SimpleNamespace(states=_States()),
        consumption_sensor="sensor.grid_power",
        _apply_meter_transform=lambda state: float(state.state),
        _last_sensor_report_time=None,
        _last_sensor_cadence_time=None,
        _last_control_sample_value=None,
        _control_sample_is_new=True,
        _slow_sensor_issue_created=False,
        _slow_sensor_intervals=0,
        _fast_sensor_intervals=0,
        config_entry=SimpleNamespace(entry_id="predictive-sample", data={}),
        _max_sensor_stale_s=65.0,
        _grid_charging_initialized=True,
        grid_charging_active=True,
        _predictive_charge_suspended_for_demand=False,
        _predictive_demand_state="charging",
        _predictive_demand_fresh_samples=0,
        _predictive_demand_recovery_samples=0,
        _predictive_demand_transition_monotonic=0.0,
        _predictive_protection_command_w=0.0,
        _predictive_protection_reason=None,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        first_execution=False,
        _predictive_charge_target_soc={coordinator: 80.0},
        capacity_protection_enabled=False,
        capacity_protection_limit=0.0,
        _get_available_batteries=lambda is_charging, **_kwargs: [coordinator],
        _filter_grid_sample=lambda raw, _elapsed: raw,
        _effective_system_capacity=lambda _batteries, is_charging: 2500.0,
        max_contracted_power=1000.0,
        deadband=50.0,
        dt=2.0,
        kp=0.3,
        kd=0.0,
        derivative_tau=3.0,
        derivative_filtered=0.0,
        previous_error=0.0,
        previous_power=-500.0,
        max_power_change_per_cycle=800.0,
        _power_distribution=SimpleNamespace(
            _select_batteries_for_operation=lambda _power, batteries, is_charging=None: batteries,
            _distribute_power_by_limits=lambda power, batteries, is_charging=None: {
                battery: power / len(batteries) for battery in batteries
            },
        ),
        coordinators=[coordinator],
        _phase_power_limiter=SimpleNamespace(enabled=False),
        _set_battery_power=lambda coordinator, charge, discharge, **_kwargs: _record_write(
            writes, coordinator, charge, discharge
        ),
    )
    controller._track_sensor_report = ChargeDischargeController._track_sensor_report.__get__(
        controller, ChargeDischargeController
    )
    controller._sensor_is_within_stale_tolerance = ChargeDischargeController._sensor_is_within_stale_tolerance.__get__(
        controller, ChargeDischargeController
    )
    controller._handle_predictive_grid_charging = ChargeDischargeController._handle_predictive_grid_charging.__get__(
        controller, ChargeDischargeController
    )
    controller._suspend_predictive_grid_charging_for_demand = ChargeDischargeController._suspend_predictive_grid_charging_for_demand.__get__(
        controller, ChargeDischargeController
    )
    controller._predictive_charge_ceiling = ChargeDischargeController._predictive_charge_ceiling.__get__(
        controller, ChargeDischargeController
    )
    controller._predictive_min_charge_power = ChargeDischargeController._predictive_min_charge_power.__get__(
        controller, ChargeDischargeController
    )
    controller._predictive_hard_limit_confirmed = ChargeDischargeController._predictive_hard_limit_confirmed.__get__(
        controller, ChargeDischargeController
    )
    controller._predictive_demand_settle_window_s = ChargeDischargeController._predictive_demand_settle_window_s.__get__(
        controller, ChargeDischargeController
    )
    controller._handle_predictive_demand_protection = ChargeDischargeController._handle_predictive_demand_protection.__get__(
        controller, ChargeDischargeController
    )
    controller._reset_predictive_demand_runtime = ChargeDischargeController._reset_predictive_demand_runtime.__get__(
        controller, ChargeDischargeController
    )
    controller._set_predictive_protection_status = ChargeDischargeController._set_predictive_protection_status.__get__(
        controller, ChargeDischargeController
    )
    controller._measured_battery_power = lambda: None
    return controller


async def _record_write(writes, coordinator, charge, discharge):
    writes.append((coordinator, charge, discharge))


def _complete_predictive_latency_wait(controller):
    controller._predictive_demand_transition_monotonic = time.monotonic() - 10.0


def _confirm_predictive_hard_limit(controller, state_holder, first_report, value=3000):
    """Feed the three fresh publications required by hard protection."""
    for seconds in (0, 4, 8):
        state_holder["state"] = _state(
            value,
            first_report + timedelta(seconds=seconds),
        )
        asyncio.run(controller._handle_predictive_grid_charging())


def test_predictive_pd_does_not_integrate_identical_publications():
    first_report = datetime.now(timezone.utc)
    state_holder = {"state": _state(100, first_report)}
    writes = []
    controller = _predictive_controller(state_holder, writes)

    asyncio.run(controller._handle_predictive_grid_charging())
    first_power = controller.previous_power
    first_write_count = len(writes)

    state_holder["state"] = _state(
        100,
        first_report + timedelta(seconds=4),
        updated_at=first_report,
    )
    asyncio.run(controller._handle_predictive_grid_charging())

    assert controller.previous_power == first_power
    assert len(writes) == first_write_count

    state_holder["state"] = _state(
        200,
        first_report + timedelta(seconds=8),
        updated_at=first_report + timedelta(seconds=8),
    )
    asyncio.run(controller._handle_predictive_grid_charging())
    assert controller.previous_power != first_power


def test_predictive_ordinary_overshoot_modulates_without_idle_command():
    first_report = datetime.now(timezone.utc)
    state_holder = {"state": _state(1100, first_report)}
    writes = []
    controller = _predictive_controller(state_holder, writes)

    asyncio.run(controller._handle_predictive_grid_charging())
    first_power = controller.previous_power
    assert first_power < 0
    assert writes[-1][1] > 0
    assert writes[-1][2] == 0
    assert controller._predictive_charge_suspended_for_demand is False

    # Still above the regulation target, but below the confirmed hard-limit
    # threshold: the incremental state continues from the previous command.
    state_holder["state"] = _state(
        1150,
        first_report + timedelta(seconds=4),
    )
    asyncio.run(controller._handle_predictive_grid_charging())
    assert controller.previous_power < 0
    assert controller.previous_power != first_power
    assert all(charge > 0 and discharge == 0 for _, charge, discharge in writes)


def test_predictive_zero_cross_is_clamped_to_positive_effective_charge():
    first_report = datetime.now(timezone.utc)
    state_holder = {"state": _state(1100, first_report)}
    writes = []
    controller = _predictive_controller(state_holder, writes)
    controller.previous_power = -50.0
    controller.previous_error = 0.0

    asyncio.run(controller._handle_predictive_grid_charging())

    # The P term asks to cross into internal discharge, but predictive mode
    # retains a positive device-side charge and its negative internal state.
    assert controller.previous_power < 0
    assert writes[-1][1] >= 100
    assert writes[-1][2] == 0
    assert (writes[-1][1], writes[-1][2]) != (0, 0)
    assert controller._predictive_charge_suspended_for_demand is False


def test_predictive_zero_cross_respects_rate_limit_before_charge_floor():
    first_report = datetime.now(timezone.utc)
    state_holder = {"state": _state(4000, first_report)}
    writes = []
    controller = _predictive_controller(state_holder, writes)
    controller.previous_power = -500.0
    controller.previous_error = 0.0

    asyncio.run(controller._handle_predictive_grid_charging())

    # A large P correction is rate-limited before the positive floor is
    # applied. The internal sign must remain charging (negative), rather than
    # becoming a positive value that would be interpreted as discharge state.
    assert controller.previous_power == -100.0
    assert writes[-1][1] == 100
    assert writes[-1][2] == 0


def test_predictive_demand_spike_keeps_predictive_slot_while_settling():
    first_report = datetime.now(timezone.utc)
    state_holder = {"state": _state(3000, first_report)}
    writes = []
    controller = _predictive_controller(state_holder, writes)

    _confirm_predictive_hard_limit(controller, state_holder, first_report)

    assert controller.grid_charging_active is True
    assert controller._predictive_charge_suspended_for_demand is True
    assert controller._grid_charging_initialized is False
    assert controller.first_execution is False
    assert controller.previous_power == 0
    assert len(writes) == 3
    assert all(charge > 0 and discharge == 0 for _, charge, discharge in writes[:2])
    assert [(charge, discharge) for _, charge, discharge in writes][-1] == (0, 0)

    # The predictive owner itself remains idle while telemetry settles; normal
    # PD must not receive this old charge-inclusive meter sample.
    write_count = len(writes)
    asyncio.run(controller._handle_predictive_grid_charging())
    assert len(writes) == write_count


def test_predictive_hard_limit_watchdog_breaks_confirmation_streak():
    first_report = datetime.now(timezone.utc)
    state_holder = {"state": _state(3000, first_report)}
    writes = []
    controller = _predictive_controller(state_holder, writes)

    asyncio.run(controller._handle_predictive_grid_charging())
    assert controller._predictive_hard_limit_samples == 1

    # A timer-only pass is not fresh evidence and breaks consecutiveness.
    asyncio.run(controller._handle_predictive_grid_charging())
    assert controller._predictive_hard_limit_samples == 0
    assert controller._predictive_charge_suspended_for_demand is False

    # Three later publications with the same value are nevertheless fresh
    # evidence and must confirm the sustained overload.
    for seconds in (4, 8, 12):
        state_holder["state"] = _state(
            3000,
            first_report + timedelta(seconds=seconds),
        )
        asyncio.run(controller._handle_predictive_grid_charging())

    assert controller._predictive_charge_suspended_for_demand is True


def test_predictive_peak_shaving_waits_for_two_fresh_samples_before_discharge():
    first_report = datetime.now(timezone.utc)
    state_holder = {"state": _state(3000, first_report)}
    writes = []
    controller = _predictive_controller(state_holder, writes)
    controller.max_contracted_power = 4000.0
    controller.capacity_protection_enabled = True
    controller.capacity_protection_limit = 2000.0
    controller._capacity_protection_status = {}
    controller._capacity_protection_active = False

    _confirm_predictive_hard_limit(controller, state_holder, first_report)
    assert len(writes) == 3
    assert [(charge, discharge) for _, charge, discharge in writes][-1] == (0, 0)
    _complete_predictive_latency_wait(controller)

    # First post-idle publication still may include inverter ramp/old charge.
    state_holder["state"] = _state(3000, first_report + timedelta(seconds=12))
    asyncio.run(controller._handle_predictive_grid_charging())
    assert len(writes) == 3

    # A second independent sample confirms a real 1 kW excess, which is the
    # only amount Peak Shaving is allowed to discharge.
    state_holder["state"] = _state(3000, first_report + timedelta(seconds=16))
    asyncio.run(controller._handle_predictive_grid_charging())
    assert [(charge, discharge) for _, charge, discharge in writes][-1] == (0, 1000)
    assert controller._predictive_demand_state == "peak_shaving"


def test_predictive_charge_blocker_keeps_peak_protection_ownership():
    first_report = datetime.now(timezone.utc)
    state_holder = {"state": _state(3000, first_report)}
    writes = []
    controller = _predictive_controller(state_holder, writes)
    controller.max_contracted_power = 4000.0
    controller.capacity_protection_enabled = True
    controller.capacity_protection_limit = 2000.0
    controller._capacity_protection_status = {}
    controller._capacity_protection_active = False

    _confirm_predictive_hard_limit(controller, state_holder, first_report)
    controller.is_charge_blocked = lambda: True
    _complete_predictive_latency_wait(controller)
    for seconds in (12, 16):
        state_holder["state"] = _state(3000, first_report + timedelta(seconds=seconds))
        asyncio.run(controller._handle_predictive_grid_charging())

    assert controller.grid_charging_active is True
    assert controller._predictive_demand_state == "peak_shaving"
    assert [(charge, discharge) for _, charge, discharge in writes][-1] == (0, 1000)


def test_predictive_protection_clears_status_while_settling_after_peak():
    first_report = datetime.now(timezone.utc)
    state_holder = {"state": _state(3000, first_report)}
    writes = []
    controller = _predictive_controller(state_holder, writes)
    controller.max_contracted_power = 4000.0
    controller.capacity_protection_enabled = True
    controller.capacity_protection_limit = 2000.0
    controller._capacity_protection_status = {}
    controller._capacity_protection_active = False

    _confirm_predictive_hard_limit(controller, state_holder, first_report)
    _complete_predictive_latency_wait(controller)
    for seconds in (12, 16):
        state_holder["state"] = _state(3000, first_report + timedelta(seconds=seconds))
        asyncio.run(controller._handle_predictive_grid_charging())
    assert controller._capacity_protection_status["active"] is True

    state_holder["state"] = _state(1500, first_report + timedelta(seconds=20))
    asyncio.run(controller._handle_predictive_grid_charging())
    assert controller._capacity_protection_active is False
    assert controller._capacity_protection_status["action"] == "settling"


def test_predictive_emergency_uses_physical_load_when_excluded_load_is_ignored():
    first_report = datetime.now(timezone.utc)
    state_holder = {"state": _state(3000, first_report)}
    writes = []
    controller = _predictive_controller(state_holder, writes)
    controller.max_contracted_power = 2000.0
    controller.capacity_protection_enabled = False
    controller.capacity_protection_limit = 0.0
    controller.capacity_protection_excluded_devices = False
    controller._excluded_included_adjustment = 2500.0
    controller._capacity_protection_status = {}
    controller._capacity_protection_active = False

    _confirm_predictive_hard_limit(controller, state_holder, first_report)
    _complete_predictive_latency_wait(controller)
    for seconds in (12, 16):
        state_holder["state"] = _state(3000, first_report + timedelta(seconds=seconds))
        asyncio.run(controller._handle_predictive_grid_charging())

    assert controller._predictive_protection_reason == "emergency"
    assert [(charge, discharge) for _, charge, discharge in writes][-1] == (0, 1000)


def test_predictive_settling_counts_samples_only_after_measured_idle():
    first_report = datetime.now(timezone.utc)
    state_holder = {"state": _state(3000, first_report)}
    writes = []
    controller = _predictive_controller(state_holder, writes)
    controller.max_contracted_power = 4000.0
    controller.capacity_protection_enabled = True
    controller.capacity_protection_limit = 2000.0
    controller._capacity_protection_status = {}
    controller._capacity_protection_active = False
    measured = {"power": 500.0}
    controller._measured_battery_power = lambda: measured["power"]

    _confirm_predictive_hard_limit(controller, state_holder, first_report)
    _complete_predictive_latency_wait(controller)

    # Reports received while the old charge is still physically present do not
    # count towards the two post-idle confirmations.
    state_holder["state"] = _state(3000, first_report + timedelta(seconds=12))
    asyncio.run(controller._handle_predictive_grid_charging())
    assert controller._predictive_demand_fresh_samples == 0

    measured["power"] = 0.0
    state_holder["state"] = _state(3000, first_report + timedelta(seconds=16))
    asyncio.run(controller._handle_predictive_grid_charging())
    assert controller._predictive_demand_fresh_samples == 1
    assert len(writes) == 3

    state_holder["state"] = _state(3000, first_report + timedelta(seconds=20))
    asyncio.run(controller._handle_predictive_grid_charging())
    assert [(charge, discharge) for _, charge, discharge in writes][-1] == (0, 1000)


def test_predictive_peak_does_not_recalculate_from_watchdog_sample():
    first_report = datetime.now(timezone.utc)
    state_holder = {"state": _state(3000, first_report)}
    writes = []
    controller = _predictive_controller(state_holder, writes)
    controller.max_contracted_power = 4000.0
    controller.capacity_protection_enabled = True
    controller.capacity_protection_limit = 2000.0
    controller._capacity_protection_status = {}
    controller._capacity_protection_active = False

    _confirm_predictive_hard_limit(controller, state_holder, first_report)
    _complete_predictive_latency_wait(controller)
    for seconds in (12, 16):
        state_holder["state"] = _state(3000, first_report + timedelta(seconds=seconds))
        asyncio.run(controller._handle_predictive_grid_charging())
    write_count = len(writes)

    # The battery has ramped to discharge, but the meter has not published. The
    # old 3 kW import must not be combined with that new battery telemetry.
    controller._measured_battery_power = lambda: -1000.0
    asyncio.run(controller._handle_predictive_grid_charging())

    assert len(writes) == write_count
    assert controller._predictive_protection_command_w == 1000.0


def test_predictive_peak_stops_discharge_when_meter_is_too_stale():
    first_report = datetime.now(timezone.utc)
    state_holder = {"state": _state(3000, first_report)}
    writes = []
    controller = _predictive_controller(state_holder, writes)
    controller.max_contracted_power = 4000.0
    controller.capacity_protection_enabled = True
    controller.capacity_protection_limit = 2000.0
    controller._capacity_protection_status = {}
    controller._capacity_protection_active = False

    _confirm_predictive_hard_limit(controller, state_holder, first_report)
    _complete_predictive_latency_wait(controller)
    for seconds in (12, 16):
        state_holder["state"] = _state(3000, first_report + timedelta(seconds=seconds))
        asyncio.run(controller._handle_predictive_grid_charging())

    stale_report = datetime.now(timezone.utc) - timedelta(seconds=70)
    state_holder["state"] = _state(3000, stale_report)
    asyncio.run(controller._handle_predictive_grid_charging())

    assert [(charge, discharge) for _, charge, discharge in writes][-1] == (0, 0)
    assert controller._predictive_demand_state == "settling_after_discharge"
    assert controller._capacity_protection_status["active"] is False
