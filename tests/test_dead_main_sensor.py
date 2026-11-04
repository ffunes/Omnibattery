"""Dead main sensor: Repairs issue and fallback for a meter that stopped publishing.

The slow-sensor repair next door is publication-driven, so it only describes a
meter that still speaks. A meter that goes completely silent produces no further
publications and never reaches it; without a time limit the loop would hold its
last command for as long as the silence lasts (issue #452). These tests pin the
lifecycle of the issue that makes that visible, including the
frozen-on-a-valid-value flavour and a meter that never publishes after a
restart, and the fallback that stops automatic control once the meter counts
as dead and hands it back when the meter returns.

``_check_main_sensor_liveness`` only touches ``self.hass``, ``self.config_entry``,
``self.consumption_sensor``, ``self.previous_power`` and its own flag, so it is
exercised as an unbound method against a lightweight stand-in.

The age it judges is the meter's own ``last_reported``, not the timestamp the
control loop records when it reads the meter: every cycle that returns before
that read (manual mode, an operation block, a predictive handler) leaves the
tracked timestamp aging while the meter keeps publishing.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.core import CoreState

import custom_components.omnibattery as omnibattery_init
from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import MAIN_SENSOR_DEAD_S

NOW = omnibattery_init.dt_util.utcnow()


class _FakeIssueRegistry:
    def __init__(self):
        self.created: list[tuple] = []
        self.deleted: list[str] = []
        self.IssueSeverity = SimpleNamespace(WARNING="warning")

    def async_create_issue(self, hass, domain, issue_id, **kwargs):
        self.created.append((issue_id, kwargs))

    def async_delete_issue(self, hass, domain, issue_id):
        self.deleted.append(issue_id)


@pytest.fixture
def issues(monkeypatch):
    fake = _FakeIssueRegistry()
    monkeypatch.setattr(omnibattery_init, "ir", fake)
    return fake


_NO_STATE = object()  # the entity itself is absent, as at a cold start


class _FakeStates:
    def __init__(self, state):
        self._state = state

    def get(self, entity_id):
        return self._state


def _state(value="1234.0", reported_s_ago=0.0, reported_at=None):
    """Meter state as Home Assistant exposes it, with its own publication clock."""
    return SimpleNamespace(
        state=value,
        attributes={"unit_of_measurement": "W"},
        last_reported=reported_at or NOW - timedelta(seconds=reported_s_ago),
    )


def _ctrl(
    silent_for_s,
    sensor="sensor.grid_power",
    previous_power=-1500.0,
    state=None,
):
    """Controller stand-in exposing only what the liveness check reads.

    ``silent_for_s`` is the tracked read time; ``state`` is what the meter is
    publishing. They are separate on purpose - the bug was reading the first
    and calling it the second.
    """
    if state is _NO_STATE:
        state = None
    elif state is None:
        state = _state(reported_s_ago=silent_for_s or 0.0)
    return SimpleNamespace(
        hass=SimpleNamespace(states=_FakeStates(state), state=CoreState.running),
        config_entry=SimpleNamespace(entry_id="abc123"),
        consumption_sensor=sensor,
        previous_power=previous_power,
        meter_inverted=False,
        offgrid_mode_enabled=False,
        offgrid_power_sensor=None,
        _last_sensor_report_time=(
            None if silent_for_s is None else NOW - timedelta(seconds=silent_for_s)
        ),
        _last_valid_meter_publication=None,
        _meter_wait_since=None,
        _dead_sensor_issue_created=False,
    )


def _check(ctrl, now=NOW):
    # Bind the helpers the check calls on itself, then run the check unbound.
    ctrl._sensor_age_seconds = lambda t, now=None: (
        ChargeDischargeController._sensor_age_seconds(ctrl, t, now)
    )
    ctrl._live_sensor_report_time = lambda: (
        ChargeDischargeController._live_sensor_report_time(ctrl)
    )
    ctrl._apply_meter_transform = lambda state: (
        ChargeDischargeController._apply_meter_transform(ctrl, state)
    )
    ctrl._sensor_report_time = ChargeDischargeController._sensor_report_time
    ChargeDischargeController._check_main_sensor_liveness(ctrl, now)


def test_silent_meter_raises_the_issue(issues):
    ctrl = _ctrl(MAIN_SENSOR_DEAD_S + 60)
    _check(ctrl)

    assert len(issues.created) == 1
    issue_id, kwargs = issues.created[0]
    assert issue_id == "dead_main_sensor_abc123"
    assert kwargs["translation_key"] == "dead_main_sensor"
    # The held command is the whole point of the warning: name it.
    assert kwargs["translation_placeholders"]["power"] == "1500"
    assert kwargs["translation_placeholders"]["sensor"] == "sensor.grid_power"
    assert ctrl._dead_sensor_issue_created is True


def test_a_meter_within_tolerance_raises_nothing(issues):
    _check(_ctrl(MAIN_SENSOR_DEAD_S - 60))
    assert issues.created == []


def test_no_reading_yet_is_a_restart_not_a_fault(issues):
    """Nothing published yet: no timestamp to age, so no fault."""
    _check(_ctrl(None, state=_NO_STATE))
    assert issues.created == []


def test_the_issue_is_raised_once_per_episode(issues):
    ctrl = _ctrl(MAIN_SENSOR_DEAD_S + 60)
    for _ in range(5):
        _check(ctrl)
    assert len(issues.created) == 1


def test_a_recovered_meter_clears_the_issue(issues):
    ctrl = _ctrl(MAIN_SENSOR_DEAD_S + 60)
    _check(ctrl)
    # The meter publishes again: age collapses back under the threshold.
    ctrl.hass.states._state = _state(reported_s_ago=0.0)
    _check(ctrl)

    assert issues.deleted == ["dead_main_sensor_abc123"]
    assert ctrl._dead_sensor_issue_created is False

    # ...and a second episode can raise it again: the meter falls silent after
    # the recovery, so its last publication ages as the clock moves on.
    _check(ctrl, now=NOW + timedelta(seconds=MAIN_SENSOR_DEAD_S + 60))
    assert len(issues.created) == 2


def test_no_configured_meter_is_not_a_fault(issues):
    _check(_ctrl(MAIN_SENSOR_DEAD_S + 60, sensor=None))
    assert issues.created == []


def test_a_publishing_meter_the_loop_never_read_is_not_dead(issues):
    """The cycle returned early for minutes; the meter never stopped talking.

    Manual mode, an operation block and the predictive handlers all return
    before the grid read, so the tracked read time ages on a healthy meter.
    """
    ctrl = _ctrl(MAIN_SENSOR_DEAD_S + 60, state=_state(reported_s_ago=1.0))
    _check(ctrl)

    assert issues.created == []
    assert ctrl._dead_sensor_issue_created is False


def test_a_brief_unavailable_blip_is_not_minutes_of_silence(issues):
    """A meter reload during a long block must not inherit the frozen read time.

    The tracked read time is minutes old because the cycle kept returning
    early. The meter was publishing a second ago and is briefly reloading, so
    the fallback has to be its own last valid publication, not that timestamp.
    """
    ctrl = _ctrl(MAIN_SENSOR_DEAD_S + 60, state=_state(reported_s_ago=1.0))
    _check(ctrl)
    ctrl.hass.states._state = _state(value="unavailable", reported_s_ago=0.0)
    _check(ctrl)

    assert issues.created == []


# --- A meter that never publishes in this run ---------------------------------


@pytest.mark.parametrize(
    "state", [_state(value="unavailable"), _NO_STATE], ids=["unavailable", "absent"]
)
def test_a_meter_silent_since_boot_ages_from_the_wait(issues, state):
    """No reading to age, so the wait for the first one is the age.

    A crash skips the unload that idles the batteries, and most brands hold the
    last command themselves. Without an age the repair and the fallback could
    never trip, and that command would run until an SOC limit. The entity
    existing but ``unavailable`` is invisible to the missing-sensor repair,
    which only names an entity that is absent.
    """
    ctrl = _ctrl(None, state=state)
    _check(ctrl, NOW)
    _check(ctrl, NOW + timedelta(seconds=MAIN_SENSOR_DEAD_S - 1))
    assert ctrl._main_sensor_dead is False
    assert issues.created == []

    _check(ctrl, NOW + timedelta(seconds=MAIN_SENSOR_DEAD_S + 1))
    assert ctrl._main_sensor_dead is True
    assert ctrl._main_sensor_never_reported is True
    assert len(issues.created) == 1
    # No command was sent in this run, so the text cannot name one.
    issue = issues.created[0][1]
    assert issue["translation_key"] == "dead_main_sensor_since_start"
    assert issue["translation_placeholders"] == {
        "sensor": "sensor.grid_power", "minutes": "5",
    }
    # The wait is not a publication: the high-water mark stays empty.
    assert ctrl._last_valid_meter_publication is None


def test_a_meter_that_reported_once_keeps_the_original_text(issues):
    ctrl = _ctrl(MAIN_SENSOR_DEAD_S + 60)
    _check(ctrl)

    issue = issues.created[0][1]
    assert ctrl._main_sensor_never_reported is False
    assert issue["translation_key"] == "dead_main_sensor"
    assert issue["translation_placeholders"]["power"] == "1500"


def test_the_since_start_text_exists_in_every_language():
    root = Path(omnibattery_init.__file__).parent
    files = [root / "strings.json", *sorted((root / "translations").glob("*.json"))]
    assert len(files) == 7
    for path in files:
        issues_text = json.loads(path.read_text(encoding="utf-8"))["issues"]
        text = issues_text["dead_main_sensor_since_start"]["description"]
        assert "{sensor}" in text and "{minutes}" in text, path.name
        assert "{power}" not in text, path.name
        assert "0W" in text and "0 W" not in text, path.name


# --- A Repairs issue left over from before the restart ------------------------


def test_a_healthy_meter_clears_an_issue_left_from_before_the_restart(issues):
    """The issue is persistent, so HA restores it as active after a restart.

    The controller starts not knowing whether one is showing; the first check
    on a live meter clears it, and later checks do not repeat the delete.
    """
    ctrl = _ctrl(1.0)
    ctrl._dead_sensor_issue_created = None
    _check(ctrl)
    _check(ctrl)

    assert issues.deleted == ["dead_main_sensor_abc123"]
    assert ctrl._dead_sensor_issue_created is False


def test_a_meter_still_dead_after_the_restart_reraises_the_issue(issues):
    """Not knowing must not count as raised: the text may be from a past run."""
    ctrl = _ctrl(MAIN_SENSOR_DEAD_S + 60)
    ctrl._dead_sensor_issue_created = None
    _check(ctrl)

    assert len(issues.created) == 1
    assert issues.deleted == []


def test_the_wait_starts_once_home_assistant_has_started(issues):
    """During startup the meter's integration may not be set up yet."""
    ctrl = _ctrl(None, state=_state(value="unavailable"))
    ctrl.hass.state = CoreState.starting
    _check(ctrl, NOW)
    _check(ctrl, NOW + timedelta(seconds=MAIN_SENSOR_DEAD_S + 60))
    assert ctrl._main_sensor_dead is False
    assert ctrl._meter_wait_since is None

    started = NOW + timedelta(seconds=MAIN_SENSOR_DEAD_S + 60)
    ctrl.hass.state = CoreState.running
    _check(ctrl, started)
    _check(ctrl, started + timedelta(seconds=MAIN_SENSOR_DEAD_S - 1))
    assert ctrl._main_sensor_dead is False

    _check(ctrl, started + timedelta(seconds=MAIN_SENSOR_DEAD_S + 1))
    assert ctrl._main_sensor_dead is True


