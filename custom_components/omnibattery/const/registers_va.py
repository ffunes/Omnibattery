"""Modbus register and entity definitions for Marstek Venus A hardware.

# ============================================================================
# VENUS A BATTERY DEFINITIONS
# WARNING: Venus A registers are UNTESTED
# ============================================================================
"""

from .registers_v3 import SWITCH_DEFINITIONS_V3, BUTTON_DEFINITIONS_V3

SENSOR_DEFINITIONS_VA = [
    {
        "name": "Battery SOC",
        "register": 32104,
        "scale": 1,
        "unit": "%",
        "device_class": "battery",
        "state_class": "measurement",
        "key": "battery_soc",
        "enabled_by_default": True,
        "data_type": "uint16",
        "precision": 1,
        "scan_interval": "medium",
    },
    {
        "name": "Battery Total Energy",
        "register": 32105,
        "scale": 0.001,
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "total",
        "key": "battery_total_energy",
        "enabled_by_default": True,
        "data_type": "uint16",
        "precision": 3,
        "scan_interval": "low",
    },
    {
        "name": "Battery Voltage",
        "register": 30100,
        "scale": 0.01,
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
        "key": "battery_voltage",
        "enabled_by_default": True,
        "data_type": "uint16",
        "precision": 1,
        "scan_interval": "medium",
    },
    {
        "name": "Battery Power",
        "register": 30001,
        "count": 1,
        "scale": 1,
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
        "key": "battery_power",
        "enabled_by_default": True,
        "data_type": "int16",
        "precision": 1,
        "scan_interval": "high",
    },
    {
        "name": "AC Offgrid Power",
        "register": 32302,
        "count": 1,
        "scale": 1,
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
        "key": "ac_offgrid_power",
        "enabled_by_default": True,
        "data_type": "int16",
        "precision": 0,
        "scan_interval": "high",
    },
    {
        "name": "Internal Temperature",
        "register": 35000,
        "scale": 0.1,
        "unit": "°C",
        "device_class": "temperature",
        "state_class": "measurement",
        "key": "internal_temperature",
        "enabled_by_default": True,
        "data_type": "int16",
        "precision": 2,
        "scan_interval": "medium",
    },
    {
        "name": "AC Power",
        "register": 30006,
        "count": 1,
        "scale": 1,
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
        "key": "ac_power",
        "enabled_by_default": True,
        "data_type": "int16",
        "precision": 0,
        "scan_interval": "high",
    },
    {
        "name": "Total Charging Energy",
        "register": 33000,
        "count": 2,
        "scale": 0.01,
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "total_increasing",
        "key": "total_charging_energy",
        "enabled_by_default": True,
        "data_type": "uint32",
        "precision": 2,
        "scan_interval": "low",
    },
    {
        "name": "Total Discharging Energy",
        "register": 33002,
        "count": 2,
        "scale": 0.01,
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "total_increasing",
        "key": "total_discharging_energy",
        "enabled_by_default": True,
        "data_type": "int32",
        "precision": 2,
        "scan_interval": "low",
    },
    {
        "name": "Inverter State",
        "register": 35100,
        "scale": 1,
        "unit": None,
        "icon": "mdi:state-machine",
        "key": "inverter_state",
        "enabled_by_default": True,
        "data_type": "uint16",
        "precision": 0,
        "states": {
            0: "Sleep",
            1: "Standby",
            2: "Charge",
            3: "Discharge",
            4: "Backup Mode",
            5: "OTA Upgrade",
            6: "Bypass",
        },
        "scan_interval": "high",
    },
    {
        "name": "MPPT1 Power",
        "register": 30037,
        "scale": 0.1,
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
        "key": "mppt1_power",
        "enabled_by_default": True,
        "data_type": "uint16",
        "precision": 1,
        "scan_interval": "high",
    },
    {
        "name": "MPPT2 Power",
        "register": 30038,
        "scale": 0.1,
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
        "key": "mppt2_power",
        "enabled_by_default": True,
        "data_type": "uint16",
        "precision": 1,
        "scan_interval": "high",
    },
    {
        "name": "MPPT3 Power",
        "register": 30039,
        "scale": 0.1,
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
        "key": "mppt3_power",
        "enabled_by_default": True,
        "data_type": "uint16",
        "precision": 1,
        "scan_interval": "high",
    },
    {
        "name": "MPPT4 Power",
        "register": 30040,
        "scale": 0.1,
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
        "key": "mppt4_power",
        "enabled_by_default": True,
        "data_type": "uint16",
        "precision": 1,
        "scan_interval": "high",
    },
    {
        "name": "Device Name",
        "register": 31000,
        "count": 10,
        "data_type": "char",
        "unit": None,
        "icon": "mdi:package-variant-closed",
        "key": "device_name",
        "enabled_by_default": True,
        "scan_interval": "very_low",
        "precision": 0,
    },
    {
        "name": "BMS Version",
        "register": 30204,
        "unit": None,
        "icon": "mdi:battery-check-outline",
        "category": "diagnostic",
        "key": "bms_version",
        "enabled_by_default": True,
        "data_type": "uint16",
        "precision": 0,
        "scan_interval": "very_low",
    },
    {
        "name": "VMS Version",
        "register": 30202,
        "unit": None,
        "icon": "mdi:battery-check-outline",
        "category": "diagnostic",
        "key": "vms_version",
        "enabled_by_default": True,
        "data_type": "uint16",
        "precision": 0,
        "scan_interval": "very_low",
    },
    {
        "name": "EMS Version",
        "register": 30200,
        "unit": None,
        "icon": "mdi:ticket-confirmation-outline",
        "category": "diagnostic",
        "key": "ems_version",
        "enabled_by_default": True,
        "data_type": "uint16",
        "scale": 1,
        "precision": 0,
        "scan_interval": "very_low",
    },
    {
        "name": "Comm Module Firmware",
        "register": 30350,
        "count": 6,
        "unit": None,
        "icon": "mdi:ticket-confirmation-outline",
        "category": "diagnostic",
        "key": "comm_module_firmware",
        "enabled_by_default": True,
        "data_type": "char",
        "precision": 0,
        "scan_interval": "very_low",
    },
    {
        "name": "MAC Address",
        "register": 30304,
        "count": 6,
        "unit": None,
        "icon": "mdi:ethernet",
        "key": "mac_address",
        "enabled_by_default": True,
        "data_type": "char",
        "precision": 0,
        "scan_interval": "very_low",
    },
    {
        "name": "Battery Cycle Count",
        "register": 34003,
        "scale": 1,
        "icon": "mdi:counter",
        "state_class": "total_increasing",
        "category": "diagnostic",
        "key": "battery_cycle_count",
        "enabled_by_default": True,
        "data_type": "uint16",
        "precision": 0,
        "scan_interval": "low",
    },
    {
        "name": "Max Cell Voltage",
        "register": 37007,
        "scale": 0.001,
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
        "key": "max_cell_voltage",
        "enabled_by_default": True,
        "data_type": "int16",
        "precision": 3,
        "scan_interval": "high",
    },
    {
        "name": "Min Cell Voltage",
        "register": 37008,
        "scale": 0.001,
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
        "key": "min_cell_voltage",
        "enabled_by_default": True,
        "data_type": "int16",
        "precision": 3,
        "scan_interval": "high",
    },
    {
        "name": "WiFi Signal Strength",
        "register": 30303,
        "scale": -1,
        "unit": "dBm",
        "device_class": "signal_strength",
        "state_class": "measurement",
        "key": "wifi_signal_strength",
        "enabled_by_default": True,
        "data_type": "uint16",
        "category": "diagnostic",
        "precision": 0,
        "scan_interval": "low",
    },
]

