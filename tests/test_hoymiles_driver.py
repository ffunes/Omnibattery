"""Hoymiles MQTT driver contract tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.omnibattery.drivers.hoymiles import (
    HoymilesMqttDriver,
    hoymiles_capacity_kwh,
    hoymiles_model_profile,
)
from custom_components.omnibattery.config_flow import (
    MarstekVenusConfigFlow,
    _hoymiles_apply_probe_caps,
    _hoymiles_power_ceilings,
)


def _hass():
    return SimpleNamespace(
        async_create_background_task=lambda coro, name: asyncio.create_task(
            coro, name=name
        )
    )


class _Mqtt:
    def __init__(self):
        self.callbacks = {}
        self.published = []
        self.unsubscribed = []
        self.fail_publish = False

    async def subscribe(self, hass, topic, callback, qos=0):
        self.callbacks[topic] = callback
        return lambda: self.unsubscribed.append(topic)

    async def publish(self, hass, topic, payload, qos=0, retain=False):
        if self.fail_publish:
            raise RuntimeError("broker unavailable")
        self.published.append((topic, payload, qos, retain))


@pytest.fixture
def mqtt_mock(monkeypatch):
    fake = _Mqtt()
    monkeypatch.setattr("custom_components.omnibattery.drivers.hoymiles.mqtt.async_subscribe", fake.subscribe)
    monkeypatch.setattr("custom_components.omnibattery.drivers.hoymiles.mqtt.async_publish", fake.publish)
    return fake


@pytest.mark.asyncio
async def test_mqtt_telemetry_uses_aggregate_values_and_inverts_wire_sign(mqtt_mock):
    hass = _hass()
    driver = HoymilesMqttDriver(
        hass, "MSA-1", max_charge_power_w=1800, max_discharge_power_w=1800
    )
    assert await driver.connect()
    assert "homeassistant/sensor/MSA-1/quick/state" in mqtt_mock.callbacks
    mqtt_mock.callbacks[driver._quick_topic](SimpleNamespace(payload='{"soc": 20, "bat_p": -80, "sys_soc": 50, "sys_bat_p": 300, "bat_sts":"discharge"}'))
    mqtt_mock.callbacks[driver._device_topic](SimpleNamespace(payload='{"bat_v":51.2,"bat_i":2,"bat_temp":24,"rssi":-62,"pack_num":2}'))
    mqtt_mock.callbacks[driver._system_topic](SimpleNamespace(payload='{"chg_e":1240,"dchg_e":530}'))
    mqtt_mock.callbacks[driver._power_config_topic](SimpleNamespace(payload='{"min":-1800,"max":1800}'))
    snapshot = await driver.read_telemetry(["battery_soc", "battery_power", "inverter_state", "battery_voltage", "total_daily_charging_energy"])
    assert snapshot == {"battery_soc": 50, "battery_power": -300, "inverter_state": 3, "battery_voltage": 51.2, "total_daily_charging_energy": 1240}
    assert next(d for d in driver.sensor_definitions if d["key"] == "total_daily_charging_energy")["scale"] == 0.001
    assert driver.capabilities.max_charge_power_w == driver.capabilities.max_discharge_power_w == 1800
    assert driver.capabilities.actuator_latency_s == 1.8
    assert driver.capabilities.readback_latency_s == 4.0
    mqtt_mock.callbacks[driver._quick_topic](SimpleNamespace(payload="not json"))
    assert (await driver.read_telemetry())["battery_soc"] == 50
    await driver.close()


@pytest.mark.asyncio
async def test_mqtt_telemetry_accepts_legacy_numeric_strings(mqtt_mock):
    driver = HoymilesMqttDriver(
        _hass(), "MSA-legacy", max_charge_power_w=1800, max_discharge_power_w=1800
    )
    await driver.connect()
    mqtt_mock.callbacks[driver._quick_topic](SimpleNamespace(
        payload='{"soc":"20.0","bat_p":"-80.0","sys_soc":"50.0","sys_bat_p":"300.0","bat_sts":"discharge"}'
    ))
    mqtt_mock.callbacks[driver._device_topic](SimpleNamespace(
        payload='{"bat_v":"51.2","bat_i":"2.0","bat_temp":"24.0","rssi":"-62","pack_num":"2"}'
    ))
    mqtt_mock.callbacks[driver._system_topic](SimpleNamespace(
        payload='{"chg_e":"1240","dchg_e":"530"}'
    ))
    mqtt_mock.callbacks[driver._power_config_topic](SimpleNamespace(
        payload='{"min":"-1800","max":"1800"}'
    ))

    snapshot = await driver.read_telemetry([
        "battery_soc",
        "battery_power",
        "inverter_state",
        "battery_voltage",
        "battery_current",
        "internal_temperature",
        "wifi_signal_strength",
        "pack_count",
        "total_daily_charging_energy",
    ])
    assert snapshot == {
        "battery_soc": 50.0,
        "battery_power": -300.0,
        "inverter_state": 3,
        "battery_voltage": 51.2,
        "battery_current": 2.0,
        "internal_temperature": 24.0,
        "wifi_signal_strength": -62.0,
        "pack_count": 2.0,
        "total_daily_charging_energy": 1240.0,
    }
    assert driver.capabilities.max_charge_power_w == driver.capabilities.max_discharge_power_w == 1800
    await driver.close()


@pytest.mark.asyncio
async def test_quick_fallback_uses_battery_values_and_inverts_charge_sign(mqtt_mock):
    driver = HoymilesMqttDriver(_hass(), "MSA-1")
    await driver.connect()
    mqtt_mock.callbacks[driver._quick_topic](SimpleNamespace(payload='{"soc": 43, "bat_p": -250, "bat_sts": "charge"}'))
    assert await driver.read_telemetry(["battery_soc", "battery_power", "inverter_state"]) == {
        "battery_soc": 43, "battery_power": 250, "inverter_state": 2,
    }
    await driver.close()


@pytest.mark.asyncio
async def test_setpoint_clamps_inverts_and_close_restores_general(mqtt_mock):
    hass = _hass()
    driver = HoymilesMqttDriver(hass, "MSA-1", max_charge_power_w=800, max_discharge_power_w=700)
    await driver.connect()
    result = await driver.apply_setpoint(900, read_back=False)
    assert result.ok and result.net_power_w == 800 and result.confirmed is False
    assert mqtt_mock.published[-1] == (driver._power_set_topic, "-800", 1, False)
    assert driver.net_power_from_data(result.applied) == 800
    await driver._refresh_command()
    assert mqtt_mock.published[-1][1] == "-800"
    await driver.close()
    assert mqtt_mock.published[-3:] == [
        (driver._ems_command_topic, "mqtt_ctrl", 1, False),
        (driver._power_set_topic, "0", 1, False),
        (driver._ems_command_topic, "general", 1, False),
    ][-3:]
    assert len(mqtt_mock.unsubscribed) == 4


@pytest.mark.asyncio
async def test_setpoint_starts_named_background_keepalive(mqtt_mock):
    task_names = []

    def create_background_task(coro, name):
        task_names.append(name)
        return asyncio.create_task(coro, name=name)

    driver = HoymilesMqttDriver(
        SimpleNamespace(async_create_background_task=create_background_task),
        "MSA-1",
    )
    await driver.connect()

    assert (await driver.apply_setpoint(100, read_back=False)).ok
    assert task_names == ["omnibattery_hoymiles_keepalive"]

    await driver.close()


@pytest.mark.asyncio
async def test_setpoint_publish_failure_and_keepalive_repeats_exact_limits(mqtt_mock):
    driver = HoymilesMqttDriver(_hass(), "MSA-1", max_charge_power_w=800, max_discharge_power_w=700)
    await driver.connect()
    mqtt_mock.fail_publish = True
    failed = await driver.apply_setpoint(100, read_back=False)
    assert not failed.ok and failed.failure_reason == "write_failed"
    mqtt_mock.fail_publish = False

    await driver.apply_setpoint(800, read_back=False)
    await driver._refresh_command()
    first = mqtt_mock.published[-1][1]
    await driver._refresh_command()
    second = mqtt_mock.published[-1][1]
    assert first == second == "-800"

    await driver.apply_setpoint(-700, read_back=False)
    await driver._refresh_command()
    first = mqtt_mock.published[-1][1]
    await driver._refresh_command()
    second = mqtt_mock.published[-1][1]
    assert first == second == "700"
    assert driver._last_net_power_w == -700
    await driver.close()


@pytest.mark.asyncio
async def test_power_config_caps_single_unit_symmetrically_without_inflating_user_limit(mqtt_mock):
    driver = HoymilesMqttDriver(
        _hass(),
        "MSA-1",
        max_charge_power_w=2000,
        max_discharge_power_w=1800,
    )
    await driver.connect()

    # A command may land before the retained discovery envelope is received.
    result = await driver.apply_setpoint(-1500, read_back=False)
    assert result.net_power_w == -1500

    # Firmware 01.06.03 advertises 2 kW discharge for a standalone 1 kW unit.
    mqtt_mock.callbacks[driver._power_config_topic](
        SimpleNamespace(payload='{"min":-1000,"max":2000}')
    )
    assert driver.capabilities.max_charge_power_w == 1000
    assert driver.capabilities.max_discharge_power_w == 1000
    assert driver._last_net_power_w == -1000
    await driver._refresh_command()
    assert mqtt_mock.published[-1][1] == "1000"

    # A later paired envelope may expand again, but never past the user's 1.8 kW
    # discharge ceiling.
    mqtt_mock.callbacks[driver._power_config_topic](
        SimpleNamespace(payload='{"min":-2000,"max":2000}')
    )
    assert driver.capabilities.max_charge_power_w == 2000
    assert driver.capabilities.max_discharge_power_w == 1800
    await driver.close()


@pytest.mark.asyncio
async def test_4020_x_discovery_uses_its_2_kw_profile_instead_of_ms_a2_limits(mqtt_mock):
    driver = HoymilesMqttDriver(
        _hass(), "HB-4020"
    )
    await driver.connect()

    mqtt_mock.callbacks[driver._power_config_topic](SimpleNamespace(payload='''{
        "min": -2000,
        "max": 2000,
        "device": {"model": "HB-4020-X"}
    }'''))

    assert driver.model_label == "HiBattery 4020 X"
    assert driver.capabilities.max_charge_power_w == 2000
    assert driver.capabilities.max_discharge_power_w == 2000
    assert (await driver.read_telemetry())["battery_total_energy"] == 4.02
    assert (await driver.apply_setpoint(-2500, read_back=False)).net_power_w == -2000
    await driver.close()


@pytest.mark.asyncio
async def test_4020_x_expansion_is_limited_to_2500_w_bidirectionally(mqtt_mock):
    driver = HoymilesMqttDriver(
        _hass(),
        "HB-4020",
        model="hibattery_4020_x",
    )
    await driver.connect()

    mqtt_mock.callbacks[driver._power_config_topic](SimpleNamespace(payload='''{
        "min": -7000,
        "max": 3000,
        "device": {"model": "HB-4020-X-3"}
    }'''))

    assert driver.capabilities.max_charge_power_w == 2500
    assert driver.capabilities.max_discharge_power_w == 2500
    assert (await driver.apply_setpoint(3000, read_back=False)).net_power_w == 2500
    assert (await driver.apply_setpoint(-3000, read_back=False)).net_power_w == -2500
    await driver.close()


@pytest.mark.asyncio
async def test_configured_4020_x_overrides_incorrect_ms_a2_discovery_model(mqtt_mock):
    driver = HoymilesMqttDriver(
        _hass(),
        "HB-4020",
        model="hibattery_4020_x",
    )
    await driver.connect()

    mqtt_mock.callbacks[driver._power_config_topic](SimpleNamespace(payload='''{
        "min": -2000,
        "max": 2000,
        "device": {"model": "MS-A2"}
    }'''))

    assert driver.model_label == "HiBattery 4020 X"
    assert driver.capabilities.max_charge_power_w == 2000
    assert driver.capabilities.max_discharge_power_w == 2000
    assert (await driver.read_telemetry())["battery_total_energy"] == 4.02
    mqtt_mock.callbacks[driver._device_topic](
        SimpleNamespace(payload='{"pack_num":4}')
    )
    assert (await driver.read_telemetry())["battery_total_energy"] == 16.08
    await driver.close()


def test_hoymiles_profiles_cover_scalable_1920_and_4020_variants():
    assert hoymiles_model_profile("HB-1920-AC-SV").label == "HiBattery 1920 AC"
    assert hoymiles_capacity_kwh("HB-1920-AC-SV", 6000, 6000) == 11.52
    assert hoymiles_model_profile("HB-4020-XM").key == "hibattery_4020_x"
    assert hoymiles_model_profile("HB-4020-ACM").key == "hibattery_4020_ac"
    assert hoymiles_capacity_kwh("HB-4020-X-1") == 8.04
    assert hoymiles_capacity_kwh("HB-4020-XM-3") == 16.08
    assert hoymiles_capacity_kwh("HB-4020-AC", pack_count=3) == 12.06
    assert HoymilesMqttDriver._device_power_caps(
        {"min": -1000, "max": 800, "device": {"model": "HB-4020-X"}}
    ) == (1000, 800)
    assert HoymilesMqttDriver._device_power_caps(
        {"min": -7000, "max": 3000, "device": {"model": "HB-4020-X-3"}}
    ) == (2500, 2500)
    assert HoymilesMqttDriver._device_power_caps(
        {"min": -7000, "max": 3000, "device": {"model": "HB-4020-AC-3"}}
    ) == (2500, 2500)


def test_4020_power_ceilings_are_bidirectional_2500_w():
    for model in ("hibattery_4020_x", "hibattery_4020_ac"):
        assert _hoymiles_power_ceilings({
            "hoymiles_model": model,
            "device_max_charge_power": 6500,
            "device_max_discharge_power": 3000,
        }) == (2500, 2500)


def test_detected_4020_profile_upgrades_only_legacy_ms_a2_defaults():
    battery = {
        "brand": "hoymiles",
        "max_charge_power": 1000,
        "max_discharge_power": 1000,
        "battery_capacity_kwh": 2.24,
    }
    _hoymiles_apply_probe_caps(
        battery,
        {
            "hoymiles_model": "hibattery_4020_x",
            "hoymiles_model_label": "HiBattery 4020 X",
            "device_max_charge_power": 2000,
            "device_max_discharge_power": 2000,
            "battery_capacity_kwh": 4.02,
        },
        upgrade_legacy_defaults=True,
    )

    assert battery["max_charge_power"] == 2000
    assert battery["max_discharge_power"] == 2000
    assert battery["battery_capacity_kwh"] == 4.02


@pytest.mark.asyncio
async def test_keepalive_retries_quickly_after_refresh_failure(monkeypatch):
    driver = HoymilesMqttDriver(_hass(), "MSA-1")
    refresh = AsyncMock(side_effect=[False, True])
    delays = []

    async def record_sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(driver, "_refresh_command", refresh)
    monkeypatch.setattr(
        "custom_components.omnibattery.drivers.hoymiles.asyncio.sleep",
        record_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        await driver._keepalive_loop()

    assert delays == [30, 5, 30]
    assert refresh.await_count == 2


@pytest.mark.asyncio
async def test_probe_accepts_quick_telemetry_and_cleans_up(mqtt_mock):
    hass = SimpleNamespace()
    probe = asyncio.create_task(HoymilesMqttDriver.probe(hass, "MSA-1", timeout=0.2))
    await asyncio.sleep(0)
    mqtt_mock.callbacks["homeassistant/sensor/MSA-1/quick/state"](SimpleNamespace(payload='{"soc":50,"bat_p":-100}'))
    ok, metadata = await probe
    assert ok and metadata == {}
    assert len(mqtt_mock.unsubscribed) == 2


@pytest.mark.asyncio
async def test_probe_accepts_legacy_string_telemetry(mqtt_mock):
    hass = SimpleNamespace()
    probe = asyncio.create_task(HoymilesMqttDriver.probe(hass, "MSA-legacy", timeout=0.2))
    await asyncio.sleep(0)
    mqtt_mock.callbacks["homeassistant/sensor/MSA-legacy/quick/state"](
        SimpleNamespace(payload='{"soc":"50.0","bat_p":"-100.0"}')
    )

    assert await probe == (True, {})


@pytest.mark.asyncio
async def test_probe_preserves_manual_model_hint_without_discovery_config(mqtt_mock):
    probe = asyncio.create_task(
        HoymilesMqttDriver.probe(
            SimpleNamespace(),
            "HB-4020",
            timeout=0.01,
            model_hint="hibattery_4020_x",
        )
    )
    await asyncio.sleep(0)
    mqtt_mock.callbacks["homeassistant/sensor/HB-4020/quick/state"](
        SimpleNamespace(payload='{"soc":50,"bat_p":-100}')
    )

    assert await probe == (
        True,
        {
            "hoymiles_model": "hibattery_4020_x",
            "hoymiles_model_label": "HiBattery 4020 X",
            "battery_capacity_kwh": 4.02,
        },
    )


@pytest.mark.asyncio
async def test_probe_timeout_cleans_up_and_caps_paired_system_metadata(mqtt_mock):
    hass = SimpleNamespace()
    timeout = await HoymilesMqttDriver.probe(hass, "MSA-timeout", timeout=0.001)
    assert timeout == (False, {})
    assert len(mqtt_mock.unsubscribed) == 2

    probe = asyncio.create_task(HoymilesMqttDriver.probe(hass, "MSA-paired", timeout=0.2))
    await asyncio.sleep(0)
    mqtt_mock.callbacks["homeassistant/number/MSA-paired/power_ctrl/config"](SimpleNamespace(payload='{"min": -2500, "max": 2500}'))
    mqtt_mock.callbacks["homeassistant/sensor/MSA-paired/quick/state"](SimpleNamespace(payload='{"soc":50,"bat_p":-100}'))
    assert await probe == (True, {"device_max_charge_power": 2000, "device_max_discharge_power": 2000})


@pytest.mark.asyncio
async def test_probe_corrects_standalone_asymmetric_discovery_envelope(mqtt_mock):
    probe = asyncio.create_task(
        HoymilesMqttDriver.probe(SimpleNamespace(), "MSA-single", timeout=0.2)
    )
    await asyncio.sleep(0)
    mqtt_mock.callbacks["homeassistant/number/MSA-single/power_ctrl/config"](
        SimpleNamespace(payload='{"min":-1000,"max":2000}')
    )
    mqtt_mock.callbacks["homeassistant/sensor/MSA-single/quick/state"](
        SimpleNamespace(payload='{"soc":50,"bat_p":-100}')
    )
    assert await probe == (
        True,
        {
            "device_max_charge_power": 1000,
            "device_max_discharge_power": 1000,
        },
    )


@pytest.mark.asyncio
async def test_probe_reports_4020_x_model_capacity_and_power(mqtt_mock):
    probe = asyncio.create_task(
        HoymilesMqttDriver.probe(SimpleNamespace(), "HB-4020", timeout=0.2)
    )
    await asyncio.sleep(0)
    mqtt_mock.callbacks["homeassistant/number/HB-4020/power_ctrl/config"](
        SimpleNamespace(payload='''{
            "min": -2000,
            "max": 2000,
            "device": {"model": "HB-4020-X"}
        }''')
    )
    mqtt_mock.callbacks["homeassistant/sensor/HB-4020/quick/state"](
        SimpleNamespace(payload='{"soc":50,"bat_p":-100}')
    )

    assert await probe == (
        True,
        {
            "hoymiles_model": "hibattery_4020_x",
            "hoymiles_model_label": "HiBattery 4020 X",
            "device_max_charge_power": 2000,
            "device_max_discharge_power": 2000,
            "battery_capacity_kwh": 4.02,
        },
    )


@pytest.mark.asyncio
async def test_probe_model_hint_overrides_incorrect_discovery_model(mqtt_mock):
    probe = asyncio.create_task(
        HoymilesMqttDriver.probe(
            SimpleNamespace(),
            "HB-4020",
            timeout=0.2,
            model_hint="hibattery_4020_x",
        )
    )
    await asyncio.sleep(0)
    mqtt_mock.callbacks["homeassistant/number/HB-4020/power_ctrl/config"](
        SimpleNamespace(payload='''{
            "min": -2000,
            "max": 2000,
            "device": {"model": "MS-A2"}
        }''')
    )
    mqtt_mock.callbacks["homeassistant/sensor/HB-4020/quick/state"](
        SimpleNamespace(payload='{"soc":50,"bat_p":-100}')
    )

    assert await probe == (
        True,
        {
            "hoymiles_model": "hibattery_4020_x",
            "hoymiles_model_label": "HiBattery 4020 X",
            "device_max_charge_power": 2000,
            "device_max_discharge_power": 2000,
            "battery_capacity_kwh": 4.02,
        },
    )


@pytest.mark.asyncio
async def test_config_flow_offers_hoymiles_and_software_capacity_defaults(monkeypatch):
    flow = MarstekVenusConfigFlow()
    flow.config_data = {"num_batteries": 1}
    form = await flow.async_step_battery_brand()
    schema = next(iter(form["data_schema"].schema.values())).config["options"]
    assert {option["value"] for option in schema} >= {"hoymiles", "marstek"}

    flow._current_battery_data = {"brand": "hoymiles"}
    limits = await flow.async_step_battery_limits()
    fields = {marker.schema for marker in limits["data_schema"].schema}
    assert {"max_charge_power", "max_discharge_power", "battery_capacity_kwh"} <= fields

    routed = await flow.async_step_battery_brand({"brand": "hoymiles"})
    assert routed["step_id"] == "battery_connection_hoymiles"
    assert "hoymiles_model" in {
        marker.schema for marker in routed["data_schema"].schema
    }

    probe = AsyncMock(return_value=(True, {"device_max_charge_power": 1800, "device_max_discharge_power": 1800}))
    monkeypatch.setattr(HoymilesMqttDriver, "probe", probe)
    flow.hass = SimpleNamespace()
    saved = await flow.async_step_battery_connection_hoymiles({"name": "Paired MS-A2", "device_id": "MSA-paired"})
    assert saved["step_id"] == "battery_limits"
    assert flow._current_battery_data == {
        "brand": "hoymiles", "name": "Paired MS-A2", "host": "MSA-paired", "port": 0,
        "device_id": "MSA-paired", "device_max_charge_power": 1800, "device_max_discharge_power": 1800,
    }
    assert {marker.schema for marker in saved["data_schema"].schema} >= {"battery_capacity_kwh", "max_charge_power"}
    await flow.async_step_battery_limits({
        "max_charge_power": 1800, "max_discharge_power": 1800, "max_soc": 100, "min_soc": 10,
        "charge_hysteresis_percent": 2, "backup_offgrid_threshold": 50, "battery_capacity_kwh": 2.24,
    })
    assert flow.battery_configs[0]["battery_capacity_kwh"] == 2.24


@pytest.mark.asyncio
async def test_config_flow_seeds_4020_x_characteristics_from_discovery(monkeypatch):
    flow = MarstekVenusConfigFlow()
    flow.hass = SimpleNamespace()
    flow.config_data = {"num_batteries": 1}
    flow._current_battery_data = {"brand": "hoymiles"}
    probe = AsyncMock(return_value=(True, {
            "hoymiles_model": "hibattery_4020_x",
            "hoymiles_model_label": "HiBattery 4020 X",
            "device_max_charge_power": 2000,
            "device_max_discharge_power": 2000,
            "battery_capacity_kwh": 4.02,
        }))
    monkeypatch.setattr(HoymilesMqttDriver, "probe", probe)

    form = await flow.async_step_battery_connection_hoymiles(
        {
            "name": "4020 X",
            "device_id": "MSA-4020",
            "hoymiles_model": "hibattery_4020_x",
        }
    )
    defaults = {
        marker.schema: marker.default()
        for marker in form["data_schema"].schema
        if callable(marker.default)
    }

    assert flow._current_battery_data["hoymiles_model"] == "hibattery_4020_x"
    assert defaults["max_charge_power"] == 2000
    assert defaults["max_discharge_power"] == 2000
    assert defaults["battery_capacity_kwh"] == 4.02
    probe.assert_awaited_once_with(
        flow.hass,
        "MSA-4020",
        model_hint="hibattery_4020_x",
    )
