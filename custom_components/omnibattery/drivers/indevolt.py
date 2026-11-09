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
    REQUEST_SPEED_SLOT_S,
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
    normal_slot,
    pacing_levels,
)
from ..infra.mac_tracking import normalise_mac
from .indevolt_settings import BY_KEY as SETTING_BY_KEY
from .indevolt_settings import (
    FLASH_WRITES_ISSUE,
    MAX_CHARGE,
    MAX_DISCHARGE,
    MIN_SOC,
    SETTINGS,
    IndevoltSettings,
)
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
P_MODEL_CODE = 1  # the model code, e.g. "YZ-E3000" (see MODELS)

P_OPERATING_STATUS = 11006  # what the device is doing (see _OPERATING_STATUS)
# The device's own forced full charge, set in the Indevolt app (read only here):
# every this many days (0 = off), at this time (high byte hour, low byte minute).
P_FULL_CHARGE_INTERVAL = 8646
P_FULL_CHARGE_START = 8647

ENERGY_MODE_REALTIME = 4

# Read tiers. The control tier goes out in every read slot; the others ride
# along when due (see IndevoltScheduler).
CONTROL_TIER = Tier("control", (P_BATTERY_POWER, P_AC_TOTAL, P_BYPASS, P_ENERGY_MODE, P_SOC))
ECHO_TIER = Tier("echo", (P_RT_STATUS, P_RT_TARGET, P_RT_POWER, P_BATTERY_STATE,
                          P_OPERATING_STATUS), 30.0)
# Battery temperatures: the battery built into the inverter unit, then up to
# five expansion batteries. An expansion battery exists when its serial number
# point is non-empty (as in the official integration). 11005 is the transformer
# (inverter) temperature.
P_INVERTER_BATTERY_TEMPERATURE = 9012
PACKS = (  # (serial point, temperature point)
    (9032, 9030), (9051, 9049), (9070, 9068), (9165, 9163), (9218, 9216),
)
P_TRANSFORMER_TEMPERATURE = 11005
P_FEED_IN_LIMIT = 11010
P_GRID_CHARGING = 2618  # 1000 off, 1001 on; set in the Indevolt app, read only here
P_DEVICE_MODE = 606  # 1000 cluster main, 1001 cluster sub, 1002 standalone
# Per PV input 1-4. The power points also make up the Solar Power sum.
P_PV_POWER = (1664, 1665, 1666, 1667)
P_PV_VOLTAGE = (1600, 1601, 1602, 1603)
P_PV_CURRENT = (1632, 1633, 1634, 1635)
P_NO_REMAINING_TIME = 9999  # what 11019/11020 report when they do not apply

# Read-only parity with the official Indevolt integration, so its dashboards
# and Energy dashboard entries can move here: AC input/output power, PV output,
# and the AC-side energy counters (the battery-side ones are 6004-6007).
# (key, point) pairs; the values are published as the device reports them, except
# the two Wh counters, which become kWh like every other energy sensor here.
# 11016 is the grid power from the meter the battery itself is paired with. It
# follows the P1 meter with the same sign (+ import) but trails it by 10-20s,
# hence a name that does not suggest the grid meter.
POWER_POINTS = (("ac_input_power", 2101), ("ac_output_power", 2108),
                ("indevolt_meter_power", 11016), ("solar_output_power", 1501))
ENERGY_POINTS = (("total_ac_input_energy", 2107), ("total_ac_output_energy", 2104),
                 ("off_grid_output_energy", 2105), ("daily_solar_energy", 1502))
WH_ENERGY_POINTS = (("bypass_input_energy", 11034), ("total_solar_energy", 1505))
GRID_POINTS = (("grid_voltage", 2600), ("grid_frequency", 2612))
REMAINING_TIME_POINTS = (("remaining_charging_time", 11019), ("remaining_discharging_time", 11020))
P_METER_CONNECTED = 7120  # 1000 connected, 1001 not connected

# Details per battery: the one in the inverter unit, then each expansion
# battery (in PACKS order). Only the batteries that are present are read. On a
# CMS-SF2000 voltage x current per battery adds up to the battery power (6000),
# although the voltage reads about 6.5 V.
INVERTER_BATTERY_DETAILS = {"soc": 9000, "cycles": 9003, "mos_temperature": 11042,
                            "voltage": 9004, "current": 9013}
P_INVERTER_BATTERY_SERIAL = 9008
# Each battery is two LFP cells in series (its voltage above is their sum):
# the inverter unit's cell points, then each expansion battery's (PACKS order).
# They read 3.34–3.38V at rest when full.
INVERTER_BATTERY_CELLS = (9009, 9011)
PACK_CELLS = ((9021, 9023), (9040, 9042), (9059, 9061), (9154, 9156), (9219, 9222))
PACK_DETAILS = (
    {"soc": 9016, "cycles": 9019, "mos_temperature": 9085, "voltage": 9020, "current": 19173},
    {"soc": 9035, "cycles": 9038, "mos_temperature": 9101, "voltage": 9039, "current": 19174},
    {"soc": 9054, "cycles": 9057, "mos_temperature": 9117, "voltage": 9058, "current": 19175},
    {"soc": 9149, "cycles": 9152, "mos_temperature": 9133, "voltage": 9153, "current": 19176},
    {"soc": 9202, "cycles": 9205, "mos_temperature": 9270, "voltage": 9206, "current": 19177},
)
# (field, name, unit, device class, state class, precision, low, high)
BATTERY_DETAIL_FIELDS = (
    ("soc", "SOC", "%", None, "measurement", 0, 0, 100),
    ("cycles", "Cycles", None, None, "total_increasing", 0, 0, 1e6),
    ("mos_temperature", "MOS Temperature", "°C", "temperature", "measurement", 1, -40, 150),
    ("voltage", "Voltage", "V", "voltage", "measurement", 2, 0, 1000),
    ("current", "Current", "A", "current", "measurement", 2, -1000, 1000),
)