def test_a_shutdown_does_not_release_a_meter_that_never_reported(issues):
    """Once the wait has started it keeps counting whatever state HA is in."""
    ctrl = _ctrl(None, state=_state(value="unavailable"))
    _check(ctrl, NOW)
    dead_at = NOW + timedelta(seconds=MAIN_SENSOR_DEAD_S + 1)
    _check(ctrl, dead_at)
    assert ctrl._main_sensor_dead is True

    ctrl.hass.state = CoreState.stopping
    _check(ctrl, dead_at + timedelta(seconds=2))
    assert ctrl._main_sensor_dead is True
    assert issues.deleted == []


def test_a_first_reading_ends_the_wait(issues):
    ctrl = _ctrl(None, state=_state(value="unavailable"))
    _check(ctrl, NOW)
    dead_at = NOW + timedelta(seconds=MAIN_SENSOR_DEAD_S + 1)
    _check(ctrl, dead_at)
    assert ctrl._main_sensor_dead is True

    ctrl.hass.states._state = _state(value="900", reported_at=dead_at)
    _check(ctrl, dead_at)
    assert ctrl._main_sensor_dead is False
    assert issues.deleted == ["dead_main_sensor_abc123"]


def test_selecting_another_meter_restarts_the_wait(issues):
    """A newly selected meter gets its own grace, as it would after a restart."""
    ctrl = _ctrl(None, state=_state(value="unavailable"))
    _check(ctrl, NOW)
    switched = NOW + timedelta(seconds=MAIN_SENSOR_DEAD_S - 60)
    ChargeDischargeController._reset_consumption_source_tracking(ctrl)
    _check(ctrl, switched)

    _check(ctrl, NOW + timedelta(seconds=MAIN_SENSOR_DEAD_S + 1))
    assert ctrl._main_sensor_dead is False
    _check(ctrl, switched + timedelta(seconds=MAIN_SENSOR_DEAD_S + 1))
    assert ctrl._main_sensor_dead is True


