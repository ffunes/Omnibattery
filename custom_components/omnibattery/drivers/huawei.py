"""Huawei SUN2000 + LUNA2000 driver.

This driver is deliberately *split-transport*, unlike every other brand here:

* **Telemetry is read natively** over Modbus TCP (FC03) from the inverter, so
  the control loop sees fresh battery power and SOC at its own cadence. Going
  through the ``huawei_solar`` integration's entities instead would cap the
  feedback at its hardcoded 30 s coordinator interval — far too slow for the PD
  loop, which polls every 2 s.
* **Set-points take either of two paths**, chosen per battery. By default they
  go through the ``huawei_solar`` integration's services, which own the ordering
  and safety semantics of the four-register forcible sequence. Optionally this
  driver writes that same sequence itself (FC16, same registers, same order),
  which removes the dependency for control as well as for reading.

The practical consequence differs by path. On the service path ``huawei_solar``
must stay installed and hold the battery device, and both connections coexist
behind a Modbus proxy — the add-on exists precisely to fan one Modbus slave out
to several clients. On the direct path this driver needs only its own
connection, and ``standby()`` must release over that same path: a release sent
as a service call has no device to address there, and fails silently.

Sign conventions:
  Omnibattery net power: +charge / −discharge
  Huawei 37001 charge/discharge power: +charge / −discharge  → identical, no flip.

**A forcible command is a ceiling, not a request.** The inverter produces
exactly what it was told and curtails the rest of the array: measured with a
315 W charge standing, 288 W harvested from strings that made 5054 W six seconds
after the command ended. A forcible discharge is worse — it serves the house
from the battery and leaves the MPPT tracker down entirely, and the missing
production then reads as the deficit that asks for more discharge. So while the
strings carry voltage this driver commands nothing at all and leaves the
inverter to its own regulation, which is the better controller in daylight
anyway. **A Huawei battery is under Omnibattery's control after dark only.**

Idle semantics deserve a note, and they are the opposite of what a register
battery does. ``stop_forcible_charge`` hands control back to the inverter's own
working mode, which resumes self-consumption; a *held* zero would instead be a
forcible charge at 0 W, which the hardware does accept and sustain. That held
zero was the first implementation here and it was wrong twice over: a pinned
battery cannot absorb its own PV, so the inverter derated the array, and the
control layer means something else by idle — manual mode idles once and then
leaves the device alone, which left the battery frozen. ``apply_setpoint(0)``
therefore *releases*, and so does ``standby()``.

Register map and control behaviour verified on a SUN2000-8K-MAP0
(V200R024C00SPC110) with a LUNA2000 13.8 kWh behind an EMMA-A02.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..infra.huawei_modbus_client import (
    HuaweiModbusClient,
    decode_i16,
    decode_i32,
    decode_string,
    decode_u16,
    decode_u32,
)
from .base import (
    BatteryDriver,
    DriverCapabilities,
    ReadGroup,
    SetpointResult,
    TelemetrySnapshot,
)

_LOGGER = logging.getLogger(__name__)

_DOMAIN_HUAWEI_SOLAR = "huawei_solar"

# Forcible charge/discharge mode (register 47100).
_FORCIBLE_STOP = 0
_FORCIBLE_CHARGE = 1
_FORCIBLE_DISCHARGE = 2

# Control registers for the direct-write path. The write order is not free: the
# parameters go first and the mode register acts on the values already in place,
# which is why each sequence below ends on 47100 — and why the release sequence
# begins there instead.
_REG_FORCED_PERIOD = 47083             # u16, minutes
_REG_FORCIBLE_TARGET_MODE = 47246      # u16, 0 = by time
_REG_FORCIBLE_CHARGE_POWER = 47247     # u32, W
_REG_FORCIBLE_DISCHARGE_POWER = 47249  # u32, W
_REG_FORCIBLE_MODE = 47100             # u16, see the constants above
_REG_CHARGE_CUTOFF = 47081             # u16, tenths of a percent
_REG_DISCHARGE_CUTOFF = 47082          # u16, tenths of a percent
_TARGET_MODE_TIME = 0

# Slave ids worth trying when scanning. 1 is the factory default for a direct
# connection; the rest are what dongles and energy managers hand out. Kept short
# because each one costs a connection, and the inverter needs 1.5 s of silence
# after every handshake.
_SLAVE_ID_CANDIDATES = (1, 0, 2, 3, 4, 5, 6, 11, 16)

# Working mode (register 47086), StorageWorkingModesC.
_WORKING_MODE_LABELS = {
    0: "Adaptive",
    1: "Fixed charge/discharge",
    2: "Maximise self consumption",
    3: "Time of use (LG)",
    4: "Fully fed to grid",
    5: "Time of use (LUNA2000)",
}

# Storage running status (register 37000), StorageStatus. Deliberately worded
# like the Marstek register map so the battery panel reads the same across
# brands — it prints this label verbatim.
_STORAGE_STATUS_LABELS = {
    0: "Offline",
    1: "Standby",
    2: "Running",
    3: "Fault",
    4: "Sleep",
}
_STORAGE_STATUS_RUNNING = 2

# "Running" says the storage is alive, not which way the energy flows, so the
# panel header would sit on that one word all day. The direction comes from
# measured battery power instead.
#
# The deadband is not cosmetic: this inverter idles around +50 W (its own
# consumption, and what a held zero settles at), so comparing against 0 would
# label a standing battery "Charge" and flip the header on noise.
_STATE_DIRECTION_DEADBAND_W = 100

# Register 32003 bitfield.
_STATE3_OFF_GRID = 0b01
_STATE3_OFF_GRID_SWITCH_ENABLED = 0b10

# Envelope ceiling. The live per-model caps come from 37046/37048; this only
# bounds a malformed reading so it cannot inflate the PD limits.
_HW_MAX_POWER_W = 15000

# A forcible command carries a duration and expires on its own. Long enough that
# a re-issue is never racing the timer, short enough to act as a watchdog: if
# Omnibattery dies mid-command, the inverter returns to its own control within
# this window instead of staying latched.
_COMMAND_DURATION_MIN = 10

# Every set-point costs four serialised Modbus writes inside huawei_solar and
# the battery needs ~10 s to reach a new target, so a 2 s PD tick must not turn
# into a 2 s write rate. Re-issue only on a meaningful change, a direction
# change, or once the command is old enough to be worth refreshing.
_WRITE_DEADBAND_W = 250
# Just under the measured ramp: by then the previous command has essentially
# landed, and revising it earlier only moves a target the battery is still
# travelling towards.
_MIN_WRITE_INTERVAL_S = 20.0
_COMMAND_REFRESH_S = 240.0

# Documented ranges of the inverter's own cutoff registers. They are narrower
# than the SOC window Omnibattery lets the user pick, which is why the software
# enforces the window and these writes are only a best-effort backstop.
_CHARGE_CUTOFF_RANGE = (90.0, 100.0)      # register 47081
_DISCHARGE_CUTOFF_RANGE = (0.0, 20.0)     # register 47082

# Measured on hardware, sampled at 1 Hz with nothing else talking to the
# inverter: 19.7 s from register write to 90 % of a +1000 W charge set-point,
# 11.3 s to reverse from +1000 W to -1000 W. Charging from idle is the slow
# case, so this is declared against it, with margin. Telemetry itself is
# milliseconds away — the delay is the physical ramp, not the transport.
#
# An earlier 15 s here came from coarser sampling and was optimistic. The
# control layer starts judging non-delivery once this elapses, so a value below
# the real ramp reports a healthy battery as unresponsive.
_ACTUATOR_LATENCY_S = 25.0

# --- register blocks (all FC03 holding, all verified on hardware) ------------
# (start, count, scan_interval, {key: (offset, decoder, scale)})
_BLOCK_LIVE = (37000, 5, "high", {
    "inverter_state": (0, "u16", 1),
    "battery_power": (1, "i32", 1),
    "battery_voltage": (3, "u16", 0.1),
    "battery_soc": (4, "u16", 0.1),
})
_BLOCK_DAILY = (37015, 8, "low", {
    "total_daily_charging_energy": (0, "u32", 0.01),
    "total_daily_discharging_energy": (2, "u32", 0.01),
    "internal_temperature": (7, "i16", 0.1),
})
_BLOCK_LIMITS = (37046, 4, "low", {
    "max_charge_power": (0, "u32", 1),
    "max_discharge_power": (2, "u32", 1),
})
_BLOCK_TOTALS = (37066, 4, "low", {
    "total_charging_energy": (0, "u32", 0.01),
    "total_discharging_energy": (2, "u32", 0.01),
})
_BLOCK_CAPACITY = (37758, 2, "very_low", {
    # 37758 reports Wh, but every consumer of this key treats it as kWh
    # (charge-delay energy balance, predictive charging, stored-energy display).
    "battery_total_energy": (0, "u32", 0.001),
})
_BLOCK_PV = (32064, 18, "high", {
    # The panel reads the DC total under "solar_power"; "pv_power" is not a key
    # it knows, so this brand would show an empty solar card under that name.
    "solar_power": (0, "i32", 1),
    # Deliberately NOT published as "ac_power". The system aggregates read that
    # key as the battery's own AC port and add it to grid and external solar to
    # derive house consumption. On this hybrid, 32080 is the whole inverter's AC
    # output — PV included — so publishing it there counted the roof array twice
    # and inflated house consumption by the full PV production. Without the key
    # the aggregates fall back to -battery_power, which is what a battery with
    # no AC port of its own actually contributes.
    "inverter_ac_power": (16, "i32", 1),
})
# Per-string DC. Huawei publishes voltage and current separately, so the power
# the panel wants is derived below rather than read.
_BLOCK_STATE3 = (32003, 2, "medium", {
    "off_grid_state": (0, "u32", 1),
})
# SUN2000 lays its strings out as voltage/current pairs from 32016. The map goes
# to 24 strings; four is where the battery panel's MPPT card stops, so reading
# further would produce entities nothing displays.
_MAX_PV_STRINGS = 4
_BLOCK_STRINGS = (32016, _MAX_PV_STRINGS * 2, "high", {
    key: (offset, kind, scale)
    for index in range(1, _MAX_PV_STRINGS + 1)
    for key, offset, kind, scale in (
        (f"pv{index}_voltage", (index - 1) * 2, "i16", 0.1),
        (f"pv{index}_current", (index - 1) * 2 + 1, "i16", 0.01),
    )
})
_BLOCK_CONFIG = (47081, 7, "low", {
    "charging_cutoff_capacity": (0, "u16", 0.1),
    "discharging_cutoff_capacity": (1, "u16", 0.1),
    "user_work_mode": (5, "u16", 1),
    "charge_from_grid": (6, "u16", 1),
})
_BLOCK_FORCIBLE_MODE = (47100, 1, "medium", {
    "force_mode": (0, "u16", 1),
})
_BLOCK_FORCIBLE_POWER = (47246, 5, "medium", {
    "set_charge_power": (1, "u32", 1),
    "set_discharge_power": (3, "u32", 1),
})
# 30071 reports how many strings this inverter actually has, so a two-string
# model does not sprout entities for strings it does not own.
_BLOCK_STRING_COUNT = (30071, 1, "very_low", {"pv_string_count": (0, "u16", 1)})
_BLOCK_RATING = (30073, 4, "very_low", {
    "inverter_rated_power": (0, "u32", 1),
    # 30075 is the ceiling the inverter actually enforces on its AC side; the
    # rated value above it is the nameplate and can be lower.
    "inverter_max_power": (2, "u32", 1),
})
# Which kind of storage is attached, if any. The inverter model says nothing
# about it — a SUN2000 runs with or without a battery, and with either brand.
_STORAGE_MODELS = {0: None, 1: "LG-RESU", 2: "LUNA2000"}
_BLOCK_STORAGE_MODEL = (47000, 1, "very_low", {"storage_product_model": (0, "u16", 1)})
# The EMMA's built-in meter, read from the EMMA's own unit id. Live on every
# read; measured at 25 ms per request on the reference installation.
_REG_METER_POWER = 31657
_BLOCK_MODEL = (30000, 25, "very_low", {
    "device_name": (0, "str", 15),
    # The inverter's own serial, which huawei_solar also uses as its device
    # identifier — so it ties a Modbus address to a device in the registry.
    "inverter_serial_number": (15, "str", 10),
})
# 37052/37814 belong to the power module (the LUNA2000-xKW-Cx that sits between
# the inverter and the packs), not to the inverter and not to a pack.
_BLOCK_SERIAL = (37052, 10, "very_low", {"power_module_serial_number": (0, "str", 10)})
_BLOCK_STORAGE_SW = (37814, 15, "very_low", {"power_module_firmware_version": (0, "str", 15)})
_BLOCK_INVERTER_SW = (30050, 15, "very_low", {"inverter_software_version": (0, "str", 15)})
# Each pack answers on its own address run; there is no contiguous block. Serial
# and firmware sit next to each other within a run, so one read covers both.
_BLOCK_PACK1 = (38200, 25, "very_low", {
    "pack1_serial_number": (0, "str", 10),
    "pack1_firmware_version": (10, "str", 15),
})
_BLOCK_PACK2 = (38242, 25, "very_low", {
    "pack2_serial_number": (0, "str", 10),
    "pack2_firmware_version": (10, "str", 15),
})
_BLOCK_PACK3 = (38284, 25, "very_low", {
    "pack3_serial_number": (0, "str", 10),
    "pack3_firmware_version": (10, "str", 15),
})

_BLOCKS = (
    _BLOCK_LIVE, _BLOCK_PV, _BLOCK_STATE3, _BLOCK_STRINGS,
    _BLOCK_FORCIBLE_MODE, _BLOCK_FORCIBLE_POWER,
    _BLOCK_DAILY, _BLOCK_LIMITS, _BLOCK_TOTALS, _BLOCK_CONFIG,
    _BLOCK_CAPACITY, _BLOCK_STRING_COUNT, _BLOCK_RATING, _BLOCK_MODEL, _BLOCK_SERIAL,
    _BLOCK_STORAGE_SW, _BLOCK_INVERTER_SW, _BLOCK_STORAGE_MODEL,
    _BLOCK_PACK1, _BLOCK_PACK2, _BLOCK_PACK3,
)

_DECODERS = {"u16": decode_u16, "i16": decode_i16, "u32": decode_u32, "i32": decode_i32}


def _u32(value: int) -> list[int]:
    """Split an unsigned 32-bit value into two registers, high word first."""
    value = max(0, int(value))
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]

# Keys computed from other keys rather than read. They belong to no register
# block, so both the poll scheduler and the key filter have to be told which
# raw values they stand on — otherwise asking for them reads nothing.
# The first source decides which read group carries the derived key, so a
# derivation whose inputs span two blocks still gets scheduled exactly once.
_DERIVED_FROM = {
    **{
        f"mppt{index}_power": (f"pv{index}_voltage", f"pv{index}_current")
        for index in range(1, _MAX_PV_STRINGS + 1)
    },
    "ac_offgrid_power": ("off_grid_state", "inverter_ac_power"),
    "backup_function": ("off_grid_state",),
}

# Keys that are read but then refined using another key. Unlike _DERIVED_FROM
# these already sit in a read group; only the key filter has to pull their
# companion along, or a poll for just this key would lose the refinement.
_REFINED_FROM = {
    "inverter_state": ("battery_power",),
}

SENSOR_DEFINITIONS = [
    {"key": "battery_soc", "name": "Battery SOC", "unit": "%", "device_class": "battery", "state_class": "measurement", "scale": 1, "precision": 1, "scan_interval": "high", "enabled_by_default": True},
    {"key": "battery_power", "name": "Battery Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "battery_voltage", "name": "Battery Voltage", "unit": "V", "device_class": "voltage", "state_class": "measurement", "scale": 1, "precision": 1, "scan_interval": "high", "enabled_by_default": True},
    {"key": "battery_total_energy", "name": "Battery Total Energy", "unit": "kWh", "device_class": "energy_storage", "state_class": "measurement", "scale": 1, "precision": 2, "scan_interval": "very_low", "enabled_by_default": True},
    {"key": "solar_power", "name": "Solar Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    *[
        row
        for index in range(1, _MAX_PV_STRINGS + 1)
        for row in (
            {"key": f"mppt{index}_power", "name": f"MPPT{index} Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
            {"key": f"pv{index}_voltage", "name": f"PV{index} Voltage", "unit": "V", "device_class": "voltage", "state_class": "measurement", "scale": 1, "precision": 1, "category": "diagnostic", "scan_interval": "high", "enabled_by_default": False},
        )
    ],
    {"key": "ac_offgrid_power", "name": "Off-grid Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "medium", "enabled_by_default": True},
    {"key": "backup_function", "name": "Backup Function", "data_type": "char", "icon": "mdi:home-lightning-bolt-outline", "category": "diagnostic", "scan_interval": "medium", "enabled_by_default": True},
    {"key": "power_module_firmware_version", "name": "Power Module Firmware", "data_type": "char", "icon": "mdi:ticket-confirmation-outline", "category": "diagnostic", "scan_interval": "very_low", "enabled_by_default": True},
    {"key": "inverter_software_version", "name": "Inverter Firmware", "data_type": "char", "icon": "mdi:ticket-confirmation-outline", "category": "diagnostic", "scan_interval": "very_low", "enabled_by_default": True},
    *[
        row
        for index in (1, 2, 3)
        for row in (
            {"key": f"pack{index}_firmware_version", "name": f"Battery Pack {index} Firmware", "data_type": "char", "icon": "mdi:ticket-confirmation-outline", "category": "diagnostic", "scan_interval": "very_low", "enabled_by_default": True},
            {"key": f"pack{index}_serial_number", "name": f"Battery Pack {index} Serial", "data_type": "char", "icon": "mdi:identifier", "category": "diagnostic", "scan_interval": "very_low", "enabled_by_default": True},
        )
    ],
    {"key": "grid_power", "name": "Grid Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "inverter_ac_power", "name": "Inverter AC Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "internal_temperature", "name": "Battery Temperature", "unit": "°C", "device_class": "temperature", "state_class": "measurement", "scale": 1, "precision": 1, "scan_interval": "low", "enabled_by_default": True},
    {"key": "total_charging_energy", "name": "Total Charging Energy", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing", "scale": 1, "precision": 2, "scan_interval": "low", "enabled_by_default": True},
    {"key": "total_discharging_energy", "name": "Total Discharging Energy", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing", "scale": 1, "precision": 2, "scan_interval": "low", "enabled_by_default": True},
    {"key": "total_daily_charging_energy", "name": "Daily Charging Energy", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing", "scale": 1, "precision": 2, "scan_interval": "low", "enabled_by_default": True},
    {"key": "total_daily_discharging_energy", "name": "Daily Discharging Energy", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing", "scale": 1, "precision": 2, "scan_interval": "low", "enabled_by_default": True},
    {"key": "max_charge_power", "name": "Max Charge Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "category": "diagnostic", "scan_interval": "low", "enabled_by_default": False},
    {"key": "max_discharge_power", "name": "Max Discharge Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "category": "diagnostic", "scan_interval": "low", "enabled_by_default": False},
    {"key": "charging_cutoff_capacity", "name": "Charging Cutoff SOC", "unit": "%", "state_class": "measurement", "scale": 1, "precision": 1, "category": "diagnostic", "scan_interval": "low", "enabled_by_default": False},
    {"key": "discharging_cutoff_capacity", "name": "Discharging Cutoff SOC", "unit": "%", "state_class": "measurement", "scale": 1, "precision": 1, "category": "diagnostic", "scan_interval": "low", "enabled_by_default": False},
    {"key": "inverter_state", "name": "Storage Status", "data_type": "char", "icon": "mdi:state-machine", "category": "diagnostic", "scan_interval": "high", "enabled_by_default": True},
    {"key": "user_work_mode", "name": "Working Mode", "data_type": "char", "icon": "mdi:cog-outline", "category": "diagnostic", "scan_interval": "low", "enabled_by_default": True},
    {"key": "inverter_max_power", "name": "Inverter Max AC Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "category": "diagnostic", "scan_interval": "very_low", "enabled_by_default": True},
    {"key": "inverter_rated_power", "name": "Inverter Rated Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "category": "diagnostic", "scan_interval": "very_low", "enabled_by_default": False},
    {"key": "device_name", "name": "Inverter Model", "data_type": "char", "icon": "mdi:information-outline", "category": "diagnostic", "scan_interval": "very_low", "enabled_by_default": True},
    {"key": "power_module_serial_number", "name": "Power Module Serial", "data_type": "char", "icon": "mdi:identifier", "category": "diagnostic", "scan_interval": "very_low", "enabled_by_default": True},
    {"key": "inverter_serial_number", "name": "Inverter Serial", "data_type": "char", "icon": "mdi:identifier", "category": "diagnostic", "scan_interval": "very_low", "enabled_by_default": True},
]


class HuaweiSolarDriver(BatteryDriver):
    """One Huawei inverter's attached LUNA2000, read natively and driven by service."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        *,
        port: int = 502,
        slave_id: int = 1,
        battery_device_id: str = "",
        direct_write: bool = False,
        max_charge_power_w: int = 5000,
        max_discharge_power_w: int = 5000,
        emma_slave_id: Optional[int] = None,
        client: Optional[HuaweiModbusClient] = None,
        meter_client: Optional[HuaweiModbusClient] = None,
    ) -> None:
        self.hass = hass
        self._battery_device_id = battery_device_id
        # Write set-points as Modbus registers instead of huawei_solar service
        # calls. Same four-register sequence either way; this path skips the HA
        # service layer and that integration's communication lock.
        self._direct_write = bool(direct_write)
        self._client = client if client is not None else HuaweiModbusClient(host, port, slave_id)
        # An EMMA answers on its own unit id and carries the grid meter this
        # installation is metered by. Reading it here rather than through the
        # huawei_solar integration is the whole point: that one polls every 30 s,
        # which is far too slow to control against.
        self._meter_client = meter_client
        if self._meter_client is None and emma_slave_id is not None:
            self._meter_client = HuaweiModbusClient(host, port, int(emma_slave_id))
        self._shutting_down = False
        # The inverter's model, read from 30000. The device this driver stands
        # for is the storage, whose own model comes from 47000.
        self._model: Optional[str] = None
        self._storage_model: Optional[str] = None
        # What the battery permits right now (37046/37048). It moves with the
        # pack count, so a configured limit may sit above it — and a command
        # above it is refused outright rather than clamped by the far end.
        self._register_limits: dict[str, int] = {}
        # Refined from register 30071 on the first poll; two is the common case
        # and keeps the entity list sane until the inverter has answered.
        self._pv_strings = 2
        # Which pack slots are populated, learned from the packs that answer.
        # Empty until one has, so a failed read never hides a pack that exists.
        self._packs: set[int] = set()
        self._serial: Optional[str] = None
        # Last command actually written, so the deadband can compare against what
        # the hardware was told rather than against what it currently delivers.
        self._last_written_w: Optional[int] = None
        self._last_write_monotonic = 0.0
        max_charge_power_w = max(0, min(int(max_charge_power_w), _HW_MAX_POWER_W))
        max_discharge_power_w = max(0, min(int(max_discharge_power_w), _HW_MAX_POWER_W))
        self._capabilities = DriverCapabilities(
            # 47081/47082 are real cutoff registers, but they only accept
            # 90-100 % and 0-20 % respectively — narrower than the window a user
            # may configure here. Claiming hardware enforcement would silently
            # leave an out-of-range limit unenforced, so the control layer owns
            # the SOC window and the registers are kept as a backstop.
            hardware_soc_cutoff=False,
            has_force_mode=True,
            push_telemetry=False,
            max_charge_power_w=max_charge_power_w,
            max_discharge_power_w=max_discharge_power_w,
            has_mppt_pv=False,
            has_alarm_registers=False,
            has_rs485_control=False,
            has_energy_counters=True,
            has_daily_energy_counters=True,
            # A forcible command is acknowledged in its registers long before the
            # battery has ramped, so an immediate readback would report a
            # mismatch that resolves itself seconds later.
            setpoint_confirm_reliable=False,
            actuator_latency_s=_ACTUATOR_LATENCY_S,
            readback_latency_s=_ACTUATOR_LATENCY_S,
            engage_grace_s=_ACTUATOR_LATENCY_S,
        )
        # One group per cadence, not per register block. The coordinator treats
        # a group that returns nothing as a failed read, and a cycle in which
        # every attempted group fails marks the whole battery unavailable. A
        # block holding a single optional value — a firmware string a given
        # inverter leaves empty, say — would therefore take the battery offline
        # on every poll of that block. Grouping by cadence keeps such a value
        # alongside others that do answer, so its absence stays what it is: one
        # missing key, not a dead device.
        grouped: dict[str, list[str]] = {}
        for _start, _count, interval, keys in _BLOCKS:
            bucket = grouped.setdefault(interval, [])
            bucket.extend(keys)
            bucket.extend(
                derived for derived, sources in _DERIVED_FROM.items()
                if sources[0] in keys
            )
        # The meter is not part of _BLOCKS: it lives on another unit id and is
        # read over its own connection, so it joins the fast group by hand.
        if self._meter_client is not None:
            grouped.setdefault("high", []).append("grid_power")
        self._read_groups = [
            ReadGroup(interval, tuple(keys)) for interval, keys in grouped.items()
        ]

    # --- identity -----------------------------------------------------------

    @property
    def capabilities(self) -> DriverCapabilities:
        return self._capabilities

    @property
    def model_label(self) -> Optional[str]:
        """What this device is, which is the storage rather than the inverter.

        The device entry stands for the battery, so naming it SUN2000 would read
        as though the packs belonged to the inverter. The inverter's own model
        keeps its own entity.
        """
        return self._storage_model or self._model or "Huawei LUNA2000"

    @property
    def serial(self) -> Optional[str]:
        return self._serial

    @property
    def connected(self) -> bool:
        return self._client.connected

    @property
    def read_groups(self) -> list[ReadGroup]:
        return self._read_groups

    @property
    def sensor_definitions(self) -> list[dict]:
        """Definitions for this unit, without the strings it does not have."""
        unused = {
            key
            for index in range(self._pv_strings + 1, _MAX_PV_STRINGS + 1)
            for key in (f"mppt{index}_power", f"pv{index}_voltage")
        }
        # A LUNA2000 holds one to three packs. An unpopulated slot answers with
        # padding, and an entity that can only ever read "unknown" is worse than
        # no entity at all. While no pack has answered yet nothing is hidden —
        # that state means "not asked", not "not there".
        # No EMMA configured means no meter to read, so the entity would only
        # ever be unknown.
        if self._meter_client is None:
            unused.add("grid_power")
        if self._packs:
            unused.update(
                key
                for index in (1, 2, 3)
                if index not in self._packs
                for key in (f"pack{index}_firmware_version", f"pack{index}_serial_number")
            )
        return [d for d in SENSOR_DEFINITIONS if d["key"] not in unused]

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
        return self.sensor_definitions

    # --- connection lifecycle ----------------------------------------------

    async def connect(self) -> bool:
        if not await self._client.async_connect():
            return False
        if self._meter_client is not None and not await self._meter_client.async_connect():
            # The battery is usable without the meter; only the grid reading is
            # lost, and that is a sensor the user may not even be relying on.
            _LOGGER.warning("Huawei driver: the EMMA meter did not answer; grid power will be missing")
        # Identity is cheap and only read here; it also proves the slave id
        # points at an inverter rather than at the EMMA or a charger.
        # The pack serials come along because the entity list depends on which
        # slots are populated, and that has to be known before setup.
        identity = await self.read_telemetry(
            [
                "device_name", "storage_product_model",
                "power_module_serial_number", "pv_string_count",
                # So the first set-point is already bounded by the hardware
                # rather than by the configured limit alone.
                "max_charge_power", "max_discharge_power",
            ]
            + [f"pack{index}_serial_number" for index in (1, 2, 3)]
        )
        self._model = identity.get("device_name") or self._model
        # The power module is the storage device's own identity; the inverter
        # has a separate serial, and the packs have theirs.
        self._serial = identity.get("power_module_serial_number") or self._serial
        return True

    async def close(self) -> None:
        await self._client.async_close()
        if self._meter_client is not None:
            await self._meter_client.async_close()

    def set_shutting_down(self, value: bool) -> None:
        self._shutting_down = bool(value)
        self._client.set_shutting_down(value)
        if self._meter_client is not None:
            self._meter_client.set_shutting_down(value)

    # --- telemetry (read) ---------------------------------------------------

    async def read_telemetry(self, keys: Optional[list[str]] = None) -> TelemetrySnapshot:
        requested = set(keys) if keys is not None else None
        if requested is not None:
            for extra_map in (_DERIVED_FROM, _REFINED_FROM):
                for key, sources in extra_map.items():
                    if key in requested:
                        requested.update(sources)
        snapshot: TelemetrySnapshot = {}

        for start, count, _interval, fields in _BLOCKS:
            wanted = fields if requested is None else {
                key: spec for key, spec in fields.items() if key in requested
            }
            if not wanted:
                continue
            regs = await self._client.async_read_holding_block(start, count)
            if regs is None:
                # A failed block omits its keys rather than publishing zeros; the
                # coordinator treats missing keys as stale, which is correct.
                continue
            for key, (offset, kind, scale) in wanted.items():
                try:
                    if kind == "str":
                        value = decode_string(regs, offset, int(scale))
                    else:
                        raw = _DECODERS[kind](regs, offset)
                        # Rounding here keeps binary-fraction artefacts such as
                        # 354 * 0.1 -> 35.300000000000004 out of the telemetry
                        # cache, which is compared and logged verbatim.
                        value = raw if scale == 1 else round(raw * scale, 4)
                except (IndexError, ValueError, KeyError):
                    continue
                if value is None:
                    continue
                snapshot[key] = value

        # Huawei reports each string as voltage and current; the panel and the
        # control layer want power, so derive it where both parts arrived.
        # The read covers every string the map defines; only the ones this
        # inverter actually has are published, so an unused pair does not become
        # a permanent 0 W entity.
        for key, side in (("max_charge_power", "charge"), ("max_discharge_power", "discharge")):
            value = snapshot.get(key)
            if value:
                self._register_limits[side] = int(value)

        if self._meter_client is not None and (
            requested is None or "grid_power" in requested
        ):
            regs = await self._meter_client.async_read_holding_block(_REG_METER_POWER, 2)
            if regs is not None:
                # Sign matches the Omnibattery convention already: positive is
                # import, negative export, verified against a separate meter.
                snapshot["grid_power"] = decode_i32(regs, 0)

        # Telemetry-only: the enum says which storage is attached, and the
        # label it resolves to is what the device entry calls itself.
        storage = snapshot.pop("storage_product_model", None)
        if storage is not None:
            self._storage_model = _STORAGE_MODELS.get(int(storage)) or self._storage_model

        for index in (1, 2, 3):
            if snapshot.get(f"pack{index}_serial_number") or snapshot.get(
                f"pack{index}_firmware_version"
            ):
                self._packs.add(index)

        if snapshot.get("pv_string_count") is not None:
            self._pv_strings = max(0, min(_MAX_PV_STRINGS, int(snapshot["pv_string_count"])))
        for index in range(1, _MAX_PV_STRINGS + 1):
            volts = snapshot.pop(f"pv{index}_voltage", None)
            amps = snapshot.pop(f"pv{index}_current", None)
            if index > self._pv_strings:
                # Read as part of the block but not wired on this inverter, so
                # it is dropped rather than left in the cache unexplained.
                continue
            if volts is not None:
                snapshot[f"pv{index}_voltage"] = volts
            if amps is not None:
                snapshot[f"pv{index}_current"] = amps
            if volts is not None and amps is not None:
                snapshot[f"mppt{index}_power"] = round(volts * amps)

        # Off-grid output is not metered separately: while the grid is
        # disconnected the inverter feeds nothing but the backup circuit, so its
        # AC power *is* the backup output. On-grid there is no such output, and
        # reporting the house supply as backup power would be plainly wrong.
        state3 = snapshot.get("off_grid_state")
        if state3 is not None:
            bits = int(state3)
            off_grid = bool(bits & _STATE3_OFF_GRID)
            snapshot["backup_function"] = (
                "Off-grid" if off_grid
                else "Ready" if bits & _STATE3_OFF_GRID_SWITCH_ENABLED
                else "Disabled"
            )
            ac_power = snapshot.get("inverter_ac_power")
            if ac_power is not None:
                snapshot["ac_offgrid_power"] = max(0, int(ac_power)) if off_grid else 0

        # Enum registers become their label; the raw number stays available to
        # the control layer under the same key only where it is numeric.
        if "inverter_state" in snapshot:
            raw_state = int(snapshot["inverter_state"])
            battery_power = snapshot.get("battery_power")
            if raw_state == _STORAGE_STATUS_RUNNING and battery_power is not None:
                power = int(battery_power)
                if power > _STATE_DIRECTION_DEADBAND_W:
                    label = "Charge"
                elif power < -_STATE_DIRECTION_DEADBAND_W:
                    label = "Discharge"
                else:
                    label = "Standby"
            else:
                # Offline / Fault / Sleep carry more than a direction ever could,
                # so they are reported as-is.
                label = _STORAGE_STATUS_LABELS.get(raw_state, f"Unknown ({raw_state})")
            snapshot["inverter_state"] = label
        if "user_work_mode" in snapshot:
            snapshot["user_work_mode"] = _WORKING_MODE_LABELS.get(
                int(snapshot["user_work_mode"]), f"Unknown ({snapshot['user_work_mode']})"
            )
        return snapshot

    # --- control (write) ----------------------------------------------------

    def _ceiling(self, side: str) -> int:
        """The lower of the configured limit and what the battery permits now.

        The configured limit may legitimately exceed the battery's present
        reading — that reading rises when a pack is added — but a command above
        it is rejected, not trimmed: the service raises and the control cycle
        dies. So the live figure binds whenever it is known.
        """
        configured = (
            self._capabilities.max_charge_power_w if side == "charge"
            else self._capabilities.max_discharge_power_w
        )
        register = self._register_limits.get(side)
        return min(configured, register) if register else configured

    async def apply_setpoint(
        self,
        net_power_w: int,
        *,
        mode_hint: Optional[str] = None,
        read_back: bool = True,
    ) -> SetpointResult:
        """Command a signed net power through the huawei_solar services."""
        applied = max(
            -self._ceiling("discharge"),
            min(self._ceiling("charge"), int(net_power_w)),
        )

        if not self._should_write(applied):
            # Nothing was sent, so report what is actually in force rather than
            # what was asked for. Echoing the request would tell the control
            # layer the battery had been commanded to a value it never received;
            # it would then measure the older power, see a battery that accepts
            # commands without delivering, and flag it as non-responsive.
            held = self._last_written_w if self._last_written_w is not None else 0
            return SetpointResult(
                ok=True, net_power_w=held, confirmed=False,
                applied=self._echo(held),
            )

        if self._direct_write:
            return await self._write_setpoint_registers(applied, read_back=read_back)

        # Only the service path needs huawei_solar's battery device; direct
        # writes address the inverter themselves, which is the whole point of
        # that option.
        if not self._battery_device_id:
            return SetpointResult(
                ok=False, net_power_w=applied, confirmed=False,
                failure_reason="no_battery_device",
            )

        if applied == 0:
            # Zero means "no work for you", and for a DC-coupled hybrid that has
            # to mean *released*, not *held*.
            #
            # This driver originally pinned a zero with a forcible charge at 0 W,
            # to keep the inverter's own self-consumption control from
            # regulating against the PD loop. On real hardware that reasoning
            # was inverted: a pinned battery cannot absorb its own PV, so the
            # inverter derates the strings instead, and the control layer's
            # single idle on entering manual mode left a 13.8 kWh battery frozen
            # with the panel showing standby. Handing it back costs a second
            # regulator; keeping it costs the solar yield.
            service, data = "stop_forcible_charge", {}
        elif applied > 0:
            service, data = "forcible_charge", {"power": applied}
            data["duration"] = _COMMAND_DURATION_MIN
        else:
            service, data = "forcible_discharge", {"power": -applied}
            data["duration"] = _COMMAND_DURATION_MIN

        if not await self._call_service(service, data):
            return SetpointResult(
                ok=False, net_power_w=applied, confirmed=False,
                failure_reason="service_call_failed",
            )
        self._last_written_w = applied
        self._last_write_monotonic = asyncio.get_running_loop().time()

        if not read_back:
            return SetpointResult(
                ok=True, net_power_w=applied, confirmed=False, applied=self._echo(applied)
            )

        echo = await self.read_telemetry(
            ["force_mode", "set_charge_power", "set_discharge_power", "battery_power"]
        )
        expected_mode = self._echo(applied)["force_mode"]
        confirmed = echo.get("force_mode") == expected_mode
        battery_power = echo.get("battery_power")
        applied_echo = self._echo(applied)
        applied_echo.update(echo)
        return SetpointResult(
            ok=True,
            net_power_w=applied,
            confirmed=confirmed,
            # The command registers echo instantly while the battery is still
            # ramping, so a confirmed echo is never an exact power match.
            exact=False,
            battery_power_w=int(battery_power) if battery_power is not None else None,
            applied=applied_echo,
        )

    @staticmethod
    def _direction(power_w: int) -> int:
        """Charging, discharging, or held at zero — as three distinct states."""
        if power_w == 0:
            return 0
        return 1 if power_w > 0 else -1

    async def _write_setpoint_registers(self, applied: int, *, read_back: bool) -> SetpointResult:
        """Write the forcible-charge sequence directly, without huawei_solar.

        Mirrors that integration's own sequence — same registers, same order, same
        FC16 block writes — so the inverter sees exactly what it would either way.
        """
        if applied == 0:
            # Release, mirroring stop_forcible_charge: stop first, then tidy up.
            writes = [
                (_REG_FORCIBLE_MODE, [_FORCIBLE_STOP]),
                (_REG_FORCIBLE_DISCHARGE_POWER, _u32(0)),
                (_REG_FORCED_PERIOD, [0]),
                (_REG_FORCIBLE_TARGET_MODE, [_TARGET_MODE_TIME]),
            ]
        else:
            limit = self._ceiling("charge" if applied > 0 else "discharge")
            magnitude = min(abs(applied), limit)
            if magnitude != abs(applied):
                # huawei_solar validates against the register maximum and refuses
                # an over-range power outright. Clamping keeps the control cycle
                # alive instead, but the discrepancy is worth a line.
                _LOGGER.debug(
                    "Huawei driver: clamped %dW to the %dW register maximum",
                    abs(applied), limit,
                )
            writes = [
                (
                    _REG_FORCIBLE_CHARGE_POWER if applied > 0
                    else _REG_FORCIBLE_DISCHARGE_POWER,
                    _u32(magnitude),
                ),
                (_REG_FORCED_PERIOD, [_COMMAND_DURATION_MIN]),
                (_REG_FORCIBLE_TARGET_MODE, [_TARGET_MODE_TIME]),
                (
                    _REG_FORCIBLE_MODE,
                    [_FORCIBLE_CHARGE if applied > 0 else _FORCIBLE_DISCHARGE],
                ),
            ]

        for address, values in writes:
            if not await self._client.async_write_registers(address, values):
                # The mode register is written last, so a sequence that fails
                # earlier leaves the inverter doing what it did before rather
                # than acting on half-written parameters.
                return SetpointResult(
                    ok=False, net_power_w=applied, confirmed=False,
                    failure_reason="register_write_failed",
                )

        self._last_written_w = applied
        self._last_write_monotonic = asyncio.get_running_loop().time()
        echo = self._echo(applied)
        if not read_back:
            return SetpointResult(ok=True, net_power_w=applied, confirmed=False, applied=echo)

        readback = await self.read_telemetry(
            ["force_mode", "set_charge_power", "set_discharge_power", "battery_power"]
        )
        confirmed = readback.get("force_mode") == echo["force_mode"]
        battery_power = readback.get("battery_power")
        echo.update(readback)
        return SetpointResult(
            ok=True, net_power_w=applied, confirmed=confirmed, exact=False,
            battery_power_w=int(battery_power) if battery_power is not None else None,
            applied=echo,
        )

    def _should_write(self, applied: int) -> bool:
        """Whether this set-point is worth four Modbus writes and a 10 s ramp."""
        if self._last_written_w is None:
            return True
        since = asyncio.get_running_loop().time() - self._last_write_monotonic
        # A change of direction is the most material change there is — but only
        # once the previous command has actually landed. This battery needs
        # ~15 s to reach a target, and a 2 s control loop that sees no response
        # yet will keep revising its request; letting every one of those through
        # because the sign flipped means a new forced command every few seconds,
        # which the inverter answers by derating its PV. So a reversal still
        # skips the deadband, but never the ramp.
        if (
            self._direction(applied) != self._direction(self._last_written_w)
            and since >= _MIN_WRITE_INTERVAL_S
        ):
            return True
        # Refresh before the command's own duration runs out, so the battery
        # never silently falls back to inverter control mid-regulation.
        if since >= _COMMAND_REFRESH_S:
            return True
        # Within one direction, rewriting mid-ramp achieves nothing: the battery
        # is still travelling towards the previous target.
        if since < _MIN_WRITE_INTERVAL_S:
            return False
        return abs(applied - self._last_written_w) >= _WRITE_DEADBAND_W

    def _echo(self, applied: int) -> dict:
        if applied == 0:
            mode = _FORCIBLE_STOP
        elif applied > 0:
            mode = _FORCIBLE_CHARGE
        else:
            mode = _FORCIBLE_DISCHARGE
        return {
            "force_mode": mode,
            "set_charge_power": applied if applied > 0 else 0,
            "set_discharge_power": -applied if applied < 0 else 0,
        }

    async def _call_service(self, service: str, data: dict) -> bool:
        try:
            await self.hass.services.async_call(
                _DOMAIN_HUAWEI_SOLAR,
                service,
                {"device_id": self._battery_device_id, **data},
                blocking=True,
            )
            return True
        except Exception as exc:
            if not self._shutting_down:
                _LOGGER.warning(
                    "Huawei driver: %s.%s failed: %s", _DOMAIN_HUAWEI_SOLAR, service, exc
                )
            return False

    async def write_control(self, key: str, value: int) -> bool:
        """No user-facing control entities are exposed by this driver."""
        return False

    def dynamic_discharge_limit_w(self, data: dict) -> Optional[int]:
        """Discharge headroom left on the inverter's AC side after PV.

        Battery and PV strings share one inverter here, so the nameplate battery
        limit is only reachable when the sun is down. At 7 kW of PV on an 8.8 kW
        inverter the battery can contribute 1.8 kW no matter what its BMS allows,
        and allocating 7 kW to it just starves the other batteries of the share
        they could have delivered.

        The subtraction deliberately excludes this battery's own contribution:
        the ceiling has to describe what PV occupies, not what the battery is
        currently doing, or the limit would chase its own output and oscillate.
        """
        ceiling = data.get("inverter_max_power")
        ac_power = data.get("inverter_ac_power")
        battery_power = data.get("battery_power")
        if ceiling is None or ac_power is None or battery_power is None:
            # No guess: the caller keeps the static envelope.
            return None
        try:
            ceiling = int(ceiling)
            # Only a discharge occupies AC capacity on the battery's behalf.
            battery_ac = max(0, -int(battery_power))
            non_battery_ac = max(0, int(ac_power) - battery_ac)
        except (TypeError, ValueError):
            return None
        return max(0, ceiling - non_battery_ac)

    def net_power_from_data(self, data: dict) -> Optional[int]:
        mode = data.get("force_mode")
        charge = data.get("set_charge_power")
        discharge = data.get("set_discharge_power")
        if mode is None or charge is None or discharge is None:
            return None
        mode = int(round(float(mode)))
        if mode == _FORCIBLE_CHARGE:
            return int(round(float(charge)))
        if mode == _FORCIBLE_DISCHARGE:
            return -int(round(float(discharge)))
        return 0

    @property
    def control_dependency_keys(self) -> frozenset:
        return frozenset({
            "force_mode", "set_charge_power", "set_discharge_power",
            "max_charge_power", "max_discharge_power",
            # Inputs of dynamic_discharge_limit_w: the allocator reads them every
            # cycle, so they must keep being polled even with their entities off.
            "inverter_max_power", "inverter_ac_power", "battery_power",
            "charging_cutoff_capacity", "discharging_cutoff_capacity",
        })

    # --- concrete methods the coordinator calls without isinstance guards ----

    async def apply_config(
        self,
        *,
        max_soc_pct: float,
        min_soc_pct: float,
        max_charge_power_w: int,
        max_discharge_power_w: int,
        **_kwargs,
    ) -> bool:
        """Push the SOC window to the inverter's own cutoff registers.

        Power caps are deliberately skipped: 37046/37048 are commissioning
        values that belong to the installer, not to a battery manager.

        Always reports success. The SOC window is enforced by the control layer
        for this brand, so tightening the inverter's own cutoffs is a bonus, not
        a requirement — reporting failure here would raise a warning about
        something that is working as designed.
        """
        await self.set_charge_cutoff(max_soc_pct)
        await self._write_cutoff("discharging", min_soc_pct)
        return True

    async def set_charge_cutoff(self, soc_pct: float) -> bool:
        return await self._write_cutoff("charging", soc_pct)

    async def _write_cutoff_register(self, which: str, soc_pct: float) -> bool:
        """Write a cutoff straight to its register (47081/47082, tenths of a percent)."""
        address = _REG_CHARGE_CUTOFF if which == "charging" else _REG_DISCHARGE_CUTOFF
        return await self._client.async_write_registers(
            address, [int(round(float(soc_pct) * 10))]
        )

    async def _write_cutoff(self, which: str, soc_pct: float) -> bool:
        """Write a cutoff through the huawei_solar number entity for it.

        Values the register cannot represent are skipped rather than clamped: a
        clamped write would move the hardware backstop somewhere the user never
        asked for. The control layer enforces the real window either way.
        """
        low, high = (
            _CHARGE_CUTOFF_RANGE if which == "charging" else _DISCHARGE_CUTOFF_RANGE
        )
        if not low <= float(soc_pct) <= high:
            _LOGGER.debug(
                "Huawei driver: %s cutoff %.1f%% is outside the register range "
                "%.0f-%.0f%%; leaving the hardware backstop untouched",
                which, float(soc_pct), low, high,
            )
            return False
        if self._direct_write:
            return await self._write_cutoff_register(which, soc_pct)
        entity_id = self._resolve_entity(f"storage_{which}_cutoff_capacity")
        if entity_id is None:
            return False
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": entity_id, "value": round(float(soc_pct), 1)},
                blocking=True,
            )
            return True
        except Exception as exc:
            if not self._shutting_down:
                _LOGGER.warning(
                    "Huawei driver: writing %s cutoff via %s failed: %s",
                    which, entity_id, exc,
                )
            return False

    def _resolve_entity(self, register_name: str) -> Optional[str]:
        """Find a huawei_solar entity by its register name.

        huawei_solar builds unique ids as ``<serial>_<register name>``, which is
        stable across renames and translations — unlike the entity id or the
        friendly name, which the user owns.

        The search spans the whole config entry rather than the battery device,
        because huawei_solar splits battery settings across devices: the charge
        cutoff sits on the inverter while the discharge cutoff sits on the
        battery. Looking only at the configured battery device finds one and
        misses the other.
        """
        if not self._battery_device_id:
            return None
        device = dr.async_get(self.hass).async_get(self._battery_device_id)
        if device is None:
            return None
        registry = er.async_get(self.hass)
        suffix = f"_{register_name}"
        for config_entry_id in device.config_entries:
            for entry in er.async_entries_for_config_entry(registry, config_entry_id):
                if (
                    entry.platform == _DOMAIN_HUAWEI_SOLAR
                    and not entry.disabled
                    and entry.unique_id.endswith(suffix)
                ):
                    return entry.entity_id
        return None

    async def standby(self) -> bool:
        """Release the battery back to the inverter before shutting down.

        Unlike ``apply_setpoint(0)``, this is a real stop: leaving a forcible
        command latched when Omnibattery goes away freezes the battery at
        whatever it was last told — and on this hybrid a latched discharge also
        keeps the strings dark, so the installation loses its solar harvest
        until someone clears the registers by hand.

        Must take the same path the set-points took. Releasing through the
        service while writing directly leaves the command exactly where it was:
        the direct path needs no huawei_solar device, so there is none to
        address, and the failure is silent because shutdown suppresses it.
        """
        if self._direct_write:
            result = await self._write_setpoint_registers(0, read_back=False)
            ok = result.ok
        else:
            ok = await self._call_service("stop_forcible_charge", {})
        if ok:
            self._last_written_w = None
            self._last_write_monotonic = 0.0
        return ok

    async def set_rs485_control(self, enable: bool) -> bool:
        return False

    async def get_rs485_control(self) -> Optional[bool]:
        return None

    # --- config-flow probe ---------------------------------------------------

    @classmethod
    async def find_emma_slave_id(
        cls, hass: HomeAssistant, host: str, port: int
    ) -> Optional[int]:
        """The unit id of an EMMA on this bus, or None.

        An EMMA carries the installation's grid meter, and reading it here gives
        a grid figure fast enough to control against — the huawei_solar
        integration publishes the same value on a 30 s coordinator, which is far
        too slow for a control loop. Worth finding automatically: a user who has
        one should not have to know its unit id.
        """
        for candidate in _SLAVE_ID_CANDIDATES:
            client = HuaweiModbusClient(host, port, candidate)
            try:
                if not await client.async_connect():
                    return None
                regs = await client.async_read_holding_block(30000, 15)
                model = decode_string(regs, 0, 15) if regs else None
                if model and model.startswith("SmartHEMS"):
                    _LOGGER.info("Found a Huawei EMMA on slave %s", candidate)
                    return candidate
            finally:
                await client.async_close()
        return None

    @classmethod
    async def scan_slave_ids(
        cls, hass: HomeAssistant, host: str, port: int = 502
    ) -> list[tuple[int, str, bool]]:
        """Find inverters on the bus and say which carry a battery.

        The slave id is not derivable and not the same everywhere: on the
        reference installation the inverter answers on 4 while 0 is the energy
        manager, 2 a backup switch and 9 a charger. Asking a user to guess that
        is the least friendly part of the setup, so the flow offers to look.

        Returns ``(slave_id, model, has_battery)`` for every id that identified
        itself, so the caller can tell "no inverter here" from "an inverter with
        no battery attached".
        """
        found: list[tuple[int, str, bool]] = []
        for slave_id in _SLAVE_ID_CANDIDATES:
            driver = cls(hass, host, port=port, slave_id=slave_id)
            try:
                if not await driver.connect():
                    # The address itself is unreachable; no point trying the rest.
                    break
                data = await driver.read_telemetry(["device_name", "battery_soc"])
            except Exception:  # a dead id must not end the scan
                continue
            finally:
                await driver.close()
            model = data.get("device_name")
            if model and model.upper().startswith("SUN2000"):
                # No early exit: Huawei inverters can be cascaded, so several
                # may answer on one bus, each with its own battery. The caller
                # needs all of them to offer a choice.
                found.append((slave_id, model, "battery_soc" in data))
        return found

    @classmethod
    async def probe(
        cls, hass: HomeAssistant, host: str, port: int = 502, slave_id: int = 1
    ) -> tuple[
        bool, Optional[str], Optional[int], Optional[int], Optional[str], Optional[int]
    ]:
        """Check the read path and report model, power caps, serial and rating.

        The serial is what lets a caller confirm that a device picked in the UI
        and a Modbus address point at the same inverter.
        """
        driver = cls(hass, host, port=port, slave_id=slave_id)
        try:
            if not await driver.connect():
                return False, None, None, None, None, None
            data = await driver.read_telemetry([
                "device_name", "battery_soc", "max_charge_power",
                "max_discharge_power", "inverter_serial_number",
                "inverter_max_power",
            ])
            serial = data.get("inverter_serial_number")
            # What the battery reports moves with the pack count; what the
            # inverter can push through does not, so both are worth having.
            inverter_max = data.get("inverter_max_power")
            # SOC proves a battery is actually attached; the model alone would
            # also match an inverter running without storage.
            if "battery_soc" not in data:
                return False, data.get("device_name"), None, None, serial, inverter_max
            return (
                True,
                data.get("device_name"),
                data.get("max_charge_power"),
                data.get("max_discharge_power"),
                serial,
                inverter_max,
            )
        finally:
            await driver.close()
