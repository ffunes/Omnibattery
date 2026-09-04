"""MQTT driver for Hoymiles micro-storage batteries.

The S-Miles Home MQTT service publishes Home Assistant discovery-style topics to
the broker already configured in HA.  This driver deliberately uses HA's MQTT
APIs; it never creates a second broker connection or stores broker credentials.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback

from .base import BatteryDriver, DriverCapabilities, ReadGroup, SetpointResult, TelemetrySnapshot

_LOGGER = logging.getLogger(__name__)
_DEFAULT_MAX_POWER_W = 1000
_UNKNOWN_MAX_POWER_W = 10000
_KEEPALIVE_S = 30
_KEEPALIVE_RETRY_S = 5


@dataclass(frozen=True)
class HoymilesModelProfile:
    """Static characteristics for one MQTT-compatible Hoymiles product."""

    key: str
    label: str
    aliases: tuple[str, ...]
    capacity_kwh: float
    max_charge_power_w: int
    max_discharge_power_w: int
    max_system_charge_power_w: int
    max_system_discharge_power_w: int
    max_units: int = 1
    capacity_scales_with_units: bool = False
    infer_units_from_power: bool = True
    correct_asymmetric_discovery: bool = False


HOYMILES_MODEL_PROFILES: tuple[HoymilesModelProfile, ...] = (
    HoymilesModelProfile(
        "ms_a2",
        "MS-A2",
        ("ms-a2", "ms-a2-fx", "ms-a2-zz", "msa2"),
        2.24,
        1000,
        1000,
        2000,
        2000,
        max_units=2,
        capacity_scales_with_units=True,
        # Firmware 01.06.03 can advertise -1000..+2000 W for one 1 kW unit.
        correct_asymmetric_discovery=True,
    ),
    HoymilesModelProfile(
        "hibattery_1920_ac",
        "HiBattery 1920 AC",
        ("hb-1920-ac-sv", "hibattery-1920-ac", "hibattery-1920-ac-sv"),
        1.92,
        1000,
        1000,
        6000,
        6000,
        max_units=6,
        capacity_scales_with_units=True,
    ),
    HoymilesModelProfile(
        "hibattery_4020_x",
        "HiBattery 4020 X",
        ("hb-4020-x", "hb-4020-xm", "hibattery-4020-x"),
        4.02,
        2000,
        2000,
        # Keep the current integration scope symmetric at 2500 W. Higher
        # charge limits available on larger expansion stacks are reserved for
        # a future feature request.
        2500,
        2500,
        max_units=4,
        capacity_scales_with_units=True,
        infer_units_from_power=False,
    ),
    HoymilesModelProfile(
        "hibattery_4020_ac",
        "HiBattery 4020 AC",
        ("hb-4020-ac", "hb-4020-acm", "hibattery-4020-ac"),
        4.02,
        2000,
        2000,
        2500,
        2500,
        max_units=4,
        capacity_scales_with_units=True,
        infer_units_from_power=False,
    ),
)
DEFAULT_HOYMILES_MODEL = "ms_a2"


def _normalise_model_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")


def hoymiles_model_profile(model: Any) -> HoymilesModelProfile | None:
    """Resolve a stored key or discovery model name to a known profile."""
    normalised = _normalise_model_name(model)
    if not normalised:
        return None
    for profile in HOYMILES_MODEL_PROFILES:
        if normalised == _normalise_model_name(profile.key):
            return profile
        if any(
            normalised == _normalise_model_name(alias)
            or _normalise_model_name(alias) in normalised
            for alias in profile.aliases
        ):
            return profile
    return None


def hoymiles_capacity_kwh(
    model: Any,
    charge_power_w: int | None = None,
    discharge_power_w: int | None = None,
    pack_count: int | None = None,
) -> float:
    """Return nominal system capacity inferred from model, packs or envelope."""
    profile = hoymiles_model_profile(model)
    if profile is None:
        return 0.0
    units = 1
    if profile.capacity_scales_with_units:
        normalised_model = _normalise_model_name(model)
        expansion_suffix = re.search(r"(?:x|xm|ac|acm)-([1-3])$", normalised_model)
        if isinstance(pack_count, int) and not isinstance(pack_count, bool) and pack_count > 0:
            units = min(profile.max_units, pack_count)
        elif expansion_suffix:
            units = min(profile.max_units, int(expansion_suffix.group(1)) + 1)
        elif profile.infer_units_from_power:
            advertised = max(int(charge_power_w or 0), int(discharge_power_w or 0))
            per_unit = max(profile.max_charge_power_w, profile.max_discharge_power_w)
            if advertised > 0:
                units = min(
                    profile.max_units,
                    max(1, (advertised + per_unit - 1) // per_unit),
                )
    return round(profile.capacity_kwh * units, 2)

SENSOR_DEFINITIONS: list[dict] = [
    {"key": "battery_soc", "name": "Battery SOC", "unit": "%", "device_class": "battery", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "battery_power", "name": "Battery Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "battery_voltage", "name": "Battery Voltage", "unit": "V", "device_class": "voltage", "state_class": "measurement", "scale": 1, "precision": 1, "scan_interval": "medium", "enabled_by_default": True},
    {"key": "battery_current", "name": "Battery Current", "unit": "A", "device_class": "current", "state_class": "measurement", "scale": 1, "precision": 1, "scan_interval": "medium", "enabled_by_default": True},
    {"key": "internal_temperature", "name": "Internal Temperature", "unit": "°C", "device_class": "temperature", "state_class": "measurement", "scale": 1, "precision": 1, "scan_interval": "medium", "enabled_by_default": True},
    {"key": "wifi_signal_strength", "name": "WiFi Signal Strength", "unit": "dBm", "device_class": "signal_strength", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "low", "enabled_by_default": True},
    {"key": "inverter_state", "name": "Inverter State", "unit": None, "device_class": None, "state_class": None, "scale": 1, "precision": 0, "icon": "mdi:state-machine", "scan_interval": "high", "enabled_by_default": True, "states": {1: "Standby", 2: "Charge", 3: "Discharge"}},
    {"key": "pack_count", "name": "Pack Count", "unit": None, "device_class": None, "state_class": "measurement", "scale": 1, "precision": 0, "icon": "mdi:battery", "scan_interval": "low", "enabled_by_default": True},
    {"key": "max_charge_power", "name": "Maximum Charge Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "low", "enabled_by_default": False},
    {"key": "max_discharge_power", "name": "Maximum Discharge Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "low", "enabled_by_default": False},
    {"key": "total_daily_charging_energy", "name": "Total Daily Charging Energy", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing", "scale": 0.001, "precision": 3, "scan_interval": "low", "enabled_by_default": True},
    {"key": "total_daily_discharging_energy", "name": "Total Daily Discharging Energy", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing", "scale": 0.001, "precision": 3, "scan_interval": "low", "enabled_by_default": True},
]


class HoymilesMqttDriver(BatteryDriver):
    """Push telemetry and external-power control for one Hoymiles system."""

    def __init__(self, hass: HomeAssistant, device_id: str, *, model: str | None = None,
                 max_charge_power_w: int | None = None,
                 max_discharge_power_w: int | None = None) -> None:
        self.hass = hass
        self.device_id = device_id
        self._model_is_configured = model is not None
        self._model_raw = model or "MS-A2"
        self._profile = hoymiles_model_profile(self._model_raw)
        profile_charge = self._profile.max_charge_power_w if self._profile else _DEFAULT_MAX_POWER_W
        profile_discharge = self._profile.max_discharge_power_w if self._profile else _DEFAULT_MAX_POWER_W
        profile_charge_max = self._profile.max_system_charge_power_w if self._profile else _UNKNOWN_MAX_POWER_W
        profile_discharge_max = self._profile.max_system_discharge_power_w if self._profile else _UNKNOWN_MAX_POWER_W
        self._uses_profile_charge_default = max_charge_power_w is None
        self._uses_profile_discharge_default = max_discharge_power_w is None
        self._configured_max_charge_w = min(
            profile_charge_max, max(0, int(max_charge_power_w or profile_charge))
        )
        self._configured_max_discharge_w = min(
            profile_discharge_max, max(0, int(max_discharge_power_w or profile_discharge))
        )
        self._max_charge_w = self._configured_max_charge_w
        self._max_discharge_w = self._configured_max_discharge_w
        self._capabilities = self._build_capabilities()
        self._cache: dict[str, Any] = {}
        self._connected = False
        self._shutting_down = False
        self._unsubscribers: list[Callable[[], Any]] = []
        self._write_lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task | None = None
        self._last_net_power_w: int | None = None
        self._read_groups = [ReadGroup("high", tuple(d["key"] for d in SENSOR_DEFINITIONS))]

    @property
    def capabilities(self): return self._capabilities
    @property
    def model_label(self):
        return self._profile.label if self._profile else (self._model_raw or None)
    @property
    def serial(self): return self.device_id
    @property
    def connected(self): return self._connected
    @property
    def read_groups(self): return self._read_groups
    @property
    def sensor_definitions(self): return SENSOR_DEFINITIONS
    @property
    def number_definitions(self): return []
    @property
    def select_definitions(self): return []
    @property
    def switch_definitions(self): return []
    @property
    def binary_sensor_definitions(self): return []
    @property
    def button_definitions(self): return []
    @property
    def all_definitions(self): return SENSOR_DEFINITIONS
    @property
    def control_dependency_keys(self): return frozenset({"battery_soc", "battery_power", "commanded_net_power"})

    def _topic(self, component: str, object_id: str, suffix: str) -> str:
        return f"homeassistant/{component}/{self.device_id}/{object_id}/{suffix}"

    @property
    def _quick_topic(self): return self._topic("sensor", "quick", "state")
    @property
    def _device_topic(self): return self._topic("sensor", "device", "state")
    @property
    def _system_topic(self): return self._topic("sensor", "system", "state")
    @property
    def _power_config_topic(self): return self._topic("number", "power_ctrl", "config")
    @property
    def _ems_command_topic(self): return self._topic("select", "ems_mode", "command")
    @property
    def _power_set_topic(self): return self._topic("number", "power_ctrl", "set")

    async def connect(self) -> bool:
        if self._connected:
            return True
        try:
            for topic, handler in ((self._quick_topic, self._handle_quick), (self._device_topic, self._handle_device),
                                   (self._system_topic, self._handle_system), (self._power_config_topic, self._handle_power_config)):
                unsubscribe = await mqtt.async_subscribe(self.hass, topic, handler, qos=1)
                self._unsubscribers.append(unsubscribe)
        except Exception as err:
            _LOGGER.debug("Unable to subscribe to Hoymiles MQTT topics: %s", err)
            await self._unsubscribe_all()
            return False
        self._connected = True
        return True

    async def _unsubscribe_all(self) -> None:
        for unsubscribe in self._unsubscribers:
            try:
                result = unsubscribe()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass
        self._unsubscribers.clear()

    async def close(self) -> None:
        task, self._keepalive_task = self._keepalive_task, None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._connected:
            try:
                async with self._write_lock:
                    await self._publish("mqtt_ctrl", 0)
                    await self._publish("general", None)
            except Exception:
                pass
        await self._unsubscribe_all()
        self._connected = False
        self._last_net_power_w = None

    def set_shutting_down(self, value: bool) -> None: self._shutting_down = value

    @staticmethod
    def _payload(message) -> dict | None:
        raw = getattr(message, "payload", message)
        if isinstance(raw, bytes): raw = raw.decode("utf-8", "replace")
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _number(data: dict, key: str) -> int | float | None:
        """Read a finite numeric MQTT value from new and legacy firmware.

        Recent firmware publishes JSON numbers, while older MS-A2 firmware can
        serialize the same telemetry as numeric strings.  Both representations
        are valid inputs for the driver; booleans and non-numeric values are not.
        """
        value = data.get(key)
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return value if math.isfinite(value) else None
        if isinstance(value, str):
            try:
                parsed = float(value.strip())
            except ValueError:
                return None
            return parsed if math.isfinite(parsed) else None
        return None

    @callback
    def _handle_quick(self, message) -> None:
        data = self._payload(message)
        if not data: return
        self._merge_battery(data)
        status = data.get("bat_sts")
        if isinstance(status, str):
            states = {"standby": 1, "charge": 2, "charging": 2, "discharge": 3, "discharging": 3}
            mapped = states.get(status.lower())
            if mapped is not None: self._cache["inverter_state"] = mapped

    @callback
    def _handle_device(self, message) -> None:
        data = self._payload(message)
        if not data: return
        self._merge_battery(data)
        for source, target in (("bat_v", "battery_voltage"), ("bat_i", "battery_current"),
                               ("bat_temp", "internal_temperature"), ("rssi", "wifi_signal_strength"),
                               ("pack_num", "pack_count")):
            value = self._number(data, source)
            if value is not None: self._cache[target] = value
        pack_count = self._number(data, "pack_num")
        if pack_count is not None:
            capacity = hoymiles_capacity_kwh(
                self._model_raw,
                pack_count=int(pack_count),
            )
            if capacity:
                self._cache["battery_total_energy"] = capacity

    @callback
    def _handle_system(self, message) -> None:
        data = self._payload(message)
        if not data: return
        self._merge_battery(data)
        for source, target in (("chg_e", "total_daily_charging_energy"), ("dchg_e", "total_daily_discharging_energy")):
            value = self._number(data, source)
            if value is not None: self._cache[target] = value
        if "ems_mode" in data: self._cache["ems_mode"] = data["ems_mode"]

    @callback
    def _handle_power_config(self, message) -> None:
        data = self._payload(message)
        if not data: return
        discovered_model = self._model_from_payload(data)
        if discovered_model and not self._model_is_configured:
            self._set_model(discovered_model)
        device_charge_w, device_discharge_w = self._device_power_caps(
            data,
            self._model_raw,
            prefer_model=self._model_is_configured,
        )
        if device_charge_w is None and device_discharge_w is None: return
        profile_charge_max = self._profile.max_system_charge_power_w if self._profile else _UNKNOWN_MAX_POWER_W
        profile_discharge_max = self._profile.max_system_discharge_power_w if self._profile else _UNKNOWN_MAX_POWER_W
        if self._uses_profile_charge_default and device_charge_w is not None:
            self._configured_max_charge_w = device_charge_w
        if self._uses_profile_discharge_default and device_discharge_w is not None:
            self._configured_max_discharge_w = device_discharge_w
        self._max_charge_w = min(
            self._configured_max_charge_w,
            device_charge_w if device_charge_w is not None else profile_charge_max,
        )
        self._max_discharge_w = min(
            self._configured_max_discharge_w,
            device_discharge_w if device_discharge_w is not None else profile_discharge_max,
        )
        self._capabilities = self._build_capabilities()
        if device_charge_w is not None:
            self._cache["max_charge_power"] = device_charge_w
        if device_discharge_w is not None:
            self._cache["max_discharge_power"] = device_discharge_w
        capacity = hoymiles_capacity_kwh(
            self._model_raw, device_charge_w, device_discharge_w
        )
        if capacity:
            self._cache["battery_total_energy"] = capacity
        if self._last_net_power_w is not None:
            self._last_net_power_w = self._clamp(self._last_net_power_w)
            self._cache["commanded_net_power"] = self._last_net_power_w

    @classmethod
    def _device_power_caps(
        cls,
        data: dict,
        model: Any = None,
        *,
        prefer_model: bool = False,
    ) -> tuple[int | None, int | None]:
        """Return safe caps from the model-specific signed discovery envelope."""
        payload_model = cls._model_from_payload(data)
        effective_model = model if prefer_model else payload_model or model
        profile = hoymiles_model_profile(effective_model or DEFAULT_HOYMILES_MODEL)
        charge_max = profile.max_system_charge_power_w if profile else _UNKNOWN_MAX_POWER_W
        discharge_max = profile.max_system_discharge_power_w if profile else _UNKNOWN_MAX_POWER_W
        minimum, maximum = cls._number(data, "min"), cls._number(data, "max")
        charge_w = (
            min(charge_max, max(0, int(-minimum)))
            if minimum is not None and minimum < 0
            else None
        )
        advertised_discharge_w = (
            min(discharge_max, max(0, int(maximum)))
            if maximum is not None and maximum > 0
            else None
        )
        if advertised_discharge_w is None:
            return charge_w, None
        if profile and profile.correct_asymmetric_discovery:
            # MS-A2 charge and discharge hardware are symmetric. The charge-side
            # magnitude distinguishes a 1 kW unit from a 2 kW pair, while one
            # firmware incorrectly advertises 2 kW discharge for a single unit.
            symmetric_ceiling_w = charge_w or profile.max_discharge_power_w
            advertised_discharge_w = min(advertised_discharge_w, symmetric_ceiling_w)
        return charge_w, advertised_discharge_w

    @classmethod
    def _model_from_payload(cls, data: dict) -> str | None:
        device = data.get("device")
        if isinstance(device, dict) and isinstance(device.get("model"), str):
            return device["model"].strip() or None
        model = data.get("model")
        return model.strip() if isinstance(model, str) and model.strip() else None

    def _set_model(self, model: str) -> None:
        profile = hoymiles_model_profile(model)
        self._model_raw = model
        self._profile = profile
        if profile:
            if self._uses_profile_charge_default:
                self._configured_max_charge_w = profile.max_charge_power_w
            else:
                self._configured_max_charge_w = min(
                    self._configured_max_charge_w,
                    profile.max_system_charge_power_w,
                )
            if self._uses_profile_discharge_default:
                self._configured_max_discharge_w = profile.max_discharge_power_w
            else:
                self._configured_max_discharge_w = min(
                    self._configured_max_discharge_w,
                    profile.max_system_discharge_power_w,
                )

    def _build_capabilities(self) -> DriverCapabilities:
        # ``has_mppt_pv`` remains false even for 4020 X: its MQTT battery-power
        # value is already the cell-side value, unlike the Marstek DC-bus value
        # for which Omnibattery's MPPT correction capability was designed.
        return DriverCapabilities(
            False, False, True, self._max_charge_w, self._max_discharge_w,
            False, False, False, has_energy_counters=True,
            has_daily_energy_counters=True, has_nominal_capacity=False,
            setpoint_confirm_reliable=False, actuator_latency_s=1.8,
            readback_latency_s=4.0,
        )

    def _merge_battery(self, data: dict) -> None:
        soc = self._number(data, "sys_soc")
        if soc is None: soc = self._number(data, "soc")
        power = self._number(data, "sys_bat_p")
        if power is None: power = self._number(data, "bat_p")
        if soc is not None: self._cache["battery_soc"] = soc
        if power is not None: self._cache["battery_power"] = -power

    async def read_telemetry(self, keys: Optional[list[str]] = None) -> TelemetrySnapshot:
        data = dict(self._cache)
        return data if keys is None else {key: data[key] for key in keys if key in data}

    def _clamp(self, net_power_w: int) -> int:
        return max(-self._max_discharge_w, min(self._max_charge_w, int(round(net_power_w))))

    @staticmethod
    def _wire_for(net_power_w: int) -> float:
        return float(-net_power_w)

    async def _publish(self, mode: str, wire_power: float | None) -> None:
        await mqtt.async_publish(self.hass, self._ems_command_topic, mode, qos=1, retain=False)
        if wire_power is not None:
            payload = f"{wire_power:.1f}" if wire_power % 1 else str(int(wire_power))
            await mqtt.async_publish(self.hass, self._power_set_topic, payload, qos=1, retain=False)

    async def apply_setpoint(self, net_power_w: int, *, mode_hint: Optional[str] = None, read_back: bool = True) -> SetpointResult:
        if not self._connected:
            return SetpointResult(False, 0, False, failure_reason="not_connected")
        applied = self._clamp(net_power_w)
        try:
            async with self._write_lock:
                wire = self._wire_for(applied)
                await self._publish("mqtt_ctrl", wire)
                self._last_net_power_w = applied
                self._cache["commanded_net_power"] = applied
        except Exception as err:
            _LOGGER.debug("Hoymiles MQTT command failed: %s", err)
            return SetpointResult(False, applied, False, failure_reason="write_failed")
        self._ensure_keepalive()
        return SetpointResult(True, applied, False, battery_power_w=self._cache.get("battery_power"),
            applied={"commanded_net_power": applied})

    def _ensure_keepalive(self) -> None:
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = self.hass.async_create_background_task(
                self._keepalive_loop(),
                "omnibattery_hoymiles_keepalive",
            )

    async def _keepalive_loop(self) -> None:
        delay_s = _KEEPALIVE_S
        try:
            while True:
                await asyncio.sleep(delay_s)
                refreshed = await self._refresh_command()
                delay_s = _KEEPALIVE_S if refreshed else _KEEPALIVE_RETRY_S
        except asyncio.CancelledError:
            raise

    async def _refresh_command(self) -> bool:
        if not self._connected or self._last_net_power_w is None:
            return False
        try:
            async with self._write_lock:
                self._last_net_power_w = self._clamp(self._last_net_power_w)
                self._cache["commanded_net_power"] = self._last_net_power_w
                wire = self._wire_for(self._last_net_power_w)
                await self._publish("mqtt_ctrl", wire)
            return True
        except Exception as err:
            _LOGGER.debug("Hoymiles MQTT keepalive failed: %s", err)
            return False

    async def standby(self) -> bool:
        if not self._connected: return False
        try:
            async with self._write_lock:
                await self._publish("mqtt_ctrl", 0)
                self._last_net_power_w = 0
                self._cache["commanded_net_power"] = 0
            self._ensure_keepalive()
            return True
        except Exception:
            return False

    async def apply_config(self, **kwargs) -> bool: return True
    async def set_charge_cutoff(self, soc_pct: float) -> bool: return False
    async def set_rs485_control(self, enabled: bool) -> bool: return False
    async def write_control(self, key: str, value: int) -> bool: return False
    def net_power_from_data(self, data: dict) -> Optional[int]:
        value = data.get("commanded_net_power")
        return int(value) if isinstance(value, (int, float)) else None

    @classmethod
    async def probe(
        cls,
        hass: HomeAssistant,
        device_id: str,
        timeout: float = 5.0,
        *,
        model_hint: str | None = None,
    ) -> tuple[bool, dict]:
        """Wait briefly for retained/live quick telemetry and clean up always."""
        event = asyncio.Event()
        config_event = asyncio.Event()
        metadata: dict[str, Any] = {}
        hinted_profile = hoymiles_model_profile(model_hint)
        if model_hint:
            metadata["hoymiles_model"] = (
                hinted_profile.key if hinted_profile else model_hint
            )
            metadata["hoymiles_model_label"] = (
                hinted_profile.label if hinted_profile else model_hint
            )
            capacity = hoymiles_capacity_kwh(model_hint)
            if capacity:
                metadata["battery_capacity_kwh"] = capacity
        valid = False

        @callback
        def quick(message) -> None:
            nonlocal valid
            data = cls._payload(message)
            if data and (cls._number(data, "sys_soc") is not None or cls._number(data, "soc") is not None) and (cls._number(data, "sys_bat_p") is not None or cls._number(data, "bat_p") is not None):
                valid = True; event.set()

        @callback
        def config(message) -> None:
            data = cls._payload(message) or {}
            discovered_model = cls._model_from_payload(data)
            model = model_hint or discovered_model
            profile = hoymiles_model_profile(model or DEFAULT_HOYMILES_MODEL)
            charge_w, discharge_w = cls._device_power_caps(
                data,
                model,
                prefer_model=model_hint is not None,
            )
            if model:
                metadata["hoymiles_model"] = profile.key if profile else model
                metadata["hoymiles_model_label"] = profile.label if profile else model
            if charge_w is not None: metadata["device_max_charge_power"] = charge_w
            if discharge_w is not None: metadata["device_max_discharge_power"] = discharge_w
            if model:
                capacity = hoymiles_capacity_kwh(
                    profile.key if profile else model, charge_w, discharge_w
                )
                if capacity:
                    metadata["battery_capacity_kwh"] = capacity
            config_event.set()

        unsubs = []
        try:
            unsubs.append(await mqtt.async_subscribe(hass, f"homeassistant/sensor/{device_id}/quick/state", quick, qos=1))
            unsubs.append(await mqtt.async_subscribe(hass, f"homeassistant/number/{device_id}/power_ctrl/config", config, qos=1))
            await asyncio.wait_for(event.wait(), timeout)
            if not config_event.is_set():
                try:
                    await asyncio.wait_for(config_event.wait(), min(timeout, 0.25))
                except asyncio.TimeoutError:
                    pass
            return valid, metadata
        except Exception:
            return False, metadata
        finally:
            for unsubscribe in unsubs:
                try:
                    result = unsubscribe()
                    if hasattr(result, "__await__"): await result
                except Exception:
                    pass