LIMITS_TIER = Tier(
    "limits",
    (11009, 11011, P_FEED_IN_LIMIT, 11032, 114, 115, P_GRID_CHARGING,
     P_NATIVE_MIN_SOC, P_DEVICE_MODE, 142, P_FULL_CHARGE_INTERVAL, P_FULL_CHARGE_START,
     *(serial for serial, _ in PACKS)),
    180.0,
)
# The dashboard power points. The device itself refreshes them every 5-16s,
# so reading them more often gains nothing.
POWER_TIER = Tier("power", tuple(point for _, point in POWER_POINTS), 10.0)
SLOW_TIER = Tier(
    "slow",
    (6006, 6007, 6004, 6005, P_INVERTER_BATTERY_TEMPERATURE, *(temp for _, temp in PACKS),
     P_TRANSFORMER_TEMPERATURE, *P_PV_POWER, 2275,
     *(point for _, point in ENERGY_POINTS + WH_ENERGY_POINTS + GRID_POINTS + REMAINING_TIME_POINTS),
     P_METER_CONNECTED, *P_PV_VOLTAGE, *P_PV_CURRENT),
    60.0,
)
# Starts with the inverter-unit battery; the expansion batteries are added once
# their serial numbers (LIMITS_TIER) show which are present (see _sync_pack_points).
PACKS_TIER = Tier("packs", (*INVERTER_BATTERY_DETAILS.values(), *INVERTER_BATTERY_CELLS,
                            P_INVERTER_BATTERY_SERIAL), 120.0)
TIERS = (CONTROL_TIER, ECHO_TIER, POWER_TIER, LIMITS_TIER, SLOW_TIER, PACKS_TIER)
ECHO_INTERVAL_OWNED_S = 30.0
ECHO_INTERVAL_IDLE_S = 60.0

# --- control policy ---------------------------------------------------------------
TARGET_STEP_PCT = 5  # target SOC = current SOC ± this
REFRESH_MARGIN_PCT = 2  # refresh the target when SOC is this close to it
# The realtime echo (6107–6109) trails an accepted STOP by 9–19s, so the
# handover waits this long, not a number of reads: at the 2s slot six reads
# take only 11s, too short for a battery that obeys. At least this many echo
# reads must have come in before it gives up, so a gap in the reads alone
# cannot fail it.
HANDOVER_CONFIRM_S = 30.0
HANDOVER_MIN_READS = 2
HANDOVER_RETRY_S = (60.0, 120.0, 300.0, 600.0)
IDLE_TOLERANCE_W = 100
RAMP_S = 2.0
# A standby SF2000 takes 10–15s to start charging or discharging after a
# realtime command, and its echo reports standby until then. Standby within
# this long of an accepted command is the start, not a device that dropped out.
START_GRACE_S = 30.0
# Near full the BMS tapers the charge on its own (its constant-voltage phase),
# down to about 200W while the command stays at the maximum. The core counts a
# charge as delivered from 10% of the command, so the driver reports what the
# battery takes as its live charge limit (dynamic_charge_limit_w), and the
# core commands that. A shortfall counts once the battery moves and has stayed
# more than TAPER_SHORTFALL_W short of its command for TAPER_CONFIRM_S without
# its power rising by more than TAPER_STEP_W: a battery whose power still rises
# follows its command, only later than the PD raises it. TAPER_CONFIRM_S is
# longer than an SF2000 needs to ramp to a new command once it moves (4–8s),
# and short enough that a start into a tapering battery (it shows power ~16s
# after the command) is limited before the core's 30s engage grace and the
# cycles after it run out. A battery that does not move at all is a stall, left
# to the core. The limit is what the battery takes plus TAPER_HEADROOM_W, in
# TAPER_STEP_W steps, so the shortfall stays below the 150W at which the core's
# anti-windup re-anchors the PD. The limit goes back up once the battery takes
# within TAPER_AT_LIMIT_W of it: an SF2000 that takes all it is allowed holds
# its command within a few watts. The limit sits 50–100W above what a tapering
# battery takes, so its reading has to rise more than 30W to count, more than
# the ~30W a tapering SF2000's readings jump now and then.
TAPER_CONFIRM_S = 10.0
TAPER_SHORTFALL_W = 100
TAPER_HEADROOM_W = 100
TAPER_STEP_W = 50
TAPER_AT_LIMIT_W = 20
THROTTLE_NOTIFY_AFTER_S = 300.0
DEFAULT_MAX_POWER_W = 2400

_STATUS_ACTION = {0: ACTION_STOP, 1: ACTION_CHARGE, 2: ACTION_DISCHARGE,
                  1000: ACTION_STOP, 1001: ACTION_CHARGE, 1002: ACTION_DISCHARGE}
# 6001 battery state -> the core's inverter_state convention (1 standby,
# 2 charging, 3 discharging).
_BATTERY_STATE = {1000: 1, 1001: 2, 1002: 3}
# 6107 as the Realtime Command sensor: the state is a key, its label comes from
# the translations. Both status families map (see _STATUS_ACTION).
_REALTIME_COMMAND = {0: "standby", 1: "charging", 2: "discharging",
                     1000: "standby", 1001: "charging", 1002: "discharging"}


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
    min_firmware: str | None  # vendor minimum CMS firmware (f_ver); None: none published
    generation: int = 2  # Indevolt's API generation; only Gen 2 is controlled


# Indevolt identifies the model by its model code (GetData point 1), which every
# firmware reports; the device type in Sys.GetConfig is an internal identifier (a
# SolidFlex 3000 AC reports CMS-SF2000). A code names a family: the exact product
# (SolidFlex or PowerFlex; AC, Hybrid, Pro or Eco) cannot be read locally. The
# BK1600 uses the Gen-1 API; it and any unknown code are monitored only.
MODELS = {
    "YZ-E2000G2-SE": _Model("SolidFlex/PowerFlex 1200", "V1407.07.202E"),
    "YZ-E2000G2": _Model("SolidFlex/PowerFlex 2000", "V1406.07.002E"),
    "YZ-E3000": _Model("SolidFlex/PowerFlex 3000", "V1409.08.3034"),
    "YZ-E3600": _Model("SolidFlex 3600", None),
    "YZ-E1600": _Model("BK1600", None, generation=1),
}