_WIFI_CLOUD_BINARY_SENSORS = [
    {
        "name": "WiFi Status",
        "register": 30300,
        "data_type": "uint16",
        "unit": None,
        "category": "diagnostic",
        "device_class": "connectivity",
        "icon": "mdi:check-network-outline",
        "key": "wifi_status",
        "enabled_by_default": True,
        "scan_interval": "low",
    },
    {
        "name": "Cloud Status",
        "register": 30302,
        "data_type": "uint16",
        "unit": None,
        "category": "diagnostic",
        "device_class": "connectivity",
        "icon": "mdi:cloud-outline",
        "key": "cloud_status",
        "enabled_by_default": False,
        "scan_interval": "low",
    },
]
BINARY_SENSOR_DEFINITIONS_VA = _WIFI_CLOUD_BINARY_SENSORS

SELECT_DEFINITIONS_VA = [
    {
        "name": "Force Mode",
        "register": 42010,
        "key": "force_mode",
        "enabled_by_default": True,
        "scan_interval": "high",
        "data_type": "uint16",
        "options": {"None": 0, "Charge": 1, "Discharge": 2},
    },
    {
        "name": "User Work Mode",
        "register": 43000,
        "key": "user_work_mode",
        "enabled_by_default": False,
        "data_type": "uint16",
        "scan_interval": "high",
        "use_shadow_state": True,
        "options": {"manual": 0, "anti_feed": 1, "trade_mode": 2},
    },
]

