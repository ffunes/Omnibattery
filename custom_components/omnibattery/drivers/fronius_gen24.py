"""Fronius GEN24 / BYD Battery-Box Modbus TCP driver.

Implements :class:`BatteryDriver` for a BYD battery controlled through a
Fronius GEN24 inverter's SunSpec storage control registers.

The driver detects the Fronius SunSpec model type from the Model 124 header and
uses the corresponding address layout. Fronius' ``float``/``int+SF`` setting
changes the preceding inverter model and therefore shifts Models 160 and 124;
those two models themselves keep their integer-plus-scale-factor encoding.

* Float: Model 160 data at 40265, Model 124 data at 40355
* int+SF: Model 160 data at 40255, Model 124 data at 40345
* Model 124 data structure: ``>10H2h4H8h``

Sign conventions:
  Omnibattery net power: +charge / -discharge
  Fronius Power Flow sensor: +discharge / -charge
  Fronius DC block used here: 3_DCW = charge, 4_DCW = discharge
  Therefore battery_power = 3_DCW - 4_DCW.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from time import monotonic
from typing import Any, Optional

try:
    import aiohttp
except ImportError:  # pragma: no cover - Home Assistant provides aiohttp.
    aiohttp = None

from ..infra.modbus_client import MarstekModbusClient, decode_registers
from .base import (
    BatteryDriver,
    DriverCapabilities,
    ReadGroup,
    SetpointResult,
    TelemetrySnapshot,
)

_LOGGER = logging.getLogger(__name__)

FRONIUS_GEN24_DEFAULT_MAX_POWER_W = 5000
FRONIUS_GEN24_DEFAULT_CAPACITY_KWH = 11.0

_DCW_BLOCK_COUNT = 88
_DCW_SF_OFFSET = 2
# Raw Modbus word offsets. The HA custom sensor exposes these as split indices
# 15/19 after skipping reserved bytes in ``>hhhh8xH16xHHH16x...``.
_DCW_TO_BATTERY_OFFSET = 59
_DCW_FROM_BATTERY_OFFSET = 79

_STORAGE_BLOCK_COUNT = 24
_IDX_WCHAMAX = 0
_IDX_STORCTL_MOD = 3
_IDX_MIN_RSV_PCT = 5
_IDX_CHA_STATE = 6
_IDX_CHA_STATUS = 9
_IDX_OUTWRTE = 10
_IDX_INWRTE = 11
_IDX_CHAGRISET = 15
_IDX_WCHAMAX_SF = 16
_IDX_MIN_RSV_PCT_SF = 19
_IDX_CHA_STATE_SF = 20
_IDX_INOUTWRTE_SF = 23

_MODEL_124_ID = 124

_MODE_AUTO = 0
_MODE_STORAGE_CONTROL = 3
_IDLE_WINDOW_WORD = 0
_MIN_OPERATING_POWER_W = 150
_DIRECTION_CHANGE_HOLD_SECONDS = 2.0
_POWER_CONFIRM_TIMEOUT_SECONDS = 4.0
_POWER_CONFIRM_POLL_SECONDS = 0.5
_IDLE_POWER_TOLERANCE_W = 150
_STORAGE_API_PATH = "/solar_api/v1/GetStorageRealtimeData.cgi"
_STORAGE_API_TIMEOUT_SECONDS = 5
_STORAGE_API_METADATA_KEYS = {
    "fronius_storage_serial",
    "fronius_storage_model",
    "fronius_storage_manufacturer",
}

SENSOR_DEFINITIONS: list[dict] = [
    {"key": "battery_soc", "name": "Battery SOC", "unit": "%",
     "device_class": "battery", "state_class": "measurement", "scale": 1,
     "precision": 1, "scan_interval": "medium", "enabled_by_default": True},
    {"key": "battery_power", "name": "Battery Power", "unit": "W",
     "device_class": "power", "state_class": "measurement", "scale": 1,
     "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "ac_power", "name": "AC Power", "unit": "W",
     "device_class": "power", "state_class": "measurement", "scale": 1,
     "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "battery_charge_power", "name": "Battery Charge Power", "unit": "W",
     "device_class": "power", "state_class": "measurement", "scale": 1,
     "precision": 0, "scan_interval": "high", "enabled_by_default": False},
    {"key": "battery_discharge_power", "name": "Battery Discharge Power", "unit": "W",
     "device_class": "power", "state_class": "measurement", "scale": 1,
     "precision": 0, "scan_interval": "high", "enabled_by_default": False},
    {"key": "internal_temperature", "name": "Internal Temperature", "unit": "°C",
     "device_class": "temperature", "state_class": "measurement", "scale": 1,
     "precision": 1, "scan_interval": "medium", "enabled_by_default": True},
    {"key": "battery_voltage", "name": "Battery Voltage", "unit": "V",
     "device_class": "voltage", "state_class": "measurement", "scale": 1,
     "precision": 1, "scan_interval": "medium", "enabled_by_default": True},
    {"key": "battery_current", "name": "Battery Current", "unit": "A",
     "device_class": "current", "state_class": "measurement", "scale": 1,
     "precision": 2, "scan_interval": "medium", "enabled_by_default": True},
    {"key": "max_charge_power", "name": "Max Charge Power", "unit": "W",
     "device_class": None, "state_class": None, "scale": 1, "precision": 0,
     "icon": "mdi:battery-charging-high", "scan_interval": "medium",
     "enabled_by_default": True},
    {"key": "max_discharge_power", "name": "Max Discharge Power", "unit": "W",
     "device_class": None, "state_class": None, "scale": 1, "precision": 0,
     "icon": "mdi:battery-arrow-down-outline", "scan_interval": "medium",
     "enabled_by_default": True},
    {"key": "storctl_mod", "name": "Storage Control Mode", "unit": None,
     "device_class": None, "state_class": None, "scale": 1, "precision": 0,
     "icon": "mdi:tune", "scan_interval": "medium", "enabled_by_default": True,
     "states": {0: "Auto", 3: "External Control"}},
    {"key": "outwrte", "name": "OutWRte", "unit": None,
     "device_class": None, "state_class": None, "scale": 1, "precision": 0,
     "icon": "mdi:export", "scan_interval": "medium", "enabled_by_default": False},
    {"key": "inwrte", "name": "InWRte", "unit": None,
     "device_class": None, "state_class": None, "scale": 1, "precision": 0,
     "icon": "mdi:import", "scan_interval": "medium", "enabled_by_default": False},
    {"key": "min_rsv_pct", "name": "Minimum Reserve", "unit": "%",
     "device_class": "battery", "state_class": "measurement", "scale": 1,
     "precision": 1, "scan_interval": "medium", "enabled_by_default": False},
    {"key": "fronius_charge_state", "name": "Fronius Charge State", "unit": "%",
     "device_class": "battery", "state_class": "measurement", "scale": 1,
     "precision": 1, "scan_interval": "medium", "enabled_by_default": False},
    {"key": "fronius_charge_status", "name": "Fronius Charge Status", "unit": None,
     "device_class": None, "state_class": None, "scale": 1, "precision": 0,
     "icon": "mdi:battery-sync", "scan_interval": "medium",
     "enabled_by_default": False},
    {"key": "chagriset", "name": "ChaGriSet", "unit": None,
     "device_class": None, "state_class": None, "scale": 1, "precision": 0,
     "icon": "mdi:transmission-tower-import", "scan_interval": "medium",
     "enabled_by_default": False},
    {"key": "fronius_sunspec_model_type", "name": "SunSpec Model Type",
     "unit": None, "device_class": None, "state_class": None, "scale": 1,
     "precision": 0, "icon": "mdi:code-braces-box", "scan_interval": "medium",
     "category": "diagnostic", "enabled_by_default": False},
    {"key": "inverter_state", "name": "Inverter State", "unit": None,
     "device_class": None, "state_class": None, "scale": 1, "precision": 0,
     "icon": "mdi:state-machine", "scan_interval": "high",
     "enabled_by_default": True,
     "states": {1: "Standby", 2: "Charge", 3: "Discharge"}},
]

NUMBER_DEFINITIONS: list[dict] = []
SELECT_DEFINITIONS: list[dict] = []
SWITCH_DEFINITIONS: list[dict] = []
BINARY_SENSOR_DEFINITIONS: list[dict] = []
BUTTON_DEFINITIONS: list[dict] = []


@dataclass(frozen=True)
class SunSpecLayout:
    """Addresses for one Fronius SunSpec inverter-model representation."""

    model_type: str
    dc_model_header: int
    storage_model_header: int

    @property
    def dc_block(self) -> int:
        return self.dc_model_header + 2

    @property
    def storage_block(self) -> int:
        return self.storage_model_header + 2

    @property
    def storctl_mod(self) -> int:
        return self.storage_block + _IDX_STORCTL_MOD

    @property
    def min_rsv_pct(self) -> int:
        return self.storage_block + _IDX_MIN_RSV_PCT

    @property
    def outwrte(self) -> int:
        return self.storage_block + _IDX_OUTWRTE

    @property
    def inwrte(self) -> int:
        return self.storage_block + _IDX_INWRTE


SUNSPEC_FLOAT_LAYOUT = SunSpecLayout("float", 40263, 40353)
SUNSPEC_INT_SF_LAYOUT = SunSpecLayout("int+SF", 40253, 40343)


@dataclass(frozen=True)
class RegisterWrite:
    """One Modbus single-register write."""

    address: int
    value: int


@dataclass(frozen=True)
class SetpointPlan:
    """Decoded command plan for a signed Omnibattery net setpoint."""

    net_power_w: int
    mode: str
    outwrte_word: int
    inwrte_word: int
    writes: tuple[RegisterWrite, ...]


def _int16_word(value: int) -> int:
    """Encode a signed int16 value as an unsigned Modbus register word."""
    return int(value) & 0xFFFF


def _decode_int16(word: int) -> int:
    return int(decode_registers([word], "int16") or 0)


def _scaled(value: float | int | None, scale_factor: float | int | None) -> float | None:
    if value is None or scale_factor is None:
        return None
    try:
        return float(value) * (10 ** int(scale_factor))
    except (TypeError, ValueError, OverflowError):
        return None


def _rate_denominator(inout_sf: int | None) -> float:
    """Return raw register units for 100% power.

    The local Fronius/BYD setup writes 10000 for 100% because InOutWRte_SF is -2.
    Keep the formula scale-factor aware so a different SunSpec encoding still
    derives the command echo correctly.
    """
    sf = -2 if inout_sf is None else int(inout_sf)
    return 100.0 / (10 ** sf)


def _clamp_power(value: int, ceiling: int) -> int:
    ceiling = max(0, int(ceiling or 0))
    return max(0, min(ceiling, int(value)))


def plan_storage_setpoint(
    net_power_w: int,
    *,
    wcha_max_w: int,
    max_charge_power_w: int,
    max_discharge_power_w: int,
    layout: SunSpecLayout = SUNSPEC_FLOAT_LAYOUT,
) -> SetpointPlan:
    """Build the exact GEN24/BYD register-write plan for one setpoint.

    ``net_power_w`` follows the Omnibattery convention (+charge / -discharge).
    The formulas mirror the working local scripts:

    * charge: ``OutWRte = 65535 - pct`` and ``InWRte = 1 + pct``
    * discharge: ``OutWRte = int((power + 1) / WChaMax * 10000)`` and
      ``InWRte = 65535 - pct``
    * idle: both limits are set to 0% while external control remains active.
      Fronius documents this as the power window ``[0 W, 0 W]``. A 100%/100%
      window would permit the complete automatic charge/discharge range.
    """
    wcha = max(1, int(wcha_max_w or 1))
    charge_ceiling = min(wcha, max(0, int(max_charge_power_w or 0)))
    discharge_ceiling = min(wcha, max(0, int(max_discharge_power_w or 0)))

    if (net_power_w > 0 and charge_ceiling == 0) or (
        net_power_w < 0 and discharge_ceiling == 0
    ):
        net_power_w = 0

    if net_power_w > 0:
        power = _clamp_power(net_power_w, charge_ceiling)
        pct = int(power / wcha * 10000)
        out_word = _int16_word(-(pct + 1))
        in_word = pct + 1
        writes = (
            RegisterWrite(layout.outwrte, out_word),
            RegisterWrite(layout.inwrte, in_word),
            RegisterWrite(layout.storctl_mod, _MODE_STORAGE_CONTROL),
            RegisterWrite(layout.outwrte, out_word),
            RegisterWrite(layout.inwrte, in_word),
        )
        return SetpointPlan(power, "charge", out_word, in_word, writes)

    if net_power_w < 0:
        power = _clamp_power(-net_power_w, discharge_ceiling)
        pct = int(power / wcha * 10000)
        out_word = int((power + 1) / wcha * 10000)
        in_word = _int16_word(-(pct + 1))
        writes = (
            RegisterWrite(layout.outwrte, out_word),
            RegisterWrite(layout.inwrte, in_word),
            RegisterWrite(layout.storctl_mod, _MODE_STORAGE_CONTROL),
            RegisterWrite(layout.outwrte, out_word),
            RegisterWrite(layout.inwrte, in_word),
        )
        return SetpointPlan(-power, "discharge", out_word, in_word, writes)

    writes = (
        RegisterWrite(layout.outwrte, _IDLE_WINDOW_WORD),
        RegisterWrite(layout.inwrte, _IDLE_WINDOW_WORD),
        RegisterWrite(layout.storctl_mod, _MODE_STORAGE_CONTROL),
        RegisterWrite(layout.outwrte, _IDLE_WINDOW_WORD),
        RegisterWrite(layout.inwrte, _IDLE_WINDOW_WORD),
    )
    return SetpointPlan(0, "idle", _IDLE_WINDOW_WORD, _IDLE_WINDOW_WORD, writes)


def plan_reset_to_auto(
    layout: SunSpecLayout = SUNSPEC_FLOAT_LAYOUT,
) -> tuple[RegisterWrite, ...]:
    """Release external control without changing the user's Fronius settings."""
    return (RegisterWrite(layout.storctl_mod, _MODE_AUTO),)


