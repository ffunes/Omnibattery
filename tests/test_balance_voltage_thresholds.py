"""Product-contract tests for the shared top-cell voltage policy."""

from custom_components.omnibattery.drivers.base import LFP_CELL_VOLTAGE_PROFILE


def test_normal_balance_pause_stays_at_3_60_v() -> None:
    """The integrated normal taper keeps its established upper stop point."""
    assert LFP_CELL_VOLTAGE_PROFILE.pause_v == 3.60