# Venus A/D share the same switch and button registers as V3
SWITCH_DEFINITIONS_VA = SWITCH_DEFINITIONS_V3
BUTTON_DEFINITIONS_VA = BUTTON_DEFINITIONS_V3

NUMBER_DEFINITIONS_VA = [
    {
        "name": "Set Charge Power",
        "register": 42020,
        "key": "set_charge_power",
        "enabled_by_default": True,
        "icon": "mdi:battery-arrow-up-outline",
        "min": 0,
        "max": 1500,
        "step": 50,
        "unit": "W",
        "data_type": "uint16",
        "scan_interval": "high",
    },
    {
        "name": "Set Discharge Power",
        "register": 42021,
        "key": "set_discharge_power",
        "enabled_by_default": True,
        "icon": "mdi:battery-arrow-down-outline",
        "min": 0,
        "max": 1500,
        "step": 50,
        "unit": "W",
        "data_type": "uint16",
        "scan_interval": "high",
    },
    {
        "name": "Max Charge Power",
        "register": 44002,
        "key": "max_charge_power",
        "enabled_by_default": True,
        "icon": "mdi:battery-arrow-up-outline",
        "min": 0,
        "max": 1500,
        "step": 50,
        "unit": "W",
        "data_type": "uint16",
        "scan_interval": "high",
    },
    {
        "name": "Max Discharge Power",
        "register": 44003,
        "key": "max_discharge_power",
        "enabled_by_default": True,
        "icon": "mdi:battery-arrow-down-outline",
        "min": 0,
        "max": 1500,
        "step": 50,
        "unit": "W",
        "data_type": "uint16",
        "scan_interval": "high",
    },
    {
        "name": "Charge To SOC",
        "register": 42011,
        "key": "charge_to_soc",
        "enabled_by_default": False,
        "icon": "mdi:battery-sync-outline",
        "min": 10,
        "max": 100,
        "step": 1,
        "unit": "%",
        "scale": 1,
        "data_type": "uint16",
        "scan_interval": "high",
    },
]

# --- per-pack SOC (issue #350) ----------------------------------------------
# Venus A/D couple several battery packs and fill them in sequence, so the
# aggregate SOC at 32104 can read 100 % while a later pack is still empty. Each
# pack publishes its own SOC on a stride-100 layout — 34000 + 100·(n−1), SOC at
# offset +2 — in deci-percent (the aggregate is whole percent). The addresses
# are 100 registers apart, so no block read applies (REGISTER_BLOCKS never pads
# gaps, issue #361) and each costs its own frame: polled at "low" because a pack
# SOC moves ~0.1 %/min in absorption and a handover takes minutes. Slots this
# installation does not have are dropped by the driver's start-up probe
# (MarstekModbusDriver._learn_packs), so an unused slot costs three probe reads
# once and nothing after that.
#
# Seven slots, not the six of #350: issue #415 reports a seven-pack Venus D. A
# slot missing from this tuple is not polled at all, so its SOC never reaches
# the min() the charge ceiling and discharge floor are taken on. 34602 is
# confirmed on that installation (#415): it tracks pack 7 independently, and the
# stride is not an artefact — adding a pack renumbers from the top, so the new
# pack takes slot 1 and the old six shift up to 34102–34602, addresses included.
# A slot beyond the last populated one still rests on the probe: three reads
# once, then off the schedule for good if nothing answers.
PACK_SOC_KEYS = tuple(f"battery_soc_pack_{n}" for n in range(1, 8))