def decode_storage_registers(regs: list[int]) -> TelemetrySnapshot:
    """Decode the local 40355 / ``>10H2h4H8h`` storage-control block."""
    if len(regs) < _STORAGE_BLOCK_COUNT:
        return {}

    wcha_sf = _decode_int16(regs[_IDX_WCHAMAX_SF])
    min_rsv_sf = _decode_int16(regs[_IDX_MIN_RSV_PCT_SF])
    cha_state_sf = _decode_int16(regs[_IDX_CHA_STATE_SF])
    inout_sf = _decode_int16(regs[_IDX_INOUTWRTE_SF])

    wcha = _scaled(regs[_IDX_WCHAMAX], wcha_sf)
    soc = _scaled(regs[_IDX_CHA_STATE], cha_state_sf)
    min_rsv = _scaled(regs[_IDX_MIN_RSV_PCT], min_rsv_sf)

    snapshot: TelemetrySnapshot = {
        "storctl_mod": int(regs[_IDX_STORCTL_MOD]),
        "outwrte": _decode_int16(regs[_IDX_OUTWRTE]),
        "inwrte": _decode_int16(regs[_IDX_INWRTE]),
        "inoutwrte_sf": inout_sf,
        "fronius_charge_status": int(regs[_IDX_CHA_STATUS]),
        "chagriset": int(regs[_IDX_CHAGRISET]),
    }
    if wcha is not None and wcha > 0:
        wcha_w = int(round(wcha))
        snapshot["wcha_max"] = wcha_w
        snapshot["max_charge_power"] = wcha_w
        snapshot["max_discharge_power"] = wcha_w
    if soc is not None:
        snapshot["battery_soc"] = soc
        snapshot["fronius_charge_state"] = soc
    if min_rsv is not None:
        snapshot["min_rsv_pct"] = min_rsv
    return snapshot


