"""Anker SOLIX Solarbank Max AC Modbus TCP driver.

Implements :class:`BatteryDriver` for the Solarbank Max AC over Modbus TCP.

Register map source of truth (FC03/FC04 batch ranges and write addresses):
https://raw.githubusercontent.com/anker-charging/ha-anker-solix-official/refs/heads/main/custom_components/anker_solix_official/config/8fcbb87c685781b1d70d784a79eb923098955df2aaf199095ce7767bb70b913d.yaml

Sign conventions:
  Omnibattery net power: +charge / −discharge
  Anker battery power (10008) and setpoint (10071): +discharge / −charge
  Therefore wire_w = −net_power_w.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..infra.anker_modbus_client import AnkerModbusClient
from ..infra.modbus_client import decode_registers
from .base import (
    BatteryDriver,
    DriverCapabilities,
    ReadGroup,
    SetpointResult,
    TelemetrySnapshot,
)

_LOGGER = logging.getLogger(__name__)

# Official YAML auto_mode_on_connect / third_party_control
_OPERATING_MODE_THIRD_PARTY = 3
_ADDR_OPERATING_MODE = 10064
_ADDR_POWER_SETPOINT = 10071
_ADDR_CHARGE_SOC_LIMIT = 60000
_ADDR_DISCHARGE_SOC_LIMIT = 60001
_ADDR_BATTERY_SOC = 10014

_HW_MAX_POWER_W = 3500
_MIN_OPERATING_POWER_W = 100

# Official batch_read_ranges (do not invent other blocks / FCs).
_BATCH_READ_RANGES: dict[str, list[tuple[int, int]]] = {
    # FC04 input
    "input": [
        (10000, 10050),
        (10090, 10156),
        (10208, 10265),
        (32768, 32774),
    ],
    # FC03 holding
    "holding": [
        (10060, 10072),
        (10074, 10081),
        (60000, 60003),
    ],
}

# Fields decoded from batch buffers. register_type must match the YAML range.
# invert: negate value to Omnibattery +charge/−discharge convention.
_FIELD_SPECS: list[dict] = [
    {"key": "battery_status", "address": 10001, "register_type": "input",
     "data_type": "uint16", "count": 1},
    {"key": "battery_power", "address": 10008, "register_type": "input",
     "data_type": "int32", "count": 2, "invert": True},
    {"key": "grid_power", "address": 10012, "register_type": "input",
     "data_type": "int32", "count": 2},
    {"key": "battery_soc", "address": 10014, "register_type": "input",
     "data_type": "uint16", "count": 1},
    {"key": "max_charge_power", "address": 10036, "register_type": "input",
     "data_type": "int32", "count": 2},
    {"key": "max_discharge_power", "address": 10038, "register_type": "input",
     "data_type": "int32", "count": 2},
    {"key": "temperature", "address": 10156, "register_type": "input",
     "data_type": "int16", "count": 1},
    {"key": "battery_total_energy", "address": 10250, "register_type": "input",
     "data_type": "uint32", "count": 2},
    {"key": "total_charging_energy", "address": 10262, "register_type": "input",
     "data_type": "uint32", "count": 2},
    {"key": "total_discharging_energy", "address": 10264, "register_type": "input",
     "data_type": "uint32", "count": 2},
    {"key": "operating_mode", "address": 10064, "register_type": "holding",
     "data_type": "uint16", "count": 1},
    {"key": "charging_cutoff_capacity", "address": 60000, "register_type": "holding",
     "data_type": "uint16", "count": 1},
    {"key": "discharging_cutoff_capacity", "address": 60001, "register_type": "holding",
     "data_type": "uint16", "count": 1},
]

SENSOR_DEFINITIONS: list[dict] = [
    {"key": "battery_soc", "name": "Battery SOC", "unit": "%",
     "device_class": "battery", "state_class": "measurement", "scale": 1, "precision": 0,
     "scan_interval": "medium", "enabled_by_default": True},
    {"key": "battery_power", "name": "Battery Power", "unit": "W",
     "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0,
     "scan_interval": "high", "enabled_by_default": True},
    {"key": "grid_power", "name": "Grid Power", "unit": "W",
     "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0,
     "scan_interval": "high", "enabled_by_default": True},
    {"key": "temperature", "name": "Temperature", "unit": "°C",
     "device_class": "temperature", "state_class": "measurement", "scale": 0.1, "precision": 1,
     "scan_interval": "low", "enabled_by_default": True},
    {"key": "battery_status", "name": "Battery Status", "unit": None,
     "device_class": None, "state_class": None, "scale": 1, "precision": 0,
     "icon": "mdi:battery", "scan_interval": "medium", "enabled_by_default": True,
     # Official Anker value_mapping for input 10001
     "states": {
         0: "Standby",
         1: "Charging",
         2: "Discharging",
         3: "Sleep",
     }},
    {"key": "operating_mode", "name": "Operating Mode", "unit": None,
     "device_class": None, "state_class": None, "scale": 1, "precision": 0,
     "icon": "mdi:cog", "scan_interval": "medium", "enabled_by_default": True,
     # Official Anker operating_mode options for holding 10064
     "states": {
         0: "Self Consumption",
         1: "TOU Mode",
         3: "Third-Party Control",
         4: "Custom Mode",
         5: "Socket Overlay Mode",
         6: "Smart Mode",
         7: "Dynamic Pricing",
     }},
    # Hardware ceilings (official YAML internal:true) — read-only sensors.
    # Not writable and not configurable in setup; PD uses the polled values.
    {"key": "max_charge_power", "name": "Max Charge Power", "unit": "W",
     "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0,
     "icon": "mdi:battery-charging-high", "scan_interval": "medium",
     "enabled_by_default": True},
    {"key": "max_discharge_power", "name": "Max Discharge Power", "unit": "W",
     "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0,
     "icon": "mdi:battery-arrow-down-outline", "scan_interval": "medium",
     "enabled_by_default": True},
    {"key": "battery_total_energy", "name": "Battery Total Energy", "unit": "kWh",
     "device_class": "energy", "state_class": "total", "scale": 0.1, "precision": 2,
     "scan_interval": "low", "enabled_by_default": True},
    {"key": "total_charging_energy", "name": "Total Charging Energy", "unit": "kWh",
     "device_class": "energy", "state_class": "total_increasing", "scale": 0.1, "precision": 2,
     "scan_interval": "low", "enabled_by_default": True},
    {"key": "total_discharging_energy", "name": "Total Discharging Energy", "unit": "kWh",
     "device_class": "energy", "state_class": "total_increasing", "scale": 0.1, "precision": 2,
     "scan_interval": "low", "enabled_by_default": True},
]

NUMBER_DEFINITIONS: list[dict] = [
    {"key": "charging_cutoff_capacity", "name": "Charging Cutoff Capacity", "unit": "%",
     "device_class": "battery", "min": 80, "max": 100, "step": 1,
     "scale": 1, "precision": 0, "scan_interval": "medium", "enabled_by_default": True},
    {"key": "discharging_cutoff_capacity", "name": "Discharging Cutoff Capacity", "unit": "%",
     "device_class": "battery", "min": 0, "max": 20, "step": 1,
     "scale": 1, "precision": 0, "scan_interval": "medium", "enabled_by_default": True},
]

SELECT_DEFINITIONS: list[dict] = []
SWITCH_DEFINITIONS: list[dict] = []
BINARY_SENSOR_DEFINITIONS: list[dict] = []
BUTTON_DEFINITIONS: list[dict] = []

_WRITE_CONTROL_MAP: dict[str, int] = {
    "operating_mode": _ADDR_OPERATING_MODE,
    "charging_cutoff_capacity": _ADDR_CHARGE_SOC_LIMIT,
    "discharging_cutoff_capacity": _ADDR_DISCHARGE_SOC_LIMIT,
}


def _range_for(address: int, register_type: str) -> tuple[int, int]:
    for start, end in _BATCH_READ_RANGES[register_type]:
        if start <= address <= end:
            return start, end
    raise ValueError(
        f"Address {address} is not in official {register_type} batch_read_ranges"
    )


def _clamp_soc(value: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(round(value))))


class AnkerModbusDriver(BatteryDriver):
    """Modbus TCP driver for Anker SOLIX Solarbank Max AC."""

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 1,
        *,
        client: Optional[AnkerModbusClient] = None,
        max_charge_power_w: int = _HW_MAX_POWER_W,
        max_discharge_power_w: int = _HW_MAX_POWER_W,
    ) -> None:
        self._host = host
        self._port = port
        self._slave_id = slave_id
        self._client = client or AnkerModbusClient(host, port, slave_id=slave_id)
        self._last_net_power_w: Optional[int] = None
        self._dynamic_max_charge_w = _HW_MAX_POWER_W
        self._dynamic_max_discharge_w = _HW_MAX_POWER_W
        # User limits are enforced upstream; capability envelope is hardware max.
        _ = (max_charge_power_w, max_discharge_power_w)

        self._field_by_key = {f["key"]: f for f in _FIELD_SPECS}
        self._read_groups = self._build_read_groups()
        self._capabilities = DriverCapabilities(
            hardware_soc_cutoff=True,
            has_force_mode=False,
            push_telemetry=False,
            max_charge_power_w=_HW_MAX_POWER_W,
            max_discharge_power_w=_HW_MAX_POWER_W,
            min_charge_power_w=_MIN_OPERATING_POWER_W,
            min_discharge_power_w=_MIN_OPERATING_POWER_W,
            has_mppt_pv=False,
            has_alarm_registers=False,
            has_rs485_control=False,
            has_energy_counters=True,
            setpoint_confirm_reliable=False,
            actuator_latency_s=1.0,
        )

    def _build_read_groups(self) -> list[ReadGroup]:
        """One ReadGroup per official batch range that contains any mapped field."""
        groups: list[ReadGroup] = []
        for register_type, ranges in _BATCH_READ_RANGES.items():
            for start, end in ranges:
                keys = tuple(
                    f["key"]
                    for f in _FIELD_SPECS
                    if f["register_type"] == register_type and start <= f["address"] <= end
                )
                if not keys:
                    continue
                # Prefer high cadence when any power/SOC key is in the group.
                high_keys = {"battery_power", "grid_power", "battery_soc"}
                scan = "high" if any(k in high_keys for k in keys) else "medium"
                if register_type == "holding" and start >= 60000:
                    scan = "medium"
                groups.append(ReadGroup(scan_interval=scan, keys=keys))
        return groups

    # --- identity -----------------------------------------------------------

    @property
    def capabilities(self) -> DriverCapabilities:
        return self._capabilities

    @property
    def model_label(self) -> Optional[str]:
        return "Solarbank Max AC"

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

    # --- connection lifecycle ----------------------------------------------

    @property
    def connected(self) -> bool:
        return self._client.connected

    async def connect(self) -> bool:
        ok = await self._client.async_connect()
        if not ok:
            return False
        self._client.unit_id = self._slave_id
        mode_ok = await self._ensure_third_party_mode()
        if not mode_ok:
            _LOGGER.warning(
                "Anker %s:%s connected but failed to set third_party_control mode",
                self._host,
                self._port,
            )
        return True

    async def close(self) -> None:
        await self._client.async_close()

    def set_shutting_down(self, value: bool) -> None:
        self._client.set_shutting_down(value)

    # --- telemetry ----------------------------------------------------------

    @property
    def read_groups(self) -> list[ReadGroup]:
        return self._read_groups

    async def read_telemetry(self, keys: Optional[list[str]] = None) -> TelemetrySnapshot:
        self._client.unit_id = self._slave_id
        wanted = set(keys) if keys is not None else set(self._field_by_key)
        snapshot: TelemetrySnapshot = {}

        # Group wanted fields by the official batch range they belong to.
        batches: dict[tuple[str, int, int], list[dict]] = {}
        for key in wanted:
            field = self._field_by_key.get(key)
            if field is None:
                continue
            start, end = _range_for(field["address"], field["register_type"])
            batches.setdefault((field["register_type"], start, end), []).append(field)

        for (register_type, start, end), fields in batches.items():
            count = end - start + 1
            if register_type == "input":
                regs = await self._client.async_read_input_block(start, count)
            else:
                regs = await self._client.async_read_holding_block(start, count)
            if regs is None:
                continue
            for field in fields:
                offset = field["address"] - start
                width = field.get("count", 1)
                if offset < 0 or offset + width > len(regs):
                    continue
                value = decode_registers(regs[offset:offset + width], field["data_type"])
                if value is None:
                    continue
                if field.get("invert"):
                    value = -int(value)
                snapshot[field["key"]] = value
                if field["key"] == "max_charge_power" and isinstance(value, (int, float)):
                    self._dynamic_max_charge_w = max(0, int(value)) or _HW_MAX_POWER_W
                if field["key"] == "max_discharge_power" and isinstance(value, (int, float)):
                    self._dynamic_max_discharge_w = max(0, int(value)) or _HW_MAX_POWER_W

        return snapshot

    # --- control ------------------------------------------------------------

    async def _ensure_third_party_mode(self) -> bool:
        mode = await self._client.async_read_holding_register(
            _ADDR_OPERATING_MODE, "uint16"
        )
        if mode == _OPERATING_MODE_THIRD_PARTY:
            return True
        return await self._client.async_write_register(
            _ADDR_OPERATING_MODE, _OPERATING_MODE_THIRD_PARTY
        )

    def _clamp_net_power(self, net_power_w: int) -> int:
        """Clamp to envelope and snap the forbidden 1–99 W band to 0 or 100."""
        max_charge = min(_HW_MAX_POWER_W, self._dynamic_max_charge_w)
        max_discharge = min(_HW_MAX_POWER_W, self._dynamic_max_discharge_w)
        if net_power_w > 0:
            net = min(net_power_w, max_charge)
        elif net_power_w < 0:
            net = -min(-net_power_w, max_discharge)
        else:
            return 0
        magnitude = abs(net)
        if 0 < magnitude < _MIN_OPERATING_POWER_W:
            # Prefer idle over an illegal sub-minimum command.
            return 0
        return net

    async def apply_setpoint(
        self,
        net_power_w: int,
        *,
        mode_hint: Optional[str] = None,
        read_back: bool = True,
    ) -> SetpointResult:
        _ = mode_hint  # unused; sign of net_power_w is authoritative
        if not self.connected:
            return SetpointResult(
                ok=False, net_power_w=0, confirmed=False, failure_reason="not_connected"
            )

        self._client.unit_id = self._slave_id
        if not await self._ensure_third_party_mode():
            return SetpointResult(
                ok=False,
                net_power_w=0,
                confirmed=False,
                failure_reason="mode_failed",
            )

        net = self._clamp_net_power(int(net_power_w))
        wire_w = -net  # Anker: +discharge / −charge
        ok = await self._client.async_write_registers_int32(_ADDR_POWER_SETPOINT, wire_w)
        if not ok:
            return SetpointResult(
                ok=False,
                net_power_w=net,
                confirmed=False,
                failure_reason="write_failed",
            )

        self._last_net_power_w = net
        applied = {"commanded_net_power": net, "operating_mode": _OPERATING_MODE_THIRD_PARTY}
        # Register 10071 is never_read_device — Modbus write success is the ACK.
        # Returning confirmed=False made the control loop treat every readback
        # cycle as ack_mismatch (Anker is a "fast" actuator so read_back is
        # periodically True). When read_back is requested, also sample delivered
        # power from input 10008 for non-delivery detection.
        battery_power_w: Optional[int] = None
        if read_back:
            snap = await self.read_telemetry(["battery_power"])
            raw = snap.get("battery_power")
            if raw is not None:
                battery_power_w = int(raw)
                applied["battery_power"] = battery_power_w
        return SetpointResult(
            ok=True,
            net_power_w=net,
            confirmed=True,
            exact=True,
            battery_power_w=battery_power_w,
            applied=applied,
        )

    async def write_control(self, key: str, value: int) -> bool:
        address = _WRITE_CONTROL_MAP.get(key)
        if address is None:
            return False
        self._client.unit_id = self._slave_id
        return await self._client.async_write_register(address, int(value))

    def net_power_from_data(self, data: dict) -> Optional[int]:
        value = data.get("commanded_net_power")
        if value is None:
            return self._last_net_power_w
        return int(round(float(value)))

    @property
    def control_dependency_keys(self) -> frozenset:
        return frozenset({
            "max_charge_power",
            "max_discharge_power",
            "charging_cutoff_capacity",
            "discharging_cutoff_capacity",
            "operating_mode",
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
        """Write SOC limits to 60000/60001. Power caps are read-only on Anker."""
        _ = (max_charge_power_w, max_discharge_power_w)
        self._client.unit_id = self._slave_id
        charge = _clamp_soc(max_soc_pct, 80, 100)
        discharge = _clamp_soc(min_soc_pct, 0, 20)
        ok = await self._client.async_write_register(_ADDR_CHARGE_SOC_LIMIT, charge)
        ok = await self._client.async_write_register(_ADDR_DISCHARGE_SOC_LIMIT, discharge) and ok
        return bool(ok)

    async def set_charge_cutoff(self, soc_pct: float) -> bool:
        """Write only the charge SOC limit (60000)."""
        self._client.unit_id = self._slave_id
        value = _clamp_soc(soc_pct, 80, 100)
        return await self._client.async_write_register(_ADDR_CHARGE_SOC_LIMIT, value)

    async def standby(self) -> bool:
        """Idle the battery (zero setpoint) for teardown."""
        self._client.unit_id = self._slave_id
        ok = await self._client.async_write_registers_int32(_ADDR_POWER_SETPOINT, 0)
        if ok:
            self._last_net_power_w = 0
        return bool(ok)

    @classmethod
    async def probe(
        cls,
        host: str,
        port: int = 502,
        slave_id: int = 1,
    ) -> tuple[bool, dict[str, int]]:
        """Probe connectivity and read hardware power caps when available.

        Returns ``(ok, caps)`` where ``caps`` may include
        ``device_max_charge_power`` / ``device_max_discharge_power`` from input
        registers 10036/10038 (Anker hardware ceilings). Used to seed the
        battery config envelope; the live values are exposed as sensors.
        """
        client = AnkerModbusClient(host, port, slave_id=slave_id, timeout=5.0)
        try:
            if not await client.async_connect():
                return False, {}
            client.unit_id = slave_id
            # Short dedicated reads: a truncated 10000–10050 batch can still
            # return SOC (offset 14) while missing caps at 10036/10038.
            soc = await client.async_read_input_register(_ADDR_BATTERY_SOC, "uint16")
            if soc is None:
                return False, {}
            caps: dict[str, int] = {}
            charge = await client.async_read_input_register(10036, "int32")
            discharge = await client.async_read_input_register(10038, "int32")
            if isinstance(charge, (int, float)) and int(charge) > 0:
                caps["device_max_charge_power"] = max(
                    100, min(_HW_MAX_POWER_W, int(charge))
                )
            if isinstance(discharge, (int, float)) and int(discharge) > 0:
                caps["device_max_discharge_power"] = max(
                    100, min(_HW_MAX_POWER_W, int(discharge))
                )
            return True, caps
        finally:
            await client.async_close()
