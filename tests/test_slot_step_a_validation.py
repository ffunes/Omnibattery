"""Time slot step A: Manual mode must force a single direction."""
from custom_components.omnibattery.config_flow import _validate_slot_step_a


def _slot(**over):
    base = {
        "start_time": "05:30:00", "end_time": "05:55:00",
        "allow_charge": True, "allow_discharge": False,
        "power_override_enabled": True, "mode": "manual",
    }
    return {**base, **over}


def test_manual_with_both_directions_is_rejected():
    assert _validate_slot_step_a(_slot(allow_discharge=True)) == {
        "base": "manual_requires_single_direction"
    }


def test_manual_single_direction_is_valid():
    assert _validate_slot_step_a(_slot()) == {}


def test_pd_with_both_directions_is_valid():
    assert _validate_slot_step_a(_slot(mode="pd", allow_discharge=True)) == {}