def test_an_unavailable_meter_is_still_dead(issues):
    """``unavailable`` keeps being republished; it is silence, not a reading."""
    ctrl = _ctrl(
        MAIN_SENSOR_DEAD_S + 60,
        state=_state(value="unavailable", reported_s_ago=1.0),
    )
    _check(ctrl)

    assert len(issues.created) == 1


# --- The liveness verdict the fallback acts on ------------------------------


def test_a_silent_meter_is_flagged_dead_and_a_recovered_one_is_not(issues):
    ctrl = _ctrl(MAIN_SENSOR_DEAD_S + 60)
    _check(ctrl)
    assert ctrl._main_sensor_dead is True

    ctrl.hass.states._state = _state(reported_s_ago=0.0)
    _check(ctrl)
    assert ctrl._main_sensor_dead is False


def test_a_slow_meter_is_not_dead(issues):
    """Slow sensors are authoritative up to the stale tolerance, far below dead."""
    _check(ctrl := _ctrl(MAIN_SENSOR_DEAD_S - 1))
    assert ctrl._main_sensor_dead is False


def test_the_dead_verdict_survives_the_fallback_clearing_the_read_time(issues):
    """The fallback clears the tracked read time; the verdict must not flap.

    With no valid publication recorded yet, the age came from the tracked read
    time alone. Clearing it would collapse the age to 0 and release the
    fallback the very next cycle, so the dead verdict pins the high-water mark.
    """
    ctrl = _ctrl(MAIN_SENSOR_DEAD_S + 60, state=_state(value="unavailable"))
    _check(ctrl)
    assert ctrl._main_sensor_dead is True

    ctrl._last_sensor_report_time = None
    _check(ctrl)
    assert ctrl._main_sensor_dead is True


