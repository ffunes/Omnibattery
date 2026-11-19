"""Local HTTP driver for the APsystems EZHI hybrid microinverter."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from .base import (
    DELIVERED_AC_POWER_KEY,
    BatteryDriver,
    DriverCapabilities,
    ReadGroup,
    SetpointResult,
    TelemetrySnapshot,
)

_LOGGER = logging.getLogger(__name__)
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=8)
_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=5)
_HTTP_ATTEMPTS = 2
_HTTP_RETRY_DELAY_S = 0.05
_MAX_CHARGE_POWER_W = 1200
_MAX_DISCHARGE_POWER_W = 1200

SENSOR_DEFINITIONS = [
    {"key": "battery_total_energy", "name": "Battery Total Energy", "unit": "kWh", "device_class": "energy_storage", "state_class": "measurement", "scale": 1, "precision": 2, "scan_interval": "low", "enabled_by_default": True},
    {"key": "battery_soc", "name": "Battery SOC", "unit": "%", "device_class": "battery", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "medium", "enabled_by_default": True},
    {"key": "battery_power", "name": "Battery Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "ac_delivered_power", "name": "AC Delivered Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "power_setpoint", "name": "Power Setpoint", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "medium", "enabled_by_default": False},
    {"key": "pv_power", "name": "PV Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "battery_temperature", "name": "Battery Temperature", "unit": "°C", "device_class": "temperature", "state_class": "measurement", "scale": 1, "precision": 1, "scan_interval": "low", "enabled_by_default": True},
    {"key": "inverter_temperature", "name": "Inverter Temperature", "unit": "°C", "device_class": "temperature", "state_class": "measurement", "scale": 1, "precision": 1, "scan_interval": "low", "enabled_by_default": False},
    {"key": "total_charging_energy", "name": "Total Charging Energy", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing", "scale": 1, "precision": 3, "scan_interval": "low", "enabled_by_default": True},
    {"key": "total_discharging_energy", "name": "Total Discharging Energy", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing", "scale": 1, "precision": 3, "scan_interval": "low", "enabled_by_default": True},
    {"key": "software_version", "name": "Software Version", "data_type": "char", "icon": "mdi:ticket-confirmation-outline", "category": "diagnostic", "scan_interval": "low", "enabled_by_default": True},
]

_TELEMETRY_KEYS = frozenset({
    "battery_soc",
    "battery_power",
    DELIVERED_AC_POWER_KEY,
    "power_setpoint",
    "pv_power",
    "battery_temperature",
    "inverter_temperature",
    "total_charging_energy",
    "total_discharging_energy",
    "battery_total_energy",
})
_DEVICE_INFO_KEYS = frozenset({"software_version"})


class ApsystemsEzhiDriver(BatteryDriver):
    """Poll and control one APsystems EZHI via its local HTTP API (port 80)."""

    def __init__(
        self,
        host: str,
        *,
        port: int = 80,
        max_charge_power_w: int = _MAX_CHARGE_POWER_W,
        max_discharge_power_w: int = _MAX_DISCHARGE_POWER_W,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._base_url = f"http://{host}" + (f":{port}" if port != 80 else "")
        self._session = session
        self._owns_session = False
        self._connected = False
        self._shutting_down = False
        self._serial: Optional[str] = None
        self._capacity_kwh: Optional[float] = None
        self._last_setpoint_w: Optional[int] = None

        max_charge_power_w = max(0, min(int(max_charge_power_w), _MAX_CHARGE_POWER_W))
        max_discharge_power_w = max(0, min(int(max_discharge_power_w), _MAX_DISCHARGE_POWER_W))

        self._capabilities = DriverCapabilities(
            hardware_soc_cutoff=False,
            has_force_mode=False,
            push_telemetry=False,
            max_charge_power_w=max_charge_power_w,
            max_discharge_power_w=max_discharge_power_w,
            has_mppt_pv=False,
            has_alarm_registers=False,
            has_rs485_control=False,
            has_energy_counters=True,
            has_nominal_capacity=True,
            has_daily_energy_counters=False,
            has_solar_telemetry=True,
            setpoint_confirm_reliable=False,
            actuator_latency_s=1.5,
            readback_latency_s=3.0,
        )
        self._read_groups = [
            ReadGroup("high", tuple(_TELEMETRY_KEYS)),
            ReadGroup("low", tuple(_DEVICE_INFO_KEYS)),
        ]

    @property
    def capabilities(self) -> DriverCapabilities:
        return self._capabilities

    @property
    def dc_coupled(self) -> bool:
        return True

    @property
    def model_label(self) -> Optional[str]:
        return "EZHI"

    @property
    def serial(self) -> Optional[str]:
        return self._serial

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def read_groups(self) -> list[ReadGroup]:
        return self._read_groups

    @property
    def sensor_definitions(self) -> list[dict]:
        return SENSOR_DEFINITIONS

    @property
    def number_definitions(self) -> list[dict]:
        return []

    @property
    def select_definitions(self) -> list[dict]:
        return []

    @property
    def switch_definitions(self) -> list[dict]:
        return []

    @property
    def binary_sensor_definitions(self) -> list[dict]:
        return []

    @property
    def button_definitions(self) -> list[dict]:
        return []

    @property
    def all_definitions(self) -> list[dict]:
        return SENSOR_DEFINITIONS

    async def connect(self) -> bool:
        self._ensure_session()
        info = await self._get_json("/getDeviceInfo", timeout=_PROBE_TIMEOUT)
        if not info or not isinstance(info.get("data"), dict):
            self._connected = False
            return False
        data = info["data"]
        serial = info.get("deviceId") or data.get("deviceId")
        if serial:
            self._serial = str(serial)
        if data.get("batteryCapacity"):
            try:
                cap = float(data["batteryCapacity"])
                if cap > 0:
                    self._capacity_kwh = cap
            except (ValueError, TypeError):
                pass
        out = await self._get_json("/getOutputData", timeout=_PROBE_TIMEOUT)
        self._connected = bool(out and isinstance(out.get("data"), dict))
        return self._connected

    async def close(self) -> None:
        self._connected = False
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def set_shutting_down(self, value: bool) -> None:
        self._shutting_down = value

    async def read_telemetry(self, keys: Optional[list[str]] = None) -> TelemetrySnapshot:
        requested = (
            set(keys)
            if keys is not None
            else set(_TELEMETRY_KEYS | _DEVICE_INFO_KEYS)
        )
        snapshot: dict = {}

        if requested & _TELEMETRY_KEYS:
            res = await self._get_json("/getOutputData")
            data = res.get("data", {}) if res else {}
            if data:
                # EZHI convention: negative = charge from grid, positive = discharge to grid.
                # Omnibattery convention: +charge / -discharge.
                og_p = data.get("ogP")
                bat_p = data.get("batP")
                ac_delivered = float(og_p) if og_p is not None else None
                bat_power = float(bat_p) if bat_p is not None else ac_delivered

                snapshot.update({
                    "battery_soc": float(data["batSoc"]) if "batSoc" in data else None,
                    "battery_power": bat_power,
                    DELIVERED_AC_POWER_KEY: ac_delivered,
                    "power_setpoint": self._last_setpoint_w,
                    "pv_power": float(data.get("pvP", 0)) if "pvP" in data else None,
                    "battery_temperature": float(data["batTemp"]) if "batTemp" in data else None,
                    "inverter_temperature": float(data["devTemp"]) if "devTemp" in data else None,
                    "total_charging_energy": float(data["batCTE"]) if "batCTE" in data else None,
                    "total_discharging_energy": float(data["batDTE"]) if "batDTE" in data else None,
                    "battery_total_energy": self._capacity_kwh,
                })

        if "software_version" in requested:
            info = await self._get_json("/getDeviceInfo")
            data = info.get("data", {}) if info else {}
            if data.get("devVer"):
                snapshot["software_version"] = str(data["devVer"])
            if data.get("deviceId"):
                self._serial = str(data["deviceId"])

        snapshot = {k: v for k, v in snapshot.items() if v is not None}
        return snapshot if keys is None else {k: v for k, v in snapshot.items() if k in keys}

    async def apply_setpoint(
        self,
        net_power_w: int,
        *,
        mode_hint: Optional[str] = None,
        read_back: bool = True,
    ) -> SetpointResult:
        applied = max(
            -self._capabilities.max_discharge_power_w,
            min(self._capabilities.max_charge_power_w, int(net_power_w)),
        )
        # Omnibattery: +charge / -discharge -> EZHI /setPower?p=: -charge / +discharge
        ezhi_watts = -applied
        res = await self._get_json("/setPower", params={"p": ezhi_watts})
        if not res or str(res.get("message", "")).upper() != "SUCCESS":
            return SetpointResult(False, applied, False, failure_reason="setpoint_write_failed")

        self._last_setpoint_w = applied
        if not read_back:
            return SetpointResult(True, applied, False, applied={"power_setpoint": applied})

        out = await self._get_json("/getOutputData")
        data = out.get("data", {}) if out else {}
        og_p = data.get("ogP")
        measured = float(og_p) if og_p is not None else None
        return SetpointResult(
            True,
            applied,
            True,
            exact=False,
            battery_power_w=int(round(measured)) if measured is not None else None,
            applied={
                "power_setpoint": applied,
                **(
                    {"battery_power": measured, DELIVERED_AC_POWER_KEY: measured}
                    if measured is not None
                    else {}
                ),
            },
        )

    async def write_control(self, key: str, value: int) -> bool:
        return False

    def net_power_from_data(self, data: dict) -> Optional[int]:
        value = data.get("power_setpoint")
        return int(value) if value is not None else None

    @property
    def control_dependency_keys(self) -> frozenset:
        return frozenset({"power_setpoint", "battery_total_energy"})

    async def apply_config(self, **kwargs) -> bool:
        return True

    async def set_charge_cutoff(self, soc_pct: float) -> bool:
        return False

    async def standby(self) -> bool:
        return (await self.apply_setpoint(0, read_back=False)).ok

    async def set_rs485_control(self, enable: bool) -> bool:
        return False

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(force_close=True, limit=1)
            self._session = aiohttp.ClientSession(connector=connector)
            self._owns_session = True
        return self._session

    async def _reset_owned_session(self) -> None:
        if not self._owns_session:
            return
        session = self._session
        self._session = None
        if session is not None and not session.closed:
            await session.close()

    async def _get_json(
        self,
        path: str,
        params: Optional[dict] = None,
        timeout: aiohttp.ClientTimeout = _HTTP_TIMEOUT,
    ) -> Optional[dict]:
        for attempt in range(_HTTP_ATTEMPTS):
            try:
                async with self._ensure_session().get(
                    self._base_url + path, params=params, timeout=timeout
                ) as response:
                    if response.status != 200:
                        if not self._shutting_down:
                            _LOGGER.warning("APsystems EZHI GET %s returned HTTP %s", path, response.status)
                        return None
                    return await response.json(content_type=None)
            except (asyncio.TimeoutError, aiohttp.ClientError, ValueError) as exc:
                if attempt + 1 < _HTTP_ATTEMPTS:
                    await self._reset_owned_session()
                    await asyncio.sleep(_HTTP_RETRY_DELAY_S)
                    continue
                if not self._shutting_down:
                    _LOGGER.warning("APsystems EZHI GET %s failed: %s", path, exc)
                return None

    @classmethod
    async def probe(cls, host: str, port: int = 80) -> bool:
        driver = cls(host, port=port)
        try:
            return await driver.connect()
        finally:
            await driver.close()
