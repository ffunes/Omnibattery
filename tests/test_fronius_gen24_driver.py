"""Dry tests for the Fronius GEN24 / BYD OmniBattery driver.

These tests intentionally avoid importing Home Assistant. The local Codex
environment used for this repository does not ship HA or pymodbus packages, so
the driver contract and Modbus client are stubbed just enough to load the driver
module and verify its register math.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class DriverCapabilities:
    hardware_soc_cutoff: bool
    has_force_mode: bool
    push_telemetry: bool
    max_charge_power_w: int
    max_discharge_power_w: int
    min_charge_power_w: int = 0
    min_discharge_power_w: int = 0
    has_mppt_pv: bool = False
    has_alarm_registers: bool = False
    has_rs485_control: bool = False
    has_energy_counters: bool = True
    has_nominal_capacity: bool = True
    cycles_from_discharge_only: bool = False
    has_daily_energy_counters: bool = True
    setpoint_confirm_reliable: bool = True
    actuator_latency_s: float = 0.5
    readback_latency_s: Optional[float] = None


@dataclass(frozen=True)
class ReadGroup:
    scan_interval: Optional[str]
    keys: tuple[str, ...]


@dataclass(frozen=True)
class SetpointResult:
    ok: bool
    net_power_w: int
    confirmed: bool
    failure_reason: Optional[str] = None
    exact: bool = True
    battery_power_w: Optional[int] = None
    applied: Optional[dict] = None


class BatteryDriver:
    pass


class MarstekModbusClient:
    def __init__(self, *args, **kwargs) -> None:
        self.connected = False
        self.unit_id = kwargs.get("slave_id", 1)


def decode_registers(regs, data_type: str = "uint16", bit_index: Optional[int] = None):
    if not regs:
        return None
    if data_type == "int16":
        value = int(regs[0])
        return value - 0x10000 if value >= 0x8000 else value
    if data_type == "uint16":
        return int(regs[0])
    raise ValueError(f"Unsupported data_type: {data_type}")


def _install_driver_stubs() -> None:
    sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
    sys.modules.setdefault("custom_components.omnibattery", types.ModuleType("custom_components.omnibattery"))
    sys.modules.setdefault("custom_components.omnibattery.drivers", types.ModuleType("custom_components.omnibattery.drivers"))
    sys.modules.setdefault("custom_components.omnibattery.infra", types.ModuleType("custom_components.omnibattery.infra"))

    base = types.ModuleType("custom_components.omnibattery.drivers.base")
    base.BatteryDriver = BatteryDriver
    base.DriverCapabilities = DriverCapabilities
    base.ReadGroup = ReadGroup
    base.SetpointResult = SetpointResult
    base.TelemetrySnapshot = dict
    sys.modules["custom_components.omnibattery.drivers.base"] = base

    modbus_client = types.ModuleType("custom_components.omnibattery.infra.modbus_client")
    modbus_client.MarstekModbusClient = MarstekModbusClient
    modbus_client.decode_registers = decode_registers
    sys.modules["custom_components.omnibattery.infra.modbus_client"] = modbus_client


def _load_driver_module():
    module_name = "custom_components.omnibattery.drivers.fronius_gen24"
    stub_names = (
        "custom_components",
        "custom_components.omnibattery",
        "custom_components.omnibattery.drivers",
        "custom_components.omnibattery.infra",
        "custom_components.omnibattery.drivers.base",
        "custom_components.omnibattery.infra.modbus_client",
        module_name,
    )
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in stub_names}
    _install_driver_stubs()
    module_path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "omnibattery"
        / "drivers"
        / "fronius_gen24.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    try:
        spec.loader.exec_module(module)
    finally:
        for name, original in previous.items():
            if original is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
    return module


driver = _load_driver_module()


def word(value: int) -> int:
    return value & 0xFFFF


class FakeClient:
    def __init__(self) -> None:
        self.connected = True
        self.unit_id = None
        self.writes: list[tuple[int, int]] = []

    async def async_connect(self) -> bool:
        self.connected = True
        return True

    async def async_close(self) -> None:
        self.connected = False

    def set_shutting_down(self, value: bool) -> None:
        self.shutting_down = value

    async def async_read_block(self, address: int, count: int, block_key: str):
        raise AssertionError("read-back is disabled in these dry tests")

    async def async_write_register(self, address: int, value: int) -> bool:
        self.writes.append((address, value))
        return True


class FakeHttpResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def json(self, content_type=None):
        return self._payload


class FakeHttpSession:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.closed = False
        self.urls: list[str] = []

    def get(self, url: str) -> FakeHttpResponse:
        self.urls.append(url)
        return FakeHttpResponse(self.payload)

    async def close(self) -> None:
        self.closed = True


STORAGE_API_PAYLOAD = {
    "Body": {
        "Data": {
            "0": {
                "Controller": {
                    "Capacity_Maximum": 10240.0,
                    "Current_DC": -0.89710383800329652,
                    "DesignedCapacity": 10240.0,
                    "Details": {
                        "Manufacturer": "BYD",
                        "Model": "BYD Battery-Box Premium HV",
                        "Serial": "P030T020Z2112160742     ",
                    },
                    "Enable": 1,
                    "StateOfCharge_Relative": 92.599998474121094,
                    "Temperature_Cell": 26.0,
                    "Voltage_DC": 424.70001220703125,
                },
                "Modules": [],
            }
        }
    }
}


class FroniusGen24DriverTests(unittest.TestCase):
    def test_charge_plan_matches_existing_ha_script(self) -> None:
        plan = driver.plan_storage_setpoint(
            500,
            wcha_max_w=5000,
            max_charge_power_w=5000,
            max_discharge_power_w=5000,
        )

        self.assertEqual(plan.net_power_w, 500)
        self.assertEqual(plan.mode, "charge")
        self.assertEqual(plan.outwrte_word, 64535)
        self.assertEqual(plan.inwrte_word, 1001)
        self.assertEqual(
            [(write.address, write.value) for write in plan.writes],
            [(40365, 64535), (40366, 1001), (40358, 3), (40365, 64535), (40366, 1001)],
        )

    def test_discharge_plan_matches_existing_ha_script(self) -> None:
        plan = driver.plan_storage_setpoint(
            -500,
            wcha_max_w=5000,
            max_charge_power_w=5000,
            max_discharge_power_w=5000,
        )

        self.assertEqual(plan.net_power_w, -500)
        self.assertEqual(plan.mode, "discharge")
        self.assertEqual(plan.outwrte_word, 1002)
        self.assertEqual(plan.inwrte_word, 64535)
        self.assertEqual(
            [(write.address, write.value) for write in plan.writes],
            [(40358, 3), (40365, 1002), (40366, 64535), (40365, 1002), (40366, 64535)],
        )

    def test_neutral_and_auto_reset_plans(self) -> None:
        neutral = driver.plan_storage_setpoint(
            0,
            wcha_max_w=5000,
            max_charge_power_w=5000,
            max_discharge_power_w=5000,
        )

        self.assertEqual(neutral.net_power_w, 0)
        self.assertEqual(neutral.mode, "neutral")
        self.assertEqual(
            [(write.address, write.value) for write in neutral.writes],
            [(40365, 10000), (40366, 10000), (40358, 3), (40365, 10000), (40366, 10000)],
        )
        self.assertEqual(
            [(write.address, write.value) for write in driver.plan_reset_to_auto()],
            [(40358, 0), (40365, 10000), (40360, 500), (40366, 10000)],
        )

    def test_decode_storage_registers_uses_local_mapping_and_scale_factors(self) -> None:
        regs = [0] * 24
        regs[0] = 4895
        regs[3] = 3
        regs[5] = 500
        regs[6] = 8612
        regs[9] = 1
        regs[10] = word(-1001)
        regs[11] = 1001
        regs[15] = 0
        regs[16] = 0
        regs[19] = word(-2)
        regs[20] = word(-2)
        regs[23] = word(-2)

        snapshot = driver.decode_storage_registers(regs)

        self.assertEqual(snapshot["wcha_max"], 4895)
        self.assertEqual(snapshot["max_charge_power"], 4895)
        self.assertEqual(snapshot["max_discharge_power"], 4895)
        self.assertAlmostEqual(snapshot["battery_soc"], 86.12)
        self.assertAlmostEqual(snapshot["min_rsv_pct"], 5.0)
        self.assertEqual(snapshot["storctl_mod"], 3)
        self.assertEqual(snapshot["outwrte"], -1001)
        self.assertEqual(snapshot["inwrte"], 1001)

    def test_decode_dc_power_registers_uses_charge_minus_discharge(self) -> None:
        regs = [0] * 88
        regs[2] = word(-1)
        regs[59] = 12000
        regs[79] = 2500

        snapshot = driver.decode_dc_power_registers(regs)

        self.assertEqual(snapshot["battery_charge_power"], 1200)
        self.assertEqual(snapshot["battery_discharge_power"], 250)
        self.assertEqual(snapshot["battery_power"], 950)
        self.assertEqual(snapshot["ac_power"], -950)
        self.assertEqual(snapshot["inverter_state"], 2)

    def test_decode_storage_api_payload_maps_byd_info_values(self) -> None:
        snapshot = driver.decode_storage_api_payload(STORAGE_API_PAYLOAD)

        self.assertAlmostEqual(snapshot["internal_temperature"], 26.0)
        self.assertAlmostEqual(snapshot["battery_voltage"], 424.70001220703125)
        self.assertAlmostEqual(snapshot["battery_current"], -0.89710383800329652)
        self.assertAlmostEqual(snapshot["battery_soc"], 92.599998474121094)
        self.assertAlmostEqual(snapshot["battery_total_energy"], 10.24)
        self.assertEqual(snapshot["fronius_storage_manufacturer"], "BYD")
        self.assertEqual(snapshot["fronius_storage_model"], "BYD Battery-Box Premium HV")
        self.assertEqual(snapshot["fronius_storage_serial"], "P030T020Z2112160742")

    def test_read_telemetry_fetches_storage_api_from_same_host(self) -> None:
        fake = FakeClient()
        http = FakeHttpSession(STORAGE_API_PAYLOAD)
        battery = driver.FroniusGen24Driver(
            "pv-harig",
            client=fake,
            http_session=http,
        )

        snapshot = asyncio.run(battery.read_telemetry(["internal_temperature", "battery_voltage"]))

        self.assertEqual(http.urls, ["http://pv-harig/solar_api/v1/GetStorageRealtimeData.cgi"])
        self.assertEqual(snapshot["internal_temperature"], 26.0)
        self.assertAlmostEqual(snapshot["battery_voltage"], 424.70001220703125)
        self.assertEqual(snapshot["fronius_storage_manufacturer"], "BYD")
        self.assertEqual(snapshot["fronius_storage_model"], "BYD Battery-Box Premium HV")
        self.assertEqual(snapshot["fronius_storage_serial"], "P030T020Z2112160742")
        self.assertEqual(battery.serial, "P030T020Z2112160742")

    def test_serial_is_unknown_until_physical_byd_serial_is_read(self) -> None:
        battery = driver.FroniusGen24Driver("192.0.2.10", client=FakeClient())

        self.assertIsNone(battery.serial)

    def test_apply_setpoint_writes_planned_registers_without_live_readback(self) -> None:
        fake = FakeClient()
        battery = driver.FroniusGen24Driver(
            "192.0.2.10",
            client=fake,
            max_charge_power_w=5000,
            max_discharge_power_w=5000,
        )

        result = asyncio.run(battery.apply_setpoint(500, read_back=False))

        self.assertTrue(result.ok)
        self.assertFalse(result.confirmed)
        self.assertEqual(result.net_power_w, 500)
        self.assertEqual(
            fake.writes,
            [(40365, 64535), (40366, 1001), (40358, 3), (40365, 64535), (40366, 1001)],
        )

    def test_small_discharge_request_is_suppressed_to_neutral(self) -> None:
        fake = FakeClient()
        battery = driver.FroniusGen24Driver(
            "192.0.2.10",
            client=fake,
            max_charge_power_w=5000,
            max_discharge_power_w=5000,
        )

        result = asyncio.run(battery.apply_setpoint(-75, read_back=False))

        self.assertTrue(result.ok)
        self.assertFalse(result.confirmed)
        self.assertEqual(result.net_power_w, 0)
        self.assertEqual(
            fake.writes,
            [(40365, 10000), (40366, 10000), (40358, 3), (40365, 10000), (40366, 10000)],
        )

    def test_discharge_suppression_includes_threshold_value(self) -> None:
        fake = FakeClient()
        battery = driver.FroniusGen24Driver(
            "192.0.2.10",
            client=fake,
            max_charge_power_w=5000,
            max_discharge_power_w=5000,
        )

        result = asyncio.run(battery.apply_setpoint(-750, read_back=False))

        self.assertTrue(result.ok)
        self.assertEqual(result.net_power_w, 0)
        self.assertEqual(
            fake.writes,
            [(40365, 10000), (40366, 10000), (40358, 3), (40365, 10000), (40366, 10000)],
        )

    def test_idle_at_max_soc_is_held_with_tiny_charge_limit(self) -> None:
        fake = FakeClient()
        battery = driver.FroniusGen24Driver(
            "192.0.2.10",
            client=fake,
            max_charge_power_w=5000,
            max_discharge_power_w=5000,
        )
        asyncio.run(
            battery.apply_config(
                max_soc_pct=90,
                min_soc_pct=16,
                max_charge_power_w=5000,
                max_discharge_power_w=5000,
            )
        )
        battery._last_soc_pct = 90.0

        result = asyncio.run(battery.apply_setpoint(0, read_back=False))
        hold = driver.plan_storage_setpoint(
            10,
            wcha_max_w=5000,
            max_charge_power_w=5000,
            max_discharge_power_w=5000,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.net_power_w, 10)
        self.assertEqual(fake.writes, [(write.address, write.value) for write in hold.writes])

    def test_idle_below_max_soc_stays_neutral(self) -> None:
        fake = FakeClient()
        battery = driver.FroniusGen24Driver(
            "192.0.2.10",
            client=fake,
            max_charge_power_w=5000,
            max_discharge_power_w=5000,
        )
        asyncio.run(
            battery.apply_config(
                max_soc_pct=90,
                min_soc_pct=16,
                max_charge_power_w=5000,
                max_discharge_power_w=5000,
            )
        )
        battery._last_soc_pct = 89.9

        result = asyncio.run(battery.apply_setpoint(0, read_back=False))

        self.assertTrue(result.ok)
        self.assertEqual(result.net_power_w, 0)
        self.assertEqual(
            fake.writes,
            [(40365, 10000), (40366, 10000), (40358, 3), (40365, 10000), (40366, 10000)],
        )

    def test_small_discharge_at_max_soc_is_held_with_tiny_charge_limit(self) -> None:
        fake = FakeClient()
        battery = driver.FroniusGen24Driver(
            "192.0.2.10",
            client=fake,
            max_charge_power_w=5000,
            max_discharge_power_w=5000,
        )
        asyncio.run(
            battery.apply_config(
                max_soc_pct=90,
                min_soc_pct=16,
                max_charge_power_w=5000,
                max_discharge_power_w=5000,
            )
        )
        battery._last_soc_pct = 91.0

        result = asyncio.run(battery.apply_setpoint(-250, read_back=False))
        hold = driver.plan_storage_setpoint(
            10,
            wcha_max_w=5000,
            max_charge_power_w=5000,
            max_discharge_power_w=5000,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.net_power_w, 10)
        self.assertEqual(fake.writes, [(write.address, write.value) for write in hold.writes])

    def test_material_discharge_request_is_still_applied(self) -> None:
        fake = FakeClient()
        battery = driver.FroniusGen24Driver(
            "192.0.2.10",
            client=fake,
            max_charge_power_w=5000,
            max_discharge_power_w=5000,
        )

        result = asyncio.run(battery.apply_setpoint(-1000, read_back=False))

        self.assertTrue(result.ok)
        self.assertEqual(result.net_power_w, -1000)
        self.assertEqual(
            fake.writes,
            [(40358, 3), (40365, 2002), (40366, 63535), (40365, 2002), (40366, 63535)],
        )

    def test_standby_returns_to_fronius_auto_mode(self) -> None:
        fake = FakeClient()
        battery = driver.FroniusGen24Driver("192.0.2.10", client=fake)

        self.assertTrue(asyncio.run(battery.standby()))
        self.assertEqual(fake.writes, [(40358, 0), (40365, 10000), (40360, 500), (40366, 10000)])


if __name__ == "__main__":
    unittest.main()
