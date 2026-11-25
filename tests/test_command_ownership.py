"""Unit tests for battery command ownership isolation."""
import pytest

from custom_components.omnibattery.control.command_ownership import (
    BatteryCommandOwnership,
)


def test_fleet_is_default_owner():
    owners = BatteryCommandOwnership()
    assert owners.permits("mix_pro_1", "fleet")
    assert not owners.permits("mix_pro_1", "phase:L1")
    assert not owners.permits("mix_pro_1", "automatic")


def test_phase_assignment_excludes_fleet_and_other_phases():
    owners = BatteryCommandOwnership()
    owners.assign_phase("mix_pro_1", "L1")
    assert owners.permits("mix_pro_1", "phase:L1")
    assert not owners.permits("mix_pro_1", "fleet")
    assert not owners.permits("mix_pro_1", "phase:L2")
    assert owners.permits("mix_pro_2", "fleet")


def test_duplicate_phase_is_rejected():
    owners = BatteryCommandOwnership()
    owners.assign_phase("mix_pro_1", "L1")
    with pytest.raises(ValueError):
        owners.assign_phase("mix_pro_2", "L1")


def test_release_restores_fleet_ownership():
    owners = BatteryCommandOwnership()
    owners.assign_phase("mix_ac_1", "L3")
    owners.release_phase("mix_ac_1")
    assert owners.permits("mix_ac_1", "fleet")
    assert not owners.permits("mix_ac_1", "phase:L3")


def test_invalid_phase_and_unknown_owner_rejected():
    owners = BatteryCommandOwnership()
    with pytest.raises(ValueError):
        owners.assign_phase("mix_pro_1", "L4")
    assert not owners.permits("mix_pro_1", "manual")
