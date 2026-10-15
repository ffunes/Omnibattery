"""Local HTTP driver for Indevolt batteries (SolidFlex / PowerFlex, Gen 2).

All device traffic goes through :class:`~..infra.indevolt_client.IndevoltScheduler`:
one request per slot, writes first, and a timestamped cache that
:meth:`IndevoltDriver.read_telemetry` serves to the coordinator. The coordinator
can poll as often as it likes; the device sees the scheduler's pace.

Control uses only the realtime RAM tuple 47015 (action, power, target SOC). The
battery must be in Realtime mode, which the user sets in the Indevolt app; the
driver never changes the work mode, because that is a flash write. Control
follows one small state machine:

* ``MONITORING``: reading, not controlling (initial state, and control disabled).
* ``ACQUIRING``: the battery is in Realtime mode; STOP was sent and the driver
  waits until the echo confirms standby.
* ``OWNED``: the core's setpoints are written to the device.
* ``PAUSED``: control stopped for a reason (see :data:`PAUSE_REASONS`). The
  driver keeps monitoring and acquires again on its own when the reason clears.

Every acquisition, including the first after a restart, starts with STOP, so a
command retained from before cannot run on.

The core sends signed power only and enforces its SOC limits in software, so the
tuple's target SOC is a failsafe: a few percent beyond the current SOC in the
commanded direction, refreshed by the driver itself before the battery gets
there. If Home Assistant disappears mid-command, the battery stops within ~5%.

Three user settings (min SOC, max charge and max discharge power) are flash on
the device. They are synced both ways by :mod:`.indevolt_settings`, written only
on a direct user action and never from the control path. Max SOC has no device
setting and stays software-only.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from ..infra.indevolt_client import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    ACTION_STOP,
    DEFAULT_PORT,
    MAX_REALTIME_POWER_W,
    MIN_TARGET_SOC,
    REQUEST_TIMEOUT_S,
    SLOT_S,
    STOP_TUPLE,
    WRITE_ACCEPTED,
    IndevoltAuthenticationError,
    IndevoltClient,
    IndevoltError,
    IndevoltScheduler,
    Tier,
    Transport,
)
from .indevolt_settings import BY_KEY as SETTING_BY_KEY
from .indevolt_settings import MAX_CHARGE, MAX_DISCHARGE, MIN_SOC, SETTINGS, IndevoltSettings
from .base import (
    DELIVERED_AC_POWER_KEY,
    BatteryDriver,
    DriverCapabilities,
    ReadGroup,
    SetpointResult,
    TelemetrySnapshot,
)

_LOGGER = logging.getLogger(__name__)

# --- points -------------------------------------------------------------------
P_BATTERY_POWER = 6000  # + discharging, - charging
P_BATTERY_STATE = 6001  # 1000 idle, 1001 charging, 1002 discharging
P_SOC = 6002
P_AC_TOTAL = 2278  # total AC power, + charging; includes the bypass port
P_BYPASS = 667  # power through the bypass port
P_ENERGY_MODE = 7101  # 1 self-consumption, 4 realtime, 5 schedule
P_RT_STATUS = 6107
P_RT_TARGET = 6108
P_RT_POWER = 6109
P_NATIVE_MIN_SOC = 6105  # the reserve; written via 1142 (see indevolt_settings)
P_HW_MAX_CHARGE = 114  # hardware ceilings; did not move when the settings changed
P_HW_MAX_DISCHARGE = 115

ENERGY_MODE_REALTIME = 4

# Read tiers. The control tier goes out in every read slot; the others ride
# along when due (see IndevoltScheduler).
CONTROL_TIER = Tier("control", (P_BATTERY_POWER, P_AC_TOTAL, P_BYPASS, P_ENERGY_MODE, P_SOC))
ECHO_TIER = Tier("echo", (P_RT_STATUS, P_RT_TARGET, P_RT_POWER, P_BATTERY_STATE), 30.0)
# Battery temperatures: the battery built into the inverter unit, then up to
# five expansion batteries. An expansion battery exists when its serial number
# point is non-empty (as in the official integration). 11005 is the transformer
# (inverter) temperature.
P_INVERTER_BATTERY_TEMPERATURE = 9012
PACKS = (  # (serial point, temperature point)
    (9032, 9030), (9051, 9049), (9070, 9068), (9165, 9163), (9218, 9216),
)
P_TRANSFORMER_TEMPERATURE = 11005

LIMITS_TIER = Tier(
    "limits",
    (11009, 11011, 11010, 11032, 114, 115, 2618, P_NATIVE_MIN_SOC, 606, 142,
     *(serial for serial, _ in PACKS)),
    180.0,
)
SLOW_TIER = Tier(
    "slow",
    (6006, 6007, 6004, 6005, P_INVERTER_BATTERY_TEMPERATURE, *(temp for _, temp in PACKS),
     P_TRANSFORMER_TEMPERATURE, 1664, 1665, 1666, 1667, 2275),
    60.0,
)
TIERS = (CONTROL_TIER, ECHO_TIER, LIMITS_TIER, SLOW_TIER)
ECHO_INTERVAL_OWNED_S = 30.0
ECHO_INTERVAL_IDLE_S = 60.0

# --- control policy ---------------------------------------------------------------
TARGET_STEP_PCT = 5  # target SOC = current SOC ± this
REFRESH_MARGIN_PCT = 2  # refresh the target when SOC is this close to it
HANDOVER_MAX_READS = 6
HANDOVER_RETRY_S = (60.0, 120.0, 300.0, 600.0)
IDLE_TOLERANCE_W = 100
RAMP_S = 2.0
# A standby SF2000 takes 10–15s to start charging or discharging after a
# realtime command, and its echo reports standby until then. Standby within
# this long of an accepted command is the start, not a device that dropped out.
START_GRACE_S = 30.0
THROTTLE_NOTIFY_AFTER_S = 300.0
DEFAULT_MAX_POWER_W = 2400

_STATUS_ACTION = {0: ACTION_STOP, 1: ACTION_CHARGE, 2: ACTION_DISCHARGE,
                  1000: ACTION_STOP, 1001: ACTION_CHARGE, 1002: ACTION_DISCHARGE}
# 6001 battery state -> the core's inverter_state convention (1 standby,
# 2 charging, 3 discharging).
_BATTERY_STATE = {1000: 1, 1001: 2, 1002: 3}


class ControlState(enum.Enum):
    MONITORING = "monitoring"
    ACQUIRING = "acquiring"
    OWNED = "owned"
    PAUSED = "paused"


PAUSE_REASONS = (
    "realtime_mode_required",
    "not_configured",
    "telemetry_stale",
    "handover_failed",
    "unknown_model",
)
CONTROL_STATUS_OPTIONS = ("active", "starting", *PAUSE_REASONS)


@dataclass(frozen=True)
class _Model:
    label: str
    min_firmware: str  # vendor minimum CMS firmware (f_ver)


# Gen-2 device types from the vendor co-maintained library (indevolt-api 1.8.8)
# and Indevolt's minimum firmware per family. Unknown types are monitored only.
MODELS = {
    "CMS-SF2000": _Model("SolidFlex 2000", "V1406.07.002E"),
    "CMS-SP2000": _Model("PowerFlex 2000", "V1406.07.002E"),
    "CMS-SF3000": _Model("SolidFlex 3000", "V1409.08.3034"),
    "CMS-SP3000": _Model("PowerFlex 3000", "V1409.08.3034"),
    "CMS-SF1200": _Model("SolidFlex 1200", "V1407.07.202E"),
}

_FW_PATTERN = re.compile(r"^V([0-9A-Fa-f]+)\.([0-9A-Fa-f]+)\.([0-9A-Fa-f]+)$")


def firmware_below(firmware: object, minimum: str) -> bool:
    """True if ``firmware`` parses and is older than ``minimum`` (hex segments)."""
    if not isinstance(firmware, str):
        return False
    have, need = _FW_PATTERN.match(firmware.strip()), _FW_PATTERN.match(minimum)
    if have is None or need is None:
        return False
    return tuple(int(p, 16) for p in have.groups()) < tuple(int(p, 16) for p in need.groups())


def _num(value: object, low: float = -1e12, high: float = 1e12) -> float | None:
    """Strict JSON number in range; strings of digits are accepted (606 is one)."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        return None
    return value


