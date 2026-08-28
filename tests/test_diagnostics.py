"""Tests for the diagnostics platform and the coordinator health snapshot.

Pure-unit: no ``hass`` fixture, no real Modbus. The coordinator's
``health_snapshot`` is exercised by calling the unbound method against a
duck-typed stub (it only reads attributes), and the diagnostics assembly is
driven with stub coordinators plus a real NonResponsiveTracker.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.omnibattery import diagnostics
from custom_components.omnibattery.const import DOMAIN
from custom_components.omnibattery.drivers import DriverCapabilities
from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
)
from custom_components.omnibattery.tracking.non_responsive_tracker import (
    NonResponsiveTracker,
)


class _FakeDatetime:
    def __init__(self, iso: str) -> None:
        self._iso = iso

    def isoformat(self) -> str:
        return self._iso


def _make_caps() -> DriverCapabilities:
    return DriverCapabilities(
        hardware_soc_cutoff=False,
        has_force_mode=True,
        push_telemetry=False,
        max_charge_power_w=2500,
        max_discharge_power_w=2500,
        has_mppt_pv=False,
        has_alarm_registers=True,
        has_rs485_control=True,
    )


def test_health_snapshot_serialises_ladder_fields():
    fake = SimpleNamespace(
        name="Battery 1",
        brand="marstek",
        battery_version="v3",
        _is_connected=True,
        is_available=True,
        _is_shutting_down=False,
        _consecutive_failures=2,
        _max_failures_before_reconnect=3,
        _max_failures_before_suspend=5,
        _suspension_reset_time=_FakeDatetime("2026-07-02T22:00:00+00:00"),
        _last_write_failure_reason="driver_exception",
        _last_update_times={("battery_soc", "battery_power"): _FakeDatetime("2026-07-02T21:59:00+00:00")},
    )

    snap = MarstekVenusDataUpdateCoordinator.health_snapshot(fake)

    assert snap["name"] == "Battery 1"
    assert snap["consecutive_failures"] == 2
    assert snap["suspended"] is True
    assert snap["suspension_reset_time"] == "2026-07-02T22:00:00+00:00"
    assert snap["last_write_failure_reason"] == "driver_exception"
    # tuple group key is joined into a single serialisable string
    assert snap["last_update_times"] == {
        "battery_soc,battery_power": "2026-07-02T21:59:00+00:00"
    }


def test_health_snapshot_not_suspended_when_no_reset_time():
    fake = SimpleNamespace(
        name="B", brand="zendure", battery_version="v2",
        _is_connected=False, is_available=False, _is_shutting_down=False,
        _consecutive_failures=0, _max_failures_before_reconnect=3,
        _max_failures_before_suspend=5, _suspension_reset_time=None,
        _last_write_failure_reason=None, _last_update_times={},
    )
    snap = MarstekVenusDataUpdateCoordinator.health_snapshot(fake)
    assert snap["suspended"] is False
    assert snap["suspension_reset_time"] is None


class _StubCoordinator:
    def __init__(self, name: str) -> None:
        self.name = name
        self.driver = SimpleNamespace(connected=True, model_label="Venus v3")
        self.capabilities = _make_caps()

    def health_snapshot(self) -> dict:
        return {"name": self.name, "connected": True, "suspended": False}


async def test_diagnostics_dump_structure_and_redaction():
    coord = _StubCoordinator("Battery 1")
    tracker = NonResponsiveTracker(fail_threshold=3)
    # Drive it to exclusion (comm failures take no wake-grace round).
    for _ in range(3):
        tracker.record_comm_failure(coord, "feedback_timeout")
    controller = SimpleNamespace(_non_responsive=tracker)

    entry = SimpleNamespace(
        entry_id="abc",
        title="Omnibattery",
        version=9,
        data={
            "host": "192.168.1.50",
            "username": "SESSY1234",
            "password": "secret",
            "consumption_sensor": "sensor.grid",
            "solar_forecast_sensor": "sensor.solcast",
            "average_price_sensor": "sensor.average_price",
            "phase_1_current_sensor": "sensor.phase_1",
            "phase_2_current_sensor": "sensor.phase_2",
            "phase_3_current_sensor": "sensor.phase_3",
            "brand": "sessy",
        },
        options={},
    )
    hass = SimpleNamespace(
        data={DOMAIN: {"abc": {"coordinators": [coord], "controller": controller}}}
    )

    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    assert result["entry"]["data"]["host"] == "**REDACTED**"
    assert result["entry"]["data"]["username"] == "**REDACTED**"
    assert result["entry"]["data"]["password"] == "**REDACTED**"
    assert result["entry"]["data"]["consumption_sensor"] == "sensor.grid"
    assert result["entry"]["data"]["solar_forecast_sensor"] == "sensor.solcast"
    assert result["entry"]["data"]["average_price_sensor"] == "sensor.average_price"
    assert result["entry"]["data"]["phase_1_current_sensor"] == "sensor.phase_1"
    assert result["entry"]["data"]["phase_2_current_sensor"] == "sensor.phase_2"
    assert result["entry"]["data"]["phase_3_current_sensor"] == "sensor.phase_3"
    assert result["entry"]["data"]["brand"] == "sessy"  # non-sensitive kept

    battery = result["batteries"][0]
    assert battery["health"]["name"] == "Battery 1"
    assert battery["driver"]["model_label"] == "Venus v3"
    assert battery["driver"]["capabilities"]["max_charge_power_w"] == 2500
    assert battery["tracker"]["excluded"] is True
    assert battery["tracker"]["reason"] == "feedback_timeout"


async def test_diagnostics_read_is_side_effect_free():
    """The dump must not reset tracker state (no is_excluded call)."""
    coord = _StubCoordinator("Battery 1")
    tracker = NonResponsiveTracker(fail_threshold=3)
    for _ in range(3):
        tracker.record_comm_failure(coord, "feedback_timeout")
    controller = SimpleNamespace(_non_responsive=tracker)
    entry = SimpleNamespace(entry_id="abc", title="t", version=9, data={}, options={})
    hass = SimpleNamespace(
        data={DOMAIN: {"abc": {"coordinators": [coord], "controller": controller}}}
    )

    await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    # fail_count untouched, still excluded
    assert tracker.batteries[coord]["fail_count"] == 3
    assert coord.name in tracker.excluded_names()


async def test_diagnostics_handles_missing_entry_data():
    entry = SimpleNamespace(entry_id="missing", title="t", version=9, data={}, options={})
    hass = SimpleNamespace(data={DOMAIN: {}})
    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert result["batteries"] == []


def test_curtailment_diagnostics_derives_opportunistic_space_and_legacy_export():
    controller = SimpleNamespace(
        _curtailment_plan=SimpleNamespace(
            current_headroom_kwh=5.0,
            required_headroom_kwh=3.0,
            status="planned",
            reason="headroom_required",
        ),
        _curtailment_runtime_status="planned",
        _curtailment_runtime_reason="selected_discharge_slot",
        _active_dynamic_slot_purpose="negative_price",
        predischarge_max_export_power_w=1200,
        _global_charge_blockers={
            "max_soc": {
                "reason": "max_soc",
                "details": {"soc": 100},
                "since": _FakeDatetime("2026-08-05T10:00:00+00:00"),
            }
        },
    )

    result = diagnostics._curtailment_info(controller)

    assert result["solar_reserve_remaining_kwh"] == 3.0
    assert result["current_free_space_kwh"] == 5.0
    assert result["opportunistic_space_available_kwh"] == 2.0
    assert result["opportunistic_space_source"] == (
        "current_free_space_kwh - solar_reserve_remaining_kwh"
    )
    assert result["charge_limit_reason"] == "max_soc: max_soc"
    assert result["charge_blocked"] is True
    assert result["export"]["mode"] == "custom"
    assert result["export"]["limit_w"] == 1200.0


def test_curtailment_diagnostics_tolerate_missing_optional_attributes():
    result = diagnostics._curtailment_info(SimpleNamespace())

    assert result["solar_reserve_remaining_kwh"] is None
    assert result["opportunistic_space_available_kwh"] is None
    assert result["charge_limit_reason"] is None
    assert result["charge_blockers"] == {"global": {}, "batteries": {}}
    assert result["export"] == {
        "mode": None,
        "mode_label": None,
        "limit_w": None,
        "limit_description": None,
    }


@pytest.mark.parametrize(
    ("mode", "limit", "expected_mode"),
    [
        ("self_consumption", 0, "self_consumption"),
        ("automatic", None, "automatic"),
        ("custom", 800, "custom"),
    ],
)
def test_export_diagnostics_reports_each_mode(mode, limit, expected_mode):
    controller = SimpleNamespace(
        predischarge_export_mode=mode,
        predischarge_max_export_power_w=limit,
    )

    result = diagnostics._curtailment_info(controller)

    assert result["export_mode"] == expected_mode
    assert result["export_limit_w"] == limit


def test_dynamic_pricing_info_reports_excluded_demand_claim():
    controller = SimpleNamespace(
        _dynamic_pricing_schedule=None,
        _last_decision_data={
            "excluded_demand_claim_kwh": 6.4732,
            "solar_available_to_battery_kwh": 7.1968,
        },
    )

    info = diagnostics._dynamic_pricing_info(controller)

    assert info["excluded_demand_claim_kwh"] == 6.473
    assert info["solar_available_to_battery_kwh"] == 7.197


def test_dynamic_pricing_info_claim_is_none_without_a_decision():
    controller = SimpleNamespace(_dynamic_pricing_schedule=None, _last_decision_data=None)

    info = diagnostics._dynamic_pricing_info(controller)

    assert info["excluded_demand_claim_kwh"] is None
