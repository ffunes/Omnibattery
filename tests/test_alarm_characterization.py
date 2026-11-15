"""Characterization of alarm/fault reporting (T7.8, plan #35).

Pins today's behaviour so the decoding of the alarm/fault bitmasks can move into
the driver without changing it: the system alarm sensor (state, attributes,
order of the labels) and the notifier (what it notifies and what it dismisses
over a sequence of reads, texts, notification ids). Every value is a literal
observed on the pre-refactor code.

Only the Marstek driver publishes ``alarm_status`` / ``fault_status`` (v2 only in
its production register set); the other seven publish neither, so for them the
only data that exists is "absent". Zendure still declares
``has_alarm_registers=True`` without publishing the keys (plan section 8): its
sensor always says "OK", pinned below.

``_check`` is the ONLY place that knows how a coordinator feeds the notifier.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.omnibattery.infra.alarm_notifier import AlarmNotifier
from custom_components.omnibattery.sensors.aggregate_sensors import SystemAlarmSensor
from tests.test_driver_construction_characterization import (
    BRAND_CONFIGS,
    _build_coordinator,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_ha_runtime(monkeypatch):
    monkeypatch.setattr(DataUpdateCoordinator, "__init__", lambda *_a, **_k: None)


class _Services:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data):
        self.calls.append((domain, service, data))


def _coordinator(config, data=None):
    coordinator = _build_coordinator(config)
    coordinator.async_add_listener = lambda *_a, **_k: None
    coordinator.data = data
    return coordinator


def _sensor(coordinators):
    sensor = SystemAlarmSensor(coordinators)
    sensor.hass = None
    return sensor


def _marstek(version="v2", name=None):
    config = dict(BRAND_CONFIGS["marstek"], battery_version=version)
    if name:
        config["name"] = name
    return config


async def _check(coordinator, notifier, data):
    """Feed one read to the notifier the way the coordinator does."""
    coordinator.data = data
    await notifier.check(coordinator.driver.active_alarms(data))


FAULT_ALL = [
    "Grid Overvoltage", "Grid Undervoltage", "Grid Overfrequency",
    "Grid Underfrequency", "Grid Peak Voltage", "Current Dcover", "Voltage Dcover",
    "BAT Overvoltage", "BAT Undervoltage", "BAT Overcurrent", "BAT Low SOC",
    "BAT Communication Failure", "BMS Protect", "Inverter Soft Start Timeout",
    "Self-Checking Failure", "EEPROM Failure", "Other System Failure",
    "Hardware Bus Overvoltage", "Hardware Output Overcurrent",
    "Hardware Trans Overcurrent", "Hardware Battery Overcurrent",
    "Hardware Protection", "Output Overcurrent",
]
ALARM_ALL = [
    "PLL Abnormal Restart", "Overtemperature Limit", "Low Temperature Limit",
    "Fan Abnormal Warning", "Low Battery SOC Warning", "Output Overcurrent Warning",
    "Abnormal Line Sequence Detection", "WiFi Abnormal", "BLE Abnormal",
    "Network Abnormal", "CT Connection Abnormal",
]

# name -> (data, expected state, expected labels for the coordinator "<name>")
SENSOR_CASES = {
    "absent": ({}, "OK", None),
    "zero": ({"alarm_status": 0, "fault_status": 0}, "OK", None),
    "none_values": ({"alarm_status": None, "fault_status": None}, "OK", None),
    "alarm_bit0": (
        {"alarm_status": 1, "fault_status": 0}, "Warning",
        ["[Alarm] PLL Abnormal Restart"]),
    "fault_bit0": (
        {"alarm_status": 0, "fault_status": 1}, "Fault",
        ["[Fault] Grid Overvoltage"]),
    "alarm_only_key": (
        {"alarm_status": 0b1000}, "Warning", ["[Alarm] Fan Abnormal Warning"]),
    "fault_only_key": (
        {"fault_status": 1 << 21}, "Fault", ["[Fault] BMS Protect"]),
    "several_alarms": (
        {"alarm_status": 1 | 2 | (1 << 16) | (1 << 19), "fault_status": 0}, "Warning",
        ["[Alarm] PLL Abnormal Restart", "[Alarm] Overtemperature Limit",
         "[Alarm] WiFi Abnormal", "[Alarm] CT Connection Abnormal"]),
    "several_faults": (
        {"alarm_status": 0, "fault_status": 1 | (1 << 5) | (1 << 16) | (1 << 31)}, "Fault",
        ["[Fault] Grid Overvoltage", "[Fault] Current Dcover",
         "[Fault] BAT Overvoltage", "[Fault] Output Overcurrent"]),
    "fault_and_alarm": (
        {"alarm_status": 0b10, "fault_status": 0b100}, "Fault",
        ["[Fault] Grid Overfrequency", "[Alarm] Overtemperature Limit"]),
    # Bits with no description still raise the state but add no label.
    "unknown_alarm_bit": ({"alarm_status": 1 << 10, "fault_status": 0}, "Warning", None),
    "unknown_fault_bit": ({"alarm_status": 0, "fault_status": 1 << 8}, "Fault", None),
    "unknown_and_known": (
        {"alarm_status": (1 << 10) | 1, "fault_status": 1 << 8}, "Fault",
        ["[Alarm] PLL Abnormal Restart"]),
    "every_bit": (
        {"alarm_status": 0xFFFFFFFF, "fault_status": 0xFFFFFFFF}, "Fault",
        [f"[Fault] {x}" for x in FAULT_ALL] + [f"[Alarm] {x}" for x in ALARM_ALL]),
}


@pytest.mark.parametrize("version", ["v2", "v3", "vA", "vD"])
@pytest.mark.parametrize("case", list(SENSOR_CASES))
async def test_marstek_sensor(version, case):
    data, state, labels = SENSOR_CASES[case]
    coordinator = _coordinator(_marstek(version, "Bat 1"), dict(data))
    sensor = _sensor([coordinator])
    assert sensor.native_value == state
    assert sensor.extra_state_attributes == ({} if labels is None else {"Bat 1": labels})


@pytest.mark.parametrize("brand", list(BRAND_CONFIGS))
async def test_every_brand_without_alarm_keys_is_ok(brand):
    for data in ({}, {"battery_soc": 50}, {"alarm_status": None, "fault_status": None}):
        coordinator = _coordinator(BRAND_CONFIGS[brand], dict(data))
        sensor = _sensor([coordinator])
        assert sensor.native_value == "OK"
        assert sensor.extra_state_attributes == {}


async def test_coordinator_without_data_is_skipped():
    for data in (None, {}):
        sensor = _sensor([_coordinator(_marstek(), data)])
        assert sensor.native_value == "OK"
        assert sensor.extra_state_attributes == {}


async def test_mixed_batteries():
    batteries = [
        _coordinator(_marstek("v2", "A"), {"alarm_status": 1, "fault_status": 0}),
        _coordinator(BRAND_CONFIGS["zendure"], {"battery_soc": 80}),
        _coordinator(_marstek("v2", "C"), {"alarm_status": 0, "fault_status": 0}),
        _coordinator(_marstek("v2", "D"), {"alarm_status": 0b10, "fault_status": 1 << 16}),
        _coordinator(_marstek("v2", "E"), None),
    ]
    sensor = _sensor(batteries)
    assert sensor.native_value == "Fault"
    assert sensor.extra_state_attributes == {
        "A": ["[Alarm] PLL Abnormal Restart"],
        "D": ["[Fault] BAT Overvoltage", "[Alarm] Overtemperature Limit"],
    }
    # Only warnings across the fleet: state is Warning.
    batteries[3].data = {"alarm_status": 0b10, "fault_status": 0}
    assert sensor.native_value == "Warning"
    batteries[0].data = {}
    batteries[3].data = {}
    assert sensor.native_value == "OK"
    assert sensor.extra_state_attributes == {}


async def test_zendure_declares_alarm_registers_but_always_ok():
    coordinator = _coordinator(BRAND_CONFIGS["zendure"], {"battery_soc": 10})
    assert coordinator.driver.capabilities.has_alarm_registers is True
    assert _sensor([coordinator]).native_value == "OK"


@pytest.mark.parametrize("brand,expected", [
    ("marstek", False), ("zendure", True), ("esphome", False), ("anker", False),
    ("sessy", False), ("indevolt", False), ("huawei", False), ("hoymiles", False),
])
async def test_has_alarm_registers_per_brand(brand, expected):
    coordinator = _coordinator(BRAND_CONFIGS[brand])
    assert coordinator.driver.capabilities.has_alarm_registers is expected


async def test_marstek_has_alarm_registers_only_on_v2():
    flags = {
        v: _coordinator(_marstek(v)).driver.capabilities.has_alarm_registers
        for v in ("v2", "v3", "vA", "vD")
    }
    assert flags == {"v2": True, "v3": False, "vA": False, "vD": False}


async def test_v2_register_set_keeps_the_alarm_registers_in_place():
    driver = _coordinator(_marstek("v2")).driver
    keys = [d["key"] for d in driver.sensor_definitions]
    assert keys == [
        "battery_soc", "battery_total_energy", "battery_power", "internal_temperature",
        "ac_power", "total_charging_energy", "total_discharging_energy",
        "inverter_state", "battery_voltage", "max_cell_voltage", "min_cell_voltage",
        "fault_status", "alarm_status", "ac_offgrid_power", "device_name", "sn_code",
        "software_version", "bms_version", "ems_version", "comm_module_firmware",
        "mac_address", "wifi_signal_strength",
    ]
    by_key = {d["key"]: d for d in driver.sensor_definitions}
    fault, alarm = by_key["fault_status"], by_key["alarm_status"]
    assert (fault["register"], fault["data_type"], fault["scan_interval"]) == (
        36100, "uint32", "medium")
    assert (alarm["register"], alarm["data_type"], alarm["scan_interval"]) == (
        36000, "uint32", "medium")
    assert [fault["bit_descriptions"][b] for b in sorted(fault["bit_descriptions"])] == FAULT_ALL
    assert [alarm["bit_descriptions"][b] for b in sorted(alarm["bit_descriptions"])] == ALARM_ALL
    assert len(driver.all_definitions) == 34
    for version in ("v3", "vA", "vD"):
        other = _coordinator(_marstek(version)).driver
        assert "alarm_status" not in {d["key"] for d in other.all_definitions}


async def test_descriptions_are_unique_per_table():
    assert len(set(FAULT_ALL)) == len(FAULT_ALL) and len(set(ALARM_ALL)) == len(ALARM_ALL)


# --------------------------------------------------------------------------- #
# Notifier: sequences of reads -> persistent_notification calls.
# --------------------------------------------------------------------------- #

async def _run(sequence, name="Bat 1"):
    services = _Services()
    hass = MagicMock()
    hass.services = services
    coordinator = _coordinator(_marstek("v2", name))
    notifier = AlarmNotifier(hass, name)
    seen = []
    for data in sequence:
        before = len(services.calls)
        await _check(coordinator, notifier, data)
        seen.append(services.calls[before:])
    return seen


def _notification_id(name="Bat 1"):
    return f"marstek_venus_battery_alarm_{name}"


def _strip(calls):
    return [(d, s, data.get("title"), data.get("message"), data["notification_id"])
            for d, s, data in calls]


async def test_notification_prefix_and_id():
    seen = await _run([{"alarm_status": 0, "fault_status": 1}])
    assert seen[0][0][2]["notification_id"] == _notification_id()


async def test_silent_when_nothing_ever_set():
    for seq in ([{}], [{"alarm_status": 0, "fault_status": 0}] * 3,
                [{"alarm_status": None, "fault_status": None}]):
        assert all(step == [] for step in await _run(seq))


async def test_new_fault_text():
    [calls] = await _run([{"alarm_status": 0, "fault_status": 1 | (1 << 16)}])
    assert _strip(calls) == [(
        "persistent_notification", "create",
        "🚨 Battery Fault: Bat 1",
        "🚨 Fault detected on Bat 1\n\n🆕 New faults:\n  🔴 Grid Overvoltage\n  🔴 BAT Overvoltage",
        _notification_id(),
    )]


async def test_new_alarm_text_is_a_warning():
    [calls] = await _run([{"alarm_status": 0b11, "fault_status": 0}])
    assert _strip(calls) == [(
        "persistent_notification", "create",
        "⚠️ Battery Warning: Bat 1",
        "⚠️ Warning detected on Bat 1\n\n🆕 New alarms:\n  🟡 PLL Abnormal Restart\n"
        "  🟡 Overtemperature Limit",
        _notification_id(),
    )]


async def test_fault_and_alarm_together_with_also_active():
    seq = [
        {"alarm_status": 0b1, "fault_status": 0b1},
        {"alarm_status": 0b11, "fault_status": 0b11},
    ]
    first, second = await _run(seq)
    assert _strip(first) == [(
        "persistent_notification", "create", "🚨 Battery Fault: Bat 1",
        "🚨 Fault detected on Bat 1\n\n🆕 New faults:\n  🔴 Grid Overvoltage\n"
        "🆕 New alarms:\n  🟡 PLL Abnormal Restart",
        _notification_id(),
    )]
    assert _strip(second) == [(
        "persistent_notification", "create", "🚨 Battery Fault: Bat 1",
        "🚨 Fault detected on Bat 1\n\n🆕 New faults:\n  🔴 Grid Undervoltage\n"
        "🆕 New alarms:\n  🟡 Overtemperature Limit\n\n⚡ Also active:\n"
        "  🔴 Grid Overvoltage\n  🟡 PLL Abnormal Restart",
        _notification_id(),
    )]


async def test_sequence_raise_hold_extend_clear_reraise():
    seq = [
        {"alarm_status": 0, "fault_status": 0},
        {"alarm_status": 0, "fault_status": 1},
        {"alarm_status": 0, "fault_status": 1},           # unchanged: silent
        {"alarm_status": 0, "fault_status": 0b101},       # new bit: renotify
        {"alarm_status": 0, "fault_status": 0b001},       # a bit cleared: silent
        {"alarm_status": 0, "fault_status": 0},           # all clear: dismiss
        {"alarm_status": 0, "fault_status": 0},           # nothing: silent
        {"alarm_status": 0b1, "fault_status": 0},         # warning again
        {"alarm_status": 0b1, "fault_status": 0b1},       # fault joins an alarm
        {"alarm_status": 0, "fault_status": 0b1},         # alarm clears, fault stays
        {"alarm_status": 0, "fault_status": 0},           # all clear: dismiss
    ]
    seen = await _run(seq)
    assert [[(s, d["title"] if s == "create" else None) for _, s, d in step]
            for step in seen] == [
        [], [("create", "🚨 Battery Fault: Bat 1")], [],
        [("create", "🚨 Battery Fault: Bat 1")], [], [("dismiss", None)], [],
        [("create", "⚠️ Battery Warning: Bat 1")],
        [("create", "🚨 Battery Fault: Bat 1")], [], [("dismiss", None)],
    ]
    assert seen[5][0][2] == {"notification_id": _notification_id()}
    # The renotification lists the already-active fault under "Also active".
    assert seen[3][0][2]["message"] == (
        "🚨 Fault detected on Bat 1\n\n🆕 New faults:\n  🔴 Grid Overfrequency\n\n"
        "⚡ Also active:\n  🔴 Grid Overvoltage")
    # A fault joining a standing alarm lists it under "Also active".
    assert seen[8][0][2]["message"] == (
        "🚨 Fault detected on Bat 1\n\n🆕 New faults:\n  🔴 Grid Overvoltage\n\n"
        "⚡ Also active:\n  🟡 PLL Abnormal Restart")


async def test_unknown_bits_notify_without_labels_and_still_dismiss():
    seq = [
        {"alarm_status": 1 << 10, "fault_status": 0},
        {"alarm_status": 1 << 10, "fault_status": 0},
        {"alarm_status": 0, "fault_status": 1 << 8},
        {"alarm_status": 0, "fault_status": 0},
    ]
    seen = await _run(seq)
    assert _strip(seen[0]) == [(
        "persistent_notification", "create", "⚠️ Battery Warning: Bat 1",
        "⚠️ Warning detected on Bat 1\n\n🆕 New alarms:", _notification_id())]
    assert seen[1] == []
    assert _strip(seen[2]) == [(
        "persistent_notification", "create", "🚨 Battery Fault: Bat 1",
        "🚨 Fault detected on Bat 1\n\n🆕 New faults:", _notification_id())]
    assert [s for _, s, _ in seen[3]] == ["dismiss"]


async def test_none_values_between_reads_count_as_cleared():
    seq = [
        {"alarm_status": 0, "fault_status": 1},
        {"alarm_status": None, "fault_status": None},
        {"alarm_status": 0, "fault_status": 1},
    ]
    seen = await _run(seq)
    assert [[s for _, s, _ in step] for step in seen] == [["create"], ["dismiss"], ["create"]]


async def test_absent_keys_count_as_cleared():
    seen = await _run([{"fault_status": 1}, {}, {"fault_status": 1}])
    assert [[s for _, s, _ in step] for step in seen] == [["create"], ["dismiss"], ["create"]]


async def test_notification_id_follows_the_device_name():
    seen = await _run([{"fault_status": 1}], name="Garage")
    assert seen[0][0][2]["notification_id"] == _notification_id("Garage")
    assert seen[0][0][2]["title"] == "🚨 Battery Fault: Garage"