def model_for(model_code: object) -> _Model | None:
    return MODELS.get(model_code.strip()) if isinstance(model_code, str) else None


def model_label_for(model_code: object) -> str | None:
    """The family name for a model code; an unknown code is shown as reported."""
    if not isinstance(model_code, str) or not model_code.strip():
        return None
    model = model_for(model_code)
    return model.label if model is not None else model_code.strip()

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
                states={1: "Standby", 2: "Charge", 3: "Discharge"}),
    _text("control_status", "Control Status", icon="mdi:remote", scan="medium"),
    # How the device keeps up with the pacing: the interval grows when the
    # scheduler backs off, the latencies are the 95th percentile of the last
    # 200 reads and of the last 200 writes (kept apart: reads take tens of ms,
    # writes hundreds), and failures count timeouts and errors since start.
    _definition("request_interval", "Request Interval (Effective)", unit="s", device_class="duration",
                scan="medium", diagnostic=True, icon="mdi:timer-sand"),
    # The request speed chosen in the connection step, next to the interval it
    # actually gets, so the choice shows on the device page. The state is the
    # speed key; its label comes from the translations (entity.sensor.state).
    _text("request_speed", "Request Speed", icon="mdi:speedometer", scan="medium"),
    _definition("read_latency", "Read Latency (P95)", unit="ms", device_class="duration",
                scan="medium", diagnostic=True, icon="mdi:timer-outline"),
    _definition("write_latency", "Write Latency (P95)", unit="ms", device_class="duration",
                scan="medium", diagnostic=True, icon="mdi:timer-cog-outline"),
    _definition("request_failures", "Request Failures", state_class="total_increasing",
                scan="medium", diagnostic=True, icon="mdi:alert-circle-outline"),
    # Every write of the three settings that may have reached the battery's
    # flash, ever (persisted; see YEARLY_ALLOWANCE in indevolt_settings).
    _definition("flash_writes", "Flash Writes", state_class="total_increasing", scan="medium",
                diagnostic=True, icon="mdi:chip"),
    _definition("realtime_power", "Realtime Command Power", unit="W", device_class="power",
                scan="low", enabled=False, diagnostic=True),
    _definition("realtime_target_soc", "Realtime Target SOC", unit="%", scan="low",
                enabled=False, diagnostic=True),
    _definition("inverter_ac_power", "Inverter AC Power", unit="W", device_class="power",
                scan="low", enabled=False, diagnostic=True),
    # Read only: the device setting is changed in the Indevolt app.
    _definition("grid_charging_allowed", "Grid Charging", state_class=None, scan="low",
                diagnostic=True, states={1000: "Off", 1001: "On"}),
    _text("software_version", "Software Version", icon="mdi:ticket-confirmation-outline",
          scan="very_low"),
    # From Sys.GetConfig, like the software version; the panel's device information shows it.
    _text("mac_address", "MAC Address", icon="mdi:ethernet", scan="very_low"),
    # Read-only parity with the official integration (see POWER_POINTS).
    _definition("ac_input_power", "AC Input Power", unit="W", device_class="power"),
    _definition("ac_output_power", "AC Output Power", unit="W", device_class="power"),
    _definition("bypass_power", "Bypass Power", unit="W", device_class="power"),
    _definition("indevolt_meter_power", "Indevolt Meter Power", unit="W", device_class="power",
                enabled=False),
    _definition("solar_output_power", "Solar Output Power", unit="W", device_class="power"),
    _definition("total_ac_input_energy", "Total AC Input Energy", unit="kWh", device_class="energy",
                state_class="total_increasing", precision=2, scan="low"),
    _definition("total_ac_output_energy", "Total AC Output Energy", unit="kWh",
                device_class="energy", state_class="total_increasing", precision=2, scan="low"),
    _definition("off_grid_output_energy", "Off-Grid Output Energy", unit="kWh",
                device_class="energy", state_class="total_increasing", precision=2, scan="low"),
    _definition("bypass_input_energy", "Bypass Input Energy", unit="kWh", device_class="energy",
                state_class="total_increasing", precision=3, scan="low"),
    _definition("daily_solar_energy", "Daily Solar Energy", unit="kWh", device_class="energy",
                state_class="total_increasing", precision=2, scan="low"),
    _definition("total_solar_energy", "Total Solar Energy", unit="kWh", device_class="energy",
                state_class="total_increasing", precision=3, scan="low"),
    # 6107 as a key (standby/charging/discharging); the label is translated.
    _text("realtime_command", "Realtime Command", icon="mdi:swap-vertical", scan="medium"),
    # 11006 as a key (see _OPERATING_STATUS); the label is translated. It names
    # what the device does on its own, such as its low-battery recovery charge
    # or its forced full charge.
    _text("operating_status", "Operating Status", icon="mdi:information-outline", scan="medium"),
    # The device's own forced full charge (8646/8647), set in the Indevolt app.
    _definition("full_charge_interval", "Forced Full Charge Interval", unit="d",
                device_class="duration", state_class=None, scan="low", diagnostic=True,
                icon="mdi:battery-sync"),
    _text("full_charge_time", "Forced Full Charge Time", icon="mdi:clock-outline", scan="low"),
    # The highest and lowest cell of the batteries present. Not max_cell_voltage:
    # the core tapers and latches on that key, with another brand's thresholds.
    _definition("highest_cell_voltage", "Highest Cell Voltage", unit="V", device_class="voltage",
                precision=2, scan="low", enabled=False, diagnostic=True),
    _definition("lowest_cell_voltage", "Lowest Cell Voltage", unit="V", device_class="voltage",
                precision=2, scan="low", enabled=False, diagnostic=True),
    _definition("feed_in_limit", "Feed-in Limit", unit="W", device_class="power", state_class=None,
                scan="low", diagnostic=True, icon="mdi:transmission-tower-import"),
    *(_definition(f"pv{n}_power", f"PV{n} Power", unit="W", device_class="power", scan="low",
                  enabled=False) for n in range(1, len(P_PV_POWER) + 1)),
    *(_definition(f"pv{n}_voltage", f"PV{n} Voltage", unit="V", device_class="voltage",
                  precision=1, scan="low", enabled=False, diagnostic=True)
      for n in range(1, len(P_PV_VOLTAGE) + 1)),
    *(_definition(f"pv{n}_current", f"PV{n} Current", unit="A", device_class="current",
                  precision=2, scan="low", enabled=False, diagnostic=True)
      for n in range(1, len(P_PV_CURRENT) + 1)),
    _definition("grid_voltage", "Grid Voltage", unit="V", device_class="voltage", precision=1,
                scan="low", enabled=False, diagnostic=True),
    _definition("grid_frequency", "Grid Frequency", unit="Hz", device_class="frequency",
                precision=2, scan="low", enabled=False, diagnostic=True),
    _definition("remaining_charging_time", "Remaining Charging Time", unit="min",
                device_class="duration", scan="low", enabled=False),
    _definition("remaining_discharging_time", "Remaining Discharging Time", unit="min",
                device_class="duration", scan="low", enabled=False),
    # 606 as a key (cluster_main/cluster_sub/standalone); the label is translated.
    _text("device_mode", "Device Mode", icon="mdi:server-network", scan="very_low", enabled=False),
]
_DEVICE_MODE = {1000: "cluster_main", 1001: "cluster_sub", 1002: "standalone"}
# 11006 as the Operating Status sensor, the values Indevolt documents.
_OPERATING_STATUS = {
    1: "powered_off", 2: "sleep", 3: "starting", 4: "mppt_operating", 5: "current_limiting",
    6: "shutting_down", 7: "inverter_fault", 8: "standby", 9: "grid_charging",
    10: "grid_discharging", 11: "off_grid_charging", 12: "off_grid_discharging",
    13: "low_battery_charging", 14: "deep_sleep", 15: "scheduled_full_charge",
    16: "off_grid_deep_sleep",
}