# --- The fallback itself ------------------------------------------------------


class _Battery(SimpleNamespace):
    __hash__ = object.__hash__


def _fallback_ctrl(*, dead=True, grid_charging_active=False):
    batteries = [_Battery(name="a"), _Battery(name="b")]
    writes: list[tuple] = []
    cleared: list[str] = []

    async def _set_battery_power(coordinator, charge, discharge, **_kwargs):
        writes.append((coordinator.name, charge, discharge))
        return True

    def _clear_predictive_runtime(reason):
        cleared.append(reason)
        ctrl.grid_charging_active = False
        ctrl.first_execution = True

    ctrl = SimpleNamespace(
        coordinators=batteries,
        consumption_sensor="sensor.grid_power",
        _main_sensor_dead=dead,
        _main_sensor_never_reported=False,
        _dead_meter_fallback_engaged=False,
        grid_charging_active=grid_charging_active,
        previous_power=-1500.0,
        previous_error=320.0,
        previous_sensor=900.0,
        error_integral=150.0,
        derivative_filtered=40.0,
        sign_changes=3,
        last_error_sign=1,
        last_output_sign=-1,
        first_execution=False,
        _zero_cross_since=NOW,
        _grid_filter_ema=900.0,
        _last_sensor_report_time=NOW,
        _last_control_sample_value=900.0,
        _control_sample_is_new=False,
        _stale_cycles=12,
        _phase_safety_pending=True,
        _active_discharge_batteries=list(batteries),
        _active_charge_batteries=[],
        _rate_limiter_was_active=True,
        _rate_limiter_last_direction=-1,
        _rate_limiter_last_logged_change=-800.0,
        _set_battery_power=_set_battery_power,
        _clear_predictive_runtime=_clear_predictive_runtime,
    )
    ctrl._clear_rate_limiter_state = lambda: (
        ChargeDischargeController._clear_rate_limiter_state(ctrl)
    )
    return ctrl, writes, cleared


