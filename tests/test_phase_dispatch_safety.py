"""Safety readiness contract for per-phase dispatch."""
from dataclasses import replace

import pytest

from custom_components.omnibattery.control.phase_dispatch_safety import (
    PhaseDispatchReadiness,
)


@pytest.fixture
def ready():
    return PhaseDispatchReadiness(
        enabled=True,
        exclusive_owner=True,
        meter_fresh=True,
        battery_available=True,
        manual_mode=False,
        backup_active=False,
        manual_slot_active=False,
        rs485_disabled=False,
        soc_allows_charge=True,
        soc_allows_discharge=True,
    )


def test_ready_allows_both_directions(ready):
    assert ready.allows(300)
    assert ready.allows(-300)


@pytest.mark.parametrize("field", [
    "enabled", "exclusive_owner", "meter_fresh", "battery_available",
])
def test_required_conditions_fail_closed(ready, field):
    assert not replace(ready, **{field: False}).allows(-300)


@pytest.mark.parametrize("field", [
    "manual_mode", "backup_active", "manual_slot_active", "rs485_disabled",
])
def test_other_owners_or_safety_modes_block(ready, field):
    assert not replace(ready, **{field: True}).allows(-300)


def test_soc_limits_are_directional(ready):
    assert not replace(ready, soc_allows_charge=False).allows(300)
    assert replace(ready, soc_allows_charge=False).allows(-300)
    assert not replace(ready, soc_allows_discharge=False).allows(-300)
    assert replace(ready, soc_allows_discharge=False).allows(300)


def test_nonfinite_commands_blocked(ready):
    assert not ready.allows(float("nan"))
    assert not ready.allows(float("inf"))
