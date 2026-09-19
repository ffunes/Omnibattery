"""Dead main sensor: Repairs issue for a grid meter that stopped publishing.

The slow-sensor repair next door is publication-driven, so it only describes a
meter that still speaks. A meter that goes completely silent produces no further
publications, never reaches it, and the loop just holds its last command for as
long as the silence lasts (issue #452). These tests pin the lifecycle of the
issue that makes that visible, including the frozen-on-a-valid-value flavour
that logs nothing at all today.

``_check_main_sensor_liveness`` only touches ``self.hass``, ``self.config_entry``,
``self.consumption_sensor``, ``self.previous_power`` and its own flag, so it is
exercised as an unbound method against a lightweight stand-in.

The age it judges is the meter's own ``last_reported``, not the timestamp the
control loop records when it reads the meter: every cycle that returns before
that read (manual mode, an operation block, a predictive handler) leaves the
tracked timestamp aging while the meter keeps publishing.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

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


def _state(value="1234.0", reported_s_ago=0.0):
    """Meter state as Home Assistant exposes it, with its own publication clock."""
    return SimpleNamespace(
        state=value,
        attributes={"unit_of_measurement": "W"},
        last_reported=NOW - timedelta(seconds=reported_s_ago),
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
        hass=SimpleNamespace(states=_FakeStates(state)),
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


def test_an_unavailable_meter_is_still_dead(issues):
    """``unavailable`` keeps being republished; it is silence, not a reading."""
    ctrl = _ctrl(
        MAIN_SENSOR_DEAD_S + 60,
        state=_state(value="unavailable", reported_s_ago=1.0),
    )
    _check(ctrl)

    assert len(issues.created) == 1
