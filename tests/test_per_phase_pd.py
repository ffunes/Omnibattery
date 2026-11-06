"""Unit tests for command-free independent phase PD planning."""
import pytest

from custom_components.omnibattery.control.per_phase import (
    PerPhasePlanner,
    PhaseAssignment,
)


def planner():
    return PerPhasePlanner((
        PhaseAssignment("mix_pro_1", "sensor.shelly_puissance_l1"),
        PhaseAssignment("mix_pro_2", "sensor.shelly_puissance_l2"),
        PhaseAssignment("mix_ac_1", "sensor.shelly_puissance_l3"),
    ))


def test_independent_phase_commands():
    control = planner()
    assert control.plan(
        "mix_pro_1", 300, max_charge_w=3000, max_discharge_w=3000, now=10
    ) == -300
    control.acknowledge("mix_pro_1", -300, now=10)
    assert control.plan(
        "mix_pro_2", -200, max_charge_w=3000, max_discharge_w=3000, now=11
    ) == 200
    assert control.states["mix_pro_1"].last_command_w == -300
    assert control.states["mix_pro_2"].last_command_w == 0


def test_deadband_and_minimum_interval():
    control = planner()
    assert control.plan(
        "mix_pro_1", 49, max_charge_w=3000, max_discharge_w=3000, now=10
    ) is None
    assert control.plan(
        "mix_pro_1", 200, max_charge_w=3000, max_discharge_w=3000, now=10
    ) == -200
    control.acknowledge("mix_pro_1", -200, now=10)
    assert control.plan(
        "mix_pro_1", 400, max_charge_w=3000, max_discharge_w=3000, now=14
    ) is None
    assert control.plan(
        "mix_pro_1", 400, max_charge_w=3000, max_discharge_w=3000, now=15
    ) == -600


def test_clamps_and_step_limit():
    control = planner()
    assert control.plan(
        "mix_ac_1", 5000, max_charge_w=2000, max_discharge_w=600, now=10
    ) == -600
    assert control.plan(
        "mix_ac_1", -5000, max_charge_w=500, max_discharge_w=2000, now=10
    ) == 500


def test_missing_measurement_does_not_issue_command():
    control = planner()
    assert control.plan(
        "mix_pro_1", None, max_charge_w=3000, max_discharge_w=3000
    ) is None
    assert control.plan(
        "mix_pro_1", float("nan"), max_charge_w=3000, max_discharge_w=3000
    ) is None


def test_duplicate_battery_assignment_is_rejected():
    with pytest.raises(ValueError):
        PerPhasePlanner((
            PhaseAssignment("battery", "sensor.phase_a"),
            PhaseAssignment("battery", "sensor.phase_b"),
        ))


def test_rejected_write_does_not_advance_cooldown():
    control = planner()
    assert control.plan(
        "mix_pro_1", 200, max_charge_w=1000, max_discharge_w=1000, now=10
    ) == -200
    assert control.plan(
        "mix_pro_1", 200, max_charge_w=1000, max_discharge_w=1000, now=11
    ) == -200


def test_custom_per_phase_parameters():
    control = PerPhasePlanner((
        PhaseAssignment(
            "battery", "sensor.phase_a",
            target_w=100, deadband_w=20, max_step_w=40,
        ),
    ))
    assert control.plan(
        "battery", 200, max_charge_w=1000, max_discharge_w=1000, now=1
    ) == -40


def test_invalid_parameters():
    with pytest.raises(ValueError):
        PhaseAssignment("battery", "sensor.phase_a", deadband_w=-1)