def decode_dc_power_registers(regs: list[int]) -> TelemetrySnapshot:
    """Decode the local GEN24 DC block into Omnibattery battery power."""
    if len(regs) <= max(_DCW_SF_OFFSET, _DCW_TO_BATTERY_OFFSET, _DCW_FROM_BATTERY_OFFSET):
        return {}

    dcw_sf = _decode_int16(regs[_DCW_SF_OFFSET])
    charge_w = _scaled(regs[_DCW_TO_BATTERY_OFFSET], dcw_sf)
    discharge_w = _scaled(regs[_DCW_FROM_BATTERY_OFFSET], dcw_sf)
    if charge_w is None or discharge_w is None:
        return {}

    battery_power = int(round(charge_w - discharge_w))
    snapshot: TelemetrySnapshot = {
        "battery_charge_power": int(round(charge_w)),
        "battery_discharge_power": int(round(discharge_w)),
        "battery_power": battery_power,
        "ac_power": -battery_power,
    }
    if battery_power > 50:
        snapshot["inverter_state"] = 2
    elif battery_power < -50:
        snapshot["inverter_state"] = 3
    else:
        snapshot["inverter_state"] = 1
    return snapshot


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def decode_storage_api_payload(payload: dict[str, Any]) -> TelemetrySnapshot:
    """Decode Fronius ``GetStorageRealtimeData.cgi`` battery telemetry."""
    try:
        data = payload["Body"]["Data"]
    except (KeyError, TypeError):
        return {}

    if not isinstance(data, dict) or not data:
        return {}

    first_storage = data.get("0")
    if not isinstance(first_storage, dict):
        first_storage = next((value for value in data.values() if isinstance(value, dict)), None)
    if not isinstance(first_storage, dict):
        return {}

    controller = first_storage.get("Controller")
    if not isinstance(controller, dict):
        return {}

    snapshot: TelemetrySnapshot = {}
    mapping = {
        "Temperature_Cell": "internal_temperature",
        "Voltage_DC": "battery_voltage",
        "Current_DC": "battery_current",
        "StateOfCharge_Relative": "battery_soc",
    }
    for source, target in mapping.items():
        value = _as_float(controller.get(source))
        if value is not None:
            snapshot[target] = value

    capacity = _as_float(controller.get("Capacity_Maximum"))
    if capacity is None or capacity <= 0:
        capacity = _as_float(controller.get("DesignedCapacity"))
    if capacity is not None and capacity > 0:
        snapshot["battery_total_energy"] = round(capacity / 1000.0, 3)

    details = controller.get("Details")
    if isinstance(details, dict):
        serial = details.get("Serial")
        model = details.get("Model")
        manufacturer = details.get("Manufacturer")
        if isinstance(serial, str) and serial.strip():
            snapshot["fronius_storage_serial"] = serial.strip()
        if isinstance(model, str) and model.strip():
            snapshot["fronius_storage_model"] = model.strip()
        if isinstance(manufacturer, str) and manufacturer.strip():
            snapshot["fronius_storage_manufacturer"] = manufacturer.strip()

    return snapshot