def _definition(key: str, name: str, *, unit: str | None = None, device_class: str | None = None,
                state_class: str | None = "measurement", precision: int = 0, scan: str = "high",
                enabled: bool = True, diagnostic: bool = False, **extra: Any) -> dict:
    definition = {"key": key, "name": name, "scale": 1, "precision": precision,
                  "scan_interval": scan, "enabled_by_default": enabled}
    if unit:
        definition["unit"] = unit
    if device_class:
        definition["device_class"] = device_class
    if state_class:
        definition["state_class"] = state_class
    if diagnostic:
        definition["category"] = "diagnostic"
    definition.update(extra)
    return definition


def _text(key: str, name: str, *, icon: str, scan: str, enabled: bool = True) -> dict:
    return {"key": key, "name": name, "data_type": "char", "icon": icon,
            "category": "diagnostic", "scan_interval": scan, "enabled_by_default": enabled}


SENSOR_DEFINITIONS = [
    _definition("battery_soc", "Battery SOC", unit="%", device_class="battery", scan="medium"),
    _definition("battery_power", "Battery Power", unit="W", device_class="power"),
    _definition("ac_power", "AC Power", unit="W", device_class="power"),
    _definition("total_charging_energy", "Total Charging Energy", unit="kWh", device_class="energy",
                state_class="total_increasing", precision=3, scan="low"),
    _definition("total_discharging_energy", "Total Discharging Energy", unit="kWh",
                device_class="energy", state_class="total_increasing", precision=3, scan="low"),
    _definition("total_daily_charging_energy", "Daily Charging Energy", unit="kWh",
                device_class="energy", state_class="total_increasing", precision=3, scan="low"),
    _definition("total_daily_discharging_energy", "Daily Discharging Energy", unit="kWh",
                device_class="energy", state_class="total_increasing", precision=3, scan="low"),
    _definition("battery_total_energy", "Battery Total Energy", unit="kWh", device_class="energy",
                state_class=None, precision=2, scan="low"),
    _definition("internal_temperature", "Internal Temperature", unit="°C",
                device_class="temperature", precision=1, scan="low"),
    _definition("inverter_battery_temperature", "Inverter Battery Temperature", unit="°C",
                device_class="temperature", precision=1, scan="low", diagnostic=True),
    _definition("transformer_temperature", "Transformer Temperature", unit="°C",
                device_class="temperature", precision=1, scan="low", enabled=False,
                diagnostic=True),
    _definition("solar_power", "Solar Power", unit="W", device_class="power", scan="low",
                enabled=False),
    _definition("energy_mode", "Working Mode", state_class=None, scan="medium", diagnostic=True,
                icon="mdi:cog-transfer",
                states={0: "Outdoor portable", 1: "Self-consumption", 4: "Realtime",
                        5: "Schedule"}),
    _definition("inverter_state", "Inverter State", state_class=None, scan="medium",
                diagnostic=True, icon="mdi:state-machine",
                states={1: "Standby", 2: "Charging", 3: "Discharging"}),
    _text("control_status", "Control Status", icon="mdi:remote", scan="medium"),
    _definition("realtime_power", "Realtime Command Power", unit="W", device_class="power",
                scan="low", enabled=False, diagnostic=True),
    _definition("realtime_target_soc", "Realtime Target SOC", unit="%", scan="low",
                enabled=False, diagnostic=True),
    _definition("inverter_ac_power", "Inverter AC Power", unit="W", device_class="power",
                scan="low", enabled=False, diagnostic=True),
    _definition("grid_charging_allowed", "Grid Charging", state_class=None, scan="low",
                enabled=False, diagnostic=True, states={1000: "Off", 1001: "On"}),
    _text("software_version", "Software Version", icon="mdi:ticket-confirmation-outline",
          scan="very_low"),
]

