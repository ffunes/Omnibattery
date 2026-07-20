"""Unit tests for the Anker Solarbank Max AC Modbus driver."""
from __future__ import annotations

from unittest.mock import AsyncMock, call

import pytest

from custom_components.omnibattery.drivers import AnkerModbusDriver, DriverCapabilities
from custom_components.omnibattery.infra.anker_modbus_client import encode_int32
from custom_components.omnibattery.infra.modbus_client import decode_registers


def _fake_client():
    client = AsyncMock()
    client.connected = True
    client.unit_id = 1
    client.async_connect = AsyncMock(return_value=True)
    client.async_close = AsyncMock()
    client.set_shutting_down = AsyncMock()
    client.async_write_register = AsyncMock(return_value=True)
    client.async_write_registers_int32 = AsyncMock(return_value=True)
    client.async_read_holding_register = AsyncMock(return_value=3)
    client.async_read_input_register = AsyncMock(return_value=55)
    client.async_read_input_block = AsyncMock(return_value=None)
    client.async_read_holding_block = AsyncMock(return_value=None)
    return client


def _driver(client=None, **kw):
    return AnkerModbusDriver(
        "1.2.3.4",
        502,
        1,
        client=client or _fake_client(),
        **kw,
    )


# ----------------------------------------------------------------------
# capabilities / identity
# ----------------------------------------------------------------------
def test_capabilities():
    caps = _driver().capabilities
    assert isinstance(caps, DriverCapabilities)
    assert caps.hardware_soc_cutoff is True
    assert caps.has_force_mode is False
    assert caps.has_rs485_control is False
    assert caps.has_mppt_pv is False
    assert caps.has_energy_counters is True
    assert caps.max_charge_power_w == 3500
    assert caps.max_discharge_power_w == 3500
    assert caps.min_charge_power_w == 100
    assert caps.min_discharge_power_w == 100
    assert caps.setpoint_confirm_reliable is False


def test_model_label():
    assert _driver().model_label == "Solarbank Max AC"


def test_power_caps_are_read_only_sensors():
    """Hardware max charge/discharge (10036/10038) are sensors only — not
    writable numbers and not setup sliders. Soft-max entities must not claim
    the same unique_ids."""
    from custom_components.omnibattery.drivers import anker as anker_mod

    sensor_keys = {d["key"] for d in anker_mod.SENSOR_DEFINITIONS}
    number_keys = {d["key"] for d in anker_mod.NUMBER_DEFINITIONS}
    field_keys = {f["key"] for f in anker_mod._FIELD_SPECS}
    assert "max_charge_power" in sensor_keys
    assert "max_discharge_power" in sensor_keys
    assert "max_charge_power" not in number_keys
    assert "max_discharge_power" not in number_keys
    assert "max_charge_power" in field_keys
    assert "max_discharge_power" in field_keys
    assert "max_charge_power" in _driver().control_dependency_keys
    assert "max_discharge_power" in _driver().control_dependency_keys


def test_status_and_mode_sensors_have_state_maps():
    from custom_components.omnibattery.drivers import anker as anker_mod

    by_key = {d["key"]: d for d in anker_mod.SENSOR_DEFINITIONS}
    assert by_key["battery_status"]["states"][1] == "Charging"
    assert by_key["battery_status"]["states"][2] == "Discharging"
    assert by_key["operating_mode"]["states"][3] == "Third-Party Control"
    # Register 10156 reports tenths of a degree (340 → 34.0 °C).
    assert by_key["temperature"]["scale"] == 0.1


def test_encode_int32_roundtrip():
    for value in (0, 500, -800, 3500, -3500):
        words = encode_int32(value)
        assert decode_registers(words, "int32") == value


# ----------------------------------------------------------------------
# connect / mode
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_connect_sets_third_party_mode_when_needed():
    client = _fake_client()
    client.async_read_holding_register = AsyncMock(return_value=0)
    drv = _driver(client=client)
    assert await drv.connect() is True
    client.async_write_register.assert_awaited_with(10064, 3)


