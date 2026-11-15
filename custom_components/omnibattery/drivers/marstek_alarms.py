"""Marstek alarm and fault registers: bit tables, register definitions, decoding.

Marstek publishes two bitmask registers (``fault_status`` 36100, ``alarm_status``
36000; v2 only in the production register set). They are decoded here so the
shared code (system alarm sensor, notifier) only ever sees
:class:`~.base.DriverAlarm` entries from ``BatteryDriver.active_alarms``.
"""
from __future__ import annotations

from .base import DriverAlarm

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

FAULT_STATUS_DEFINITION: dict = {
    # Fault status bits indicating various device faults
    "name": "Fault Status",
    "register": 36100,
    "data_type": "uint32",
    "key": "fault_status",
    "device_class": "problem",
    "icon": "mdi:alert",
    "category": "diagnostic",
    "enabled_by_default": True,
    "scan_interval": "medium",
    "bit_descriptions": FAULT_BIT_DESCRIPTIONS,
}

ALARM_STATUS_DEFINITION: dict = {
    # Alarm status bits indicating various device alarms
    "name": "Alarm Status",
    "register": 36000,
    "data_type": "uint32",
    "key": "alarm_status",
    "device_class": "problem",
    "icon": "mdi:alert",
    "enabled_by_default": True,
    "category": "diagnostic",
    "unit": None,
    "precision": 0,
    "scan_interval": "medium",
    "bit_descriptions": ALARM_BIT_DESCRIPTIONS,
}


def with_alarm_registers(sensor_definitions: list[dict]) -> list[dict]:
    """Return the v2 sensor list with the fault and alarm registers inserted.

    They sit right after ``min_cell_voltage`` (the position they had in the
    shared v2 register file), which keeps entity order and register blocks as
    they were.
    """
    at = next(
        i for i, d in enumerate(sensor_definitions) if d["key"] == "min_cell_voltage"
    ) + 1
    return [
        *sensor_definitions[:at],
        FAULT_STATUS_DEFINITION,
        ALARM_STATUS_DEFINITION,
        *sensor_definitions[at:],
    ]


def _decode(kind: str, value: int, descriptions: dict[int, str]) -> list[DriverAlarm]:
    return [
        DriverAlarm(kind, bit, descriptions.get(bit))
        for bit in range(value.bit_length())
        if value & (1 << bit)
    ]


def decode_alarms(data: dict) -> list[DriverAlarm]:
    """Active faults then alarms from the two status bitmasks in ``data``.

    A missing or ``None`` mask counts as 0. A set bit with no description is
    still reported (with ``description=None``).
    """
    return _decode("fault", data.get("fault_status") or 0, FAULT_BIT_DESCRIPTIONS) + _decode(
        "alarm", data.get("alarm_status") or 0, ALARM_BIT_DESCRIPTIONS
    )
