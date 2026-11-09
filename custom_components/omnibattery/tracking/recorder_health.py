"""Report Recorder exclusions that prevent profile history recovery."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.recorder import is_entity_recorded
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started

from ..const import DOMAIN


class RecorderHealthMonitor:
    """Check learning inputs independently of battery control and DB queries."""

    def __init__(self, hass: Any, entry: Any, controller: Any) -> None:
        self.hass = hass
        self.entry = entry
        self.controller = controller
        self.issue_id = f"recorder_excluded_sensors_{entry.entry_id}"
        self._reported: tuple[str, ...] | None = None
        self._stopped = False

    def source_entities(self) -> set[str]:
        """Use the same sources as consumption and direct-PV backfill."""
        tracker = self.controller._consumption_tracker
        # Resolve each time: the user can rename the aggregate entity at runtime.
        home = tracker._home_consumption_entity_id()
        sources = {home} if home else set()
        solar = self.controller.solar_production_sensor
        if solar and tracker.solar_profile.mode != "off":
            sources.add(solar)
        for device in self.entry.data.get("excluded_devices", []) or []:
            if (
                isinstance(device, dict)
                and device.get("enabled", True)
                and not device.get("ev_charger_no_telemetry", False)
                and device.get("power_sensor")
            ):
                sources.add(device["power_sensor"])
        return sources

    @callback
    def async_check(self, _now: Any = None) -> None:
        """Check configuration, not empty history or transient sensor states."""
        if self._stopped or "recorder" not in self.hass.config.components:
            return
        excluded = tuple(sorted(
            entity for entity in self.source_entities()
            if not is_entity_recorded(self.hass, entity)
        ))
        if excluded == self._reported:
            return
        self._reported = excluded
        if not excluded:
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self.issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="recorder_excluded_sensors",
            translation_placeholders={"sensors": ", ".join(excluded)},
        )

    @callback
    def async_stop(self) -> None:
        """Remove the entry-owned warning when the integration unloads."""
        self._stopped = True
        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)


@callback
def async_setup_recorder_health(hass: Any, entry: Any, controller: Any) -> None:
    """Wait for HA startup and own every callback through the config entry."""
    monitor = RecorderHealthMonitor(hass, entry, controller)

    @callback
    def start(_hass: Any) -> None:
        monitor.async_check()
        entry.async_on_unload(
            async_track_time_interval(hass, monitor.async_check, timedelta(minutes=5))
        )

    entry.async_on_unload(async_at_started(hass, start))
    entry.async_on_unload(monitor.async_stop)