@pytest.mark.asyncio
async def test_connect_skips_mode_write_when_already_third_party():
    client = _fake_client()
    client.async_read_holding_register = AsyncMock(return_value=3)
    drv = _driver(client=client)
    assert await drv.connect() is True
    client.async_write_register.assert_not_awaited()


# ----------------------------------------------------------------------
# telemetry — FC03/FC04
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_read_telemetry_uses_fc04_for_input_and_inverts_battery_power():
    client = _fake_client()

    # Range 10000–10050: SOC at 10014, battery power at 10008 (+discharge on wire)
    # Build a buffer of zeros then poke the fields.
    buf = [0] * 51
    # int32 +612 discharge at 10008 → offsets 8,9
    buf[8], buf[9] = encode_int32(612)
    buf[14] = 47  # SOC
    client.async_read_input_block = AsyncMock(return_value=buf)
    client.async_read_holding_block = AsyncMock(return_value=None)

    drv = _driver(client=client)
    snap = await drv.read_telemetry(["battery_soc", "battery_power"])

    # Must use input (FC04), not holding
    client.async_read_input_block.assert_awaited()
    for c in client.async_read_input_block.await_args_list:
        assert c.args[0] == 10000
    client.async_read_holding_block.assert_not_awaited()

    assert snap["battery_soc"] == 47
    assert snap["battery_power"] == -612  # inverted to Omnibattery convention


@pytest.mark.asyncio
async def test_read_telemetry_caps_peak_charge_power_at_hw_max():
    """Register 10036 can report peak/aggregate values (e.g. 7000 W); clamp to 3500."""
    client = _fake_client()
    buf = [0] * 51
    buf[36], buf[37] = encode_int32(7000)
    buf[38], buf[39] = encode_int32(3000)
    client.async_read_input_block = AsyncMock(return_value=buf)
    client.async_read_holding_block = AsyncMock(return_value=None)

    drv = _driver(client=client)
    snap = await drv.read_telemetry(["max_charge_power", "max_discharge_power"])

    assert snap["max_charge_power"] == 3500
    assert snap["max_discharge_power"] == 3000


@pytest.mark.asyncio
async def test_read_telemetry_uses_fc03_for_operating_mode_and_soc_limits():
    client = _fake_client()
    client.async_read_input_block = AsyncMock(return_value=None)

    holding_mode = [0] * 13  # 10060–10072
    holding_mode[4] = 3  # 10064
    holding_soc = [95, 12, 0, 0]  # 60000–60003; ignore 60002/60003

    async def _holding(start, count):
        if start == 10060:
            return holding_mode
        if start == 60000:
            return holding_soc
        return None

    client.async_read_holding_block = AsyncMock(side_effect=_holding)
    drv = _driver(client=client)
    snap = await drv.read_telemetry(
        ["operating_mode", "charging_cutoff_capacity", "discharging_cutoff_capacity"]
    )

    assert snap["operating_mode"] == 3
    assert snap["charging_cutoff_capacity"] == 95
    assert snap["discharging_cutoff_capacity"] == 12
    starts = [c.args[0] for c in client.async_read_holding_block.await_args_list]
    assert 10060 in starts
    assert 60000 in starts
    client.async_read_input_block.assert_not_awaited()


# ----------------------------------------------------------------------
# apply_setpoint
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_apply_setpoint_charge_writes_negative_wire_value():
    client = _fake_client()
    drv = _driver(client=client)
    result = await drv.apply_setpoint(500)
    assert result.ok is True
    assert result.net_power_w == 500
    assert result.confirmed is True
    client.async_write_registers_int32.assert_awaited_with(10071, -500)


@pytest.mark.asyncio
async def test_apply_setpoint_discharge_writes_positive_wire_value():
    client = _fake_client()
    drv = _driver(client=client)
    result = await drv.apply_setpoint(-800)
    assert result.ok is True
    assert result.net_power_w == -800
    client.async_write_registers_int32.assert_awaited_with(10071, 800)


@pytest.mark.asyncio
async def test_apply_setpoint_idle_writes_zero():
    client = _fake_client()
    drv = _driver(client=client)
    result = await drv.apply_setpoint(0)
    assert result.ok is True
    assert result.net_power_w == 0
    client.async_write_registers_int32.assert_awaited_with(10071, 0)