_HIGH_KEYS = ("battery_power", "ac_power", DELIVERED_AC_POWER_KEY)
_SETTING_KEYS = tuple(s.key for s in SETTINGS)
_MEDIUM_KEYS = ("battery_soc", "energy_mode", "inverter_state", "control_status",
                *_SETTING_KEYS)
# One temperature sensor per expansion battery that is present (see PACKS).
PACK_TEMPERATURE_DEFINITIONS = [
    _definition(f"expansion_battery_{n}_temperature", f"Expansion Battery {n} Temperature", unit="°C",
                device_class="temperature", precision=1, scan="low", diagnostic=True)
    for n in range(1, len(PACKS) + 1)
]

_LOW_KEYS = ("total_charging_energy", "total_discharging_energy", "total_daily_charging_energy",
             "total_daily_discharging_energy", "battery_total_energy", "internal_temperature",
             "inverter_battery_temperature", "transformer_temperature",
             "solar_power", "realtime_power", "realtime_target_soc", "inverter_ac_power",
             "grid_charging_allowed")
_VERY_LOW_KEYS = ("software_version",)


class Notifier(Protocol):
    def paused(self, reason: str, *, model: str | None = None) -> None: ...
    def resumed(self) -> None: ...
    def firmware_outdated(self, firmware: str, minimum: str) -> None: ...
    def setting_failed(self, key: str, value: int) -> None: ...
    def setting_budget_exhausted(self, key: str, budget: int) -> None: ...
    def setting_ok(self, key: str) -> None: ...
    def throttled(self, interval_s: float) -> None: ...
    def throttle_cleared(self) -> None: ...