def _fallback(ctrl):
    return asyncio.run(ChargeDischargeController._apply_dead_meter_fallback(ctrl))


def test_a_live_meter_leaves_the_cycle_alone():
    ctrl, writes, _ = _fallback_ctrl(dead=False)
    assert _fallback(ctrl) is False
    assert writes == []
    assert ctrl.previous_power == -1500.0


def test_a_dead_meter_stops_every_automatic_battery():
    ctrl, writes, cleared = _fallback_ctrl()
    assert _fallback(ctrl) is True

    assert writes == [("a", 0, 0), ("b", 0, 0)]
    assert ctrl.previous_power == 0
    assert ctrl._active_discharge_batteries == []
    assert ctrl._dead_meter_fallback_engaged is True
    # Not a predictive grid charge: nothing to release.
    assert cleared == []


def test_entering_the_fallback_resets_the_controller_like_a_restart():
    """Nothing from before the outage may steer the first reading after it."""
    ctrl, _, _ = _fallback_ctrl()
    _fallback(ctrl)

    assert ctrl.first_execution is True
    assert ctrl.error_integral == 0.0
    assert ctrl.previous_error == 0.0
    assert ctrl.derivative_filtered == 0.0
    assert ctrl.previous_sensor is None
    assert ctrl.sign_changes == 0
    assert ctrl.last_output_sign == 0
    assert ctrl._zero_cross_since is None
    assert ctrl._grid_filter_ema is None
    assert ctrl._rate_limiter_was_active is False
    # The first sample after recovery must not measure the whole outage as
    # one interval, nor be mistaken for the frozen value it replaces.
    assert ctrl._last_sensor_report_time is None
    assert ctrl._last_control_sample_value is None
    assert ctrl._control_sample_is_new is True
    assert ctrl._phase_safety_pending is False


def test_the_stop_is_reasserted_but_announced_once(caplog):
    ctrl, writes, _ = _fallback_ctrl()
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert _fallback(ctrl) is True

    assert len(writes) == 6
    warnings = [r for r in caplog.records if "stopping automatic" in r.getMessage()]
    assert len(warnings) == 1
    assert "-1500W" in warnings[0].getMessage()


def test_a_meter_that_never_reported_is_announced_as_such(caplog):
    """No command was sent in this run, so the log names the restart instead."""
    ctrl, writes, _ = _fallback_ctrl()
    ctrl._main_sensor_never_reported = True
    ctrl.previous_power = 0
    with caplog.at_level(logging.WARNING):
        assert _fallback(ctrl) is True

    assert writes == [("a", 0, 0), ("b", 0, 0)]
    (warning,) = [
        r.getMessage() for r in caplog.records if "stopping automatic" in r.getMessage()
    ]
    assert "since Home Assistant started" in warning
    assert "command was" not in warning


def test_a_dead_meter_releases_a_predictive_grid_charge():
    """Its import ceiling is regulated on the meter, so it cannot run blind."""
    ctrl, writes, cleared = _fallback_ctrl(grid_charging_active=True)
    assert _fallback(ctrl) is True

    assert cleared == ["dead_meter"]
    assert ctrl.grid_charging_active is False
    assert writes == [("a", 0, 0), ("b", 0, 0)]


