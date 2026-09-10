"""Shared Modbus register infrastructure for all Marstek Venus battery versions.

Contains: REGISTER_MAP, MESSAGE_WAIT_MS, bit-description maps, and
calculated-sensor definition lists (cycle, solar power, battery cell power).
"""

# Version-specific register map for control operations
# Maps logical register names to physical addresses per battery version
REGISTER_MAP = {
    "v2": {
        "rs485_control": 42000,
        "force_mode": 42010,
        "set_charge_power": 42020,
        "set_discharge_power": 42021,
        "charging_cutoff_capacity": 44000,      # Hardware cutoff
        "discharging_cutoff_capacity": 44001,   # Hardware cutoff
        "max_charge_power": 44002,
        "max_discharge_power": 44003,
        "battery_soc": 32104,
        "battery_power": 32102,
        "user_work_mode": 43000,
        "backup_function": 41200,
    },
    "v3": {
        "rs485_control": 42000,
        "force_mode": 42010,
        "set_charge_power": 42020,
        "set_discharge_power": 42021,
        "charging_cutoff_capacity": None,       # NOT AVAILABLE - software enforcement
        "discharging_cutoff_capacity": None,    # NOT AVAILABLE - software enforcement
        "max_charge_power": 44002,
        "max_discharge_power": 44003,
        "battery_soc": 37005,
        "battery_power": 30001,
        "user_work_mode": 43000,
        "backup_function": 41200,
    },
    "vA": {
        "rs485_control": 42000,
        "force_mode": 42010,
        "set_charge_power": 42020,
        "set_discharge_power": 42021,
        "charging_cutoff_capacity": None,       # NOT AVAILABLE - software enforcement
        "discharging_cutoff_capacity": None,    # NOT AVAILABLE - software enforcement
        "max_charge_power": 44002,
        "max_discharge_power": 44003,
        "battery_soc": 32104,
        "battery_power": 30001,
        "user_work_mode": 43000,
        "backup_function": 41200,
    },
    "vD": {
        "rs485_control": 42000,
        "force_mode": 42010,
        "set_charge_power": 42020,
        "set_discharge_power": 42021,
        "charging_cutoff_capacity": None,       # NOT AVAILABLE - software enforcement
        "discharging_cutoff_capacity": None,    # NOT AVAILABLE - software enforcement
        "max_charge_power": 44002,
        "max_discharge_power": 44003,
        "battery_soc": 32104,
        "battery_power": 30001,
        "user_work_mode": 43000,
        "backup_function": 41200,
    },
}

# Version-specific Modbus timing (ms between messages)
MESSAGE_WAIT_MS = {
    "v2": 50,
    "v3": 150,  # Firmware v3 requires minimum 150ms between messages
    "vA": 150,
    "vD": 150,
}

# The waits above live in the battery's Modbus *TCP* server task, not in its
# RS485 one (firmware disassembly of a v3, issue #411): reached through an
# RS485 gateway (Elfin EW11, USB-RS485 adapter, ...) that delay buys nothing
# and costs ~1 s of every v3 poll cycle. Opt-in per battery via
# CONF_RS485_GATEWAY, and it replaces the wait for every firmware version.
# Each battery paces its own socket, so N batteries daisy-chained on one
# gateway put N times the frames on the shared bus; only a single-battery
# gateway is field-tested (#411).
MESSAGE_WAIT_MS_RS485_GATEWAY = 30

# Version-specific per-attempt timeout (seconds), passed to pymodbus.
# The v3 weak MCU answers in well under a second at the 150ms cadence, but
# occasionally stalls for several seconds and then flushes queued replies in a
# burst. Each pymodbus-internal retry re-sends the SAME transaction_id (>=3.8),
# so a reply arriving after a per-attempt timeout still matches the retry and
# is consumed (issue #361 history in infra/modbus_client.py). Keep v3 attempts
# short so the retries land inside the stall window.
READ_TIMEOUT_S = {
    "v2": 10,
    "v3": 3,
    "vA": 3,
    "vD": 3,
}

# Standalone bit-description maps — used by both sensor definitions and the
# alarm notification / SystemAlarmSensor logic so we avoid duplicating them.
FAULT_BIT_DESCRIPTIONS: dict[int, str] = {
    # Register 36100 (bits 0-15)
    0: "Grid Overvoltage",
    1: "Grid Undervoltage",
    2: "Grid Overfrequency",
    3: "Grid Underfrequency",
    4: "Grid Peak Voltage",
    5: "Current Dcover",
    6: "Voltage Dcover",
    # Register 36101 (bits 16-31)
    16: "BAT Overvoltage",
    17: "BAT Undervoltage",
    18: "BAT Overcurrent",
    19: "BAT Low SOC",
    20: "BAT Communication Failure",
    21: "BMS Protect",
    22: "Inverter Soft Start Timeout",
    23: "Self-Checking Failure",
    24: "EEPROM Failure",
    25: "Other System Failure",
    26: "Hardware Bus Overvoltage",
    27: "Hardware Output Overcurrent",
    28: "Hardware Trans Overcurrent",
    29: "Hardware Battery Overcurrent",
    30: "Hardware Protection",
    31: "Output Overcurrent",
}

ALARM_BIT_DESCRIPTIONS: dict[int, str] = {
    # Register 36000 (bits 0-15)
    0: "PLL Abnormal Restart",
    1: "Overtemperature Limit",
    2: "Low Temperature Limit",
    3: "Fan Abnormal Warning",
    4: "Low Battery SOC Warning",
    5: "Output Overcurrent Warning",
    6: "Abnormal Line Sequence Detection",
    # Register 36001 (bits 16-31)
    16: "WiFi Abnormal",
    17: "BLE Abnormal",
    18: "Network Abnormal",
    19: "CT Connection Abnormal",
}

# Calculated cycle count. Sessy uses total_discharge / battery_capacity; legacy
# drivers retain (total_discharge + total_charge) / 2 / battery_capacity.
CYCLE_SENSOR_DEFINITIONS = [
    {
        "name": "Battery Cycle Count Calc",
        "key": "battery_cycle_count_calc",
        "icon": "mdi:counter",
        "state_class": "measurement",
        "dependency_keys": {
            "discharge": "total_discharging_energy",
            "charge": "total_charging_energy",
            "capacity": "battery_total_energy",
        },
    }
]

# Per-battery total DC-coupled PV power (sum of MPPT inputs) — vA/vD only.
SOLAR_POWER_SENSOR_DEFINITIONS = [
    {
        "name": "Solar Power",
        "key": "solar_power",
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
        "icon": "mdi:solar-power",
        "dependency_keys": {
            "mppt": ["mppt1_power", "mppt2_power", "mppt3_power", "mppt4_power"],
        },
    }
]

# Per-battery true battery cell power (DC terminal net of DC PV) — vA/vD only. The
# battery_power register lumps in the DC PV feeding the bus, so subtract the unit's
# MPPT to recover the battery's own charge/discharge. Sign follows battery_power
# (+ charge / - discharge).
BATTERY_CELL_POWER_SENSOR_DEFINITIONS = [
    {
        "name": "Battery Cell Power",
        "key": "battery_cell_power",
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
        "icon": "mdi:home-battery",
        "dependency_keys": {
            "battery": "battery_power",
            "mppt": ["mppt1_power", "mppt2_power", "mppt3_power", "mppt4_power"],
        },
    }
]