class IndevoltDriver(BatteryDriver):
    """Monitor and control one Indevolt battery through its local HTTP API."""

    def __init__(
        self,
        host: str,
        *,
        port: int = DEFAULT_PORT,
        max_charge_power_w: int = DEFAULT_MAX_POWER_W,
        max_discharge_power_w: int = DEFAULT_MAX_POWER_W,
        control_enabled: bool = True,
        notifier: Notifier | None = None,
        persist: Callable[[str, Any], None] | None = None,
        stored: Callable[[str], Any] | None = None,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        slot_s: float = SLOT_S,
        name: str = "Indevolt",
    ) -> None:
        self._host = host
        self._port = port
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._slot_s = slot_s
        self._name = name
        self._control_enabled = control_enabled
        self._notifier = notifier
        self._client: IndevoltClient | None = None
        self._scheduler: IndevoltScheduler | None = None
        self._connected = False
        self._shutting_down = False
        self._config: dict[str, Any] = {}
        self._model: _Model | None = None
        self._capabilities = DriverCapabilities(
            hardware_soc_cutoff=False,
            has_force_mode=False,
            push_telemetry=True,
            max_charge_power_w=max(0, min(int(max_charge_power_w), MAX_REALTIME_POWER_W)),
            max_discharge_power_w=max(0, min(int(max_discharge_power_w), MAX_REALTIME_POWER_W)),
            has_mppt_pv=False,
            has_alarm_registers=False,
            has_rs485_control=False,
            has_solar_telemetry=True,
            setpoint_confirm_reliable=False,
            # A write waits at most one slot, then the battery ramps; the effect
            # shows up in the read slot after that.
            actuator_latency_s=slot_s + RAMP_S,
            readback_latency_s=2 * slot_s + RAMP_S,
            telemetry_liveness_checked=True,
            discharge_cutoff_range=(float(MIN_TARGET_SOC), 50.0),
        )
        self._read_groups = [
            ReadGroup("high", _HIGH_KEYS),
            ReadGroup("medium", _MEDIUM_KEYS),
            ReadGroup("low", _LOW_KEYS),
            ReadGroup("very_low", _VERY_LOW_KEYS),
        ]
        # control state
        self._state = ControlState.MONITORING
        self._reason: str | None = None
        self._command: tuple[int, int, int] | None = None
        self._command_net: int | None = None
        self._command_accepted_at: float | None = None
        self._resend_done = False
        self._stop_accepted_at: float | None = None
        self._handover_reads = 0
        self._handover_failures = 0
        self._next_acquire_at = 0.0
        self._throttle_notified = False
        self._settings = IndevoltSettings(
            client=lambda: self._client,
            read=self._setting_device_value,
            expedite=self._expedite_limits,
            clock=clock,
            sleep=sleep or asyncio.sleep,
            persist=persist,
            stored=stored,
            notifier=notifier,
            name=name,
        )

    # --- identity ------------------------------------------------------------------

    @property
    def capabilities(self) -> DriverCapabilities:
        return self._capabilities

    @property
    def model_label(self) -> Optional[str]:
        if self._model is not None:
            return self._model.label
        return self._config.get("type")

    @property
    def serial(self) -> Optional[str]:
        return self._config.get("sn")

    @property
    def firmware(self) -> Optional[str]:
        return self._config.get("f_ver") or self._config.get("fw")

    @property
    def control_state(self) -> ControlState:
        return self._state

    @property
    def paused_reason(self) -> str | None:
        return self._reason if self._state is ControlState.PAUSED else None

    @property
    def control_status(self) -> str:
        if self._state is ControlState.OWNED:
            return "active"
        if self._state is ControlState.PAUSED and self._reason:
            return self._reason
        if not self._control_enabled:
            return "not_configured"
        return "starting"

    @property
    def scheduler(self) -> IndevoltScheduler | None:
        return self._scheduler

    # --- definitions -----------------------------------------------------------------

    @property
    def read_groups(self) -> list[ReadGroup]:
        packs = tuple(d["key"] for d in self._pack_definitions())
        return [*self._read_groups, ReadGroup("low", packs)] if packs else self._read_groups
    @property
    def sensor_definitions(self) -> list[dict]: return SENSOR_DEFINITIONS + self._pack_definitions()
    @property
    def number_definitions(self) -> list[dict]:
        """The three device settings, as register-style numbers.

        The core writes them through :meth:`write_control` and adopts the polled
        values back (``discharging_cutoff_capacity`` -> min SOC, the power keys
        -> the configured ceilings), which is the two-way sync. Built after
        connect, so the power maxima are this battery's hardware ceilings.
        """
        charge_cap, discharge_cap = self._hardware_ceilings()
        low, high = self._capabilities.discharge_cutoff_range
        return [
            {"key": MIN_SOC.key, "name": "Discharge Cutoff", "unit": "%", "min": low,
             "max": high, "step": 1, "scale": 1, "icon": "mdi:battery-arrow-down",
             "scan_interval": "medium"},
            {"key": MAX_CHARGE.key, "name": "Max Charge Power", "unit": "W", "min": 100,
             "max": charge_cap, "step": 10, "scale": 1, "icon": "mdi:battery-arrow-up-outline",
             "scan_interval": "medium"},
            {"key": MAX_DISCHARGE.key, "name": "Max Discharge Power", "unit": "W", "min": 0,
             "max": discharge_cap, "step": 10, "scale": 1,
             "icon": "mdi:battery-arrow-down-outline", "scan_interval": "medium"},
        ]
    @property
    def select_definitions(self) -> list[dict]: return []
    @property
    def switch_definitions(self) -> list[dict]: return []
    @property
    def binary_sensor_definitions(self) -> list[dict]: return []
    @property
    def button_definitions(self) -> list[dict]: return []
    @property
    def all_definitions(self) -> list[dict]: return self.sensor_definitions + self.number_definitions

    def _present_packs(self) -> list[int]:
        """Numbers (1-5) of the expansion batteries that report a serial number."""
        if self._scheduler is None:
            return []
        return [n for n, (serial, _) in enumerate(PACKS, start=1)
                if self._scheduler.value(serial) not in (None, "", 0)]

    def _pack_definitions(self) -> list[dict]:
        return [PACK_TEMPERATURE_DEFINITIONS[n - 1] for n in self._present_packs()]

    @property
    def control_dependency_keys(self) -> frozenset:
        # The settings too: the core adopts them from data even when their
        # entities are disabled.
        return frozenset({"battery_power", "battery_soc", DELIVERED_AC_POWER_KEY, "ac_power",
                          *_SETTING_KEYS})

    # --- connection ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    def _new_client(self) -> IndevoltClient:
        kwargs: dict[str, Any] = {"clock": self._clock}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        return IndevoltClient(
            self._host, port=self._port, slot_s=self._slot_s, transport=self._transport, **kwargs,
        )

    async def connect(self) -> bool:
        """Start the scheduler once; later calls only check that telemetry flows."""
        if self._scheduler is not None:
            # Already running: a reconnect must not add device traffic.
            self._connected = self._control_fresh()
            return self._connected
        if self._client is None or self._client.closed:
            self._client = self._new_client()
        try:
            config = await self._client.get_config()
        except IndevoltError as err:
            if not self._shutting_down:
                _LOGGER.warning("[%s] Indevolt not reachable: %s", self._name, err)
            self._connected = False
            return False
        device = config["device"]
        self._config = {k: str(device[k]) for k in ("type", "sn", "fw", "f_ver") if k in device}
        self._model = MODELS.get(self._config.get("type", ""))
        if self._model is not None and firmware_below(self._config.get("f_ver"), self._model.min_firmware):
            _LOGGER.warning("[%s] Indevolt firmware %s is older than the recommended %s",
                            self._name, self._config.get("f_ver"), self._model.min_firmware)
            if self._notifier is not None:
                self._notifier.firmware_outdated(self._config["f_ver"], self._model.min_firmware)
        self._scheduler = IndevoltScheduler(
            self._client, TIERS, clock=self._clock, on_telemetry=self._on_telemetry,
            on_write=self._on_write, name=self._name,
        )
        self._scheduler.set_tier_interval("echo", ECHO_INTERVAL_IDLE_S)
        self._scheduler.start()
        first = await self._scheduler.wait_first_read(2 * self._client.slot_s + REQUEST_TIMEOUT_S + 1)
        self._connected = first
        return first

    async def close(self) -> None:
        self._connected = False
        await self._settings.close()
        if self._scheduler is not None:
            await self._scheduler.close()
            self._scheduler = None
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._state = ControlState.MONITORING
        self._command = None
        self._command_net = None

    def set_shutting_down(self, value: bool) -> None:
        self._shutting_down = value

    @classmethod
    async def probe(cls, host: str, port: int = DEFAULT_PORT, *,
                    transport: Transport | None = None,
                    clock: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], Awaitable[Any]] | None = None) -> dict | None:
        """Read identity, mode and limits once, for the config flow. Never writes.

        Shares the device's pacing gate with a running driver, so a probe of a
        configured battery waits for its slot instead of adding a burst.
        Raises IndevoltAuthenticationError when the local API is set to HTTP
        Digest, so the flow can say so; returns None when the battery is not
        reachable.
        """
        kwargs: dict[str, Any] = {"clock": clock}
        if sleep is not None:
            kwargs["sleep"] = sleep
        client = IndevoltClient(host, port=port, transport=transport, **kwargs)
        try:
            config = await client.get_config()
            points = [*CONTROL_TIER.points, *LIMITS_TIER.points]
            data = await client.get_data(points)
        except IndevoltAuthenticationError:
            raise
        except (IndevoltError, ValueError) as err:
            _LOGGER.info("Indevolt probe of %s:%s failed: %s", host, port, err)
            return None
        finally:
            await client.close()
        device = config["device"]
        dtype = str(device.get("type", ""))
        if _num(data.get(str(P_SOC)), 0, 100) is None:
            return None
        charge, discharge = _hardware_limits({int(k): v for k, v in data.items() if k.isdigit()})
        model = MODELS.get(dtype)
        return {
            "type": dtype,
            "model": model.label if model else dtype,
            "known_model": model is not None,
            "serial": str(device.get("sn", "")),
            "firmware": str(device.get("f_ver") or device.get("fw") or ""),
            "energy_mode": _num(data.get(str(P_ENERGY_MODE))),
            "realtime_mode": _num(data.get(str(P_ENERGY_MODE))) == ENERGY_MODE_REALTIME,
            "max_charge_power": charge,
            "max_discharge_power": discharge,
            "native_min_soc": _num(data.get(str(P_NATIVE_MIN_SOC)), 0, 100),
            "battery_capacity_kwh": _num(data.get("142"), 0, 1000),
        }

    # --- telemetry -------------------------------------------------------------------

    def _fresh_value(self, point: int, max_age_s: float | None = None) -> Any:
        if self._scheduler is None:
            return None
        if max_age_s is None:
            max_age_s = self._scheduler.control_max_age_s
        return self._scheduler.value(point, max_age_s)

    def _tier_max_age(self, tier: Tier) -> float:
        assert self._scheduler is not None
        interval = max(tier.interval_s, ECHO_INTERVAL_IDLE_S if tier is ECHO_TIER else 0)
        return 3 * interval + self._scheduler.control_max_age_s

    def _control_fresh(self) -> bool:
        return all(self._fresh_value(p) is not None for p in CONTROL_TIER.points)

    def _soc(self) -> float | None:
        return _num(self._fresh_value(P_SOC), 0, 100)

    def _decode(self) -> dict[str, Any]:
        """The cache as logical keys; stale values are left out."""
        if self._scheduler is None:
            return {}
        out: dict[str, Any] = {}
        c = self._fresh_value
        bp = _num(c(P_BATTERY_POWER), -MAX_REALTIME_POWER_W * 2, MAX_REALTIME_POWER_W * 2)
        if bp is not None:
            out["battery_power"] = -bp  # native + discharging; Omnibattery + charging
        total, bypass = _num(c(P_AC_TOTAL)), _num(c(P_BYPASS))
        if total is not None and bypass is not None:
            # The inverter's own AC contribution: 2278 minus what passes through
            # the bypass port. + charging, the battery_power convention.
            delivered = total - bypass
            out[DELIVERED_AC_POWER_KEY] = delivered
            out["ac_power"] = -delivered  # core convention: + discharging
        soc = _num(c(P_SOC), 0, 100)
        if soc is not None:
            out["battery_soc"] = soc
        mode = _num(c(P_ENERGY_MODE))
        if mode is not None:
            out["energy_mode"] = int(mode)
        if out:
            out["control_status"] = self.control_status

        echo_age = self._tier_max_age(ECHO_TIER)
        state = _num(c(P_BATTERY_STATE, echo_age))
        if state is not None and int(state) in _BATTERY_STATE:
            out["inverter_state"] = _BATTERY_STATE[int(state)]
        rt_power = _num(c(P_RT_POWER, echo_age), 0, MAX_REALTIME_POWER_W)
        if rt_power is not None:
            out["realtime_power"] = rt_power
        rt_target = _num(c(P_RT_TARGET, echo_age), 0, 100)
        if rt_target is not None:
            out["realtime_target_soc"] = rt_target

        limits_age = self._tier_max_age(LIMITS_TIER)
        limits = {p: c(p, limits_age) for p in LIMITS_TIER.points}
        # The user settings. The coordinator adopts these as min SOC and the
        # configured power ceilings; a value Omnibattery is writing is shown
        # until the device reports it back.
        for key in _SETTING_KEYS:
            value = self._settings.published(key)
            if value is not None and (key == MIN_SOC.key or value > 0):
                out[key] = value
        capacity = _num(limits[142], 0.01, 1000)
        if capacity is not None:
            out["battery_total_energy"] = capacity
        grid = _num(limits[2618])
        if grid is not None:
            out["grid_charging_allowed"] = int(grid)

        slow_age = self._tier_max_age(SLOW_TIER)
        for key, point in (("total_charging_energy", 6006), ("total_discharging_energy", 6007),
                           ("total_daily_charging_energy", 6004),
                           ("total_daily_discharging_energy", 6005)):
            value = _num(c(point, slow_age), 0, 1e9)
            if value is not None:
                out[key] = value
        # The core derates charging on internal_temperature: use the warmest
        # battery (inverter unit and present expansion batteries), not the transformer.
        inverter_battery = _num(c(P_INVERTER_BATTERY_TEMPERATURE, slow_age), -40, 100)
        if inverter_battery is not None:
            out["inverter_battery_temperature"] = inverter_battery
        battery_temps = [inverter_battery] if inverter_battery is not None else []
        for n in self._present_packs():
            temp = _num(c(PACKS[n - 1][1], slow_age), -40, 100)
            if temp is not None:
                out[f"expansion_battery_{n}_temperature"] = temp
                battery_temps.append(temp)
        if battery_temps:
            out["internal_temperature"] = max(battery_temps)
        transformer = _num(c(P_TRANSFORMER_TEMPERATURE, slow_age), -40, 150)
        if transformer is not None:
            out["transformer_temperature"] = transformer
        pv = [_num(c(p, slow_age), 0, 10000) for p in (1664, 1665, 1666, 1667)]
        if all(v is not None for v in pv):
            out["solar_power"] = sum(pv)  # type: ignore[arg-type]
        inverter_ac = _num(c(2275, slow_age))
        if inverter_ac is not None:
            out["inverter_ac_power"] = inverter_ac
        if self.firmware and out:
            out["software_version"] = self.firmware
        return out

    async def read_telemetry(self, keys: Optional[list[str]] = None) -> TelemetrySnapshot:
        """Serve the scheduler's cache. Never causes a device request."""
        self._evaluate()
        snapshot = self._decode()
        if keys is None:
            return snapshot
        wanted = set(keys)
        return {k: v for k, v in snapshot.items() if k in wanted}

    # --- control -----------------------------------------------------------------------

    def _limits(self) -> tuple[int, int]:
        """What the device itself allows: its settings and hardware ceilings.

        The configured ones until the device reported its own. The user's limit
        is applied by the coordinator before apply_setpoint.
        """
        charge, discharge = self._capabilities.max_charge_power_w, self._capabilities.max_discharge_power_w
        if self._scheduler is not None:
            native_c, native_d = _native_limits(self._limit_values())
            charge = native_c or charge
            discharge = native_d or discharge
        return charge, discharge

    def _limit_values(self) -> dict[int, Any]:
        if self._scheduler is None:
            return {}
        age = self._tier_max_age(LIMITS_TIER)
        return {p: self._fresh_value(p, age) for p in LIMITS_TIER.points}

    def _hardware_ceilings(self) -> tuple[int, int]:
        """The power maxima of this battery (114/115), for the number entities."""
        charge, discharge = _hardware_limits(self._limit_values())
        return (charge or self._capabilities.max_charge_power_w or DEFAULT_MAX_POWER_W,
                discharge or self._capabilities.max_discharge_power_w or DEFAULT_MAX_POWER_W)

    def _setting_device_value(self, point: int) -> float | None:
        if self._scheduler is None:
            return None
        return _num(self._fresh_value(point, self._tier_max_age(LIMITS_TIER)), 0, MAX_REALTIME_POWER_W)

    def _expedite_limits(self) -> None:
        if self._scheduler is not None:
            self._scheduler.expedite("limits")

    def _floor(self) -> int:
        native = _num(self._fresh_value(P_NATIVE_MIN_SOC, math.inf), 0, 100)
        return max(MIN_TARGET_SOC, math.ceil(native) if native is not None else MIN_TARGET_SOC)

    def _tuple_for(self, net_power_w: int, soc: float) -> tuple[tuple[int, int, int], int]:
        """Map a signed net power to the realtime tuple and the net actually applied."""
        charge_max, discharge_max = self._limits()
        if net_power_w > 0:
            power = min(int(net_power_w), charge_max)
            if power > 0:
                return (ACTION_CHARGE, power, min(100, int(soc) + TARGET_STEP_PCT)), power
        elif net_power_w < 0:
            power = min(-int(net_power_w), discharge_max)
            if power > 0:
                floor = self._floor()
                return (ACTION_DISCHARGE, power, max(floor, math.ceil(soc) - TARGET_STEP_PCT)), -power
        return STOP_TUPLE, 0

    def _send(self, values: tuple[int, int, int], net: int, *, force: bool = False) -> None:
        if values != self._command:
            self._resend_done = False
            self._command_accepted_at = None
        self._command = values
        self._command_net = net
        assert self._scheduler is not None
        if not self._scheduler.queue_write(values, force=force) and values == self._scheduler.last_accepted:
            # The device already holds this tuple.
            self._command_accepted_at = self._command_accepted_at or self._clock()

    async def apply_setpoint(self, net_power_w: int, *, mode_hint: Optional[str] = None,
                             read_back: bool = True) -> SetpointResult:
        """Queue the tuple for the next slot and return at once (never blocks a slot)."""
        self._evaluate()
        if self._state is not ControlState.OWNED or self._scheduler is None:
            if int(net_power_w) == 0:
                # Nothing to stop: while not in control Omnibattery runs no
                # command on the battery, so idle is already true from its
                # side. Nothing is sent. The core's manual-control handoff
                # needs this to succeed.
                return SetpointResult(True, 0, False,
                                      applied={"control_status": self.control_status})
            return SetpointResult(False, 0, False, failure_reason=self.control_status)
        soc = self._soc()
        if soc is None:
            return SetpointResult(False, 0, False, failure_reason="telemetry_stale")
        values, applied = self._tuple_for(int(net_power_w), soc)
        self._send(values, applied)
        return SetpointResult(True, applied, False, applied={"control_status": self.control_status})

    def net_power_from_data(self, data: dict) -> Optional[int]:
        """The commanded net power while owned; None makes the core write again."""
        if self._state is not ControlState.OWNED:
            return None
        return self._command_net

    def dynamic_discharge_limit_w(self, data: dict) -> Optional[int]:
        """No discharge headroom while control is not owned."""
        return None if self._state is ControlState.OWNED else 0

    def dynamic_charge_limit_w(self, data: dict) -> Optional[int]:
        """No charge headroom while control is not owned."""
        return None if self._state is ControlState.OWNED else 0

    async def standby(self) -> bool:
        """Send STOP and wait for the device to accept it (used on unload)."""
        if self._scheduler is None or self._state not in (ControlState.OWNED, ControlState.ACQUIRING):
            return True
        self._command, self._command_net = STOP_TUPLE, 0
        timeout = self._scheduler.interval_s * 2 + REQUEST_TIMEOUT_S
        return await self._scheduler.write_now(STOP_TUPLE, timeout)

    async def write_control(self, key: str, value: int) -> bool:
        """A number entity changed a device setting: a direct user action.

        Hands it to the settings sync (debounced, compared, budgeted); True
        means it will be applied. Only the three settings are writable.
        """
        if key not in SETTING_BY_KEY:
            return False
        return self._settings.request(key, int(value), origin="entity")

    async def apply_config(self, **kwargs) -> bool:
        """Setup passes the battery config; only an options-flow change is written.

        Max SOC has no device setting and stays software-only.
        """
        self._settings.apply_config(kwargs)
        return True

    async def set_charge_cutoff(self, soc_pct: float) -> bool:
        # No max-SOC setting on the device: weekly full charge stays in software.
        return False

    async def set_rs485_control(self, enable: bool) -> bool:
        return False

    async def get_rs485_control(self) -> None:
        return None

    # --- the state machine ---------------------------------------------------------------

    def _on_telemetry(self, updated: frozenset[int]) -> None:
        if (
            self._state is ControlState.ACQUIRING
            and self._stop_accepted_at is not None
            and P_RT_STATUS in updated
        ):
            self._handover_reads += 1
        if any(s.read_point in updated for s in SETTINGS):
            self._settings.on_telemetry()
        self._evaluate(updated)

    def _on_write(self, values: tuple[int, int, int], outcome: str) -> None:
        assert self._scheduler is not None
        if outcome == WRITE_ACCEPTED:
            if self._state is ControlState.ACQUIRING and values == STOP_TUPLE:
                self._stop_accepted_at = self._clock()
                self._handover_reads = 0
            if values == self._command:
                self._command_accepted_at = self._clock()
            # Confirm through the echo in the next read slot.
            self._scheduler.expedite("echo")
            return
        if self._state is ControlState.ACQUIRING and values == STOP_TUPLE:
            self._handover_failed()
        elif values == self._command:
            # Outcome unknown: let the core's next cycle write again.
            self._command_net = None

    def _pause(self, reason: str) -> None:
        if self._state is ControlState.PAUSED and self._reason == reason:
            return
        was_controlling = self._state in (ControlState.OWNED, ControlState.ACQUIRING)
        self._state, self._reason = ControlState.PAUSED, reason
        _LOGGER.warning("[%s] Indevolt control paused: %s", self._name, reason)
        if self._scheduler is not None:
            self._scheduler.cancel_write()
            self._scheduler.forget_last_accepted()
            self._scheduler.set_tier_interval("echo", ECHO_INTERVAL_IDLE_S)
            if reason == "telemetry_stale" and was_controlling:
                # Best effort: the device may still take writes. A failed STOP
                # leaves the rolling target as the backstop.
                self._scheduler.queue_write(STOP_TUPLE, force=True)
        self._command = None
        self._command_net = None
        if self._notifier is not None:
            self._notifier.paused(reason, model=self._config.get("type"))

    def _start_acquire(self) -> None:
        assert self._scheduler is not None
        _LOGGER.info("[%s] Indevolt in Realtime mode; sending STOP to take control", self._name)
        self._state, self._reason = ControlState.ACQUIRING, None
        self._stop_accepted_at = None
        self._handover_reads = 0
        self._command, self._command_net = STOP_TUPLE, 0
        self._scheduler.set_tier_interval("echo", 0.0)
        self._scheduler.queue_write(STOP_TUPLE, force=True)

    def _handover_failed(self) -> None:
        delay = HANDOVER_RETRY_S[min(self._handover_failures, len(HANDOVER_RETRY_S) - 1)]
        self._handover_failures += 1
        self._next_acquire_at = self._clock() + delay
        self._pause("handover_failed")

    def _echo(self) -> tuple[int, int, int] | None:
        """The device's realtime state (action, power, target) if read recently."""
        age = self._tier_max_age(ECHO_TIER)
        status = _num(self._fresh_value(P_RT_STATUS, age))
        if status is None or int(status) not in _STATUS_ACTION:
            return None
        power = _num(self._fresh_value(P_RT_POWER, age), 0, MAX_REALTIME_POWER_W) or 0
        target = _num(self._fresh_value(P_RT_TARGET, age), 0, 100) or 0
        return _STATUS_ACTION[int(status)], int(power), int(target)

    def _evaluate(self, updated: frozenset[int] = frozenset()) -> None:
        if self._scheduler is None:
            return
        self._check_advisories()
        if not self._control_enabled:
            self._state, self._reason = ControlState.MONITORING, None
            return
        if self._model is None:
            if self._config:
                self._pause("unknown_model")
            return
        if not self._control_fresh():
            if self._state is not ControlState.MONITORING:
                self._pause("telemetry_stale")
            return
        mode = _num(self._fresh_value(P_ENERGY_MODE))
        if mode != ENERGY_MODE_REALTIME:
            self._pause("realtime_mode_required")
            return
        if self._state in (ControlState.MONITORING, ControlState.PAUSED):
            if self._clock() >= self._next_acquire_at:
                self._start_acquire()
            return
        if self._state is ControlState.ACQUIRING:
            self._check_handover()
            return
        self._maintain_command(updated)

    def _check_handover(self) -> None:
        if self._stop_accepted_at is None or self._handover_reads == 0:
            return
        echo = self._echo()
        delivered = _num(self._fresh_value(P_AC_TOTAL)), _num(self._fresh_value(P_BYPASS))
        battery = _num(self._fresh_value(P_BATTERY_POWER))
        quiet = (battery is not None and abs(battery) <= IDLE_TOLERANCE_W) or (
            None not in delivered and abs(delivered[0] - delivered[1]) <= IDLE_TOLERANCE_W  # type: ignore[operator]
        )
        if echo is not None and echo[0] == ACTION_STOP and quiet:
            assert self._scheduler is not None
            _LOGGER.info("[%s] Indevolt confirmed standby; control active", self._name)
            self._state, self._reason = ControlState.OWNED, None
            self._handover_failures = 0
            self._command, self._command_net = STOP_TUPLE, 0
            self._command_accepted_at = self._stop_accepted_at
            self._scheduler.set_tier_interval("echo", ECHO_INTERVAL_OWNED_S)
            if self._notifier is not None:
                self._notifier.resumed()
        elif self._handover_reads >= HANDOVER_MAX_READS:
            _LOGGER.warning("[%s] Indevolt did not confirm standby after STOP (echo %s)",
                            self._name, echo)
            self._handover_failed()

    def _maintain_command(self, updated: frozenset[int]) -> None:
        """Rolling target refresh and the once-per-episode standby re-send."""
        command, net = self._command, self._command_net
        if command is None or net is None or command[0] == ACTION_STOP:
            return
        soc = self._soc()
        if soc is None:
            return
        action, _, target = command
        if action == ACTION_CHARGE:
            near = target < 100 and soc >= target - REFRESH_MARGIN_PCT
        else:
            near = target > self._floor() and soc <= target + REFRESH_MARGIN_PCT
        if near:
            # Same action and power, new target. Not core-driven on purpose:
            # under a steady load the core sends nothing new for hours.
            values, applied = self._tuple_for(net, soc)
            if values != command:
                _LOGGER.debug("[%s] Indevolt target SOC refresh %s -> %s", self._name, command, values)
                self._send(values, applied)
            return
        if P_RT_STATUS not in updated or self._resend_done or self._command_accepted_at is None:
            return
        echo = self._echo()
        echo_age = self._scheduler.age(P_RT_STATUS) if self._scheduler else None
        observed_after_start = (
            echo_age is not None
            and self._clock() - echo_age > self._command_accepted_at + START_GRACE_S
        )
        if echo is not None and echo[0] == ACTION_STOP and observed_after_start:
            # The device dropped to standby although a move is commanded (BMS
            # SOC jump, a reboot). Re-send once, with a fresh target; if it
            # stays in standby, the core's non-delivery handling takes over.
            _LOGGER.info("[%s] Indevolt in standby during %s; re-sending once", self._name, command)
            values, applied = self._tuple_for(net, soc)
            self._send(values, applied, force=True)
            self._resend_done = True

    def _check_advisories(self) -> None:
        assert self._scheduler is not None
        if self._notifier is None:
            return
        throttled = (
            self._scheduler.throttled_for_s >= THROTTLE_NOTIFY_AFTER_S
            or self._scheduler.consecutive_failures >= 6
        )
        if throttled and not self._throttle_notified:
            self._throttle_notified = True
            self._notifier.throttled(self._scheduler.interval_s)
        elif not throttled and self._scheduler.backoff_factor == 1 and self._throttle_notified:
            self._throttle_notified = False
            self._notifier.throttle_cleared()


def _lowest(values: dict[int, Any], points: tuple[int, ...]) -> int:
    found = [_num(values.get(p), 1, MAX_REALTIME_POWER_W) for p in points]
    known = [int(v) for v in found if v is not None]
    return min(known) if known else 0


def _native_limits(values: dict[int, Any]) -> tuple[int, int]:
    """What the device allows (charge, discharge): its settings and hardware; 0 = unknown.

    The feed-in limit (11010) is deliberately not a discharge limit: it caps
    export to the grid, not what the battery delivers to the house.
    """
    return (_lowest(values, (MAX_CHARGE.read_point, P_HW_MAX_CHARGE)),
            _lowest(values, (MAX_DISCHARGE.read_point, 11032, P_HW_MAX_DISCHARGE)))


def _hardware_limits(values: dict[int, Any]) -> tuple[int, int]:
    """The hardware ceilings (114/115); 0 = unknown."""
    return _lowest(values, (P_HW_MAX_CHARGE,)), _lowest(values, (P_HW_MAX_DISCHARGE,))