class FroniusGen24Driver(BatteryDriver):
    """Modbus TCP driver for a Fronius GEN24-controlled BYD battery."""

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 1,
        *,
        client: Optional[MarstekModbusClient] = None,
        http_session: Optional[Any] = None,
        max_charge_power_w: int = FRONIUS_GEN24_DEFAULT_MAX_POWER_W,
        max_discharge_power_w: int = FRONIUS_GEN24_DEFAULT_MAX_POWER_W,
    ) -> None:
        self._host = host
        self._port = port
        self._slave_id = slave_id
        self._client = client or MarstekModbusClient(
            host,
            port,
            message_wait_ms=50,
            timeout=5,
            is_v3=False,
            slave_id=slave_id,
        )
        self._wcha_max_w = max(1, int(max(max_charge_power_w, max_discharge_power_w)))
        self._max_charge_w = max(0, int(max_charge_power_w))
        self._max_discharge_w = max(0, int(max_discharge_power_w))
        self._max_soc_pct = 100.0
        self._min_soc_pct = 0.0
        self._last_soc_pct: Optional[float] = None
        self._last_net_power_w: Optional[int] = None
        self._last_inout_sf = -2
        self._serial: Optional[str] = None
        # Fail-safe default: retain external storage control across teardown.
        # Releasing control to Fronius must be an explicit persisted choice.
        self._internal_control_disabled = True
        self._last_active_sign = 0
        self._idle_since_monotonic: Optional[float] = None
        # Float is the Fronius default and preserves the established behavior
        # until connect() verifies the Model 124 header.
        self._sunspec_layout = SUNSPEC_FLOAT_LAYOUT
        self._sunspec_layout_detected = False
        self._http_session = http_session
        self._owns_http_session = http_session is None
        self._storage_api_url = f"http://{host}{_STORAGE_API_PATH}"
        self._read_groups = [
            ReadGroup(
                "high",
                (
                    "battery_power",
                    "ac_power",
                    "battery_charge_power",
                    "battery_discharge_power",
                    "inverter_state",
                ),
            ),
            ReadGroup(
                "medium",
                (
                    "battery_soc",
                    "fronius_charge_state",
                    "fronius_charge_status",
                    "max_charge_power",
                    "max_discharge_power",
                    "storctl_mod",
                    "outwrte",
                    "inwrte",
                    "min_rsv_pct",
                    "chagriset",
                    "fronius_sunspec_model_type",
                    "internal_temperature",
                    "battery_voltage",
                    "battery_current",
                    "battery_total_energy",
                ),
            ),
        ]
        self._capabilities = DriverCapabilities(
            hardware_soc_cutoff=False,
            has_force_mode=False,
            push_telemetry=False,
            max_charge_power_w=max(1, self._max_charge_w),
            max_discharge_power_w=max(1, self._max_discharge_w),
            min_charge_power_w=_MIN_OPERATING_POWER_W,
            min_discharge_power_w=_MIN_OPERATING_POWER_W,
            has_mppt_pv=False,
            has_alarm_registers=False,
            has_rs485_control=False,
            has_energy_counters=False,
            has_nominal_capacity=False,
            has_daily_energy_counters=False,
            setpoint_confirm_reliable=False,
            actuator_latency_s=2.0,
            readback_latency_s=2.0,
        )

    @property
    def capabilities(self) -> DriverCapabilities:
        return self._capabilities

    @property
    def model_label(self) -> Optional[str]:
        return "GEN24 / BYD"

    @property
    def serial(self) -> Optional[str]:
        """Return the physical BYD serial used by synthetic-energy backup."""
        return self._serial

    @property
    def sunspec_model_type(self) -> Optional[str]:
        """Return the detected Fronius inverter-model representation."""
        return (
            self._sunspec_layout.model_type
            if self._sunspec_layout_detected
            else None
        )

    @property
    def sensor_definitions(self) -> list[dict]:
        return SENSOR_DEFINITIONS

    @property
    def number_definitions(self) -> list[dict]:
        return NUMBER_DEFINITIONS

    @property
    def select_definitions(self) -> list[dict]:
        return SELECT_DEFINITIONS

    @property
    def switch_definitions(self) -> list[dict]:
        return SWITCH_DEFINITIONS

    @property
    def binary_sensor_definitions(self) -> list[dict]:
        return BINARY_SENSOR_DEFINITIONS

    @property
    def button_definitions(self) -> list[dict]:
        return BUTTON_DEFINITIONS

    @property
    def all_definitions(self) -> list[dict]:
        return (
            SENSOR_DEFINITIONS
            + NUMBER_DEFINITIONS
            + SELECT_DEFINITIONS
            + SWITCH_DEFINITIONS
            + BINARY_SENSOR_DEFINITIONS
            + BUTTON_DEFINITIONS
        )

    @property
    def connected(self) -> bool:
        return self._client.connected

    async def connect(self) -> bool:
        ok = await self._client.async_connect()
        if ok:
            if not await self._detect_sunspec_layout():
                _LOGGER.warning(
                    "Fronius GEN24 at %s:%s slave %s exposes no supported "
                    "SunSpec Model 124 header",
                    self._host,
                    self._port,
                    self._slave_id,
                )
                await self._client.async_close()
                return False
            await self._refresh_storage_cache()
        return ok

    async def close(self) -> None:
        if self._owns_http_session and self._http_session is not None:
            await self._http_session.close()
            self._http_session = None
        await self._client.async_close()

    def set_shutting_down(self, value: bool) -> None:
        self._client.set_shutting_down(value)

    @property
    def read_groups(self) -> list[ReadGroup]:
        return self._read_groups

    async def _detect_sunspec_layout(self) -> bool:
        """Detect float versus int+SF from the Basic Storage Model header."""
        self._client.unit_id = self._slave_id
        for layout in (SUNSPEC_FLOAT_LAYOUT, SUNSPEC_INT_SF_LAYOUT):
            regs = await self._client.async_read_block(
                layout.storage_model_header,
                2,
                block_key=f"fronius_model_124_{layout.model_type}",
            )
            if (
                regs
                and len(regs) >= 2
                and int(regs[0]) == _MODEL_124_ID
                and int(regs[1]) == _STORAGE_BLOCK_COUNT
            ):
                self._sunspec_layout = layout
                self._sunspec_layout_detected = True
                _LOGGER.info(
                    "Detected Fronius GEN24 SunSpec model type %s at %s:%s "
                    "slave %s",
                    layout.model_type,
                    self._host,
                    self._port,
                    self._slave_id,
                )
                return True
        self._sunspec_layout_detected = False
        return False

    async def _read_storage_block(self) -> TelemetrySnapshot:
        self._client.unit_id = self._slave_id
        regs = await self._client.async_read_block(
            self._sunspec_layout.storage_block,
            _STORAGE_BLOCK_COUNT,
            block_key=f"fronius_storage_{self._sunspec_layout.model_type}",
        )
        if not regs:
            return {}
        snapshot = decode_storage_registers(regs)
        if self._sunspec_layout_detected:
            snapshot["fronius_sunspec_model_type"] = (
                self._sunspec_layout.model_type
            )
        return snapshot

    async def _read_dc_power_block(self) -> TelemetrySnapshot:
        self._client.unit_id = self._slave_id
        regs = await self._client.async_read_block(
            self._sunspec_layout.dc_block,
            _DCW_BLOCK_COUNT,
            block_key=f"fronius_dc_power_{self._sunspec_layout.model_type}",
        )
        if not regs:
            return {}
        return decode_dc_power_registers(regs)

    def _ensure_http_session(self) -> Any:
        if self._http_session is None or getattr(self._http_session, "closed", False):
            if aiohttp is None:
                raise RuntimeError("aiohttp is not available")
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_STORAGE_API_TIMEOUT_SECONDS)
            )
            self._owns_http_session = True
        return self._http_session

    async def _read_storage_api(self) -> TelemetrySnapshot:
        try:
            session = self._ensure_http_session()
            async with session.get(self._storage_api_url) as response:
                if response.status != 200:
                    _LOGGER.debug(
                        "Fronius storage API %s returned HTTP %s",
                        self._storage_api_url,
                        response.status,
                    )
                    return {}
                payload = await response.json(content_type=None)
        except (asyncio.TimeoutError, RuntimeError, ValueError) as exc:
            _LOGGER.debug("Fronius storage API read failed: %s", exc)
            return {}
        except Exception as exc:
            if aiohttp is not None and isinstance(exc, aiohttp.ClientError):
                _LOGGER.debug("Fronius storage API read failed: %s", exc)
                return {}
            raise
        return decode_storage_api_payload(payload)

    async def _refresh_storage_cache(self) -> None:
        snapshot = await self._read_storage_block()
        max_power = snapshot.get("wcha_max") or snapshot.get("max_charge_power")
        if isinstance(max_power, (int, float)) and int(max_power) > 0:
            self._wcha_max_w = int(max_power)
        inout_sf = snapshot.get("inoutwrte_sf")
        if isinstance(inout_sf, (int, float)):
            self._last_inout_sf = int(inout_sf)
        soc = snapshot.get("battery_soc")
        if isinstance(soc, (int, float)):
            self._last_soc_pct = float(soc)

    def _remember_soc(self, snapshot: TelemetrySnapshot) -> None:
        soc = snapshot.get("battery_soc")
        if isinstance(soc, (int, float)):
            self._last_soc_pct = float(soc)

    def _max_soc_reached(self) -> bool:
        return (
            self._last_soc_pct is not None
            and self._max_soc_pct < 100.0
            and self._last_soc_pct >= self._max_soc_pct
        )

    async def read_telemetry(self, keys: Optional[list[str]] = None) -> TelemetrySnapshot:
        wanted = set(keys) if keys is not None else {d["key"] for d in SENSOR_DEFINITIONS}
        snapshot: TelemetrySnapshot = {}

        storage_keys = {
            "battery_soc",
            "fronius_charge_state",
            "fronius_charge_status",
            "max_charge_power",
            "max_discharge_power",
            "storctl_mod",
            "outwrte",
            "inwrte",
            "min_rsv_pct",
            "chagriset",
            "wcha_max",
            "inoutwrte_sf",
            "fronius_sunspec_model_type",
        }
        storage_api_keys = {
            "internal_temperature",
            "battery_voltage",
            "battery_current",
            "battery_total_energy",
            "battery_soc",
            "fronius_storage_serial",
            "fronius_storage_model",
            "fronius_storage_manufacturer",
        }
        dc_keys = {
            "battery_power",
            "ac_power",
            "battery_charge_power",
            "battery_discharge_power",
            "inverter_state",
        }

        if wanted & storage_keys:
            storage = await self._read_storage_block()
            snapshot.update(storage)
            self._remember_soc(storage)
            max_power = storage.get("wcha_max") or storage.get("max_charge_power")
            if isinstance(max_power, (int, float)) and int(max_power) > 0:
                self._wcha_max_w = int(max_power)
            inout_sf = storage.get("inoutwrte_sf")
            if isinstance(inout_sf, (int, float)):
                self._last_inout_sf = int(inout_sf)

        if wanted & dc_keys:
            snapshot.update(await self._read_dc_power_block())

        if wanted & storage_api_keys:
            storage_api = await self._read_storage_api()
            snapshot.update(storage_api)
            self._remember_soc(storage_api)
            serial = storage_api.get("fronius_storage_serial")
            if isinstance(serial, str) and serial:
                self._serial = serial
            wanted.update(_STORAGE_API_METADATA_KEYS)

        if keys is None:
            return snapshot
        return {key: value for key, value in snapshot.items() if key in wanted}

    async def apply_setpoint(
        self,
        net_power_w: int,
        *,
        mode_hint: Optional[str] = None,
        read_back: bool = True,
    ) -> SetpointResult:
        _ = mode_hint
        if not self.connected:
            return SetpointResult(
                ok=False,
                net_power_w=0,
                confirmed=False,
                failure_reason="not_connected",
            )

        if self._wcha_max_w <= 1:
            await self._refresh_storage_cache()
        if self._wcha_max_w <= 1:
            return SetpointResult(
                ok=False,
                net_power_w=0,
                confirmed=False,
                failure_reason="missing_wchamax",
            )

        requested_power_w = int(net_power_w)
        if 0 < abs(requested_power_w) < _MIN_OPERATING_POWER_W:
            _LOGGER.debug(
                "Suppressing sub-minimum Fronius GEN24 request %d W below %d W; "
                "writing idle storage control instead",
                requested_power_w,
                _MIN_OPERATING_POWER_W,
            )
            requested_power_w = 0

        requested_sign = (
            1 if requested_power_w > 0 else -1 if requested_power_w < 0 else 0
        )
        now = monotonic()
        if requested_sign == 0:
            if (
                self._last_net_power_w not in (None, 0)
                and self._idle_since_monotonic is None
            ):
                self._idle_since_monotonic = now
        elif self._last_active_sign and requested_sign != self._last_active_sign:
            if self._idle_since_monotonic is None:
                self._idle_since_monotonic = now
            idle_s = now - self._idle_since_monotonic
            if idle_s < _DIRECTION_CHANGE_HOLD_SECONDS:
                _LOGGER.info(
                    "Fronius GEN24 direction change held at idle for %.1fs/%.1fs",
                    idle_s,
                    _DIRECTION_CHANGE_HOLD_SECONDS,
                )
                requested_power_w = 0
                requested_sign = 0
        elif requested_sign == self._last_active_sign:
            self._idle_since_monotonic = None

        plan = plan_storage_setpoint(
            requested_power_w,
            wcha_max_w=self._wcha_max_w,
            max_charge_power_w=self._max_charge_w,
            max_discharge_power_w=self._max_discharge_w,
            layout=self._sunspec_layout,
        )
        ok = await self._write_plan(plan.writes)
        if not ok:
            return SetpointResult(
                ok=False,
                net_power_w=plan.net_power_w,
                confirmed=False,
                failure_reason="write_failed",
            )

        self._last_net_power_w = plan.net_power_w
        applied_sign = (
            1 if plan.net_power_w > 0 else -1 if plan.net_power_w < 0 else 0
        )
        if applied_sign:
            self._last_active_sign = applied_sign
            self._idle_since_monotonic = None
        applied = {
            "commanded_net_power": plan.net_power_w,
            "storctl_mod": _MODE_STORAGE_CONTROL,
            "outwrte": _decode_int16(plan.outwrte_word),
            "inwrte": _decode_int16(plan.inwrte_word),
        }

        if not read_back:
            return SetpointResult(
                ok=True,
                net_power_w=plan.net_power_w,
                confirmed=False,
                applied=applied,
            )

        echo, registers_confirmed, power_confirmed = (
            await self._confirm_applied_plan(plan)
        )
        if not echo:
            return SetpointResult(
                ok=True,
                net_power_w=plan.net_power_w,
                confirmed=False,
                failure_reason="feedback_timeout",
            )

        confirmed = registers_confirmed and power_confirmed
        applied.update({
            "storctl_mod": echo.get("storctl_mod", applied["storctl_mod"]),
            "outwrte": echo.get("outwrte", applied["outwrte"]),
            "inwrte": echo.get("inwrte", applied["inwrte"]),
        })
        battery_power = echo.get("battery_power")
        if battery_power is not None:
            applied["battery_power"] = battery_power
        return SetpointResult(
            ok=True,
            net_power_w=plan.net_power_w,
            confirmed=confirmed,
            exact=confirmed,
            failure_reason=(
                None
                if confirmed
                else "ack_mismatch"
                if not registers_confirmed
                else "power_not_settled"
            ),
            battery_power_w=int(battery_power) if battery_power is not None else None,
            applied=applied,
        )

    @staticmethod
    def _registers_match_plan(
        plan: SetpointPlan, echo: TelemetrySnapshot
    ) -> bool:
        try:
            return (
                int(echo["storctl_mod"]) == _MODE_STORAGE_CONTROL
                and int(echo["outwrte"]) == _decode_int16(plan.outwrte_word)
                and int(echo["inwrte"]) == _decode_int16(plan.inwrte_word)
            )
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _power_matches_plan(plan: SetpointPlan, battery_power: Any) -> bool:
        try:
            measured_w = float(battery_power)
        except (TypeError, ValueError):
            return False
        if plan.net_power_w == 0:
            return abs(measured_w) <= _IDLE_POWER_TOLERANCE_W
        threshold_w = max(_MIN_OPERATING_POWER_W, abs(plan.net_power_w) * 0.10)
        return (
            measured_w >= threshold_w
            if plan.net_power_w > 0
            else measured_w <= -threshold_w
        )

    async def _confirm_applied_plan(
        self, plan: SetpointPlan
    ) -> tuple[TelemetrySnapshot, bool, bool]:
        """Wait for both the register acknowledgement and delivered direction."""
        deadline = monotonic() + _POWER_CONFIRM_TIMEOUT_SECONDS
        last_echo: TelemetrySnapshot = {}
        registers_confirmed = False
        while True:
            last_echo = await self.read_telemetry(
                ["storctl_mod", "outwrte", "inwrte", "battery_power"]
            )
            registers_confirmed = self._registers_match_plan(plan, last_echo)
            if registers_confirmed and self._power_matches_plan(
                plan, last_echo.get("battery_power")
            ):
                return last_echo, True, True
            remaining_s = deadline - monotonic()
            if remaining_s <= 0:
                return last_echo, registers_confirmed, False
            await asyncio.sleep(min(_POWER_CONFIRM_POLL_SECONDS, remaining_s))

    async def _write_plan(self, writes: tuple[RegisterWrite, ...]) -> bool:
        self._client.unit_id = self._slave_id
        index = 0
        while index < len(writes):
            write = writes[index]
            if (
                index + 1 < len(writes)
                and write.address == self._sunspec_layout.outwrte
                and writes[index + 1].address == self._sunspec_layout.inwrte
                and hasattr(self._client, "async_write_registers")
            ):
                ok = await self._client.async_write_registers(
                    self._sunspec_layout.outwrte,
                    [write.value, writes[index + 1].value],
                )
                index += 2
            else:
                ok = await self._client.async_write_register(
                    write.address, write.value
                )
                index += 1
            if not ok:
                return False
        return True

    async def write_control(self, key: str, value: int) -> bool:
        address_by_key = {
            "storctl_mod": self._sunspec_layout.storctl_mod,
            "outwrte": self._sunspec_layout.outwrte,
            "inwrte": self._sunspec_layout.inwrte,
            "min_rsv_pct": self._sunspec_layout.min_rsv_pct,
        }
        address = address_by_key.get(key)
        if address is None:
            return False
        wire_value = int(value)
        if key in {"outwrte", "inwrte"}:
            wire_value = _int16_word(wire_value)
        self._client.unit_id = self._slave_id
        return await self._client.async_write_register(address, wire_value)

    def net_power_from_data(self, data: dict) -> Optional[int]:
        try:
            mode = int(round(float(data["storctl_mod"])))
            outwrte = int(round(float(data["outwrte"])))
            inwrte = int(round(float(data["inwrte"])))
        except (KeyError, TypeError, ValueError):
            return None
        if mode != _MODE_STORAGE_CONTROL:
            return None
        if outwrte == _IDLE_WINDOW_WORD and inwrte == _IDLE_WINDOW_WORD:
            return 0

        wcha = data.get("wcha_max") or data.get("max_charge_power") or self._wcha_max_w
        try:
            wcha_w = float(wcha)
        except (TypeError, ValueError):
            return None
        if wcha_w <= 0:
            return None

        inout_sf = data.get("inoutwrte_sf", self._last_inout_sf)
        denom = _rate_denominator(int(inout_sf))
        if outwrte < 0 and inwrte > 0:
            pct = max(0, inwrte - 1)
            return int(round((pct / denom) * wcha_w))
        if outwrte > 0 and inwrte < 0:
            pct = max(0, abs(inwrte) - 1)
            return -int(round((pct / denom) * wcha_w))
        return None

    @property
    def control_dependency_keys(self) -> frozenset:
        return frozenset({
            "storctl_mod",
            "outwrte",
            "inwrte",
            "inoutwrte_sf",
            "max_charge_power",
            "max_discharge_power",
            "battery_power",
            "commanded_net_power",
        })

    async def apply_config(
        self,
        *,
        max_soc_pct: float,
        min_soc_pct: float,
        max_charge_power_w: int,
        max_discharge_power_w: int,
    ) -> bool:
        """Keep initial setup non-invasive for the Fronius storage controller.

        Omnibattery enforces max/min SOC and software power ceilings itself for
        this driver. We deliberately do not rewrite MinRsvPct or grid-charge
        flags during setup/reload.
        """
        self._max_soc_pct = max(0.0, min(100.0, float(max_soc_pct)))
        self._min_soc_pct = max(0.0, min(100.0, float(min_soc_pct)))
        self._max_charge_w = max(0, int(max_charge_power_w))
        self._max_discharge_w = max(0, int(max_discharge_power_w))
        return True

    async def set_charge_cutoff(self, soc_pct: float) -> bool:
        _ = soc_pct
        return False

    def configure_internal_control_disabled(self, disabled: bool) -> None:
        """Choose the persistent ownership policy used by :meth:`standby`."""
        self._internal_control_disabled = bool(disabled)

    async def set_internal_control_disabled(self, disabled: bool) -> bool:
        """Apply an explicit BYD/Fronius ownership transition immediately."""
        if not self.connected:
            return False
        if disabled:
            result = await self.apply_setpoint(0, read_back=False)
            if result.ok:
                self._internal_control_disabled = True
            return result.ok

        ok = await self._write_plan(
            plan_reset_to_auto(self._sunspec_layout)
        )
        if ok:
            self._internal_control_disabled = False
            self._last_net_power_w = 0
            self._last_active_sign = 0
            self._idle_since_monotonic = None
        return ok

    async def standby(self) -> bool:
        """Apply the persisted ownership policy during integration unload.

        The safe default retains external control with a genuine 0/0 idle
        window. Fronius automatic control is restored only after an explicit
        release through the dedicated device switch.
        """
        if not self.connected:
            return False
        if not self._internal_control_disabled:
            return await self._write_plan(
                plan_reset_to_auto(self._sunspec_layout)
            )
        result = await self.apply_setpoint(0, read_back=False)
        return result.ok

    @classmethod
    async def probe(
        cls,
        host: str,
        port: int = 502,
        slave_id: int = 1,
    ) -> tuple[bool, dict[str, int]]:
        """Probe a GEN24 storage block and return detected power ceilings."""
        driver = cls(host, port, slave_id)
        try:
            if not await driver.connect():
                return False, {}
            snapshot = await driver.read_telemetry(["battery_soc", "max_charge_power"])
            max_power = snapshot.get("max_charge_power")
            caps: dict[str, int] = {}
            if isinstance(max_power, (int, float)) and int(max_power) > 0:
                caps["device_max_charge_power"] = int(max_power)
                caps["device_max_discharge_power"] = int(max_power)
            return bool(snapshot.get("battery_soc") is not None or max_power), caps
        except Exception as err:
            _LOGGER.debug("Fronius GEN24 probe failed for %s:%s slave %s: %s",
                          host, port, slave_id, err)
            return False, {}
        finally:
            await driver.close()