@pytest.mark.asyncio
async def test_apply_setpoint_snaps_forbidden_band_to_idle():
    client = _fake_client()
    drv = _driver(client=client)
    result = await drv.apply_setpoint(50)
    assert result.ok is True
    assert result.net_power_w == 0
    client.async_write_registers_int32.assert_awaited_with(10071, 0)


@pytest.mark.asyncio
async def test_apply_setpoint_clamps_to_3500():
    client = _fake_client()
    drv = _driver(client=client)
    result = await drv.apply_setpoint(5000)
    assert result.net_power_w == 3500
    client.async_write_registers_int32.assert_awaited_with(10071, -3500)


@pytest.mark.asyncio
async def test_net_power_from_data_uses_applied_echo():
    client = _fake_client()
    drv = _driver(client=client)
    result = await drv.apply_setpoint(-400)
    assert drv.net_power_from_data(result.applied) == -400


@pytest.mark.asyncio
async def test_net_power_from_data_rejects_stale_echo_after_reconnect():
    client = _fake_client()
    drv = _driver(client=client)
    result = await drv.apply_setpoint(-400)

    assert await drv.connect() is True
    assert drv.net_power_from_data(result.applied) is None


@pytest.mark.asyncio
async def test_net_power_from_data_rejects_echo_outside_third_party_mode():
    drv = _driver()
    result = await drv.apply_setpoint(400)
    result.applied["operating_mode"] = 0

    assert drv.net_power_from_data(result.applied) is None


# ----------------------------------------------------------------------
# SOC limits
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_apply_config_writes_60000_and_60001_only():
    client = _fake_client()
    drv = _driver(client=client)
    ok = await drv.apply_config(
        max_soc_pct=95,
        min_soc_pct=10,
        max_charge_power_w=3500,
        max_discharge_power_w=3500,
    )
    assert ok is True
    assert client.async_write_register.await_args_list == [
        call(60000, 95),
        call(60001, 10),
    ]
    written_addrs = [c.args[0] for c in client.async_write_register.await_args_list]
    assert 60002 not in written_addrs
    assert 60003 not in written_addrs


@pytest.mark.asyncio
async def test_apply_config_clamps_to_device_ranges():
    client = _fake_client()
    drv = _driver(client=client)
    await drv.apply_config(
        max_soc_pct=70,  # below 80
        min_soc_pct=40,  # above 20
        max_charge_power_w=3500,
        max_discharge_power_w=3500,
    )
    assert client.async_write_register.await_args_list == [
        call(60000, 80),
        call(60001, 20),
    ]


@pytest.mark.asyncio
async def test_set_charge_cutoff_writes_60000_only():
    client = _fake_client()
    drv = _driver(client=client)
    assert await drv.set_charge_cutoff(100) is True
    client.async_write_register.assert_awaited_once_with(60000, 100)


@pytest.mark.asyncio
async def test_standby_writes_zero_setpoint():
    client = _fake_client()
    drv = _driver(client=client)
    assert await drv.standby() is True
    client.async_write_registers_int32.assert_awaited_with(10071, 0)


@pytest.mark.asyncio
async def test_probe_reads_soc_and_power_caps(monkeypatch):
    created = {}

    class FakeClient:
        def __init__(self, host, port, slave_id=1, timeout=5.0):
            created["args"] = (host, port, slave_id)
            self.unit_id = slave_id
            self.connected = True
            self.async_connect = AsyncMock(return_value=True)
            self.async_close = AsyncMock()

            async def _read_input(address, data_type="uint16", count=None):
                if address == 10014:
                    return 55
                if address == 10036:
                    return 3000
                if address == 10038:
                    return 2800
                return None

            self.async_read_input_register = AsyncMock(side_effect=_read_input)

    monkeypatch.setattr(
        "custom_components.omnibattery.drivers.anker.AnkerModbusClient",
        FakeClient,
    )
    ok, caps = await AnkerModbusDriver.probe("10.0.0.5", 502, 1)
    assert ok is True
    assert created["args"] == ("10.0.0.5", 502, 1)
    assert caps["device_max_charge_power"] == 3000
    assert caps["device_max_discharge_power"] == 2800
