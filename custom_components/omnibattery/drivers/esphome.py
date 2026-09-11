"""ESPHome-entity driver for Marstek batteries behind a LilyGo RS485 bridge.

Supports the community LilyGO T-CAN485 firmware
(https://github.com/whyisthisbroken/marstek-lilygo-rs485), which talks Modbus
RTU to the Venus E over its RS485 port and exposes the decoded registers as
ESPHome entities in Home Assistant. The RS485 port is occupied by the ESP32,
so there is no direct Modbus path — this driver reads and writes the HA
entities that firmware creates instead:

  Read:  ``hass.states`` lookups (ESPHome pushes state; the ESP polls the
         battery every 3 s, so telemetry freshness is ~3 s).
  Write: ``select.select_option`` / ``number.set_value`` service calls on the
         ESPHome control entities.

The underlying hardware is a Marstek Venus E with the v2 register map, so this
driver reuses the Marstek logical keys and control semantics verbatim
(force_mode + set_charge/discharge_power); the whole register-style entity and
control machinery works unchanged. Entities are matched by the slugified
ESPHome entity name (the ``name:`` field of the upstream YAML), which survives
user entity_id renames via the registry's original_name.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util, slugify

from .base import (
    BatteryDriver,
    DriverCapabilities,
    ReadGroup,
    SetpointResult,
    TelemetrySnapshot,
)

_LOGGER = logging.getLogger(__name__)

# Marstek force_mode wire values (same as the Modbus driver).
_FORCE_NONE = 0
_FORCE_CHARGE = 1
_FORCE_DISCHARGE = 2

# Diagnostic binary sensors published by the LilyGo firmware.  Keep the
# upstream names here: entity matching is intentionally based on the ESPHome
# ``name:`` field rather than the generated entity_id.
_ESPHOME_WARNING_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    ("pll_abnormal_restart", "PLL Abnormal Restart", "mdi:flash-triangle"),
    ("overtemperature_limit", "Overtemperature Limit", "mdi:thermometer-alert"),
    ("low_temperature_limit", "Low Temperature Limit", "mdi:thermometer-alert"),
    ("fan_abnormal_warning", "Fan Abnormal Warning", "mdi:fan-alert"),
    ("low_battery_soc_warning", "Low Battery SOC Warning", "mdi:battery-off-outline"),
    ("output_overcurrent_warning", "Output Overcurrent Warning", "mdi:flash-triangle"),
    ("abnormal_line_sequence_detection", "Abnormal Line Sequence Detection", "mdi:flash-triangle"),
    ("wifi_abnormal", "Wifi Abnormal", "mdi:wifi-alert"),
    ("ble_abnormal", "BLE abnormal", "mdi:bluetooth-off"),
    ("network_abnormal", "Network abnormal", "mdi:network-off"),
    ("ct_connection_abnormal", "CT connection abnormal", "mdi:robot-vacuum-alert"),
    ("grid_overvoltage", "Grid overvoltage", "mdi:flash-triangle"),
    ("grid_undervoltage", "Grid undervoltage", "mdi:flash-triangle-outline"),
    ("grid_overfrequency", "Grid overfrequency", "mdi:flash-triangle"),
    ("grid_underfrequency", "Grid underfrequency", "mdi:flash-triangle"),
    ("grid_peak_voltage_abnormal", "Grid peak voltage abnormal", "mdi:alert-plus"),
    ("current_dcover", "Current Dcover", "mdi:flash-triangle"),
    ("voltage_dcover", "Voltage Dcover", "mdi:flash-triangle"),
    ("bat_overvoltage", "BAT overvoltage", "mdi:flash-triangle"),
    ("bat_undervoltage", "BAT undervoltage", "mdi:flash-triangle-outline"),
    ("bat_overcurrent", "BAT overcurrent", "mdi:wave-undercurrent"),
    ("bat_low_soc", "BAT low SOC", "mdi:battery-off-outline"),
    ("bat_communication_failure", "BAT communication failure", "mdi:flash-triangle"),
    ("bms_protect", "BMS protect", "mdi:flash-triangle"),
)

# Logical key → (HA domain, upstream ESPHome entity name). The name is the
# ``name:`` field of the upstream YAML; matching is on its slug so punctuation
# ("Max. Cell Voltage") is irrelevant.
_ENTITY_MAP: dict[str, tuple[str, str]] = {
    # telemetry
    "battery_soc":                    ("sensor", "Battery State Of Charge"),
    "battery_power":                  ("sensor", "Battery Power"),
    "battery_voltage":                ("sensor", "Battery Voltage"),
    "battery_total_energy":           ("sensor", "Battery Total Energy"),
    "ac_power":                       ("sensor", "AC Power"),
    "internal_temperature":           ("sensor", "Internal Temperature"),
    "max_cell_voltage":               ("sensor", "Max. Cell Voltage"),
    "min_cell_voltage":               ("sensor", "Min. Cell Voltage"),
    "wifi_signal_strength":           ("sensor", "Wifi Signal Strength"),
    "inverter_state":                 ("sensor", "Inverter State"),
    "total_charging_energy":          ("sensor", "Total Charging Energy"),
    "total_discharging_energy":       ("sensor", "Total Discharging Energy"),
    "total_daily_charging_energy":    ("sensor", "Daily Charging Energy"),
    "total_daily_discharging_energy": ("sensor", "Daily Discharging Energy"),
    # diagnostics / auxiliary telemetry
    # ESPHome text_sensor entities are exposed by HA's sensor platform.
    "software_version":               ("sensor", "Software Version"),
    "ems_version":                    ("sensor", "Firmware Version"),
    "bms_version":                    ("sensor", "BMS Version"),
    "battery_runtime_estimate":       ("sensor", "Battery Runtime Estimate"),
    "esp_ip":                          ("sensor", "ESPHome IP"),
    "esp_ssid":                        ("sensor", "ESPHome SSID"),
    "esp_version":                     ("sensor", "ESP Version"),
    "wifi_status":                     ("sensor", "Battery Wifi Status"),
    "bt_status":                       ("sensor", "BT Status"),
    "cloud_status":                    ("sensor", "Cloud Status"),
    "power_restriction":               ("sensor", "Power restriction"),
    "internal_mos1_temperature":       ("sensor", "Internal MOS1 Temperature"),
    "internal_mos2_temperature":       ("sensor", "Internal MOS2 Temperature"),
    "max_cell_temperature":            ("sensor", "Max. Cell Temperature"),
    "min_cell_temperature":            ("sensor", "Min. Cell Temperature"),
    "cell_voltage_delta":              ("sensor", "Cell Voltage Delta"),
    "esp_wifi_signal_strength":        ("sensor", "ESP WiFi Signal Strength"),
    "esp_wifi_status":                 ("binary_sensor", "ESP WiFi Status"),
    # controls
    "force_mode":                     ("select", "Forcible Charge-Discharge"),
    "user_work_mode":                 ("select", "User Work Mode"),
    "backup_function":                ("select", "Backup Function"),
    "rs485_control_mode":             ("select", "RS485 Control Mode"),
    "set_charge_power":               ("number", "Forcible Charge Power"),
    "set_discharge_power":            ("number", "Forcible Discharge Power"),
    "max_charge_power":               ("number", "Max. Charge Power"),
    "max_discharge_power":            ("number", "Max. Discharge Power"),
    "charging_cutoff_capacity":       ("number", "Charging Cutoff Capacity"),
    "discharging_cutoff_capacity":    ("number", "Discharging Cutoff Capacity"),
    **{
        key: ("binary_sensor", name)
        for key, name, _icon in _ESPHOME_WARNING_DEFINITIONS
    },
}

# Wire value → ESPHome select option, per select key. Wire values match the
# Marstek register semantics so the shared entity layer (which speaks wire
# values through write_control / coordinator.data) needs no changes.
_SELECT_WIRE_TO_OPTION: dict[str, dict[int, str]] = {
    "force_mode":         {0: "None", 1: "Charge", 2: "Discharge"},
    "user_work_mode":     {0: "manual", 1: "anti-feed", 2: "ai"},
    "backup_function":    {0: "enable", 1: "disable"},
    "rs485_control_mode": {21930: "enable", 21947: "disable"},
}
_SELECT_OPTION_TO_WIRE: dict[str, dict[str, int]] = {
    key: {opt: wire for wire, opt in table.items()}
    for key, table in _SELECT_WIRE_TO_OPTION.items()
}

# Older versions of the community ESPHome YAML exposed these three options in
# lowercase (and used ``stop`` for the idle value).  The integration's public
# contract is now the same for every Marstek path, but accepting the old state
# spelling keeps telemetry and already-installed bridges readable while users
# update their ESPHome package.
_FORCE_MODE_OPTION_ALIASES = {
    "None": ("None", "stop", "none", "standby"),
    "Charge": ("Charge", "charge"),
    "Discharge": ("Discharge", "discharge"),
}

# The upstream "Inverter State" text sensor's strings → the v2 numeric state
# codes, so the shared inverter_state sensor definition ("states" map) applies.
_INVERTER_STATE_TO_CODE: dict[str, int] = {
    "sleep": 0,
    "standby": 1,
    "charge": 2,
    "discharge": 3,
    "fault": 4,
    "idle": 5,
    "ac bypass": 6,
}

_TEXT_KEYS = frozenset({
    "software_version", "ems_version", "bms_version", "battery_runtime_estimate",
    "esp_ip", "esp_ssid", "esp_version", "bt_status", "power_restriction",
})
_BINARY_TEXT_KEYS = frozenset({"wifi_status", "cloud_status"})
_BINARY_STATE_KEYS = frozenset(
    {"esp_wifi_status"}
    | {key for key, _name, _icon in _ESPHOME_WARNING_DEFINITIONS}
)

# --- bus liveness ----------------------------------------------------------
# The ESP's Modbus poll loop can jam (write queue full, bridge wedged) while its
# API connection stays up. HA then keeps the last state of every entity forever —
# nothing turns "unavailable" — so a poll "succeeds" on hours-old values and the
# control layer judges non-delivery, idle-runaway and PD error on frozen data
# (issue #452). Every other driver reads a wire that can fail; this one cannot,
# so the coordinator's failure ladder never engages. The guard below gives it
# something to fail on.
#
# It is a liveness heartbeat, NOT a per-key freshness check. The upstream
# firmware deliberately suppresses publications — `filters: - delta: 5.0` on
# Battery Power and AC Power, `skip_updates: 4` on the set-point numbers — so a
# single key can legitimately stay silent for minutes on a perfectly healthy
# bus. What cannot happen while the bus is alive is *every* battery-sourced
# entity going quiet at once: the unfiltered ones publish on the ESP's 3 s poll.
#
# ESP-local entities are excluded: wifi_signal / wifi_info / version keep
# publishing from the ESP itself while the Modbus bus is dead, which would mask
# exactly the fault being looked for.
_HEARTBEAT_EXCLUDED_KEYS: frozenset[str] = frozenset({
    "esp_ip", "esp_ssid", "esp_version",
    "esp_wifi_signal_strength", "esp_wifi_status",
})
# 120 s against a 3 s poll floor is a ~40x margin over the fastest publisher,
# and well inside the multi-hour freezes reported. ponytail: a module constant,
# overridable per driver instance; promote to a config-flow knob only if a
# firmware variant actually needs a different value.
DEFAULT_STALE_AFTER_S = 120.0

# Without these the driver cannot run the control loop; connect() refuses the
# device so the config flow can tell the user which entities are missing.
_REQUIRED_KEYS: frozenset[str] = frozenset({
    "battery_soc", "battery_power", "ac_power",
    "force_mode", "set_charge_power", "set_discharge_power",
    "rs485_control_mode",
})


# ---------------------------------------------------------------------------
# Entity definitions
# ---------------------------------------------------------------------------
# Same logical keys and presentation as registers_v2 (shared translations,
# dashboard cards, control machinery), minus register/data_type: HA states are
# already final scaled values, so every definition is scale 1.

SENSOR_DEFINITIONS: list[dict] = [
    {"name": "Battery SOC", "key": "battery_soc", "unit": "%",
     "device_class": "battery", "state_class": "measurement",
     "scale": 1, "precision": 1, "scan_interval": "medium", "enabled_by_default": True},
    {"name": "Battery Total Energy", "key": "battery_total_energy", "unit": "kWh",
     "device_class": "energy", "state_class": "total",
     "scale": 1, "precision": 3, "scan_interval": "low", "enabled_by_default": True},
    {"name": "Battery Power", "key": "battery_power", "unit": "W",
     "device_class": "power", "state_class": "measurement",
     "scale": 1, "precision": 1, "scan_interval": "high", "enabled_by_default": True},
    {"name": "Internal Temperature", "key": "internal_temperature", "unit": "°C",
     "device_class": "temperature", "state_class": "measurement",
     "category": "diagnostic", "scale": 1, "precision": 1,
     "scan_interval": "medium", "enabled_by_default": True},
    {"name": "AC Power", "key": "ac_power", "unit": "W",
     "device_class": "power", "state_class": "measurement",
     "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"name": "Total Charging Energy", "key": "total_charging_energy", "unit": "kWh",
     "device_class": "energy", "state_class": "total_increasing",
     "scale": 1, "precision": 2, "scan_interval": "low", "enabled_by_default": True},
    {"name": "Total Discharging Energy", "key": "total_discharging_energy", "unit": "kWh",
     "device_class": "energy", "state_class": "total_increasing",
     "scale": 1, "precision": 2, "scan_interval": "low", "enabled_by_default": True},
    {"name": "Total Daily Charging Energy", "key": "total_daily_charging_energy", "unit": "kWh",
     "device_class": "energy", "state_class": "total_increasing",
     "scale": 1, "precision": 2, "scan_interval": "low", "enabled_by_default": True},
    {"name": "Total Daily Discharging Energy", "key": "total_daily_discharging_energy", "unit": "kWh",
     "device_class": "energy", "state_class": "total_increasing",
     "scale": 1, "precision": 2, "scan_interval": "low", "enabled_by_default": True},
    {"name": "Inverter State", "key": "inverter_state", "unit": None,
     "icon": "mdi:state-machine", "scale": 1, "precision": 0,
     "scan_interval": "high", "enabled_by_default": True,
     "states": {
         0: "Sleep", 1: "Standby", 2: "Charge", 3: "Discharge",
         4: "Fault", 5: "Idle", 6: "AC Bypass",
     }},
    {"name": "Battery Voltage", "key": "battery_voltage", "unit": "V",
     "device_class": "voltage", "state_class": "measurement",
     "scale": 1, "precision": 1, "scan_interval": "medium", "enabled_by_default": True},
    {"name": "Max Cell Voltage", "key": "max_cell_voltage", "unit": "V",
     "device_class": "voltage", "state_class": "measurement",
     "category": "diagnostic", "scale": 1, "precision": 2,
     "scan_interval": "high", "enabled_by_default": True},
    {"name": "Min Cell Voltage", "key": "min_cell_voltage", "unit": "V",
     "device_class": "voltage", "state_class": "measurement",
     "category": "diagnostic", "scale": 1, "precision": 2,
     "scan_interval": "high", "enabled_by_default": True},
    {"name": "WiFi Signal Strength", "key": "wifi_signal_strength", "unit": "dBm",
     "device_class": "signal_strength", "state_class": "measurement",
     "category": "diagnostic", "scale": 1, "precision": 0,
     "scan_interval": "low", "enabled_by_default": True},
    {"name": "Software Version", "key": "software_version",
     "icon": "mdi:factory", "category": "diagnostic",
     "scan_interval": "low", "enabled_by_default": True},
    {"name": "Firmware Version", "key": "ems_version",
     "icon": "mdi:factory", "category": "diagnostic",
     "scan_interval": "low", "enabled_by_default": True},
    {"name": "BMS Version", "key": "bms_version",
     "icon": "mdi:factory", "category": "diagnostic",
     "scan_interval": "low", "enabled_by_default": True},
    {"name": "Battery Runtime Estimate", "key": "battery_runtime_estimate",
     "icon": "mdi:clock-outline", "scan_interval": "medium",
     "enabled_by_default": True},
    {"name": "ESPHome IP", "key": "esp_ip", "icon": "mdi:ip",
     "category": "diagnostic", "scan_interval": "low", "enabled_by_default": True},
    {"name": "ESPHome SSID", "key": "esp_ssid", "icon": "mdi:wifi",
     "category": "diagnostic", "scan_interval": "low", "enabled_by_default": True},
    {"name": "ESP Version", "key": "esp_version", "icon": "mdi:new-box",
     "category": "diagnostic", "scan_interval": "low", "enabled_by_default": True},
    {"name": "Internal MOS1 Temperature", "key": "internal_mos1_temperature",
     "unit": "°C", "device_class": "temperature", "state_class": "measurement",
     "category": "diagnostic", "scale": 1, "precision": 1,
     "scan_interval": "low", "enabled_by_default": True},
    {"name": "Internal MOS2 Temperature", "key": "internal_mos2_temperature",
     "unit": "°C", "device_class": "temperature", "state_class": "measurement",
     "category": "diagnostic", "scale": 1, "precision": 1,
     "scan_interval": "low", "enabled_by_default": True},
    {"name": "Max Cell Temperature", "key": "max_cell_temperature",
     "unit": "°C", "device_class": "temperature", "state_class": "measurement",
     "category": "diagnostic", "scale": 1, "precision": 1,
     "scan_interval": "low", "enabled_by_default": True},
    {"name": "Min Cell Temperature", "key": "min_cell_temperature",
     "unit": "°C", "device_class": "temperature", "state_class": "measurement",
     "category": "diagnostic", "scale": 1, "precision": 1,
     "scan_interval": "low", "enabled_by_default": True},
    {"name": "Cell Voltage Delta", "key": "cell_voltage_delta", "unit": "V",
     "device_class": "voltage", "state_class": "measurement",
     "category": "diagnostic", "scale": 1, "precision": 3,
     "scan_interval": "low", "enabled_by_default": True},
    {"name": "ESP WiFi Signal Strength", "key": "esp_wifi_signal_strength",
     "unit": "dBm", "device_class": "signal_strength", "state_class": "measurement",
     "category": "diagnostic", "scale": 1, "precision": 0,
     "scan_interval": "low", "enabled_by_default": True},
    {"name": "BT Status", "key": "bt_status", "icon": "mdi:bluetooth",
     "category": "diagnostic", "scan_interval": "low", "enabled_by_default": True},
    {"name": "Power Restriction", "key": "power_restriction",
     "icon": "mdi:power-plug-off-outline", "category": "diagnostic",
     "scan_interval": "low", "enabled_by_default": True},
]

SELECT_DEFINITIONS: list[dict] = [
    {"name": "Force Mode", "key": "force_mode", "enabled_by_default": True,
     "scan_interval": "high",
     "options": {"None": 0, "Charge": 1, "Discharge": 2}},
    {"name": "User Work Mode", "key": "user_work_mode", "enabled_by_default": False,
     "scan_interval": "high",
     "options": {"manual": 0, "anti_feed": 1, "trade_mode": 2}},
]

SWITCH_DEFINITIONS: list[dict] = [
    {"name": "Backup Function", "key": "backup_function",
     "command_on": 0, "command_off": 1,
     "enabled_by_default": True, "scan_interval": "medium"},
    {"name": "RS485 Control Mode", "key": "rs485_control_mode",
     "command_on": 21930, "command_off": 21947,
     "enabled_by_default": True, "scan_interval": "medium"},
]

BINARY_SENSOR_DEFINITIONS: list[dict] = [
    {"name": "WiFi Status", "key": "wifi_status",
     "device_class": "connectivity", "icon": "mdi:check-network-outline",
     "category": "diagnostic", "enabled_by_default": True, "scan_interval": "low"},
    {"name": "Cloud Status", "key": "cloud_status",
     "device_class": "connectivity", "icon": "mdi:cloud-outline",
     "category": "diagnostic", "enabled_by_default": False, "scan_interval": "low"},
    {"name": "ESP WiFi Status", "key": "esp_wifi_status",
     "device_class": "connectivity", "icon": "mdi:wifi-alert",
     "category": "diagnostic", "enabled_by_default": True, "scan_interval": "low"},
    *[
        {"name": name, "key": key, "device_class": "problem", "icon": icon,
         "category": "diagnostic", "enabled_by_default": True,
         "scan_interval": "low"}
        for key, name, icon in _ESPHOME_WARNING_DEFINITIONS
    ],
]

NUMBER_DEFINITIONS: list[dict] = [
    {"name": "Set Forcible Charge Power", "key": "set_charge_power",
     "icon": "mdi:battery-arrow-up-outline", "min": 0, "max": 2500, "step": 5,
     "unit": "W", "enabled_by_default": True, "scan_interval": "high"},
    {"name": "Set Forcible Discharge Power", "key": "set_discharge_power",
     "icon": "mdi:battery-arrow-down-outline", "min": 0, "max": 2500, "step": 5,
     "unit": "W", "enabled_by_default": True, "scan_interval": "high"},
    {"name": "Max Charge Power", "key": "max_charge_power",
     "icon": "mdi:battery-arrow-up-outline", "min": 800, "max": 2500, "step": 50,
     "unit": "W", "enabled_by_default": True, "scan_interval": "medium"},
    {"name": "Max Discharge Power", "key": "max_discharge_power",
     "icon": "mdi:battery-arrow-down-outline", "min": 800, "max": 2500, "step": 50,
     "unit": "W", "enabled_by_default": True, "scan_interval": "medium"},
    # Percent on both sides (scale 1): the ESPHome number is already in %, its
    # firmware does the ×10 deci-percent conversion on the RS485 wire.
    {"name": "Charging Cutoff Capacity", "key": "charging_cutoff_capacity",
     "icon": "mdi:battery-arrow-up-outline", "min": 80, "max": 100, "step": 1,
     "unit": "%", "scale": 1, "enabled_by_default": True, "scan_interval": "medium"},
    {"name": "Discharging Cutoff Capacity", "key": "discharging_cutoff_capacity",
     "icon": "mdi:battery-arrow-down-outline", "min": 12, "max": 30, "step": 1,
     "unit": "%", "scale": 1, "enabled_by_default": True, "scan_interval": "medium"},
]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

class EsphomeEntityDriver(BatteryDriver):
    """Drives a Marstek battery through the LilyGo/ESPHome HA entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        *,
        max_charge_power_w: int = 2500,
        max_discharge_power_w: int = 2500,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
    ) -> None:
        self.hass = hass
        self._device_id = device_id
        self._connected = False
        self._shutting_down = False
        # logical key → HA entity_id, resolved from the registry on connect().
        self._entities: dict[str, str] = {}
        self._stale_after_s = stale_after_s
        self._bus_stalled = False

        self._capabilities = DriverCapabilities(
            hardware_soc_cutoff=True,     # cutoff registers exposed as numbers
            has_force_mode=True,
            push_telemetry=True,          # ESPHome pushes state into HA
            # read_telemetry drops the whole snapshot once the bus heartbeat
            # goes quiet, so the coordinator's reconnect probe is meaningful.
            telemetry_liveness_checked=True,
            max_charge_power_w=max_charge_power_w,
            max_discharge_power_w=max_discharge_power_w,
            has_mppt_pv=False,            # Venus E is AC-coupled
            # The firmware exposes individual warning entities, not the
            # aggregate alarm/fault bitmasks expected by SystemAlarmSensor.
            has_alarm_registers=False,
            has_rs485_control=True,
            has_energy_counters=True,
            has_daily_energy_counters=True,
            # State echoes ride the ESP's 3 s battery poll (plus delta filters),
            # so a readback right after a write compares against stale values.
            setpoint_confirm_reliable=False,
            actuator_latency_s=4.0,       # HA service → ESP write → 3 s telemetry grain
            # Venus E firmware floor, same as the direct Modbus v2 driver.
            min_charge_power_w=800,
            min_discharge_power_w=800,
        )

        self._definitions: dict[str, list[dict]] = {
            "sensor":        SENSOR_DEFINITIONS,
            "number":        NUMBER_DEFINITIONS,
            "select":        SELECT_DEFINITIONS,
            "switch":        SWITCH_DEFINITIONS,
            "binary_sensor": BINARY_SENSOR_DEFINITIONS,
            "button":        [],
            "all": SENSOR_DEFINITIONS + NUMBER_DEFINITIONS + SELECT_DEFINITIONS
                    + SWITCH_DEFINITIONS + BINARY_SENSOR_DEFINITIONS,
        }

        # Single read group: telemetry is a set of hass.states lookups, so
        # there is nothing to batch or pace — return everything every poll and
        # let the coordinator gate per key.
        self._read_groups = [
            ReadGroup(
                scan_interval="high",
                keys=tuple(d["key"] for d in self._definitions["all"]),
            )
        ]

    # --- entity definitions ---------------------------------------------------

    @property
    def sensor_definitions(self) -> list[dict]:
        return self._definitions["sensor"]

    @property
    def number_definitions(self) -> list[dict]:
        return self._definitions["number"]

    @property
    def select_definitions(self) -> list[dict]:
        return self._definitions["select"]

    @property
    def switch_definitions(self) -> list[dict]:
        return self._definitions["switch"]

    @property
    def binary_sensor_definitions(self) -> list[dict]:
        return self._definitions["binary_sensor"]

    @property
    def button_definitions(self) -> list[dict]:
        return self._definitions["button"]

    @property
    def all_definitions(self) -> list[dict]:
        return self._definitions["all"]

    # --- identity ---------------------------------------------------------

    @property
    def capabilities(self) -> DriverCapabilities:
        return self._capabilities

    @property
    def model_label(self) -> Optional[str]:
        return "Venus E (LilyGo RS485)"

    # --- entity resolution --------------------------------------------------

    @staticmethod
    def _match_entities(entries: list[tuple[str, Optional[str], str]]) -> dict[str, str]:
        """Match registry entries to logical keys.

        ``entries`` is (domain, original_name, entity_id) per registry entry of
        the ESPHome device. Matching is by slugified original_name (the
        firmware's ``name:``, stable across user entity_id renames), falling
        back to the entity_id suffix for entries whose original_name was lost.
        Pure function so tests need no registry.
        """
        by_slug: dict[tuple[str, str], str] = {}
        for domain, original_name, entity_id in entries:
            if original_name:
                by_slug.setdefault((domain, slugify(original_name)), entity_id)
            # entity_id fallback: "sensor.mydevice_battery_power" → suffix match
            object_id = entity_id.split(".", 1)[1]
            by_slug.setdefault((domain, object_id), entity_id)

        resolved: dict[str, str] = {}
        for key, (domain, name) in _ENTITY_MAP.items():
            slug = slugify(name)
            entity_id = by_slug.get((domain, slug))
            if entity_id is None:
                # suffix match against the object_id fallback entries
                entity_id = next(
                    (
                        eid for (dom, oid), eid in by_slug.items()
                        if dom == domain and oid.endswith(f"_{slug}")
                    ),
                    None,
                )
            if entity_id:
                resolved[key] = entity_id
        return resolved

    @classmethod
    def resolve(
        cls, hass: HomeAssistant, device_id: str
    ) -> tuple[dict[str, str], list[str]]:
        """Resolve the device's entities and report missing required keys.

        Returns (logical key → entity_id, missing required keys). Used by both
        connect() and the config flow's validation step.
        """
        registry = er.async_get(hass)
        entries = [
            (entry.domain, entry.original_name, entry.entity_id)
            for entry in er.async_entries_for_device(
                registry, device_id, include_disabled_entities=True
            )
        ]
        resolved = cls._match_entities(entries)
        missing = sorted(_REQUIRED_KEYS - resolved.keys())
        return resolved, missing

    # --- connection lifecycle -----------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        """Resolve the entity map and verify the device is reporting."""
        resolved, missing = self.resolve(self.hass, self._device_id)
        if missing:
            if not self._shutting_down:
                _LOGGER.error(
                    "ESPHome device %s is missing required entities for %s",
                    self._device_id, missing,
                )
            self._connected = False
            return False
        self._entities = resolved

        soc_state = self.hass.states.get(resolved["battery_soc"])
        if soc_state is None or soc_state.state in ("unavailable", "unknown"):
            if not self._shutting_down:
                _LOGGER.warning(
                    "ESPHome device %s found but %s is %s (device offline?)",
                    self._device_id, resolved["battery_soc"],
                    soc_state.state if soc_state else "absent",
                )
            self._connected = False
            return False

        self._connected = True
        _LOGGER.info(
            "Connected to ESPHome-bridged Marstek (device %s, %d entities mapped)",
            self._device_id, len(resolved),
        )
        return True

    async def close(self) -> None:
        self._connected = False

    def set_shutting_down(self, value: bool) -> None:
        self._shutting_down = value

    # --- telemetry (read) -----------------------------------------------------

    @property
    def read_groups(self) -> list[ReadGroup]:
        return self._read_groups

    def _bus_stall_age_s(self) -> Optional[float]:
        """Seconds since the most recently reported battery-sourced entity.

        ``last_reported`` advances on every state write, including one that
        repeats the previous value, so this measures when the ESP last spoke —
        not when a value last moved. Returns None when no mapped entity carries
        a usable timestamp, so the guard fails open on a firmware or a test
        double the heartbeat cannot date.
        """
        newest = None
        for key, entity_id in self._entities.items():
            if key in _HEARTBEAT_EXCLUDED_KEYS:
                continue
            state = self.hass.states.get(entity_id)
            reported = getattr(state, "last_reported", None) if state else None
            if reported is not None and (newest is None or reported > newest):
                newest = reported
        if newest is None:
            return None
        return (dt_util.utcnow() - newest).total_seconds()

    async def read_telemetry(self, keys: Optional[list[str]] = None) -> TelemetrySnapshot:
        """Return the mapped entities' current HA states as a snapshot.

        States are already final scaled values (the ESP applies the register
        scaling), so the entity definitions all carry scale 1. Unavailable /
        unknown / unparseable states are omitted; an all-offline device yields
        an empty snapshot, which drives the coordinator's failure ladder.

        A stalled bus yields that same empty snapshot even though every state
        object still holds a readable value — see the bus-liveness note above.
        The ladder then marks the battery unavailable, which stops the control
        loop writing to it, so the guard also removes the write pressure on an
        already-congested bridge.
        """
        age = self._bus_stall_age_s()
        if age is not None and age > self._stale_after_s:
            if not self._bus_stalled:
                self._bus_stalled = True
                _LOGGER.warning(
                    "ESPHome device %s has not reported any battery telemetry "
                    "for %.0fs (limit %.0fs) — its Modbus poll loop looks "
                    "stalled. Treating the cached states as unusable rather "
                    "than controlling on them.",
                    self._device_id, age, self._stale_after_s,
                )
            return {}
        if self._bus_stalled:
            self._bus_stalled = False
            _LOGGER.info(
                "ESPHome device %s is reporting battery telemetry again",
                self._device_id,
            )

        wanted = keys if keys is not None else list(self._entities)
        snapshot: TelemetrySnapshot = {}
        for key in wanted:
            entity_id = self._entities.get(key)
            if entity_id is None:
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state in ("unavailable", "unknown"):
                continue
            value = self._decode(key, state.state)
            if value is not None:
                snapshot[key] = value
        return snapshot

    @staticmethod
    def _decode(key: str, raw: str):
        """Decode one HA state string into the logical wire value."""
        raw = str(raw).strip()
        option_map = _SELECT_OPTION_TO_WIRE.get(key)
        if option_map is not None:
            if raw in option_map:
                return option_map[raw]
            if key == "force_mode":
                for canonical, aliases in _FORCE_MODE_OPTION_ALIASES.items():
                    if raw in aliases:
                        return option_map[canonical]
            return None
        if key == "inverter_state":
            return _INVERTER_STATE_TO_CODE.get(raw.lower())
        if key in _TEXT_KEYS:
            return raw
        if key in _BINARY_TEXT_KEYS or key in _BINARY_STATE_KEYS:
            normalized = raw.lower()
            if normalized in {"on", "true", "1", "active", "connected", "enabled"}:
                return True
            if normalized in {"off", "false", "0", "inactive", "disconnected", "disabled"}:
                return False
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return int(value) if value.is_integer() else value

    # --- service-call helpers -------------------------------------------------

    async def _call(self, domain: str, service: str, entity_id: str, data: dict) -> bool:
        """Call an HA service on an ESPHome entity; False on any failure."""
        try:
            await self.hass.services.async_call(
                domain, service, {"entity_id": entity_id, **data}, blocking=True
            )
            return True
        except Exception as exc:
            if not self._shutting_down:
                _LOGGER.warning(
                    "ESPHome driver: %s.%s on %s failed: %s",
                    domain, service, entity_id, exc,
                )
            return False

    async def _write_number(self, key: str, value: float) -> bool:
        entity_id = self._entities.get(key)
        if entity_id is None:
            return False
        return await self._call("number", "set_value", entity_id, {"value": value})

    async def _write_select(self, key: str, wire_value: int) -> bool:
        entity_id = self._entities.get(key)
        if entity_id is None:
            return False
        option = _SELECT_WIRE_TO_OPTION.get(key, {}).get(int(wire_value))
        if option is None:
            _LOGGER.warning(
                "ESPHome driver: no option for %s wire value %s", key, wire_value
            )
            return False
        # Keep an older external ESPHome entity usable if it still advertises
        # lowercase force-mode options. New integration-owned entities always
        # expose the canonical spelling above.
        if key == "force_mode":
            state = self.hass.states.get(entity_id)
            options = getattr(state, "attributes", {}).get("options", []) if state else []
            if options:
                for canonical, aliases in _FORCE_MODE_OPTION_ALIASES.items():
                    if option == canonical:
                        option = next((candidate for candidate in aliases if candidate in options), option)
                        break
        return await self._call("select", "select_option", entity_id, {"option": option})

    # --- control (write) --------------------------------------------------------

    async def apply_setpoint(
        self,
        net_power_w: int,
        *,
        mode_hint: Optional[str] = None,
        read_back: bool = True,
    ) -> SetpointResult:
        """Translate a signed net power into the force select + power numbers.

        Same wire semantics and write order as the Marstek Modbus driver
        (discharge, charge, then force mode), delivered via HA service calls.
        """
        if net_power_w > 0:
            charge = min(net_power_w, self._capabilities.max_charge_power_w)
            discharge = 0
            force_mode = _FORCE_CHARGE
            applied_net = charge
        elif net_power_w < 0:
            charge = 0
            discharge = min(-net_power_w, self._capabilities.max_discharge_power_w)
            force_mode = _FORCE_DISCHARGE
            applied_net = -discharge
        else:
            charge = discharge = 0
            force_mode = _FORCE_NONE
            applied_net = 0

        ok1 = await self._write_number("set_discharge_power", discharge)
        ok2 = await self._write_number("set_charge_power", charge)
        ok3 = await self._write_select("force_mode", force_mode)
        if not (ok1 and ok2 and ok3):
            return SetpointResult(
                ok=False, net_power_w=applied_net, confirmed=False,
                failure_reason="service_call_failed",
            )

        applied = {
            "force_mode": force_mode,
            "set_charge_power": charge,
            "set_discharge_power": discharge,
        }
        if not read_back:
            return SetpointResult(ok=True, net_power_w=applied_net, confirmed=False, applied=applied)

        # ESPHome's modbus number/select publish the new state right after the
        # RTU write is queued, so a short settle is enough for the command echo;
        # battery_power still rides the 3 s battery poll and may lag.
        await asyncio.sleep(1.0)

        echo = await self.read_telemetry(
            ["force_mode", "set_charge_power", "set_discharge_power", "battery_power"]
        )
        if not echo:
            return SetpointResult(
                ok=True, net_power_w=applied_net, confirmed=False,
                failure_reason="feedback_timeout", applied=applied,
            )

        confirmed = (
            echo.get("force_mode") == force_mode
            and echo.get("set_charge_power") == charge
            and echo.get("set_discharge_power") == discharge
        )
        battery_power = echo.get("battery_power")
        applied.update(echo)
        return SetpointResult(
            ok=True, net_power_w=applied_net, confirmed=confirmed,
            battery_power_w=int(battery_power) if battery_power is not None else None,
            applied=applied,
        )

    async def write_control(self, key: str, value: int) -> bool:
        """Write one logical control (entity-write path, wire values in)."""
        if key in _SELECT_WIRE_TO_OPTION:
            return await self._write_select(key, value)
        if self._entities.get(key) and _ENTITY_MAP[key][0] == "number":
            return await self._write_number(key, value)
        _LOGGER.debug("EsphomeEntityDriver: no control mapping for key %r", key)
        return False

    def net_power_from_data(self, data: dict):
        force = data.get("force_mode")
        charge = data.get("set_charge_power")
        discharge = data.get("set_discharge_power")
        if force is None or charge is None or discharge is None:
            return None
        force = int(round(float(force)))
        if force == _FORCE_CHARGE:
            return int(round(float(charge)))
        if force == _FORCE_DISCHARGE:
            return -int(round(float(discharge)))
        return 0

    @property
    def control_dependency_keys(self) -> frozenset:
        return frozenset({
            "set_charge_power", "set_discharge_power",
            "max_charge_power", "max_discharge_power",
            "force_mode",
            "charging_cutoff_capacity", "discharging_cutoff_capacity",
        })

    # --- concrete methods (not on BatteryDriver ABC) ------------------------
    # Mirror the Marstek/Zendure concrete API so the coordinator can call them
    # without isinstance guards.

    async def apply_config(
        self,
        *,
        max_soc_pct: float,
        min_soc_pct: float,
        max_charge_power_w: int,
        max_discharge_power_w: int,
    ) -> bool:
        """Write SOC cutoffs and power caps through the ESPHome numbers."""
        ok = True
        ok &= await self._write_number("charging_cutoff_capacity", int(max_soc_pct))
        ok &= await self._write_number("discharging_cutoff_capacity", int(min_soc_pct))
        ok &= await self._write_number("max_charge_power", int(max_charge_power_w))
        ok &= await self._write_number("max_discharge_power", int(max_discharge_power_w))
        return bool(ok)

    async def set_charge_cutoff(self, soc_pct: float) -> bool:
        """Write only the charge-cutoff number (weekly full charge / balance)."""
        return await self._write_number("charging_cutoff_capacity", int(soc_pct))

    async def standby(self) -> bool:
        """Idle the battery (zero set-points, no force mode) for teardown."""
        ok = True
        ok &= await self._write_number("set_discharge_power", 0)
        ok &= await self._write_number("set_charge_power", 0)
        ok &= await self._write_select("force_mode", _FORCE_NONE)
        return bool(ok)

    async def set_rs485_control(self, enable: bool) -> bool:
        """Toggle RS485 control mode through the ESPHome select."""
        return await self._write_select(
            "rs485_control_mode", 21930 if enable else 21947
        )

    async def get_rs485_control(self) -> Optional[bool]:
        """Read back RS485 control mode from the select's state."""
        snapshot = await self.read_telemetry(["rs485_control_mode"])
        value = snapshot.get("rs485_control_mode")
        if value is None:
            return None
        return value == 21930
