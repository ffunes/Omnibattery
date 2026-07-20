"""Daily energy derived from lifetime hardware counters (Anker)."""
from __future__ import annotations

import pytest

from custom_components.omnibattery.sensors.calculated_sensors import (
    _CumulativeDailyEnergyData,
)


def test_accumulates_counter_deltas_within_same_day():
    state = _CumulativeDailyEnergyData(0.0, None, "2026-07-20")

    state.update(491.0, "2026-07-20")
    state.update(491.4, "2026-07-20")
    state.update(492.1, "2026-07-20")

    assert state.kwh == pytest.approx(1.1)
    assert state.last_total == 492.1


def test_roundtrip_restores_value_and_baseline():
    original = _CumulativeDailyEnergyData(1.7, 492.7, "2026-07-20")
    restored = _CumulativeDailyEnergyData.from_dict(original.as_dict())

    assert restored == original
    restored.update(493.0, "2026-07-20")
    assert restored.kwh == pytest.approx(2.0)


def test_first_sample_after_midnight_starts_new_day():
    state = _CumulativeDailyEnergyData(2.4, 492.4, "2026-07-20")

    state.update(492.6, "2026-07-21")

    assert state.kwh == 0.0
    assert state.last_total == 492.6
    assert state.reset_date == "2026-07-21"


def test_counter_reset_preserves_daily_value_and_rebases():
    state = _CumulativeDailyEnergyData(1.4, 492.4, "2026-07-20")

    state.update(0.0, "2026-07-20")
    assert state.kwh == 1.4
    state.update(0.3, "2026-07-20")

    assert state.kwh == pytest.approx(1.7)
    assert state.last_total == 0.3


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"kwh": "unavailable", "last_total": 10, "reset_date": "2026-07-20"},
        {"kwh": 1, "last_total": "bad", "reset_date": "2026-07-20"},
    ),
)
def test_malformed_restore_payload_is_rejected(payload):
    assert _CumulativeDailyEnergyData.from_dict(payload) is None