def test_recovery_hands_the_cycle_back(caplog):
    ctrl, writes, _ = _fallback_ctrl()
    _fallback(ctrl)
    writes.clear()

    ctrl._main_sensor_dead = False
    with caplog.at_level(logging.INFO):
        assert _fallback(ctrl) is False

    assert writes == []
    assert ctrl._dead_meter_fallback_engaged is False
    assert ctrl.first_execution is True
    assert any("publishing again" in r.getMessage() for r in caplog.records)


# --- Wired into the real control cycle ----------------------------------------


def _cycle_ctrl(state_holder, pd_calls, writes):
    from tests.test_control_sample_scheduling import _main_controller

    ctrl = _main_controller(state_holder, pd_calls)

    async def _record(coordinator, charge, discharge, **_kwargs):
        writes.append((charge, discharge))
        return True

    def _transform(state):
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    ctrl._set_battery_power = _record
    ctrl._apply_meter_transform = _transform
    ctrl._log_consumption_sensor_issue = lambda _state: None
    ctrl._consumption_sensor_issue = None
    ctrl.meter_inverted = False
    ctrl.offgrid_mode_enabled = False
    ctrl.offgrid_power_sensor = None
    ctrl._last_valid_meter_publication = None
    ctrl._meter_wait_since = None
    ctrl.hass.state = CoreState.running
    ctrl._dead_sensor_issue_created = False
    ctrl._main_sensor_dead = False
    ctrl._dead_meter_fallback_engaged = False
    ctrl._zero_cross_since = None
    ctrl._grid_filter_ema = None
    ctrl._rate_limiter_was_active = False
    ctrl._rate_limiter_last_direction = 0
    ctrl._rate_limiter_last_logged_change = None
    ctrl._sensor_report_time = ChargeDischargeController._sensor_report_time
    for name in (
        "_check_main_sensor_liveness",
        "_live_sensor_report_time",
        "_apply_dead_meter_fallback",
        "_clear_rate_limiter_state",
    ):
        setattr(
            ctrl,
            name,
            getattr(ChargeDischargeController, name).__get__(
                ctrl, ChargeDischargeController
            ),
        )
    return ctrl


def _meter(value, reported_at):
    from tests.test_control_sample_scheduling import _state as _ha_state

    return _ha_state(value, reported_at, attributes={"unit_of_measurement": "W"})


@pytest.mark.parametrize("silent_value", ["900", "unavailable"])
def test_the_cycle_stops_on_a_dead_meter_and_resumes_on_recovery(
    issues, silent_value
):
    """Both flavours of silence reach the fallback, and recovery restarts cleanly.

    A meter frozen on a valid value goes through the stale safety
    recalculation; an unavailable one through the early return. Neither may
    hold the command past MAIN_SENSOR_DEAD_S.
    """
    t0 = NOW
    state_holder = {"state": _meter(900, t0)}
    pd_calls, writes = [], []
    ctrl = _cycle_ctrl(state_holder, pd_calls, writes)

    asyncio.run(ctrl._run_control_cycle(now=t0 + timedelta(seconds=1)))
    assert len(pd_calls) == 1
    assert ctrl.previous_power != 0

    # The meter goes silent: frozen (no new last_reported) or unavailable.
    if silent_value == "unavailable":
        state_holder["state"] = _meter("unavailable", t0 + timedelta(seconds=30))
    asyncio.run(ctrl._run_control_cycle(now=t0 + timedelta(seconds=120)))
    assert ctrl._main_sensor_dead is False

    writes.clear()
    dead_at = t0 + timedelta(seconds=MAIN_SENSOR_DEAD_S + 5)
    asyncio.run(ctrl._run_control_cycle(now=dead_at))
    assert ctrl._main_sensor_dead is True
    assert writes == [(0, 0)]
    assert ctrl.previous_power == 0
    assert len(pd_calls) == 1
    assert len(issues.created) == 1

    # It stays stopped while the silence lasts.
    asyncio.run(ctrl._run_control_cycle(now=dead_at + timedelta(seconds=60)))
    assert ctrl.previous_power == 0
    assert len(pd_calls) == 1

    # A fresh reading restarts through first-execution initialisation: the
    # command is rebuilt from the new reading, not from pre-outage state.
    back_at = dead_at + timedelta(seconds=90)
    state_holder["state"] = _meter(700, back_at)
    asyncio.run(ctrl._run_control_cycle(now=back_at))
    assert ctrl._main_sensor_dead is False
    assert ctrl._dead_meter_fallback_engaged is False
    assert issues.deleted == ["dead_main_sensor_control-sample"]
    assert ctrl.first_execution is False
    assert ctrl.previous_power == -700


