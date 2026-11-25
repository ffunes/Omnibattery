"""Tests for single-entry, three-phase command-free arbitration."""
import pytest

from custom_components.omnibattery.control.phase_channels import (
    PhaseChannel,
    PhaseChannelManager,
    validate_channels,
)
from custom_components.omnibattery.control.phase_meter import PhaseMeterReading


def channels():
    return (
        PhaseChannel("L1", "mix_pro_1", "sensor.l1", enabled=True),
        PhaseChannel("L2", "mix_pro_2", "sensor.l2", enabled=True),
        PhaseChannel("L3", "mix_ac_1", "sensor.l3", enabled=True),
    )


def test_independent_three_phase_planning():
    manager = PhaseChannelManager(channels())
    limits = {"max_charge_w": 3000, "max_discharge_w": 3000}
    assert manager.plan("L1", PhaseMeterReading(12), now=10, **limits) is None
    assert manager.plan("L2", PhaseMeterReading(-982), now=10, **limits) == 800
    assert manager.plan("L3", PhaseMeterReading(988), now=10, **limits) == -800
    manager.acknowledge("L2", 800, now=10)
    assert manager.plan("L2", PhaseMeterReading(-982), now=14, **limits) is None
    assert manager.plan("L3", PhaseMeterReading(988), now=14, **limits) == -800


def test_disabled_channel_never_plans():
    manager = PhaseChannelManager((
        PhaseChannel("L1", "battery", "sensor.l1"),
    ))
    assert manager.plan(
        "L1", PhaseMeterReading(200), now=1,
        max_charge_w=1000, max_discharge_w=1000,
    ) is None
    with pytest.raises(ValueError):
        manager.acknowledge("L1", -200, now=1)


def test_missing_reading_never_plans():
    manager = PhaseChannelManager(channels())
    assert manager.plan(
        "L2", PhaseMeterReading(None, "sensor_stale"), now=1,
        max_charge_w=1000, max_discharge_w=1000,
    ) is None


@pytest.mark.parametrize("bad", [
    (PhaseChannel("L1", "a", "sensor.a"), PhaseChannel("L1", "b", "sensor.b")),
    (PhaseChannel("L1", "a", "sensor.a"), PhaseChannel("L2", "a", "sensor.b")),
    (PhaseChannel("L1", "a", "sensor.a"), PhaseChannel("L2", "b", "sensor.a")),
])
def test_duplicate_assignments_rejected(bad):
    with pytest.raises(ValueError):
        validate_channels(bad)


def test_invalid_phase_rejected():
    with pytest.raises(ValueError):
        PhaseChannel("L4", "a", "sensor.a")