def _battery_detail_definitions(prefix: str, label: str) -> list[dict]:
    """SOC, cycles, MOS temperature, voltage, current and serial of one battery."""
    return [
        *(_definition(f"{prefix}_{field}", f"{label} {name}", unit=unit, device_class=device_class,
                      state_class=state_class, precision=precision, scan="low", enabled=False,
                      diagnostic=True)
          for field, name, unit, device_class, state_class, precision, _, _ in BATTERY_DETAIL_FIELDS),
        _text(f"{prefix}_serial_number", f"{label} Serial Number", icon="mdi:barcode",
              scan="very_low", enabled=False),
    ]


INVERTER_BATTERY_DETAIL_DEFINITIONS = _battery_detail_definitions("inverter_battery",
                                                                  "Inverter Battery")
SENSOR_DEFINITIONS += INVERTER_BATTERY_DETAIL_DEFINITIONS

BINARY_SENSOR_DEFINITIONS = [
    {"key": "indevolt_meter_connected", "name": "Indevolt Meter Connected",
     "device_class": "connectivity", "category": "diagnostic", "scan_interval": "low",
     "enabled_by_default": False},
]

_HIGH_KEYS = ("battery_power", "ac_power", DELIVERED_AC_POWER_KEY, "bypass_power",
              *(key for key, _ in POWER_POINTS))
# The request speed a slot belongs to, for the Request Speed sensor.
_SPEED_FOR_SLOT = {slot: speed for speed, slot in REQUEST_SPEED_SLOT_S.items()}
_SETTING_KEYS = tuple(s.key for s in SETTINGS)
_MEDIUM_KEYS = ("battery_soc", "energy_mode", "inverter_state", "control_status",
                "request_interval", "request_speed", "read_latency", "write_latency",
                "request_failures", "flash_writes", "realtime_command", "operating_status",
                *_SETTING_KEYS)
# One temperature sensor per expansion battery that is present (see PACKS).
PACK_TEMPERATURE_DEFINITIONS = [
    _definition(f"expansion_battery_{n}_temperature", f"Expansion Battery {n} Temperature", unit="°C",
                device_class="temperature", precision=1, scan="low", diagnostic=True)
    for n in range(1, len(PACKS) + 1)
]
# The details of each expansion battery that is present (see PACK_DETAILS).
PACK_DETAIL_DEFINITIONS = [
    _battery_detail_definitions(f"expansion_battery_{n}", f"Expansion Battery {n}")
    for n in range(1, len(PACKS) + 1)
]

_LOW_KEYS = ("total_charging_energy", "total_discharging_energy", "total_daily_charging_energy",
             "total_daily_discharging_energy", "battery_total_energy", "internal_temperature",
             "inverter_battery_temperature", "transformer_temperature",
             "solar_power", "realtime_power", "realtime_target_soc", "inverter_ac_power",
             "grid_charging_allowed", "feed_in_limit", "indevolt_meter_connected",
             "full_charge_interval", "full_charge_time", "highest_cell_voltage",
             "lowest_cell_voltage",
             *(key for key, _ in ENERGY_POINTS + WH_ENERGY_POINTS + GRID_POINTS + REMAINING_TIME_POINTS),
             *(f"pv{n}_{kind}" for kind in ("power", "voltage", "current")
               for n in range(1, len(P_PV_POWER) + 1)),
             *(d["key"] for d in INVERTER_BATTERY_DETAIL_DEFINITIONS if d["scan_interval"] == "low"))
_VERY_LOW_KEYS = ("software_version", "mac_address", "device_mode",
                  *(d["key"] for d in INVERTER_BATTERY_DETAIL_DEFINITIONS
                    if d["scan_interval"] == "very_low"))


