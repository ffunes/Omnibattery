"""Cell balance monitor for Marstek Venus batteries."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from ..const import (
    BALANCE_BASELINE_OFFSET_MV,
    BALANCE_HISTORY_MAX,
    BALANCE_NOTIFY_COOLDOWN_DAYS,
    BALANCE_RED_CONSECUTIVE_ALERT,
    BALANCE_STORAGE_KEY,
    BALANCE_STORAGE_VERSION,
    BALANCE_THRESHOLD_ORANGE,
    BALANCE_THRESHOLD_RED,
    BALANCE_THRESHOLD_YELLOW,
    BALANCE_TREND_ALERT_AVG_MV,
    DOMAIN,
    NOTIFICATION_ID_PREFIX,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


@dataclass
class _BatteryState:
    phase: str = "IDLE"
    phase_started: datetime | None = None
    stable_polls: int = 0
    prev_vmax: float | None = None


class BalanceSensorGroup:
    """Thin container holding the 5 sensor entities for one battery."""

    def __init__(self) -> None:
        self._entities: list[Any] = []

    def register(self, entity: Any) -> None:
        self._entities.append(entity)


class BalanceMonitor:
    """Manages cell-voltage balance readings for all batteries in one entry."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, controller: Any) -> None:
        self._hass = hass
        self._controller = controller
        self._store: Store = Store(
            hass,
            BALANCE_STORAGE_VERSION,
            f"{DOMAIN}.{config_entry.entry_id}.{BALANCE_STORAGE_KEY}",
        )
        self._data: dict[str, Any] = {}
        self._states: dict[str, _BatteryState] = {}
        self._sensor_groups: dict[str, BalanceSensorGroup] = {}

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Load persisted data from store."""
        stored = await self._store.async_load()
        if stored:
            self._data = stored

    async def async_restore_coordinator(self, coordinator: Any) -> None:
        """Restore state machine phase for a coordinator after HA restart."""
        await self._migrate_legacy_host_key(coordinator)
        host = coordinator.device_key
        bat = self._data.get(host, {})
        phase = bat.get("phase", "IDLE")
        phase_started = None
        if bat.get("phase_started_ts"):
            try:
                phase_started = datetime.fromisoformat(bat["phase_started_ts"])
            except ValueError:
                phase_started = None

        # stable_polls and prev_vmax are transient — reset so stale pre-shutdown
        # values don't cause a premature OCV read on the first poll after restart.
        self._states[host] = _BatteryState(
            phase=phase,
            phase_started=phase_started,
            stable_polls=0,
            prev_vmax=None,
        )

        if phase in ("WAITING_OCV", "HOLD_ORANGE"):
            coordinator.balance_hold = False
            self._states[host] = _BatteryState()
            await self._persist_state(host, self._states[host])
            _LOGGER.info(
                "[%s] Balance monitor: cleared legacy phase %s from store",
                coordinator.name,
                phase,
            )

    async def _migrate_legacy_host_key(self, coordinator: Any) -> None:
        """Rename persisted data keyed by bare host to the device_key scheme.

        Pre-slave-id installs keyed balance data by host alone. device_key is
        ``{host}_{port}`` for slave 1, so rename the old entry in place to keep
        history. No-op once migrated or for fresh installs.
        """
        legacy = coordinator.host
        new_key = coordinator.device_key
        if legacy != new_key and legacy in self._data and new_key not in self._data:
            self._data[new_key] = self._data.pop(legacy)
            await self._store.async_save(self._data)
            _LOGGER.info(
                "[%s] Balance monitor: migrated store key %s -> %s",
                coordinator.name,
                legacy,
                new_key,
            )

    # ------------------------------------------------------------------
    # Main entry point — called every coordinator poll cycle
    # ------------------------------------------------------------------

    async def async_process(self, coordinator: Any) -> None:
        """Clear legacy OCV state.

        Imbalance readings are recorded by explicit top-charge or settled
        blueprint measurements. Historical active-balance records remain
        readable but are no longer produced by the control loop.
        """
        host = coordinator.device_key
        if host not in self._states:
            self._states[host] = _BatteryState()

        state = self._states[host]
        if state.phase != "IDLE" or coordinator.balance_hold:
            coordinator.balance_hold = False
            state.phase = "IDLE"
            state.phase_started = None
            state.stable_polls = 0
            state.prev_vmax = None
            await self._persist_state(host, state)

    async def async_record_top_balance_measurement(
        self,
        coordinator: Any,
        vmax: float,
        vmin: float,
        soc: float | None,
        phase: str | None = None,
        source: str | None = None,
        measurement_id: str | None = None,
    ) -> None:
        """Record an explicit settled top-voltage delta measurement."""
        try:
            vmax_f = float(vmax)
            vmin_f = float(vmin)
        except (TypeError, ValueError):
            return
        try:
            soc_f = float(soc) if soc is not None else None
        except (TypeError, ValueError):
            soc_f = None
        delta_mv = (vmax_f - vmin_f) * 1000
        await self._save_reading(
            coordinator.device_key,
            delta_mv,
            vmax_f,
            vmin_f,
            soc_f,
            "top_balance_measurement",
            coordinator,
            extra={
                "phase": phase,
                "source": source,
                "measurement_id": measurement_id,
            },
        )

    async def async_record_blueprint_balance_measurement(
        self,
        coordinator: Any,
        *,
        phase: str | None = None,
        measurement_id: str | None = None,
    ) -> bool:
        """Record a settled cell delta reported by the active-balance blueprint.

        The blueprint owns the battery while this method is called, but the
        voltage values always come from Omnibattery's coordinator. This keeps
        the integration's history and sensor updates on the same telemetry
        path as normal top-charge measurements.
        """
        phase_name = str(phase or "WAIT_MEASURE").strip()
        if phase_name not in {"WAIT_MEASURE", "blueprint_wait_measure"}:
            _LOGGER.warning(
                "[%s] Ignoring blueprint balance measurement with invalid phase %s",
                getattr(coordinator, "name", "unknown"),
                phase_name,
            )
            return False

        if not getattr(coordinator, "battery_manual_mode_enabled", False):
            _LOGGER.warning(
                "[%s] Ignoring blueprint balance measurement because Battery Manual Mode is off",
                getattr(coordinator, "name", "unknown"),
            )
            return False

        data = coordinator.data or {}
        try:
            vmax = float(data.get("max_cell_voltage"))
            vmin = float(data.get("min_cell_voltage"))
        except (TypeError, ValueError):
            _LOGGER.warning(
                "[%s] Ignoring blueprint balance measurement with invalid cell telemetry",
                getattr(coordinator, "name", "unknown"),
            )
            return False
        if not math.isfinite(vmax) or not math.isfinite(vmin) or vmax < vmin:
            _LOGGER.warning(
                "[%s] Ignoring blueprint balance measurement with invalid voltage ordering: %.4f/%.4f",
                getattr(coordinator, "name", "unknown"),
                vmax,
                vmin,
            )
            return False

        host = coordinator.device_key
        measurement_key = str(measurement_id).strip() if measurement_id is not None else ""
        if measurement_key:
            readings = self._data.get(host, {}).get("readings", [])
            if any(
                reading.get("measurement_id") == measurement_key
                for reading in readings
            ):
                _LOGGER.debug(
                    "[%s] Ignoring duplicate blueprint balance measurement %s",
                    getattr(coordinator, "name", "unknown"),
                    measurement_key,
                )
                return False

        await self.async_record_top_balance_measurement(
            coordinator,
            vmax,
            vmin,
            data.get("battery_soc"),
            phase="blueprint_wait_measure",
            source="blueprint",
            measurement_id=measurement_key or None,
        )
        _LOGGER.debug(
            "[%s] Recorded blueprint balance measurement: %.4f V - %.4f V",
            getattr(coordinator, "name", "unknown"),
            vmax,
            vmin,
        )
        return True

    def get_recent_readings(self, host: str, limit: int = 10) -> list[dict]:
        """Return the most-recent comparable readings (newest last)."""
        readings = self._comparable_readings(host)
        return list(readings[-limit:])

    # ------------------------------------------------------------------
    # Persistence and evaluation
    # ------------------------------------------------------------------

    async def _save_reading(
        self,
        host: str,
        delta_mv: float,
        vmax: float,
        vmin: float,
        soc: float | None,
        reading_type: str,
        coordinator: Any = None,
        extra: dict | None = None,
    ) -> str:
        bat = self._data.setdefault(
            host, {"readings": [], "consecutive_red": 0}
        )
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "delta_mV": round(delta_mv, 1),
            "vmax_V": round(vmax, 4),
            "vmin_V": round(vmin, 4),
            "soc": soc,
            "type": reading_type,
        }
        if extra:
            entry.update({k: v for k, v in extra.items() if v is not None})
        bat["readings"].append(entry)
        bat["readings"] = bat["readings"][-BALANCE_HISTORY_MAX:]

        status = self._status_for_delta(delta_mv)
        issues: list[str] = []
        severe = False
        if reading_type == "top_balance_measurement" and coordinator is not None:
            status, severe = self._evaluate(delta_mv, bat, issues)

        comparable = self._is_comparable_reading(entry)
        trend = self._trend(host)
        if reading_type == "top_balance_measurement" and coordinator is not None:
            self._check_trend_alert(trend, issues)
            if issues:
                self._maybe_notify(host, coordinator.name, bat, issues, severe)

        await self._store.async_save(self._data)
        if comparable:
            self._push_sensors(host, delta_mv, status, trend, entry["ts"])
        return status

    def _evaluate(
        self, delta_mv: float, bat: dict, issues: list[str]
    ) -> tuple[str, bool]:
        if delta_mv < BALANCE_THRESHOLD_YELLOW:
            status = "green"
            bat["consecutive_red"] = 0
        elif delta_mv < BALANCE_THRESHOLD_ORANGE:
            status = "yellow"
            bat["consecutive_red"] = 0
        elif delta_mv < BALANCE_THRESHOLD_RED:
            status = "orange"
            bat["consecutive_red"] = 0
        else:
            status = "red"
            bat["consecutive_red"] = bat.get("consecutive_red", 0) + 1

        severe = False
        if status == "red":
            issues.append(f"Delta: {delta_mv:.0f} mV — high cell imbalance.")
        elif status == "orange":
            issues.append(f"Delta: {delta_mv:.0f} mV — moderate imbalance.")

        if status == "red" and bat["consecutive_red"] >= BALANCE_RED_CONSECUTIVE_ALERT:
            severe = True
            issues.append(
                f"High cell imbalance ({delta_mv:.0f} mV) for "
                f"{bat['consecutive_red']} consecutive full charges. Consider "
                f"running the Marstek active-balance blueprint for this battery "
                f"to rebalance the cells."
            )

        return status, severe

    def _trend(self, host: str) -> dict:
        delta_readings = self._comparable_readings(host)
        if not delta_readings:
            return {"trend": "unknown", "avg_4w": None}

        last4 = delta_readings[-4:]
        values = [r["delta_mV"] for r in last4]
        avg = sum(values) / len(values)
        if len(values) < 2:
            return {"trend": "unknown", "avg_4w": round(avg, 1), "slope": 0.0}

        slope = (values[-1] - values[0]) / max(len(values) - 1, 1)

        if slope > 2:
            trend = "rising"
        elif slope < -2:
            trend = "falling"
        else:
            trend = "stable"

        return {"trend": trend, "avg_4w": round(avg, 1), "slope": slope}

    def _check_trend_alert(self, trend: dict, issues: list[str]) -> None:
        if (
            trend["trend"] == "rising"
            and trend["avg_4w"] is not None
            and self._effective_delta(trend["avg_4w"]) > BALANCE_TREND_ALERT_AVG_MV
        ):
            issues.append(
                f"Rising imbalance trend: +{trend['slope']:.1f} mV/reading, "
                f"avg {trend['avg_4w']:.0f} mV over last readings."
            )

    def _maybe_notify(
        self, host: str, name: str, bat: dict, issues: list[str], severe: bool
    ) -> None:
        now = datetime.now(timezone.utc)
        last_ts = bat.get("last_notify_ts")
        if last_ts:
            try:
                last_dt = datetime.fromisoformat(last_ts)
                if now - last_dt < timedelta(days=BALANCE_NOTIFY_COOLDOWN_DAYS):
                    return
            except ValueError:
                pass
        bat["last_notify_ts"] = now.isoformat()
        icon = "🔴" if severe else "⚠️"
        title = f"{icon} Cell balance — {name}"
        message = "\n".join(f"• {line}" for line in issues)
        coroutine = self._notify(
            f"{NOTIFICATION_ID_PREFIX}marstek_balance_{host}", title, message
        )
        create = getattr(self._controller, "_create_entry_background_task", None)
        if callable(create):
            create(coroutine, "omnibattery_balance_notification")
        else:
            self._hass.async_create_task(coroutine)

    async def _persist_state(self, host: str, state: _BatteryState) -> None:
        bat = self._data.setdefault(host, {"readings": [], "consecutive_red": 0})
        bat["phase"] = state.phase
        bat["phase_started_ts"] = (
            state.phase_started.isoformat() if state.phase_started else None
        )
        bat["stable_polls"] = state.stable_polls
        bat["prev_vmax"] = state.prev_vmax
        await self._store.async_save(self._data)

    # ------------------------------------------------------------------
    # Sensor integration
    # ------------------------------------------------------------------

    def register_sensor_group(self, host: str, group: BalanceSensorGroup) -> None:
        self._sensor_groups[host] = group

    def _push_sensors(
        self, host: str, delta_mv: float, status: str, trend: dict, last_ts: str
    ) -> None:
        group = self._sensor_groups.get(host)
        if not group:
            return
        for entity in group._entities:
            # A disabled entity is never added to the state machine, so hass
            # stays None and on_reading()'s async_write_ha_state() would raise
            # RuntimeError and abort the measurement-recording path. Skip it.
            if entity.hass is None:
                continue
            entity.on_reading(delta_mv, status, trend["trend"], trend.get("avg_4w"), last_ts)

    def get_initial_state(self, host: str) -> dict:
        """Return state derived from store — used by sensors at startup."""
        delta_readings = self._comparable_readings(host)
        if not delta_readings:
            return {
                "delta_mV": None,
                "status": "unknown",
                "trend": "unknown",
                "avg_4w": None,
                "last_ts": None,
            }
        last = delta_readings[-1]
        trend = self._trend(host)
        return {
            "delta_mV": last["delta_mV"],
            "status": self._status_for_delta(last["delta_mV"]),
            "trend": trend["trend"],
            "avg_4w": trend.get("avg_4w"),
            "last_ts": last["ts"],
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_comparable_reading(reading: dict) -> bool:
        """Return whether a reading belongs to the repeatable top-voltage series."""
        if reading.get("delta_mV") is None:
            return False
        reading_type = reading.get("type")
        # Keep filtering records written by pre-blueprint releases so an old
        # history store remains interpretable after the integrated runner is
        # removed. New code never writes either legacy reading type.
        if reading_type == "active_balance_transition":
            return False
        if reading_type == "active_balance_measurement":
            # Older valid readings may predate the phase attribute. Explicit
            # CHARGE readings are below-stop rejection diagnostics and must not
            # affect the sensor, four-reading average or trend.
            return reading.get("phase") in {None, "WAIT_MEASURE"}
        return True

    def _comparable_readings(self, host: str) -> list[dict]:
        """Return stored readings suitable for like-for-like balance analysis."""
        readings = self._data.get(host, {}).get("readings", [])
        return [
            reading
            for reading in readings
            if self._is_comparable_reading(reading)
        ]

    @staticmethod
    def _effective_delta(delta_mv: float) -> float:
        """Subtract the factory baseline imbalance, floored at 0.

        Marstek cells ship with a large top-of-charge spread that is normal, not
        a fault. Used only by the rising-trend magnitude gate so steady
        factory-level readings do not trip a trend alert. Status thresholds are
        absolute and applied to the raw delta directly.
        """
        return max(0.0, delta_mv - BALANCE_BASELINE_OFFSET_MV)

    def _status_for_delta(self, delta_mv: float) -> str:
        effective_mv = self._effective_delta(delta_mv)
        if effective_mv < BALANCE_THRESHOLD_YELLOW:
            return "green"
        if effective_mv < BALANCE_THRESHOLD_ORANGE:
            return "yellow"
        if effective_mv < BALANCE_THRESHOLD_RED:
            return "orange"
        return "red"

    async def _notify(self, notification_id: str, title: str, message: str) -> None:
        await self._hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "notification_id": notification_id,
                "title": title,
                "message": message,
            },
        )
