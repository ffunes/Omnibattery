"""Recorder exclusions must be actionable without querying historical data."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.omnibattery.tracking import recorder_health


@pytest.fixture
def monitor(monkeypatch):
    hass = SimpleNamespace(config=SimpleNamespace(components={"recorder"}))
    entry = SimpleNamespace(entry_id="entry1", data={"excluded_devices": []})
    tracker = SimpleNamespace(
        _home_consumption_entity_id=lambda: "sensor.renamed_home",
        solar_profile=SimpleNamespace(mode="active"),
    )
    controller = SimpleNamespace(
        _consumption_tracker=tracker, solar_production_sensor="sensor.pv"
    )
    monkeypatch.setattr(recorder_health.ir, "async_create_issue", Mock())
    monkeypatch.setattr(recorder_health.ir, "async_delete_issue", Mock())
    return recorder_health.RecorderHealthMonitor(hass, entry, controller)


def test_excluded_live_solar_sensor_creates_repair(monitor, monkeypatch):
    monkeypatch.setattr(
        recorder_health, "is_entity_recorded", lambda hass, entity: entity != "sensor.pv"
    )
    monitor.async_check()
    recorder_health.ir.async_create_issue.assert_called_once()
    args, kwargs = recorder_health.ir.async_create_issue.call_args
    assert args[2] == "recorder_excluded_sensors_entry1"
    assert kwargs["translation_placeholders"] == {"sensors": "sensor.pv"}


def test_no_duplicate_warning_and_recovery_clears(monitor, monkeypatch):
    excluded = {"sensor.pv"}
    monkeypatch.setattr(
        recorder_health, "is_entity_recorded", lambda hass, entity: entity not in excluded
    )
    monitor.async_check()
    monitor.async_check()
    assert recorder_health.ir.async_create_issue.call_count == 1
    excluded.clear()
    monitor.async_check()
    recorder_health.ir.async_delete_issue.assert_called_with(
        monitor.hass, "omnibattery", monitor.issue_id
    )
    excluded.add("sensor.renamed_home")
    monitor.async_check()
    assert recorder_health.ir.async_create_issue.call_count == 2


def test_sources_follow_backfill_and_ignore_live_only_forecasts(monitor):
    monitor.entry.data.update(
        solar_forecast_remaining_sensor="sensor.remaining",
        consumption_sensor="sensor.grid",
        excluded_devices=[
            {"power_sensor": "sensor.ev"},
            {"power_sensor": "sensor.extra", "included_in_consumption": False},
            {"power_sensor": "sensor.disabled", "enabled": False},
            {"power_sensor": "sensor.no_telemetry", "ev_charger_no_telemetry": True},
            {},
        ],
    )
    assert monitor.source_entities() == {
        "sensor.renamed_home", "sensor.pv", "sensor.ev", "sensor.extra"
    }
    monitor.controller._consumption_tracker.solar_profile.mode = "off"
    assert "sensor.pv" not in monitor.source_entities()


def test_included_sensors_need_no_history_to_be_healthy(monitor, monkeypatch):
    monkeypatch.setattr(recorder_health, "is_entity_recorded", lambda *args: True)
    monitor.async_check()
    recorder_health.ir.async_create_issue.assert_not_called()


def test_recorder_optional_and_unload_clears_warning(monitor, monkeypatch):
    monitor.hass.config.components.clear()
    check = Mock(side_effect=AssertionError("Recorder is not loaded"))
    monkeypatch.setattr(recorder_health, "is_entity_recorded", check)
    monitor.async_check()
    check.assert_not_called()
    monitor.async_stop()
    recorder_health.ir.async_delete_issue.assert_called_once()


def test_real_recorder_include_exclude_rules(monitor, monkeypatch):
    from homeassistant.components.recorder import entity_options
    from homeassistant.helpers.entityfilter import generate_filter

    recorder = SimpleNamespace(entity_filter=generate_filter(
        include_domains=[], include_entities=["sensor.renamed_home"],
        exclude_domains=["sensor"], exclude_entities=[],
        include_entity_globs=[], exclude_entity_globs=["sensor.pv*"],
    ))
    monkeypatch.setattr(entity_options, "get_instance", lambda hass: recorder)
    monitor.async_check()
    assert recorder_health.ir.async_create_issue.call_args.kwargs[
        "translation_placeholders"
    ] == {"sensors": "sensor.pv"}
    recorder.entity_filter = None
    monitor.async_check()
    recorder_health.ir.async_delete_issue.assert_called_with(
        monitor.hass, "omnibattery", monitor.issue_id
    )


def test_renamed_and_removed_sources_update_existing_issue(monitor, monkeypatch):
    monkeypatch.setattr(recorder_health, "is_entity_recorded", lambda *args: False)
    monitor.async_check()
    monitor.controller.home_consumption_sensor = "sensor.stale_cached_name"
    monitor.controller._consumption_tracker._home_consumption_entity_id = (
        lambda: "sensor.new_name"
    )
    monitor.controller.solar_production_sensor = None
    monitor.async_check()
    assert recorder_health.ir.async_create_issue.call_args.kwargs[
        "translation_placeholders"
    ] == {"sensors": "sensor.new_name"}
    monitor.controller._consumption_tracker._home_consumption_entity_id = lambda: None
    monitor.async_check()
    recorder_health.ir.async_delete_issue.assert_called_with(
        monitor.hass, "omnibattery", monitor.issue_id
    )


def test_setup_waits_for_start_and_unload_cancels_callbacks(monitor, monkeypatch):
    callbacks = []
    monitor.entry.async_on_unload = callbacks.append
    start_cancel, timer_cancel = Mock(), Mock()
    start = Mock(return_value=start_cancel)
    timer = Mock(return_value=timer_cancel)
    monkeypatch.setattr(recorder_health, "async_at_started", start)
    monkeypatch.setattr(recorder_health, "async_track_time_interval", timer)
    monkeypatch.setattr(recorder_health, "is_entity_recorded", lambda *args: False)
    recorder_health.async_setup_recorder_health(
        monitor.hass, monitor.entry, monitor.controller
    )
    timer.assert_not_called()
    recorder_health.ir.async_create_issue.assert_not_called()
    start.call_args.args[1](monitor.hass)
    recorder_health.ir.async_create_issue.assert_called_once()
    assert timer.call_args.args[2].total_seconds() == 300
    for cancel in callbacks:
        cancel()
    start_cancel.assert_called_once()
    timer_cancel.assert_called_once()
    # A queued timer cannot recreate a warning after unload.
    timer.call_args.args[1]()
    assert recorder_health.ir.async_create_issue.call_count == 1
