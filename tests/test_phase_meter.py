"""Unit tests for fail-closed per-phase power telemetry."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.omnibattery.control.phase_meter import read_phase_power


NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def state(value, unit="W", age=0):
    return SimpleNamespace(
        state=value,
        attributes={"unit_of_measurement": unit},
        last_reported=NOW - timedelta(seconds=age),
    )


def test_import_export_and_kw():
    assert read_phase_power(state("988"), now=NOW).power_w == 988
    assert read_phase_power(state("-982"), now=NOW).power_w == -982
    assert read_phase_power(state("1.2", "kW"), now=NOW).power_w == 1200
    assert read_phase_power(state("988"), now=NOW, inverted=True).power_w == -988


def test_unavailable_stale_and_missing():
    assert read_phase_power(None, now=NOW).reason == "sensor_not_found"
    assert read_phase_power(state("unavailable"), now=NOW).power_w is None
    assert read_phase_power(state("100", age=20), now=NOW).reason == "sensor_stale"
    assert read_phase_power(state("100", "A"), now=NOW).reason == "sensor_invalid_unit"
    assert read_phase_power(state("nan"), now=NOW).power_w is None
    s = state("100")
    s.last_reported = None
    assert read_phase_power(s, now=NOW).reason == "sensor_missing_timestamp"


def test_future_measurement_is_rejected():
    assert read_phase_power(state("100", age=-20), now=NOW).reason == "sensor_future_timestamp"
