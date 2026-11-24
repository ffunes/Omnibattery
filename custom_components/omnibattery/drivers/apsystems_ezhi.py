"""Local HTTP driver for the APsystems EZHI hybrid microinverter + battery.

The EZHI exposes an unauthenticated local JSON API once "Local Mode" is enabled
in the AP EasyPower app: GET /getDeviceInfo, GET /getOutputData and
GET /setPower?p=<watts> (-1200..1200). Modelled on sessy.py.

Sign handling is isolated in apsystems_ezhi_api.py: this driver only ever sees
the Omnibattery convention (+W charge, -W discharge).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from .base import (
    DELIVERED_AC_POWER_KEY,
    BatteryDriver,
    DriverCapabilities,
    DriverContext,
    DriverTraits,
    ReadGroup,
    SetpointResult,
    TelemetrySnapshot,
)
from .apsystems_ezhi_api import MAX_POWER_W, EzhiApiClient, EzhiError
from .apsystems_ezhi_flow import EZHI_SETUP

_LOGGER = logging.getLogger(__name__)

_RETRY_DELAY_S = 0.05
_ATTEMPTS = 2

SENSOR_DEFINITIONS = [
    {"key": "battery_soc", "name": "Battery SOC", "unit": "%", "device_class": "battery", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "medium", "enabled_by_default": True},
    {"key": "battery_power", "name": "Battery Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "power_setpoint", "name": "Power Setpoint", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "medium", "enabled_by_default": False},
    {"key": "solar_power", "name": "Solar Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": DELIVERED_AC_POWER_KEY, "name": "AC Delivered Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "internal_temperature", "name": "Internal Temperature", "unit": "°C", "device_class": "temperature", "state_class": "measurement", "scale": 1, "precision": 1, "scan_interval": "medium", "enabled_by_default": True},
    {"key": "battery_soh", "name": "State of Health", "unit": "%", "state_class": "measurement", "scale": 1, "precision": 0, "icon": "mdi:battery-heart-variant", "category": "diagnostic", "scan_interval": "low", "enabled_by_default": True},
    {"key": "device_temperature", "name": "Inverter Temperature", "unit": "°C", "device_class": "temperature", "state_class": "measurement", "scale": 1, "precision": 1, "category": "diagnostic", "scan_interval": "medium", "enabled_by_default": False},
    {"key": "total_charging_energy", "name": "Total Charging Energy", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing", "scale": 1, "precision": 3, "scan_interval": "low", "enabled_by_default": True},
    {"key": "total_discharging_energy", "name": "Total Discharging Energy", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing", "scale": 1, "precision": 3, "scan_interval": "low", "enabled_by_default": True},
    {"key": "software_version", "name": "Software Version", "data_type": "char", "icon": "mdi:ticket-confirmation-outline", "category": "diagnostic", "scan_interval": "low", "enabled_by_default": True},
]

_POWER_KEYS = frozenset({"battery_soc", "battery_power", "power_setpoint", "solar_power", DELIVERED_AC_POWER_KEY})
_DIAG_KEYS = frozenset({"internal_temperature", "device_temperature"})
_ENERGY_KEYS = frozenset({"total_charging_energy", "total_discharging_energy"})
_HEALTH_KEYS = frozenset({"battery_soh"})
# Served by one /getOutputData request
_OUTPUT_KEYS = frozenset({"battery_soc", "battery_power", "solar_power", DELIVERED_AC_POWER_KEY}) | _DIAG_KEYS | _ENERGY_KEYS | _HEALTH_KEYS
_ALL_KEYS = _POWER_KEYS | _DIAG_KEYS | _ENERGY_KEYS | _HEALTH_KEYS | {"software_version"}


class ApsystemsEzhiDriver(BatteryDriver):
    """Poll and control one EZHI through its documented local API."""

    BRAND = "apsystems_ezhi"
    SETUP = EZHI_SETUP
    TRAITS = DriverTraits(
        supports_voltage_taper=False,
        fixed_power_envelope=True,
        ip_addressed=True,
        # /getDeviceInfo reports a capacity and the config flow stores it; the
        # flow only asks the user when the device reports none (like Sessy).
        capacity_field="required",
    )
    LABEL = "APsystems EZHI"
    MANUFACTURER = "APsystems"
    DEFAULT_MODEL = "EZHI"

    @classmethod
    def from_config(cls, battery_config, context: DriverContext) -> "ApsystemsEzhiDriver":
        return cls(
            battery_config["host"],
            port=battery_config.get("port", 80),
            max_charge_power_w=context.max_charge_power_w,
            max_discharge_power_w=context.max_discharge_power_w,
        )

    def __init__(
        self,
        host: str,
        *,
        port: int = 80,
        max_charge_power_w: int = MAX_POWER_W,
        max_discharge_power_w: int = MAX_POWER_W,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._host = host
        self._base_url = f"http://{host}" + (f":{port}" if port != 80 else "")
        self._session = session
        self._owns_session = False
        self._connected = False
        self._shutting_down = False
        self._info: dict = {}
        # The EZHI does not echo its setpoint in /getOutputData, so the last
        # commanded value (Omnibattery sign) is remembered for the coordinator.
        self._last_setpoint: Optional[int] = None
        self._client = EzhiApiClient(self._ensure_session, host, port)
        max_charge_power_w = max(0, min(int(max_charge_power_w), MAX_POWER_W))
        max_discharge_power_w = max(0, min(int(max_discharge_power_w), MAX_POWER_W))
        self._capabilities = DriverCapabilities(
            hardware_soc_cutoff=False,
            has_force_mode=False,
            push_telemetry=False,
            max_charge_power_w=max_charge_power_w,
            max_discharge_power_w=max_discharge_power_w,
            has_mppt_pv=False,
            has_alarm_registers=False,
            has_rs485_control=False,
            # batCTE / batDTE are lifetime kWh totals on the battery (DC) side:
            # no daily counters, and their ratio is the cell efficiency, not the
            # AC round trip. Like Sessy, cycles come from discharged energy.
            has_energy_counters=True,
            has_daily_energy_counters=False,
            cycles_from_discharge_only=True,
            lifetime_counters_ac=False,
            # PV feeds the AC port directly, so AC power minus cell power is not
            # a conversion loss: the plane-based efficiency would read ~100 %.
            # Fall back to the cumulative charge/discharge counters instead.
            independent_power_planes=False,
            has_nominal_capacity=False,  # capacity lives in the stored config
            has_solar_telemetry=True,  # pvP: independent aggregate PV input
            actuator_latency_s=2.0,
        )
        self._read_groups = [
            # Temperatures come from the same /getOutputData request as the power
            # keys, so they are polled with them at no extra cost.
            ReadGroup("high", tuple(sorted(_POWER_KEYS | _DIAG_KEYS))),
            ReadGroup("low", tuple(sorted(_ENERGY_KEYS | _HEALTH_KEYS | {"software_version"}))),
        ]

    # ------------------------------------------------------------- identity
    @property
    def capabilities(self) -> DriverCapabilities: return self._capabilities
    @property
    def model_label(self) -> Optional[str]: return "EZHI"
    @property
    def configuration_url(self) -> Optional[str]: return self._base_url + "/"
    def software_version(self, data: dict) -> Optional[str]: return data.get("software_version")
    @property
    def connected(self) -> bool: return self._connected
    @property
    def read_groups(self) -> list[ReadGroup]: return self._read_groups
    @property
    def sensor_definitions(self) -> list[dict]: return SENSOR_DEFINITIONS
    @property
    def number_definitions(self) -> list[dict]: return []
    @property
    def select_definitions(self) -> list[dict]: return []
    @property
    def switch_definitions(self) -> list[dict]: return []
    @property
    def binary_sensor_definitions(self) -> list[dict]: return []
    @property
    def button_definitions(self) -> list[dict]: return []
    @property
    def all_definitions(self) -> list[dict]: return SENSOR_DEFINITIONS

    # ----------------------------------------------------------- connection
    async def connect(self) -> bool:
        self._ensure_session()
        info = await self._request(self._client.get_device_info)
        self._connected = bool(info and info.get("device_id"))
        if self._connected:
            self._info = info
        return self._connected

    async def close(self) -> None:
        self._connected = False
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def set_shutting_down(self, value: bool) -> None: self._shutting_down = value

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # Small embedded HTTP servers drop keep-alive sockets silently; a
            # fresh connection per request avoids stale-socket failures.
            connector = aiohttp.TCPConnector(force_close=True, limit=1)
            self._session = aiohttp.ClientSession(connector=connector)
            self._owns_session = True
        return self._session

    async def _reset_owned_session(self) -> None:
        if not self._owns_session:
            return
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    async def _request(self, func, *args):
        """Run a client call with one retry on a fresh session; None on failure."""
        for attempt in range(_ATTEMPTS):
            try:
                return await func(*args)
            except EzhiError as err:
                if attempt + 1 < _ATTEMPTS:
                    await self._reset_owned_session()
                    await asyncio.sleep(_RETRY_DELAY_S)
                    continue
                if not self._shutting_down:
                    _LOGGER.warning("APsystems EZHI %s: %s", self._host, err)
                return None

    # ------------------------------------------------------------ telemetry
    async def read_telemetry(self, keys: Optional[list[str]] = None) -> TelemetrySnapshot:
        """Return {} unless the device answered in THIS call.

        Remembered values (last setpoint, cached firmware) never stand in for a
        read: an unreachable device must look unreachable so the coordinator's
        failure counter rises. A group that holds only such values therefore
        still issues a real request.
        """
        requested = set(keys) if keys is not None else set(_ALL_KEYS)
        snapshot: dict = {}
        answered = False

        if requested & _OUTPUT_KEYS:
            t = await self._request(self._client.get_output_data)
            if t is None:
                return {}
            answered = True
            snapshot.update({
                "battery_soc": t["soc"],
                "battery_soh": t["soh"],                     # batSoh, % state of health
                "battery_power": t["battery_power_w"],       # cell, +charge / -discharge
                DELIVERED_AC_POWER_KEY: t["ac_power_w"],     # on-grid port, same sign
                "solar_power": t["solar_power_w"],
                # The panel's Temperature row and thermal protection both read
                # internal_temperature, which for the EZHI is the battery (batTemp),
                # like Anker's alias. The inverter (devTemp) is a separate diagnostic.
                "internal_temperature": t["battery_temp"],
                "device_temperature": t["device_temp"],
                "total_charging_energy": t["charged_kwh"],      # lifetime kWh
                "total_discharging_energy": t["discharged_kwh"],  # lifetime kWh
            })

        if "power_setpoint" in requested:
            power = await self._request(self._client.get_power)
            if power is None:
                if not answered:
                    return {}
            else:
                answered = True
                value = power["setpoint_w"]
                snapshot["power_setpoint"] = value if value is not None else self._last_setpoint

        if "software_version" in requested:
            info = await self._request(self._client.get_device_info)
            if info is None:
                if not answered:
                    return {}
            else:
                answered = True
                if info.get("device_id"):
                    self._info = info
                snapshot["software_version"] = self._info.get("firmware")

        snapshot = {k: v for k, v in snapshot.items() if v is not None}
        return snapshot if keys is None else {k: v for k, v in snapshot.items() if k in keys}

    # -------------------------------------------------------------- control
    async def apply_setpoint(self, net_power_w: int, *, mode_hint: Optional[str] = None,
                             read_back: bool = True) -> SetpointResult:
        applied = max(-self._capabilities.max_discharge_power_w,
                      min(self._capabilities.max_charge_power_w, int(net_power_w)))
        # Sign inversion for the device happens inside the API client.
        sent = await self._request(self._client.set_power, applied)
        if sent is None:
            return SetpointResult(False, applied, False, failure_reason="setpoint_write_failed")
        self._last_setpoint = applied
        if not read_back:
            return SetpointResult(True, applied, False, applied={"power_setpoint": applied})

        # /setPower answers SUCCESS in every system mode but only acts in Local
        # mode, so its answer proves nothing. Confirm against /getPower, which
        # reports the device's own setpoint; if its answer has no recognisable
        # field, the command stays unconfirmed.
        power = await self._request(self._client.get_power)
        device_value = power["setpoint_w"] if power else None
        confirmed = device_value is not None and device_value == applied
        echo: dict = {"power_setpoint": device_value if device_value is not None else applied}

        # Delivered power for non-delivery detection is the AC port that
        # /setPower acts on, not the cell: with PV present the two differ.
        out = await self._request(self._client.get_output_data)
        delivered = None
        if out:
            if out.get("ac_power_w") is not None:
                delivered = int(round(out["ac_power_w"]))
                echo[DELIVERED_AC_POWER_KEY] = delivered
            if out.get("battery_power_w") is not None:
                echo["battery_power"] = int(round(out["battery_power_w"]))
        return SetpointResult(True, applied, confirmed, exact=confirmed,
                              battery_power_w=delivered, applied=echo)

    async def write_control(self, key: str, value: int) -> bool: return False
    def net_power_from_data(self, data: dict) -> Optional[int]:
        value = data.get("power_setpoint")
        return int(value) if value is not None else None
    @property
    def control_dependency_keys(self) -> frozenset:
        return frozenset({"power_setpoint", DELIVERED_AC_POWER_KEY, "solar_power"})
    async def apply_config(self, **kwargs) -> bool: return True
    async def set_charge_cutoff(self, soc_pct: float) -> bool: return False
    async def standby(self) -> bool: return (await self.apply_setpoint(0, read_back=False)).ok
    async def set_rs485_control(self, enable: bool) -> bool: return False

    # ----------------------------------------------------- flow-facing probes
    @classmethod
    async def fetch_info(cls, host: str, port: int = 80) -> Optional[dict]:
        """Device identity/capacity for the config flow, or None if unreachable."""
        driver = cls(host, port=port)
        try:
            return dict(driver._info) if await driver.connect() else None
        finally:
            await driver.close()

    @classmethod
    async def probe(cls, host: str, port: int = 80) -> bool:
        """Same call shape as SessyLocalDriver.probe (minus credentials)."""
        return await cls.fetch_info(host, port) is not None