def test_the_cycle_stops_a_command_retained_from_before_a_crash(issues):
    """The meter never publishes after a crash-restart: stop, then resume.

    A crash skips the unload that idles the batteries, so the battery can still
    be running the last command from before it. The controller never got a
    reading in this run, yet must not leave that command running.
    """
    t0 = NOW
    state_holder = {"state": _meter("unavailable", t0)}
    pd_calls, writes = [], []
    ctrl = _cycle_ctrl(state_holder, pd_calls, writes)
    ctrl.first_execution = True
    ctrl.previous_power = 0

    asyncio.run(ctrl._run_control_cycle(now=t0 + timedelta(seconds=1)))
    asyncio.run(ctrl._run_control_cycle(now=t0 + timedelta(seconds=120)))
    assert ctrl._main_sensor_dead is False
    assert writes == []

    dead_at = t0 + timedelta(seconds=MAIN_SENSOR_DEAD_S + 5)
    asyncio.run(ctrl._run_control_cycle(now=dead_at))
    assert ctrl._main_sensor_dead is True
    assert writes == [(0, 0)]
    assert len(issues.created) == 1
    assert issues.created[0][1]["translation_key"] == "dead_main_sensor_since_start"
    assert pd_calls == []

    back_at = dead_at + timedelta(seconds=90)
    state_holder["state"] = _meter(700, back_at)
    asyncio.run(ctrl._run_control_cycle(now=back_at))
    assert ctrl._main_sensor_dead is False
    assert ctrl._dead_meter_fallback_engaged is False
    assert issues.deleted == ["dead_main_sensor_control-sample"]
    assert ctrl.first_execution is False
    assert ctrl.previous_power == -700


def test_global_manual_mode_keeps_the_cycle(issues):
    """Manual mode belongs to the user: a dead meter raises the issue, nothing more."""
    t0 = NOW
    state_holder = {"state": _meter(900, t0)}
    pd_calls, writes = [], []
    ctrl = _cycle_ctrl(state_holder, pd_calls, writes)
    ctrl.manual_mode_enabled = True
    ctrl._pricing_mgr.clear_curtailment_runtime = lambda _reason: None
    ctrl._surplus_hold_mgr = None
    ctrl._discharge_reserve_mgr = None

    async def _manual_setpoints(global_mode=True):
        return None

    ctrl._apply_software_manual_setpoints = _manual_setpoints
    ctrl._last_valid_meter_publication = t0
    ctrl.previous_power = -1200.0

    asyncio.run(
        ctrl._run_control_cycle(now=t0 + timedelta(seconds=MAIN_SENSOR_DEAD_S + 5))
    )

    assert len(issues.created) == 1
    assert writes == []
    assert ctrl._dead_meter_fallback_engaged is False
    assert ctrl.previous_power == -1200.0


def test_predictive_dispatch_does_not_run_on_a_dead_meter(issues):
    t0 = NOW
    state_holder = {"state": _meter(900, t0)}
    pd_calls, writes, handler_calls, cleared = [], [], [], []
    ctrl = _cycle_ctrl(state_holder, pd_calls, writes)
    ctrl._last_valid_meter_publication = t0
    ctrl.predictive_charging_enabled = True
    ctrl.predictive_charging_mode = "time_slot"
    ctrl.grid_charging_active = True

    async def _handler():
        handler_calls.append("time_slot")

    def _clear(reason):
        cleared.append(reason)
        ctrl.grid_charging_active = False

    ctrl._handle_time_slot_predictive_charging = _handler
    ctrl._clear_predictive_runtime = _clear

    asyncio.run(
        ctrl._run_control_cycle(now=t0 + timedelta(seconds=MAIN_SENSOR_DEAD_S + 5))
    )

    assert handler_calls == []
    assert cleared == ["dead_meter"]
    assert writes == [(0, 0)]