SENSOR_DEFINITIONS_VA.extend(
    {
        "name": f"Battery SOC Pack {n}",
        "register": 34002 + 100 * (n - 1),
        "scale": 0.1,
        "unit": "%",
        "device_class": "battery",
        "state_class": "measurement",
        "key": key,
        "enabled_by_default": False,
        "data_type": "uint16",
        "precision": 1,
        "scan_interval": "low",
    }
    for n, key in enumerate(PACK_SOC_KEYS, start=1)
)

# --- per-pack cell voltage (issue #439) --------------------------------------
# 37007/37008 are not a device-wide max/min: they are pack 1's, and only pack
# 1's. Firmware v150 confirms it — 37007 and 34005 read through the same source
# pointer 0x20014FC4, 37008 and 34006 through 0x20014FC6 — and #415 proved it
# twice on hardware: adding a seventh pack renumbered the slots from the top and
# the registers followed the *new* pack 1, while the taper latched the instant
# pack 1 crossed 3.48 V rather than when the battery did.
#
# That makes the single "Cell Delta" a pack-1 reading wearing a whole-battery
# label. A Venus A/D charges one pack at a time (register 32111 is the active
# pack index) and rotates every 7-50 minutes, so pack 1 is the pack under load
# in only about one interval in six; the rest of the time the delta describes a
# resting pack. Reading each pack's own pair — offsets +5 and +6 on the same
# stride-100 block the SOC at +2 already uses — is what makes the number
# attributable, and it is cheap: 14 registers, not the 112 of individual cells.
#
# The entities are off by default, because fourteen extra diagnostic rows per
# battery is clutter for the many owners who will never plot them. The *reads*
# are not: the balance monitor needs these values to attribute a delta at all, so
# the driver declares them as balance dependencies and they keep polling with
# their entities disabled — the same split the pack SOCs and 37007/37008 already
# use. A user who wants the per-pack numbers on a chart enables the entities; the
# delta is right either way.
#
# One frame per pack, not two: each pair is adjacent, so it is block-read
# (REGISTER_BLOCKS_VA_PACK_CELLS below) at "low". On a four-pack Venus D that is
# four extra frames per 30 s cycle, ~600 ms of a bus with one TCP slot.
#
# Pack 1's pair is firmware-confirmed (the shared pointers above). Packs 2-7 are
# the same stride the SOC uses at +2, which is confirmed on hardware up to
# 34602 (#415), applied to offsets +5/+6 — reasoned, not read. Nothing guards
# that beyond what the hardware itself says: a slot whose registers do not answer
# is written off by the start-up probe after three tries, exactly like a slot
# with no pack in it.
PACK_MAX_CELL_KEYS = tuple(f"max_cell_voltage_pack_{n}" for n in range(1, 8))
PACK_MIN_CELL_KEYS = tuple(f"min_cell_voltage_pack_{n}" for n in range(1, 8))

SENSOR_DEFINITIONS_VA.extend(
    {
        "name": f"{label} Cell Voltage Pack {n}",
        "register": 34000 + 100 * (n - 1) + offset,
        "scale": 0.001,
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
        "key": key,
        "enabled_by_default": False,
        "data_type": "int16",
        "precision": 3,
        "scan_interval": "low",
    }
    for label, offset, keys in (
        ("Max", 5, PACK_MAX_CELL_KEYS),
        ("Min", 6, PACK_MIN_CELL_KEYS),
    )
    for n, key in enumerate(keys, start=1)
)

# One request per pack: max/min sit next to each other, so the pair costs a
# single frame instead of two. Venus A/D only — a v3 shares the entity map but
# has no 34000-block, and an unconditional block group would burn a failing read
# on it every cycle. An absent slot's group is pruned by the SOC probe along with
# its keys.
REGISTER_BLOCKS_VA_PACK_CELLS = [
    {
        "start": 34000 + 100 * (n - 1) + 5,
        "count": 2,
        "scan_interval": "low",
        "members": [
            {"key": f"max_cell_voltage_pack_{n}", "offset": 0, "count": 1, "data_type": "int16"},
            {"key": f"min_cell_voltage_pack_{n}", "offset": 1, "count": 1, "data_type": "int16"},
        ],
    }
    for n in range(1, 8)
]