class Notifier(Protocol):
    def paused(self, reason: str, *, model: str | None = None) -> None: ...
    def resumed(self) -> None: ...
    def firmware_outdated(self, firmware: str, minimum: str) -> None: ...
    def setting_failed(self, key: str, value: int) -> None: ...
    def setting_ok(self, key: str) -> None: ...
    def flash_writes_paused(self, allowance: int) -> None: ...
    def flash_writes_resumed(self) -> None: ...
    def throttled(self, interval_s: float, *, slower_speed: str | None = None) -> None: ...
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
        wall_clock: Callable[[], float] = time.time,
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
        self._model_code: Any = None
        pace = pacing_levels(normal_slot(slot_s))[0]
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
            # A write waits at most one write interval, then the battery ramps;
            # the effect shows up in the read after that.
            actuator_latency_s=pace.write_s + RAMP_S,
            readback_latency_s=pace.write_s + pace.read_s + RAMP_S,
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
        # What the device holds (the last accepted tuple), and when it was last
        # told to move while it stood still: a standby battery state read
        # before START_GRACE_S after that means it is still starting (_decode).
        self._held: tuple[int, int, int] | None = None
        self._start_at: float | None = None
        self._start_echo_due = False
        # The charge limit while the BMS tapers (see TAPER_CONFIRM_S).
        self._taper_limit_w: int | None = None
        self._taper_raised_at: float | None = None
        self._shortfall_since: float | None = None
        self._shortfall_command = 0
        self._shortfall_delivered = 0.0
        self._operating_status: str | None = None
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
            wall_clock=wall_clock,
        )

    # --- identity ------------------------------------------------------------------

    @property
    def capabilities(self) -> DriverCapabilities:
        return self._capabilities

    @property
    def model_label(self) -> Optional[str]:
        return model_label_for(self._model_code)

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
        packs: dict[str, list[str]] = {}
        for definition in self._pack_definitions():
            packs.setdefault(definition["scan_interval"], []).append(definition["key"])
        return [*self._read_groups, *(ReadGroup(scan, tuple(keys)) for scan, keys in packs.items())]
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
    def binary_sensor_definitions(self) -> list[dict]: return BINARY_SENSOR_DEFINITIONS
    @property
    def button_definitions(self) -> list[dict]: return []
    @property
    def all_definitions(self) -> list[dict]:
        return self.sensor_definitions + self.number_definitions + self.binary_sensor_definitions

    def _present_packs(self) -> list[int]:
        """Numbers (1-5) of the expansion batteries that report a serial number."""
        if self._scheduler is None:
            return []
        return [n for n, (serial, _) in enumerate(PACKS, start=1)
                if self._scheduler.value(serial) not in (None, "", 0)]

    def _pack_definitions(self) -> list[dict]:
        present = self._present_packs()
        return ([PACK_TEMPERATURE_DEFINITIONS[n - 1] for n in present]
                + [d for n in present for d in PACK_DETAIL_DEFINITIONS[n - 1]])

    def _batteries(self) -> list[tuple[str, dict[str, int], int]]:
        """(key prefix, detail points, serial point) of each battery that is present."""
        return [("inverter_battery", INVERTER_BATTERY_DETAILS, P_INVERTER_BATTERY_SERIAL),
                *((f"expansion_battery_{n}", PACK_DETAILS[n - 1], PACKS[n - 1][0])
                  for n in self._present_packs())]

    def _cell_points(self) -> list[int]:
        """The cell voltage points of the batteries that are present."""
        return [*INVERTER_BATTERY_CELLS,
                *(p for n in self._present_packs() for p in PACK_CELLS[n - 1])]

    def _sync_pack_points(self) -> None:
        """Read the details of the batteries that are present, and only those."""
        if self._scheduler is None:
            return
        points = [p for _, details, _ in self._batteries() for p in details.values()]
        self._scheduler.set_tier_points(
            "packs", (*points, *self._cell_points(), P_INVERTER_BATTERY_SERIAL))

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
        self._config = {k: str(device[k]) for k in ("type", "sn", "fw", "f_ver", "mac") if k in device}
        _LOGGER.info("[%s] Indevolt %s, firmware %s / %s, request speed %s", self._name,
                     self._config.get("type"), self._config.get("f_ver"), self._config.get("fw"),
                     _SPEED_FOR_SLOT.get(self._client.slot_s, self._client.slot_s))
        # The model decides the name, control and the firmware check, so it is
        # read first, on its own: known before Home Assistant registers the
        # device and before the first control decision, however small the
        # battery's batches are.
        self._scheduler = IndevoltScheduler(
            self._client, TIERS, clock=self._clock, on_telemetry=self._on_telemetry,
            on_write=self._on_write, name=self._name, first_read=(P_MODEL_CODE,),
        )
        self._scheduler.set_tier_interval("echo", ECHO_INTERVAL_IDLE_S)
        self._scheduler.start()
        first = await self._scheduler.wait_first_read(
            2 * self._scheduler.read_interval_s + REQUEST_TIMEOUT_S + 1)
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
        self._held, self._start_at = None, None
        self._end_taper()

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
            points = [P_MODEL_CODE, *CONTROL_TIER.points, *LIMITS_TIER.points]
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
        values = {int(k): v for k, v in data.items() if k.isdigit()}
        charge, discharge = _hardware_limits(values)
        model = model_for(data.get(str(P_MODEL_CODE)))
        return {
            "type": dtype,
            "model": model_label_for(data.get(str(P_MODEL_CODE))),
            "known_model": model is not None and model.generation == 2,
            "serial": str(device.get("sn", "")),
            "firmware": str(device.get("f_ver") or device.get("fw") or ""),
            "mac": normalise_mac(device.get("mac")),
            "energy_mode": _num(data.get(str(P_ENERGY_MODE))),
            "realtime_mode": _num(data.get(str(P_ENERGY_MODE))) == ENERGY_MODE_REALTIME,
            "max_charge_power": charge,
            "max_discharge_power": discharge,
            "native_min_soc": _num(data.get(str(P_NATIVE_MIN_SOC)), 0, 100),
            "battery_capacity_kwh": _num(data.get("142"), 0, 1000),
            "settings": _setting_values(values),
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
        if bypass is not None:
            out["bypass_power"] = bypass
        soc = _num(c(P_SOC), 0, 100)
        if soc is not None:
            out["battery_soc"] = soc
        mode = _num(c(P_ENERGY_MODE))
        if mode is not None:
            out["energy_mode"] = int(mode)
        if out:
            out["control_status"] = self.control_status
        stats = self._scheduler.stats()
        out["request_interval"] = stats["interval_s"]
        out["request_speed"] = _SPEED_FOR_SLOT.get(self._client.slot_s)
        for key, stat in (("read_latency", "read_latency_p95_ms"),
                          ("write_latency", "write_latency_p95_ms")):
            if stats[stat] is not None:
                out[key] = stats[stat]
        out["request_failures"] = stats["timeouts"] + stats["errors"]
        out["flash_writes"] = self._settings.flash_writes

        echo_age = self._tier_max_age(ECHO_TIER)
        state = _num(c(P_BATTERY_STATE, echo_age))
        if state is not None and int(state) in _BATTERY_STATE:
            inverter_state = _BATTERY_STATE[int(state)]
            # Read before the battery could have started, standby says nothing
            # yet. None (not left out): the coordinator keeps a missing key.
            starting = inverter_state == 1 and self._read_before_start(P_BATTERY_STATE)
            out["inverter_state"] = None if starting else inverter_state
        operating = _num(c(P_OPERATING_STATUS, echo_age))
        if operating is not None and int(operating) in _OPERATING_STATUS:
            out["operating_status"] = _OPERATING_STATUS[int(operating)]
        rt_status = _num(c(P_RT_STATUS, echo_age))
        if rt_status is not None and int(rt_status) in _REALTIME_COMMAND:
            out["realtime_command"] = _REALTIME_COMMAND[int(rt_status)]
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
        grid = _num(limits[P_GRID_CHARGING])
        if grid is not None:
            out["grid_charging_allowed"] = int(grid)
        feed_in = _num(limits[P_FEED_IN_LIMIT], 0, 100000)
        if feed_in is not None:
            out["feed_in_limit"] = feed_in
        device_mode = _num(limits[P_DEVICE_MODE])  # the device sends it as a string
        if device_mode is not None and int(device_mode) in _DEVICE_MODE:
            out["device_mode"] = _DEVICE_MODE[int(device_mode)]
        interval = _num(limits[P_FULL_CHARGE_INTERVAL], 0, 60)
        if interval is not None:
            out["full_charge_interval"] = int(interval)
        start = _num(limits[P_FULL_CHARGE_START], 0, 0xFFFF)
        if start is not None and int(start) >> 8 < 24 and int(start) & 0xFF < 60:
            out["full_charge_time"] = f"{int(start) >> 8:02d}:{int(start) & 0xFF:02d}"

        power_age = self._tier_max_age(POWER_TIER)
        for key, point in POWER_POINTS:
            value = _num(c(point, power_age), -100000, 100000)
            if value is not None:
                out[key] = value

        slow_age = self._tier_max_age(SLOW_TIER)
        for key, point in (("total_charging_energy", 6006), ("total_discharging_energy", 6007),
                           ("total_daily_charging_energy", 6004),
                           ("total_daily_discharging_energy", 6005)):
            value = _num(c(point, slow_age), 0, 1e9)
            if value is not None:
                out[key] = value
        for key, point in ENERGY_POINTS:
            value = _num(c(point, slow_age), 0, 1e9)
            if value is not None:
                out[key] = value
        for key, point in WH_ENERGY_POINTS:
            value = _num(c(point, slow_age), 0, 1e12)
            if value is not None:
                out[key] = value / 1000
        for key, point in GRID_POINTS:
            value = _num(c(point, slow_age), 0, 1000)
            if value is not None:
                out[key] = value
        for key, point in REMAINING_TIME_POINTS:
            value = _num(c(point, slow_age), 0, P_NO_REMAINING_TIME - 1)
            if value is not None:
                out[key] = value
        meter = _num(c(P_METER_CONNECTED, slow_age))
        if meter in (1000, 1001):
            out["indevolt_meter_connected"] = meter == 1000
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
        pv = [_num(c(p, slow_age), 0, 10000) for p in P_PV_POWER]
        for n, value in enumerate(pv, start=1):
            if value is not None:
                out[f"pv{n}_power"] = value
        if all(v is not None for v in pv):
            out["solar_power"] = sum(pv)  # type: ignore[arg-type]
        for kind, points, high in (("voltage", P_PV_VOLTAGE, 1000), ("current", P_PV_CURRENT, 100)):
            for n, point in enumerate(points, start=1):
                value = _num(c(point, slow_age), 0, high)
                if value is not None:
                    out[f"pv{n}_{kind}"] = value
        self._decode_batteries(out, limits_age)
        inverter_ac = _num(c(2275, slow_age))
        if inverter_ac is not None:
            out["inverter_ac_power"] = inverter_ac
        if self.firmware and out:
            out["software_version"] = self.firmware
        mac = normalise_mac(self._config.get("mac"))
        if mac and out:
            out["mac_address"] = mac
        return out

    def _decode_batteries(self, out: dict[str, Any], limits_age: float) -> None:
        """The details of each battery that is present (see PACKS_TIER)."""
        age = self._tier_max_age(PACKS_TIER)
        cells = [_num(self._fresh_value(p, age), 1.0, 5.0) for p in self._cell_points()]
        cells = [v for v in cells if v is not None]
        if cells:
            out["highest_cell_voltage"] = max(cells)
            out["lowest_cell_voltage"] = min(cells)
        for prefix, details, serial_point in self._batteries():
            for field, _, _, _, _, _, low, high in BATTERY_DETAIL_FIELDS:
                value = _num(self._fresh_value(details[field], age), low, high)
                if value is not None:
                    out[f"{prefix}_{field}"] = value
            # The expansion battery serials are read with the limits.
            serial_age = age if serial_point == P_INVERTER_BATTERY_SERIAL else limits_age
            serial = self._fresh_value(serial_point, serial_age)
            if serial not in (None, "", 0):
                out[f"{prefix}_serial_number"] = str(serial).strip()

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
        """No charge headroom while control is not owned; near full, what the BMS takes."""
        if self._state is not ControlState.OWNED:
            return 0
        return self._taper_limit_w

    async def standby(self) -> bool:
        """Send STOP and wait for the device to accept it (used on unload)."""
        if self._scheduler is None or self._state not in (ControlState.OWNED, ControlState.ACQUIRING):
            return True
        self._command, self._command_net = STOP_TUPLE, 0
        timeout = self._scheduler.interval_s * 2 + REQUEST_TIMEOUT_S
        return await self._scheduler.write_now(STOP_TUPLE, timeout)

    async def write_control(self, key: str, value: int) -> bool:
        """A number entity changed a device setting: a direct user action.

        Hands it to the settings sync (debounced, compared, within the yearly
        allowance); True means it will be applied. Only the three settings are
        writable.
        """
        if key not in SETTING_BY_KEY:
            return False
        return self._settings.request(key, int(value), origin="entity")

    async def async_fix_issue(self, issue_id: str) -> bool:
        """The user turned setting writes back on in Repairs (see YEARLY_ALLOWANCE)."""
        if not issue_id.startswith(FLASH_WRITES_ISSUE):
            return False
        self._settings.resume_flash_writes()
        return True

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
        if any(serial in updated for serial, _ in PACKS):
            self._sync_pack_points()
        if P_OPERATING_STATUS in updated:
            self._log_operating_status()
        if P_MODEL_CODE in updated:
            self._set_model_code(self._scheduler.value(P_MODEL_CODE))
        self._evaluate(updated)

    def _set_model_code(self, code: Any) -> None:
        if code is None or code == self._model_code:
            return
        self._model_code, self._model = code, model_for(code)
        _LOGGER.info("[%s] Indevolt model code %s, shown as %s",
                     self._name, code, self.model_label)
        minimum = self._model.min_firmware if self._model is not None else None
        if minimum is not None and firmware_below(self._config.get("f_ver"), minimum):
            _LOGGER.warning("[%s] Indevolt firmware %s is older than the recommended %s",
                            self._name, self._config.get("f_ver"), minimum)
            if self._notifier is not None:
                self._notifier.firmware_outdated(self._config["f_ver"], minimum)

    def _log_operating_status(self) -> None:
        assert self._scheduler is not None
        raw = _num(self._scheduler.value(P_OPERATING_STATUS))
        status = _OPERATING_STATUS.get(int(raw)) if raw is not None else None
        if status is None:
            status = f"unknown ({self._scheduler.value(P_OPERATING_STATUS)})"
        if status != self._operating_status:
            if self._operating_status is not None:
                _LOGGER.info("[%s] Indevolt operating status %s -> %s",
                             self._name, self._operating_status, status)
            self._operating_status = status

    def _on_write(self, values: tuple[int, int, int], outcome: str) -> None:
        assert self._scheduler is not None
        if outcome == WRITE_ACCEPTED:
            now = self._clock()
            if self._state is ControlState.ACQUIRING and values == STOP_TUPLE:
                self._stop_accepted_at = now
                self._handover_reads = 0
            if values == self._command:
                self._command_accepted_at = now
            self._track_held(values, now)
            # Confirm through the echo in the next read slot.
            self._scheduler.expedite("echo")
            return
        # The device may or may not hold it now.
        self._held = None
        if self._state is ControlState.ACQUIRING and values == STOP_TUPLE:
            self._handover_failed()
        elif values == self._command:
            # Outcome unknown: let the core's next cycle write again.
            self._command_net = None

    def _track_held(self, values: tuple[int, int, int], now: float) -> None:
        """Note what the device holds now: starts, charges and raised powers."""
        previous, self._held = self._held, values
        action, power, _ = values
        if action == ACTION_STOP:
            self._start_at = None
        elif not self._moving():
            # Told to move while it stands still: it starts now.
            self._start_at, self._start_echo_due = now, True
        charging = previous is not None and previous[0] == ACTION_CHARGE
        if action != ACTION_CHARGE:
            self._end_taper()
        elif not charging or power > previous[1]:
            self._taper_raised_at = now

    def _delivered(self) -> float | None:
        """The inverter's own AC power (2278 minus the bypass), + charging."""
        total, bypass = _num(self._fresh_value(P_AC_TOTAL)), _num(self._fresh_value(P_BYPASS))
        if total is None or bypass is None:
            return None
        return total - bypass

    def _moving(self) -> bool:
        delivered = self._delivered()
        return delivered is not None and abs(delivered) > IDLE_TOLERANCE_W

    def _read_before_start(self, point: int) -> bool:
        """True if ``point`` was read before a move just started could show in it."""
        if self._start_at is None or self._scheduler is None:
            return False
        age = self._scheduler.age(point)
        return age is not None and self._clock() - age < self._start_at + START_GRACE_S

    def _follow_taper(self, updated: frozenset[int]) -> None:
        """Set, follow and lift the charge limit while the BMS tapers the charge.

        Runs on each fresh AC power reading. ``commanded`` is what the device
        holds. The limit is set once the battery moves but stays short of its
        command for TAPER_CONFIRM_S. The clock restarts when the command at
        least doubles, a new step the battery has not had time for, and when
        the battery's power has risen by more than TAPER_STEP_W since, as it
        still follows its command. Then the limit only goes down while the
        battery stays short, and back up when it takes all it is allowed, until
        it is lifted at the battery's maximum.
        """
        held = self._held
        if P_AC_TOTAL not in updated or held is None or held[0] != ACTION_CHARGE:
            return
        delivered = self._delivered()
        if delivered is None:
            return
        now, commanded, limit = self._clock(), held[1], self._taper_limit_w
        short = commanded - delivered > TAPER_SHORTFALL_W
        if limit is not None:
            if delivered >= limit - TAPER_AT_LIMIT_W:
                self._set_taper_limit(max(limit + TAPER_HEADROOM_W, _step_down(limit * 3 / 2)),
                                      delivered, commanded)
            elif short and (self._taper_raised_at is None
                            or now - self._taper_raised_at >= TAPER_CONFIRM_S):
                self._set_taper_limit(min(limit, _step_down(delivered) + TAPER_HEADROOM_W),
                                      delivered, commanded)
            return
        if not short or delivered <= IDLE_TOLERANCE_W:
            self._shortfall_since = None
            return
        if (self._shortfall_since is None or commanded >= 2 * self._shortfall_command
                or delivered > self._shortfall_delivered + TAPER_STEP_W):
            self._shortfall_since = now
            self._shortfall_command, self._shortfall_delivered = commanded, delivered
            return
        if now - self._shortfall_since >= TAPER_CONFIRM_S:
            self._set_taper_limit(_step_down(delivered) + TAPER_HEADROOM_W, delivered, commanded)

    def _set_taper_limit(self, limit: int, delivered: float, commanded: int) -> None:
        charge_max, _ = self._limits()
        if limit >= charge_max:
            if self._taper_limit_w is not None:
                _LOGGER.info("[%s] Indevolt takes the full charge power again; charge limit lifted",
                             self._name)
            self._end_taper()
            return
        if limit == self._taper_limit_w:
            return
        if self._taper_limit_w is None:
            soc = self._soc()
            _LOGGER.info("[%s] Indevolt takes %.0fW of %dW at %s SOC while its BMS tapers the "
                         "charge; limiting the charge to %dW", self._name, delivered, commanded,
                         "unknown" if soc is None else f"{soc:.0f}%", limit)
        else:
            _LOGGER.debug("[%s] Indevolt charge limit %dW -> %dW (takes %.0fW)", self._name,
                          self._taper_limit_w, limit, delivered)
            if limit > self._taper_limit_w:
                self._taper_raised_at = self._clock()
        self._taper_limit_w = limit

    def _end_taper(self) -> None:
        self._taper_limit_w = None
        self._shortfall_since = None

    def _pause(self, reason: str) -> None:
        if self._state is ControlState.PAUSED and self._reason == reason:
            return
        was_controlling = self._state in (ControlState.OWNED, ControlState.ACQUIRING)
        self._state, self._reason = ControlState.PAUSED, reason
        if reason == "unknown_model":
            # The model code is what a new entry in MODELS needs.
            _LOGGER.warning("[%s] Indevolt control paused: unknown_model (model code %s)",
                            self._name, self._model_code)
        else:
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
        self._held, self._start_at = None, None
        self._end_taper()
        if self._notifier is not None:
            self._notifier.paused(
                reason, model=str(self._model_code) if self._model_code is not None else None)

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
        _LOGGER.info("[%s] Indevolt next attempt to take control in %.0fs", self._name, delay)

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
        if self._model is None or self._model.generation != 2:
            # Control waits for the model code's first read.
            if self._model_code is not None or self._scheduler.is_unsupported(P_MODEL_CODE):
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
        if self._start_echo_due and self._start_at is not None and (
            self._clock() >= self._start_at + START_GRACE_S
        ):
            # Read the battery state again as soon as the start had its time.
            self._start_echo_due = False
            self._scheduler.expedite("echo")
        self._follow_taper(updated)
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
        waited = self._clock() - self._stop_accepted_at
        if echo is not None and echo[0] == ACTION_STOP and quiet:
            assert self._scheduler is not None
            _LOGGER.info("[%s] Indevolt confirmed standby %.0fs after STOP; control active",
                         self._name, waited)
            self._state, self._reason = ControlState.OWNED, None
            self._handover_failures = 0
            self._command, self._command_net = STOP_TUPLE, 0
            self._command_accepted_at = self._stop_accepted_at
            self._scheduler.set_tier_interval("echo", ECHO_INTERVAL_OWNED_S)
            if self._notifier is not None:
                self._notifier.resumed()
        elif waited >= HANDOVER_CONFIRM_S and self._handover_reads >= HANDOVER_MIN_READS:
            _LOGGER.warning("[%s] Indevolt did not confirm standby %.0fs after STOP (echo %s)",
                            self._name, waited, echo)
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
            # Faster than Conservative the user chose the extra speed, so the text
            # names the speed one step slower as the lasting fix.
            self._notifier.throttled(
                self._scheduler.interval_s, slower_speed=_slower_speed(self._slot_s),
            )
        elif not throttled and self._scheduler.backoff_level == 0 and self._throttle_notified:
            self._throttle_notified = False
            self._notifier.throttle_cleared()


def _slower_speed(slot_s: float) -> str | None:
    """The request speed one step slower than ``slot_s``; None at Conservative."""
    slower = [(slot, speed) for speed, slot in REQUEST_SPEED_SLOT_S.items() if slot > slot_s]
    return min(slower)[1] if slower else None


def _step_down(power: float) -> int:
    """``power`` rounded down to a TAPER_STEP_W step, never below 0."""
    return max(0, int(power // TAPER_STEP_W) * TAPER_STEP_W)


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


def _setting_values(values: dict[int, Any]) -> dict[str, int]:
    """The battery's own reserve and power limits by config key, as the settings sync reads them.

    A driver adopts these on its first start without writing anything, so the
    config flow offers them as the defaults of a new battery.
    """
    found = {}
    for setting in SETTINGS:
        value = _num(values.get(setting.read_point), 0, MAX_REALTIME_POWER_W)
        if value is not None:
            found[setting.config_key] = int(round(value))
    return found


def _hardware_limits(values: dict[int, Any]) -> tuple[int, int]:
    """The hardware ceilings (114/115); 0 = unknown."""
    return _lowest(values, (P_HW_MAX_CHARGE,)), _lowest(values, (P_HW_MAX_DISCHARGE,))
