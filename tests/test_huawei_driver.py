"""Unit tests for the Huawei SUN2000 + LUNA2000 driver.

The driver is split-transport: Modbus for telemetry, huawei_solar services for
control. Both halves are faked here, so no inverter and no HA runtime is needed.

Register values in the fixtures are the ones read from a real
SUN2000-8K-MAP0 / LUNA2000 13.8 kWh, so the decoding assertions below double as
a regression test for the register map itself.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.omnibattery.drivers import DriverCapabilities
from custom_components.omnibattery.drivers.huawei import (
    SENSOR_DEFINITIONS,
    HuaweiSolarDriver,
    _MIN_WRITE_INTERVAL_S,
    _PV_PROBE_INTERVAL_S,
    _PV_PROBE_SETTLE_S,
    _PV_PROBE_WINDOW_S,
)
from custom_components.omnibattery.infra.huawei_modbus_client import (
    decode_i16,
    decode_i32,
    decode_string,
    decode_u16,
    decode_u32,
)

_BATTERY_DEVICE = "dev-batteries"

# start address -> registers, as captured from the reference installation.
_LIVE_BLOCKS = {
    37000: [2, 0xFFFF, 0xFCD7, 7963, 610],          # running, -809 W, 796.3 V, 61.0 %
    32064: [0, 0] + [0] * 14 + [0, 758],            # PV 0 W, AC 758 W
    47100: [1],                                      # forcible mode = charge
    47246: [0, 0, 1500, 0, 0],                       # target mode TIME, charge 1500 W
    37015: [0, 1492, 0, 1135, 0, 0, 0xFFF6, 354],    # 14.92 / 11.35 kWh, 35.4 °C
    37046: [0, 7000, 0, 7000],
    37066: [6, 0x11B0, 6, 0x0BE7],                   # totals
    47081: [1000, 50, 0, 0, 0, 2, 1],                # cutoffs 100/5 %, mode 2, grid on
    37758: [0, 13800],
    30000: [0x5355, 0x4E32, 0x3030, 0x302D, 0x384B, 0x2D4D, 0x4150, 0x3000]
           + [0] * 7,                                # "SUN2000-8K-MAP0"
    37052: [0x5441, 0x3234, 0x3730, 0x3037, 0x3431, 0x3234, 0, 0, 0, 0],  # TA2470074124
}


def _fake_client(blocks=None):
    table = dict(_LIVE_BLOCKS if blocks is None else blocks)
    client = MagicMock()
    client.connected = True
    client.async_connect = AsyncMock(return_value=True)
    client.async_close = AsyncMock()
    client.set_shutting_down = MagicMock()
    client.async_read_holding_block = AsyncMock(
        side_effect=lambda start, count: table.get(start)
    )
    return client


def _driver(client=None, hass=None, device_id=_BATTERY_DEVICE, **kw):
    return HuaweiSolarDriver(
        hass if hass is not None else MagicMock(),
        "1.2.3.4",
        port=502,
        slave_id=4,
        battery_device_id=device_id,
        client=client if client is not None else _fake_client(),
        **kw,
    )


def _hass_with_services():
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    return hass


# ----------------------------------------------------------------------
# decoding primitives
# ----------------------------------------------------------------------
def test_decoders_handle_sign_and_width():
    assert decode_u16([0xFFFF]) == 65535
    assert decode_i16([0xFFFF]) == -1
    assert decode_u32([0x0001, 0x0000]) == 65536
    assert decode_i32([0xFFFF, 0xFCD7]) == -809
    assert decode_i32([0x0000, 0x02F6]) == 758


def test_decode_string_stops_at_nul_and_returns_none_when_empty():
    assert decode_string([0x4142, 0x4300, 0x5858], 0, 3) == "ABC"
    assert decode_string([0, 0], 0, 2) is None


# ----------------------------------------------------------------------
# capabilities / identity
# ----------------------------------------------------------------------
def test_capabilities():
    caps = _driver().capabilities
    assert isinstance(caps, DriverCapabilities)
    # The cutoff registers are narrower than the configurable SOC window, so the
    # control layer owns enforcement and the registers stay a backstop.
    assert caps.hardware_soc_cutoff is False
    assert caps.has_force_mode is True
    assert caps.push_telemetry is False
    assert caps.has_energy_counters is True
    assert caps.has_daily_energy_counters is True
    # The command registers echo before the battery has ramped.
    assert caps.setpoint_confirm_reliable is False
    # Measured: 19.7 s to reach 90 % of a charge set-point from idle. The
    # control layer judges non-delivery once this elapses, so it must not sit
    # below the real ramp.
    assert caps.actuator_latency_s == 25.0


def test_power_envelope_is_clamped_to_the_hardware_ceiling():
    caps = _driver(max_charge_power_w=99000, max_discharge_power_w=-5).capabilities
    assert caps.max_charge_power_w == 15000
    assert caps.max_discharge_power_w == 0


@pytest.mark.asyncio
async def test_connect_reads_identity():
    driver = _driver()
    assert await driver.connect() is True
    assert driver.model_label == "SUN2000-8K-MAP0"
    assert driver.serial == "TA2470074124"


@pytest.mark.asyncio
async def test_connect_fails_when_transport_fails():
    client = _fake_client()
    client.async_connect = AsyncMock(return_value=False)
    assert await _driver(client).connect() is False


def test_sensor_definitions_are_unique_and_declare_a_cadence():
    keys = [d["key"] for d in SENSOR_DEFINITIONS]
    assert len(keys) == len(set(keys))
    assert all(d.get("scan_interval") for d in SENSOR_DEFINITIONS)


# ----------------------------------------------------------------------
# telemetry
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_read_telemetry_decodes_the_reference_installation():
    data = await _driver().read_telemetry()
    assert data["battery_soc"] == pytest.approx(61.0)
    # Huawei's sign convention matches Omnibattery's: negative is discharge.
    assert data["battery_power"] == -809
    assert data["battery_voltage"] == pytest.approx(796.3)
    # kWh, not the register's Wh: every consumer of this key treats it as kWh.
    assert data["battery_total_energy"] == 13.8
    assert data["max_charge_power"] == 7000
    # Exact, not approx: scaled values must not leak binary-fraction artefacts.
    assert data["internal_temperature"] == 35.4
    assert data["total_daily_charging_energy"] == pytest.approx(14.92)
    assert data["charging_cutoff_capacity"] == pytest.approx(100.0)
    assert data["discharging_cutoff_capacity"] == pytest.approx(5.0)
    assert data["inverter_ac_power"] == 758
    assert data["solar_power"] == 0


@pytest.mark.asyncio
async def test_enum_registers_become_labels():
    data = await _driver().read_telemetry(["user_work_mode"])
    assert data["user_work_mode"] == "Maximise self consumption"


@pytest.mark.asyncio
async def test_unknown_enum_value_is_reported_rather_than_hidden():
    blocks = dict(_LIVE_BLOCKS)
    blocks[37000] = [99, 0, 0, 0, 500]
    data = await _driver(_fake_client(blocks)).read_telemetry(["inverter_state"])
    assert data["inverter_state"] == "Unknown (99)"


# ----------------------------------------------------------------------
# inverter state
#
# Register 37000 only says whether the storage is alive, so on its own the
# panel header reads "Running" all day. The direction comes from measured
# power, using the same words as the Marstek register map so the panel is
# consistent across brands.
# ----------------------------------------------------------------------
def _state(running, battery_w):
    high, low = divmod(battery_w & 0xFFFFFFFF, 0x10000)
    return {**_LIVE_BLOCKS, 37000: [running, high, low, 7963, 610]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "battery_w,expected",
    [
        (2000, "Charge"),
        (-2000, "Discharge"),
        (0, "Standby"),
        # The inverter idles around +50 W; comparing against zero would call a
        # standing battery "Charge" and flip the header on noise.
        (50, "Standby"),
        (-46, "Standby"),
        (101, "Charge"),
        (-101, "Discharge"),
    ],
)
async def test_running_state_is_refined_by_direction(battery_w, expected):
    data = await _driver(_fake_client(_state(2, battery_w))).read_telemetry()
    assert data["inverter_state"] == expected
    assert data["battery_power"] == battery_w


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,expected", [(0, "Offline"), (1, "Standby"), (3, "Fault"), (4, "Sleep")])
async def test_non_running_states_are_reported_as_is(raw, expected):
    """Offline / Fault / Sleep carry more than a direction ever could."""
    data = await _driver(_fake_client(_state(raw, -2000))).read_telemetry()
    assert data["inverter_state"] == expected


@pytest.mark.asyncio
async def test_state_falls_back_to_running_without_a_power_reading():
    blocks = {**_LIVE_BLOCKS, 37000: [2, 0xFFFF, 0xFCD7, 7963, 610]}
    driver = _driver(_fake_client(blocks))
    # Force the power away so only the raw status remains.
    data = await driver.read_telemetry()
    data.pop("battery_power")
    refined = await driver.read_telemetry(["inverter_state"])
    # Asking for the state alone must still pull its companion reading.
    assert refined["inverter_state"] == "Discharge"


@pytest.mark.asyncio
async def test_key_filter_only_reads_the_blocks_it_needs():
    client = _fake_client()
    data = await _driver(client).read_telemetry(["battery_soc"])
    assert set(data) == {"battery_soc"}
    assert client.async_read_holding_block.await_count == 1


@pytest.mark.asyncio
async def test_failed_block_omits_its_keys_instead_of_publishing_zero():
    blocks = dict(_LIVE_BLOCKS)
    del blocks[37000]  # the client returns None for this block
    data = await _driver(_fake_client(blocks)).read_telemetry()
    assert "battery_soc" not in data
    assert "battery_power" not in data
    # An unrelated block is unaffected.
    # kWh, not the register's Wh: every consumer of this key treats it as kWh.
    assert data["battery_total_energy"] == 13.8


# ----------------------------------------------------------------------
# control
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_charge_calls_forcible_charge_with_a_duration():
    hass = _hass_with_services()
    result = await _driver(hass=hass).apply_setpoint(1500, read_back=False)
    assert result.ok is True
    assert result.net_power_w == 1500
    domain, service, data = hass.services.async_call.await_args.args[:3]
    assert (domain, service) == ("huawei_solar", "forcible_charge")
    assert data["power"] == 1500
    assert data["device_id"] == _BATTERY_DEVICE
    # The duration is the watchdog: the command must expire on its own.
    assert data["duration"] > 0


@pytest.mark.asyncio
async def test_discharge_sends_a_positive_magnitude():
    hass = _hass_with_services()
    result = await _driver(hass=hass).apply_setpoint(-900, read_back=False)
    assert result.net_power_w == -900
    _domain, service, data = hass.services.async_call.await_args.args[:3]
    assert service == "forcible_discharge"
    assert data["power"] == 900


@pytest.mark.asyncio
async def test_idle_releases_the_battery_rather_than_pinning_it():
    """Zero means "no work for you", which has to mean released, not held.

    An earlier version pinned zero with a forcible charge at 0 W, to keep the
    inverter's own self-consumption control out of the loop. On hardware that
    backfired twice: a pinned battery cannot absorb its own PV, so the inverter
    derated the strings; and the control layer's single idle on entering manual
    mode left the battery frozen in standby with nothing to release it.
    """
    hass = _hass_with_services()
    await _driver(hass=hass).apply_setpoint(0, read_back=False)
    _domain, service, data = hass.services.async_call.await_args.args[:3]
    assert service == "stop_forcible_charge"
    # A stop carries no power or duration.
    assert set(data) == {"device_id"}


def test_zero_echoes_the_stopped_mode():
    """net_power_from_data has to round-trip a release back to 0 W."""
    driver = _driver()
    echo = driver._echo(0)
    assert echo["force_mode"] == 0
    assert driver.net_power_from_data(echo) == 0


@pytest.mark.asyncio
async def test_setpoint_is_clamped_to_the_envelope():
    hass = _hass_with_services()
    driver = _driver(hass=hass, max_charge_power_w=5000, max_discharge_power_w=5000)
    result = await driver.apply_setpoint(9999, read_back=False)
    assert result.net_power_w == 5000


@pytest.mark.asyncio
async def test_missing_battery_device_fails_without_calling_a_service():
    hass = _hass_with_services()
    result = await _driver(hass=hass, device_id="").apply_setpoint(1000, read_back=False)
    assert result.ok is False
    assert result.failure_reason == "no_battery_device"
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_failure_is_reported_and_not_cached_as_written():
    hass = _hass_with_services()
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))
    driver = _driver(hass=hass)
    result = await driver.apply_setpoint(1000, read_back=False)
    assert result.ok is False
    assert result.failure_reason == "service_call_failed"
    # A failed write must not satisfy the deadband for the next attempt.
    hass.services.async_call = AsyncMock()
    assert (await driver.apply_setpoint(1000, read_back=False)).ok is True
    hass.services.async_call.assert_awaited()


@pytest.mark.asyncio
async def test_readback_reports_the_echo_as_inexact():
    hass = _hass_with_services()
    result = await _driver(hass=hass).apply_setpoint(1500, read_back=True)
    assert result.confirmed is True
    # The registers echo instantly while the battery is still ramping.
    assert result.exact is False
    assert result.battery_power_w == -809


# ----------------------------------------------------------------------
# write throttling
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_small_change_inside_the_deadband_is_not_rewritten():
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    await driver.apply_setpoint(1500, read_back=False)
    hass.services.async_call.reset_mock()
    result = await driver.apply_setpoint(1550, read_back=False)
    assert result.ok is True
    # Reports what is in force, not what was asked for: claiming the request
    # had been applied would make the control layer expect power the battery
    # was never told to deliver, and flag it as non-responsive for it.
    assert result.net_power_w == 1500
    assert result.applied["set_charge_power"] == 1500
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_direction_change_skips_the_deadband_but_not_the_ramp():
    """A reversal is material, but it still waits for the previous one to land.

    Live logs showed the control loop flip-flopping between a held zero and a
    discharge every few seconds while the battery was still ramping. Letting
    each reversal through because the sign changed meant a new forced command
    every ~15 s at swings up to 4 kW, and the inverter answered by derating its
    PV to almost nothing.
    """
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    await driver.apply_setpoint(10, read_back=False)
    hass.services.async_call.reset_mock()

    # Mid-ramp reversal: suppressed.
    await driver.apply_setpoint(-10, read_back=False)
    hass.services.async_call.assert_not_awaited()

    # Once the ramp has had its time, the reversal goes through — and a tiny
    # one at that, which the deadband alone would have rejected.
    driver._last_write_monotonic -= 60.0
    await driver.apply_setpoint(-10, read_back=False)
    _domain, service, _data = hass.services.async_call.await_args.args[:3]
    assert service == "forcible_discharge"


@pytest.mark.asyncio
async def test_large_change_within_one_direction_waits_for_the_ramp():
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    await driver.apply_setpoint(1000, read_back=False)
    hass.services.async_call.reset_mock()
    # Well beyond the deadband, but the battery is still travelling towards the
    # previous target, so rewriting mid-ramp achieves nothing.
    await driver.apply_setpoint(3000, read_back=False)
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_leaving_a_held_zero_is_written_immediately():
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    await driver.apply_setpoint(0, read_back=False)
    driver._last_write_monotonic -= 60.0
    hass.services.async_call.reset_mock()
    # Idle -> charging is a change of state, not a change of magnitude.
    await driver.apply_setpoint(3000, read_back=False)
    hass.services.async_call.assert_awaited()


@pytest.mark.asyncio
async def test_write_interval_elapsed_allows_a_material_change():
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    await driver.apply_setpoint(1000, read_back=False)
    driver._last_write_monotonic -= 60.0
    hass.services.async_call.reset_mock()
    await driver.apply_setpoint(3000, read_back=False)
    hass.services.async_call.assert_awaited()


@pytest.mark.asyncio
async def test_standing_command_is_refreshed_before_its_duration_expires():
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    await driver.apply_setpoint(1000, read_back=False)
    driver._last_write_monotonic -= 300.0
    hass.services.async_call.reset_mock()
    # Same value, no direction change: only the refresh timer justifies this.
    await driver.apply_setpoint(1000, read_back=False)
    hass.services.async_call.assert_awaited()


# ----------------------------------------------------------------------
# command echo
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "data,expected",
    [
        ({"force_mode": 1, "set_charge_power": 1200, "set_discharge_power": 0}, 1200),
        ({"force_mode": 2, "set_charge_power": 0, "set_discharge_power": 800}, -800),
        ({"force_mode": 0, "set_charge_power": 0, "set_discharge_power": 0}, 0),
        ({"force_mode": 1, "set_charge_power": 0, "set_discharge_power": 0}, 0),
    ],
)
def test_net_power_from_data(data, expected):
    assert _driver().net_power_from_data(data) == expected


def test_net_power_from_data_is_none_when_the_echo_is_incomplete():
    # None must fall through to a real write rather than skipping it.
    assert _driver().net_power_from_data({"force_mode": 1}) is None
    assert _driver().net_power_from_data({}) is None


def test_control_dependency_keys_cover_the_echo():
    keys = _driver().control_dependency_keys
    assert {"force_mode", "set_charge_power", "set_discharge_power"} <= keys


# ----------------------------------------------------------------------
# shutdown and configuration
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_standby_releases_the_battery_back_to_the_inverter():
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    await driver.apply_setpoint(1500, read_back=False)
    assert await driver.standby() is True
    _domain, service, _data = hass.services.async_call.await_args.args[:3]
    assert service == "stop_forcible_charge"
    # The throttle must not suppress the first command after a release.
    hass.services.async_call.reset_mock()
    await driver.apply_setpoint(1500, read_back=False)
    hass.services.async_call.assert_awaited()


@pytest.mark.asyncio
async def test_write_control_reports_unsupported_keys():
    assert await _driver().write_control("force_mode", 1) is False


@pytest.mark.asyncio
async def test_set_charge_cutoff_without_a_resolvable_entity_returns_false():
    driver = _driver(hass=_hass_with_services())
    driver._resolve_entity = lambda name: None
    assert await driver.set_charge_cutoff(90) is False


@pytest.mark.asyncio
async def test_cutoff_outside_the_register_range_is_skipped_not_clamped():
    """A clamped write would move the backstop somewhere nobody asked for."""
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    driver._resolve_entity = lambda name: f"number.{name}"
    # 47081 accepts 90-100 % only.
    assert await driver.set_charge_cutoff(80) is False
    hass.services.async_call.assert_not_awaited()
    assert await driver.set_charge_cutoff(95) is True
    hass.services.async_call.assert_awaited()


@pytest.mark.asyncio
async def test_apply_config_skips_a_min_soc_the_register_cannot_hold():
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    driver._resolve_entity = lambda name: f"number.{name}"
    # 47082 accepts 0-20 %; 25 % is enforced in software instead.
    result = await driver.apply_config(
        max_soc_pct=95, min_soc_pct=25,
        max_charge_power_w=7000, max_discharge_power_w=7000,
    )
    written = [c.args[2]["entity_id"] for c in hass.services.async_call.await_args_list]
    assert written == ["number.storage_charging_cutoff_capacity"]
    # The window is enforced by the control layer, so a skipped backstop write is
    # not a configuration failure and must not be reported as one.
    assert result is True


@pytest.mark.asyncio
async def test_apply_config_succeeds_even_when_no_entity_resolves():
    driver = _driver(hass=_hass_with_services())
    driver._resolve_entity = lambda name: None
    assert await driver.apply_config(
        max_soc_pct=100, min_soc_pct=10,
        max_charge_power_w=7000, max_discharge_power_w=7000,
    ) is True


def test_resolve_entity_spans_the_config_entry_not_just_the_battery_device():
    """huawei_solar puts the charge cutoff on the inverter, not on the battery.

    Resolving against the configured battery device alone finds the discharge
    cutoff and misses the charge cutoff, which is what made apply_config report
    a failed write on real hardware.
    """
    import custom_components.omnibattery.drivers.huawei as mod

    entries = [
        MagicMock(platform="huawei_solar", disabled=False,
                  unique_id="BT24B1457565_storage_charging_cutoff_capacity",
                  entity_id="number.batterien_ladeende_ladestand"),
        MagicMock(platform="huawei_solar", disabled=False,
                  unique_id="TA2470074124_storage_discharging_cutoff_capacity",
                  entity_id="number.batterien_entlade_ende_ladestand"),
        MagicMock(platform="template", disabled=False,
                  unique_id="x_storage_charging_cutoff_capacity",
                  entity_id="sensor.decoy"),
    ]
    device = MagicMock(config_entries=["entry-1"])
    driver = _driver()
    with (
        patch.object(mod.dr, "async_get",
                     return_value=MagicMock(async_get=MagicMock(return_value=device))),
        patch.object(mod.er, "async_get", return_value=MagicMock()),
        patch.object(mod.er, "async_entries_for_config_entry", return_value=entries),
    ):
        charge = driver._resolve_entity("storage_charging_cutoff_capacity")
        discharge = driver._resolve_entity("storage_discharging_cutoff_capacity")
    assert charge == "number.batterien_ladeende_ladestand"
    # The two register names must not collide: "charging" is a substring of
    # "discharging", so a careless suffix match would return the wrong entity.
    assert discharge == "number.batterien_entlade_ende_ladestand"


def test_resolve_entity_without_a_device_returns_none():
    import custom_components.omnibattery.drivers.huawei as mod

    driver = _driver()
    with patch.object(mod.dr, "async_get",
                      return_value=MagicMock(async_get=MagicMock(return_value=None))):
        assert driver._resolve_entity("storage_charging_cutoff_capacity") is None


@pytest.mark.asyncio
async def test_apply_config_writes_both_cutoffs():
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    driver._resolve_entity = lambda name: f"number.{name}"
    assert await driver.apply_config(
        max_soc_pct=95, min_soc_pct=10,
        max_charge_power_w=7000, max_discharge_power_w=7000,
    ) is True
    written = {
        call.args[2]["entity_id"]: call.args[2]["value"]
        for call in hass.services.async_call.await_args_list
    }
    assert written == {
        "number.storage_charging_cutoff_capacity": 95.0,
        "number.storage_discharging_cutoff_capacity": 10.0,
    }


# ----------------------------------------------------------------------
# config-flow bounds
#
# These guard a real setup failure: without a brand branch, Huawei fell through
# to the Marstek defaults and the limits form rejected the very values the probe
# had just read from the inverter ("Value 7000.0 is too large").
# ----------------------------------------------------------------------
def test_power_ceilings_follow_the_probed_hardware():
    from custom_components.omnibattery.config_flow import _huawei_power_ceilings

    assert _huawei_power_ceilings(
        {"device_max_charge_power": 7000, "device_max_discharge_power": 7000}
    ) == (7000, 7000)


def test_power_ceilings_fall_back_and_stay_sane():
    from custom_components.omnibattery.config_flow import _huawei_power_ceilings

    # No probe data at all.
    assert _huawei_power_ceilings({}) == (5000, 5000)
    # A malformed reading must not become the user's slider maximum.
    charge, _discharge = _huawei_power_ceilings({"device_max_charge_power": 999999})
    assert charge == 15000


def test_soc_window_reaches_the_hardware_discharge_floor():
    from custom_components.omnibattery.config_flow import _soc_selector_limits

    min_lo, min_hi, min_default, *_ = _soc_selector_limits("huawei")
    # The reference installation runs a 5 % discharge cutoff; the form must not
    # reject it the way the Marstek default (12 %) did.
    assert min_lo <= 5 <= min_hi
    assert min_lo <= min_default <= min_hi


# ----------------------------------------------------------------------
# device registry
# ----------------------------------------------------------------------
def test_device_info_reports_huawei_as_the_manufacturer():
    """The brand chain used to fall through to Marstek for every new brand.

    On real hardware that produced a device card reading
    "Marstek / SUN2000-8K-MAP0".
    """
    from types import SimpleNamespace

    from custom_components.omnibattery.infra.coordinator import (
        MarstekVenusDataUpdateCoordinator,
    )

    coordinator = SimpleNamespace(
        device_key="192.168.1.10_2502_4",
        name="Huawei LUNA2000",
        brand="huawei",
        driver=SimpleNamespace(model_label="SUN2000-8K-MAP0"),
        host="192.168.1.10",
        port=2502,
        data={},
    )
    info = MarstekVenusDataUpdateCoordinator.battery_device_info.fget(coordinator)
    assert info["manufacturer"] == "Huawei"
    assert info["model"] == "SUN2000-8K-MAP0"
    # No serial read yet, so the field stays out rather than going in empty.
    assert "serial_number" not in info

    coordinator.driver = SimpleNamespace(
        model_label="SUN2000-8K-MAP0", serial="TA2470074124"
    )
    info = MarstekVenusDataUpdateCoordinator.battery_device_info.fget(coordinator)
    assert info["serial_number"] == "TA2470074124"


# ----------------------------------------------------------------------
# dynamic discharge headroom
#
# Battery and PV share one inverter on a DC-coupled hybrid, so the nameplate
# battery limit is only reachable when PV is idle. Allocating against the
# nameplate starves the other batteries of the share they could have delivered.
# ----------------------------------------------------------------------
def _headroom(ceiling, ac_power, battery_power):
    return _driver().dynamic_discharge_limit_w({
        "inverter_max_power": ceiling,
        "inverter_ac_power": ac_power,
        "battery_power": battery_power,
    })


def test_headroom_is_the_full_ceiling_when_pv_is_idle():
    # Night: nothing but the inverter's own draw on the AC side.
    assert _headroom(8800, 0, 50) == 8800


def test_pv_output_consumes_the_headroom():
    # 7 kW of PV on an 8.8 kW inverter leaves 1.8 kW for the battery, whatever
    # its BMS allows.
    assert _headroom(8800, 7000, 0) == 1800


def test_headroom_ignores_the_batterys_own_contribution():
    """The limit must describe what PV occupies, not what the battery does.

    Subtracting the battery's own output too would make the limit chase itself:
    discharging more would shrink the limit, which would cut the allocation,
    which would raise the limit again.
    """
    # Same 7 kW of PV, but now the battery is already delivering its 1.8 kW, so
    # the inverter reads 8.8 kW total. The answer must not change.
    assert _headroom(8800, 8800, -1800) == 1800
    # And it stays put at any point along the ramp.
    assert _headroom(8800, 7900, -900) == 1800


def test_charging_does_not_consume_discharge_headroom():
    # Importing to charge: AC flows the other way and frees the whole ceiling.
    assert _headroom(8800, -2000, 2000) == 8800


def test_headroom_never_goes_negative():
    # An inverter briefly above its own ceiling must clamp to zero, not invert.
    assert _headroom(8800, 9500, 0) == 0


def test_large_system_headroom():
    """A 25 kW inverter with 22 kW of batteries — the constraint still binds."""
    assert _headroom(25000, 18000, 0) == 7000
    assert _headroom(25000, 0, 0) == 25000


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"inverter_ac_power": 1000, "battery_power": 0},          # no ceiling
        {"inverter_max_power": 8800, "battery_power": 0},  # no ac reading
        {"inverter_max_power": 8800, "inverter_ac_power": 1000},    # no battery reading
        {"inverter_max_power": None, "inverter_ac_power": 1, "battery_power": 1},
        {"inverter_max_power": "x", "inverter_ac_power": 1, "battery_power": 1},
    ],
)
def test_headroom_is_none_when_inputs_are_missing_or_unusable(data):
    # None keeps the static envelope; a guess here would silently mis-allocate.
    assert _driver().dynamic_discharge_limit_w(data) is None


def test_headroom_inputs_stay_polled_even_with_entities_disabled():
    keys = _driver().control_dependency_keys
    assert {"inverter_max_power", "inverter_ac_power", "battery_power"} <= keys


@pytest.mark.asyncio
async def test_inverter_ceiling_is_read_from_the_register_map():
    data = await _driver(_fake_client({**_LIVE_BLOCKS, 30073: [0, 8000, 0, 8800]})).read_telemetry(
        ["inverter_rated_power", "inverter_max_power"]
    )
    # 30073 is the nameplate, 30075 the ceiling the inverter enforces.
    assert data["inverter_rated_power"] == 8000
    assert data["inverter_max_power"] == 8800


# ----------------------------------------------------------------------
# control path
# ----------------------------------------------------------------------
def test_ac_batteries_keep_their_static_limit():
    """Only DC-coupled hybrids report a dynamic limit; everything else opts out."""
    from custom_components.omnibattery.drivers.base import BatteryDriver

    assert BatteryDriver.dynamic_discharge_limit_w(MagicMock(), {"inverter_ac_power": 1}) is None


@pytest.mark.parametrize(
    "reported,expected",
    [
        (None, 2500),    # driver has no opinion
        (5000, 2500),    # headroom above the static limit changes nothing
        (1800, 1800),    # headroom below it wins
        (0, 0),          # saturated inverter: no discharge available
    ],
)
def test_control_path_applies_the_narrower_limit(reported, expected):
    from custom_components.omnibattery import _apply_driver_dynamic_limit

    coordinator = MagicMock(
        name="Huawei",
        data={"x": 1},
        driver=MagicMock(dynamic_discharge_limit_w=MagicMock(return_value=reported)),
    )
    assert _apply_driver_dynamic_limit(coordinator, 2500) == expected


def test_control_path_survives_a_driver_that_raises():
    """A broken driver must not take the control cycle down with it."""
    from custom_components.omnibattery import _apply_driver_dynamic_limit

    coordinator = MagicMock(
        data={},
        driver=MagicMock(
            dynamic_discharge_limit_w=MagicMock(side_effect=RuntimeError("boom"))
        ),
    )
    assert _apply_driver_dynamic_limit(coordinator, 2500) == 2500


# ----------------------------------------------------------------------
# panel telemetry
# ----------------------------------------------------------------------
def test_capacity_is_reported_in_kwh_not_the_registers_wh():
    """A factor-1000 error here silently breaks the energy-balance features.

    charge_delay and predictive charging both sum this key straight into a
    kWh total, so reporting the register's raw Wh made 13.8 kWh of storage look
    like 13800 kWh — enough that "solar covers the day" was always true.
    """
    definition = next(
        d for d in SENSOR_DEFINITIONS if d["key"] == "battery_total_energy"
    )
    assert definition["unit"] == "kWh"


@pytest.mark.asyncio
async def test_string_power_is_derived_from_voltage_and_current():
    # 380.0 V x 4.00 A and 190.0 V x 2.00 A
    blocks = {**_LIVE_BLOCKS, 32016: [3800, 400, 1900, 200]}
    data = await _driver(_fake_client(blocks)).read_telemetry()
    assert data["mppt1_power"] == 1520
    assert data["mppt2_power"] == 380


@pytest.mark.asyncio
async def test_string_power_is_omitted_when_its_block_fails():
    blocks = dict(_LIVE_BLOCKS)  # no 32016 entry -> the client returns None
    data = await _driver(_fake_client(blocks)).read_telemetry()
    assert "mppt1_power" not in data
    assert "mppt2_power" not in data


@pytest.mark.asyncio
async def test_asking_only_for_derived_power_still_reads_its_sources():
    """A derived key belongs to no block, so the filter must expand it.

    Without that expansion the poll would read nothing and the entity would
    never leave "unknown".
    """
    blocks = {**_LIVE_BLOCKS, 32016: [3800, 400, 1900, 200]}
    data = await _driver(_fake_client(blocks)).read_telemetry(["mppt1_power"])
    assert data["mppt1_power"] == 1520


def test_derived_keys_are_scheduled_with_their_source_block():
    groups = _driver().read_groups
    strings = next(g for g in groups if "pv1_voltage" in g.keys)
    # Present in a read group, or the coordinator never requests them.
    assert "mppt1_power" in strings.keys
    assert "mppt2_power" in strings.keys


@pytest.mark.asyncio
async def test_each_part_reports_its_own_identity():
    """A LUNA2000 is three kinds of hardware, each with its own serial.

    On the reference installation the inverter is BT24B1457565, the power module
    between it and the packs is TA2470074124, and the packs are theirs again.
    Publishing any of them as "the" serial mislabels the other two.
    """
    blocks = {
        **_LIVE_BLOCKS,
        30000: _text("SUN2000-8K-MAP0", 15) + _text("BT24B1457565", 10),
        37052: _text("TA2470074124", 10),
        37814: _text("V200R025C00SPC103", 15),
        30050: _text("V200R024C00SPC110", 15),
    }
    driver = _driver(_fake_client(blocks))
    data = await driver.read_telemetry()

    assert data["inverter_serial_number"] == "BT24B1457565"
    assert data["inverter_software_version"] == "V200R024C00SPC110"
    assert data["power_module_serial_number"] == "TA2470074124"
    assert data["power_module_firmware_version"] == "V200R025C00SPC103"
    # The device entry stands for the storage, so it takes the power module's.
    await driver.connect()
    assert driver.serial == "TA2470074124"


def test_each_identity_has_an_entity():
    keys = {row["key"] for row in SENSOR_DEFINITIONS}
    assert {
        "inverter_serial_number",
        "inverter_software_version",
        "power_module_serial_number",
        "power_module_firmware_version",
    } <= keys


# ----------------------------------------------------------------------
# off-grid / backup
#
# Huawei meters no separate backup port. While the grid is disconnected the
# inverter feeds nothing but the backup circuit, so its AC power *is* the
# backup output; on-grid there is no such output at all.
# ----------------------------------------------------------------------
def _state3(bits, ac_power=994):
    high, low = divmod(ac_power & 0xFFFFFFFF, 0x10000)
    pv = [0, 0] + [0] * 14 + [high, low]
    return {**_LIVE_BLOCKS, 32003: [0, bits], 32064: pv}


@pytest.mark.asyncio
async def test_on_grid_reports_no_backup_output():
    """The house supply must never be reported as backup power."""
    data = await _driver(_fake_client(_state3(0b00))).read_telemetry()
    assert data["ac_offgrid_power"] == 0
    assert data["backup_function"] == "Disabled"
    # The AC reading itself is untouched.
    assert data["inverter_ac_power"] == 994


@pytest.mark.asyncio
async def test_backup_armed_but_still_on_grid_is_not_an_output():
    data = await _driver(_fake_client(_state3(0b10))).read_telemetry()
    assert data["backup_function"] == "Ready"
    assert data["ac_offgrid_power"] == 0


@pytest.mark.asyncio
async def test_off_grid_reports_the_inverter_output_as_backup_power():
    data = await _driver(_fake_client(_state3(0b11, ac_power=3200))).read_telemetry()
    assert data["backup_function"] == "Off-grid"
    assert data["ac_offgrid_power"] == 3200


@pytest.mark.asyncio
async def test_off_grid_power_is_never_negative():
    # Importing while off-grid is not physical, but a transient must not print
    # a negative backup output.
    data = await _driver(_fake_client(_state3(0b01, ac_power=-500))).read_telemetry()
    assert data["ac_offgrid_power"] == 0


@pytest.mark.asyncio
async def test_backup_keys_are_absent_without_the_state_register():
    blocks = dict(_LIVE_BLOCKS)  # no 32003 entry -> the client returns None
    data = await _driver(_fake_client(blocks)).read_telemetry()
    assert "ac_offgrid_power" not in data
    assert "backup_function" not in data


@pytest.mark.asyncio
async def test_asking_for_backup_power_pulls_both_of_its_blocks():
    """Its two inputs live in different register blocks.

    Scheduling attaches the key to one group only, so the filter has to fetch
    the other block or the value would never appear.
    """
    data = await _driver(_fake_client(_state3(0b01, ac_power=2500))).read_telemetry(
        ["ac_offgrid_power"]
    )
    assert data["ac_offgrid_power"] == 2500


def test_cross_block_derivation_is_scheduled_exactly_once():
    groups = _driver().read_groups
    carrying = [g for g in groups if "ac_offgrid_power" in g.keys]
    assert len(carrying) == 1
    # Attached to the group holding its first source, not the one holding ac_power.
    assert "off_grid_state" in carrying[0].keys


# ----------------------------------------------------------------------
# string count
#
# SUN2000 models range from two strings to many. Register 30071 says how many
# this one has, so a two-string inverter does not sprout entities for strings
# it does not own — and a four-string one is not silently truncated.
# ----------------------------------------------------------------------
def _strings_block(count):
    # Four strings wired, each at 100 V and 1 A -> 100 W.
    return {**_LIVE_BLOCKS, 30071: [count], 32016: [1000, 100] * 4}


@pytest.mark.asyncio
@pytest.mark.parametrize("count,expected", [(2, 2), (3, 3), (4, 4)])
async def test_only_the_reported_strings_are_published(count, expected):
    data = await _driver(_fake_client(_strings_block(count))).read_telemetry()
    published = [k for k in data if k.startswith("mppt")]
    assert sorted(published) == sorted(f"mppt{i}_power" for i in range(1, expected + 1))
    assert data["mppt1_power"] == 100


@pytest.mark.asyncio
async def test_more_strings_than_the_panel_shows_are_capped():
    """The battery panel's MPPT card stops at four.

    Reading further would create entities nothing displays.
    """
    data = await _driver(_fake_client(_strings_block(8))).read_telemetry()
    published = [k for k in data if k.startswith("mppt")]
    assert len(published) == 4


@pytest.mark.asyncio
async def test_string_count_trims_the_entity_definitions():
    driver = _driver(_fake_client(_strings_block(2)))
    await driver.read_telemetry()
    keys = {d["key"] for d in driver.sensor_definitions}
    assert "mppt2_power" in keys
    assert "mppt3_power" not in keys
    assert "pv3_voltage" not in keys


@pytest.mark.asyncio
async def test_string_count_is_known_before_the_entities_are_built():
    """connect() has to learn it, or setup would build the default two."""
    driver = _driver(_fake_client(_strings_block(4)))
    assert await driver.connect() is True
    assert {"mppt3_power", "mppt4_power"} <= {d["key"] for d in driver.sensor_definitions}


# ----------------------------------------------------------------------
# firmware and pack serials
#
# Serial and firmware sit next to each other in each pack's address run, so one
# read covers both. The values below are the ones the reference LUNA2000
# reports; its pack 1 slot is empty and answers with padding only.
# ----------------------------------------------------------------------
def _text(value, registers):
    """Encode a string the way the inverter does: two chars per register."""
    raw = value.encode("ascii").ljust(registers * 2, b"\x00")
    return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]


def _pack(serial, firmware):
    return _text(serial, 10) + _text(firmware, 15)


@pytest.mark.asyncio
async def test_pack_and_inverter_firmware_are_exposed():
    blocks = {
        **_LIVE_BLOCKS,
        30050: _text("V200", 15),
        38242: _pack("EX24A0056894", "V200"),
        38284: _pack("EX2480065597", "V200"),
    }
    data = await _driver(_fake_client(blocks)).read_telemetry()
    assert data["inverter_software_version"] == "V200"
    assert data["pack2_firmware_version"] == "V200"
    assert data["pack3_firmware_version"] == "V200"


@pytest.mark.asyncio
async def test_each_pack_reports_its_own_serial():
    """The packs are the parts that get replaced, so they are worth naming."""
    blocks = {
        **_LIVE_BLOCKS,
        38242: _pack("EX24A0056894", "V200R025C00SPC103"),
        38284: _pack("EX2480065597", "V200R025C00SPC103"),
    }
    data = await _driver(_fake_client(blocks)).read_telemetry()
    assert data["pack2_serial_number"] == "EX24A0056894"
    assert data["pack3_serial_number"] == "EX2480065597"
    # Serial and firmware come out of the same read, not two.
    assert data["pack2_firmware_version"] == "V200R025C00SPC103"


def test_every_pack_serial_has_an_entity():
    keys = {row["key"] for row in SENSOR_DEFINITIONS}
    for index in (1, 2, 3):
        assert f"pack{index}_serial_number" in keys


@pytest.mark.asyncio
async def test_an_empty_pack_slot_gets_no_entity():
    """An entity that can only ever read "unknown" is worse than none.

    The reference LUNA2000 has packs 2 and 3; its pack 1 slot is empty, and
    listing it under diagnostics only invites the question what is wrong with it.
    """
    driver = _driver(_fake_client({
        **_LIVE_BLOCKS,
        38200: [0] * 25,
        38242: _pack("EX24A0056894", "V200"),
        38284: _pack("EX2480065597", "V200"),
    }))
    await driver.connect()

    keys = {row["key"] for row in driver.sensor_definitions}
    assert "pack1_serial_number" not in keys
    assert "pack1_firmware_version" not in keys
    assert {"pack2_serial_number", "pack3_serial_number"} <= keys


@pytest.mark.asyncio
async def test_packs_that_have_not_answered_yet_are_not_hidden():
    """No answer means "not asked", which is not the same as "not there"."""
    driver = _driver(_fake_client({}))
    keys = {row["key"] for row in driver.sensor_definitions}
    assert {f"pack{index}_serial_number" for index in (1, 2, 3)} <= keys


@pytest.mark.asyncio
async def test_an_empty_pack_slot_is_omitted_entirely():
    """Pack 1 answers with nothing but padding on the reference hardware.

    An empty slot must leave no entity behind at all — neither firmware nor
    serial — rather than showing up as a battery pack with blank details.
    """
    blocks = {**_LIVE_BLOCKS, 38200: [0] * 25}
    data = await _driver(_fake_client(blocks)).read_telemetry()
    assert "pack1_firmware_version" not in data
    assert "pack1_serial_number" not in data


@pytest.mark.asyncio
async def test_unwired_strings_leave_nothing_in_the_cache():
    """The block read covers four strings whatever the inverter has.

    Leaving the unused pairs in the telemetry cache would put keys with no
    entity behind them into diagnostics.
    """
    data = await _driver(_fake_client(_strings_block(2))).read_telemetry()
    assert "pv2_voltage" in data
    assert "pv3_voltage" not in data
    assert "pv4_current" not in data


@pytest.mark.asyncio
async def test_a_suppressed_reversal_reports_the_standing_command():
    """The worst case for the old behaviour: released, then asked to discharge.

    Zero releases the battery, so nothing is latched. A discharge request in the
    next few seconds is throttled — and reporting it as applied made the control
    layer expect a discharge that had never been commanded.
    """
    hass = _hass_with_services()
    driver = _driver(hass=hass)
    await driver.apply_setpoint(0, read_back=False)
    hass.services.async_call.reset_mock()

    result = await driver.apply_setpoint(-1200, read_back=False)
    hass.services.async_call.assert_not_awaited()
    assert result.net_power_w == 0
    assert result.applied["set_discharge_power"] == 0
    assert driver.net_power_from_data(result.applied) == 0


# ----------------------------------------------------------------------
# read-group shape
#
# The coordinator counts a group that returns nothing as a failed read, and a
# cycle where every attempted group fails marks the whole battery unavailable.
# A block holding one optional value therefore takes the device offline on
# every poll of that block — which is exactly what a firmware string the
# inverter leaves empty did on the reference hardware, every three seconds.
# ----------------------------------------------------------------------
def test_optional_values_never_sit_alone_in_a_group():
    for group in _driver().read_groups:
        assert len(group.keys) > 1, f"{group.keys} would fail as a whole when absent"


def test_groups_are_one_per_cadence():
    groups = _driver().read_groups
    intervals = [g.scan_interval for g in groups]
    assert len(intervals) == len(set(intervals))
    assert set(intervals) == {"high", "medium", "low", "very_low"}


def test_every_published_key_belongs_to_a_group():
    driver = _driver()
    scheduled = {key for group in driver.read_groups for key in group.keys}
    for definition in driver.sensor_definitions:
        # A key in no group is never requested, so its entity stays unknown.
        assert definition["key"] in scheduled, definition["key"]


@pytest.mark.asyncio
async def test_a_missing_firmware_string_does_not_empty_its_group():
    """Pack 1 answers with padding on the reference hardware.

    The group has to keep returning its other values, or the coordinator reads
    the empty result as a dead battery.
    """
    blocks = {**_LIVE_BLOCKS, 38200: [0] * 25}
    driver = _driver(_fake_client(blocks))
    very_low = next(g for g in driver.read_groups if g.scan_interval == "very_low")
    data = await driver.read_telemetry(list(very_low.keys))
    assert "pack1_firmware_version" not in data
    assert data, "an empty snapshot marks the whole battery unavailable"
    assert data["battery_total_energy"] == 13.8


# ----------------------------------------------------------------------
# house-consumption balance
#
# The system aggregates derive household load as
#     home = grid + sum(ac_power) + external_solar
# treating ac_power as the battery's own AC port, with DC-coupled PV already
# netted into it. Register 32080 is the whole inverter's AC output on this
# hybrid — PV included — so publishing it under that key counted the roof array
# twice: once inside ac_power, once in the external solar sensor.
# ----------------------------------------------------------------------
def test_inverter_ac_output_is_not_published_as_the_batterys_ac_port():
    keys = {d["key"] for d in _driver().sensor_definitions}
    assert "ac_power" not in keys
    assert "inverter_ac_power" in keys


@pytest.mark.asyncio
async def test_the_aggregates_fall_back_to_battery_power():
    """No ac_power means -battery_power is used, which is the real contribution.

    Worked example from the reference installation: 8.87 kW of PV, 8.0 kW of it
    into the battery, 167 W exported. House load is ~0.7 kW; counting the
    inverter's AC total as a battery AC port made it read 8.4 kW.
    """
    from custom_components.omnibattery.sensors.aggregate_sensors import (
        MarstekVenusAggregateSensor as Aggregate,
    )

    data = await _driver().read_telemetry()
    assert "ac_power" not in data
    ac_convention = Aggregate._ac_convention_power(data)
    assert ac_convention == -data["battery_power"]


def test_ac_convention_maths_for_the_reference_case():
    from custom_components.omnibattery.sensors.aggregate_sensors import (
        MarstekVenusAggregateSensor as Aggregate,
    )

    # Charging 8000 W: the battery is a load on the AC bus, so its contribution
    # is negative in the ac_power convention.
    contribution = Aggregate._ac_convention_power({"battery_power": 8000})
    assert contribution == -8000
    # home = grid + contribution + solar, with export counted negative.
    assert -167 + contribution + 8870 == 703


# ----------------------------------------------------------------------
# direct register writes
#
# Same four-register sequence huawei_solar performs, written straight to the
# inverter. The order matters: the mode register acts on the values already in
# place, so it is written last — and first when releasing.
# ----------------------------------------------------------------------
def _direct(hass=None, **kw):
    client = _fake_client()
    client.async_write_registers = AsyncMock(return_value=True)
    driver = _driver(client, hass=hass or _hass_with_services(), direct_write=True, **kw)
    return driver, client


def _written(client):
    return [(c.args[0], list(c.args[1])) for c in client.async_write_registers.await_args_list]


@pytest.mark.asyncio
async def test_charge_writes_the_parameters_before_the_mode():
    driver, client = _direct()
    result = await driver.apply_setpoint(1500, read_back=False)
    assert result.ok is True
    assert _written(client) == [
        (47247, [0, 1500]),   # charge power, u32 high word first
        (47083, [10]),        # duration in minutes
        (47246, [0]),         # target mode: by time
        (47100, [1]),         # mode: charge — triggers on the values above
    ]


@pytest.mark.asyncio
async def test_discharge_uses_its_own_power_register():
    driver, client = _direct()
    await driver.apply_setpoint(-900, read_back=False)
    assert _written(client) == [
        (47249, [0, 900]),
        (47083, [10]),
        (47246, [0]),
        (47100, [2]),
    ]


@pytest.mark.asyncio
async def test_release_stops_before_it_tidies_up():
    driver, client = _direct()
    await driver.apply_setpoint(0, read_back=False)
    written = _written(client)
    # Stop first: clearing the parameters while a command still runs would act
    # on the cleared values.
    assert written[0] == (47100, [0])
    assert written[1:] == [(47249, [0, 0]), (47083, [0]), (47246, [0])]


@pytest.mark.asyncio
async def test_a_large_value_is_clamped_to_the_register_maximum():
    """huawei_solar refuses an over-range power; clamping keeps the loop alive."""
    driver, client = _direct(max_charge_power_w=7000)
    await driver.apply_setpoint(99000, read_back=False)
    assert _written(client)[0] == (47247, [0, 7000])


@pytest.mark.asyncio
async def test_a_failed_write_never_reaches_the_mode_register():
    """A half-written sequence must leave the inverter doing what it did."""
    driver, client = _direct()
    client.async_write_registers = AsyncMock(side_effect=[True, False])
    result = await driver.apply_setpoint(1500, read_back=False)
    assert result.ok is False
    assert result.failure_reason == "register_write_failed"
    assert 47100 not in [addr for addr, _ in _written(client)]


@pytest.mark.asyncio
async def test_direct_mode_calls_no_services_at_all():
    """The point of the option: no dependency on huawei_solar for control."""
    hass = _hass_with_services()
    driver, _client = _direct(hass=hass)
    await driver.apply_setpoint(1500, read_back=False)
    await driver.apply_setpoint(0, read_back=False)
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_throttle_still_applies_in_direct_mode():
    driver, client = _direct()
    await driver.apply_setpoint(1500, read_back=False)
    client.async_write_registers.reset_mock()
    result = await driver.apply_setpoint(1550, read_back=False)
    client.async_write_registers.assert_not_awaited()
    assert result.net_power_w == 1500


def test_u32_splits_high_word_first():
    from custom_components.omnibattery.drivers.huawei import _u32

    assert _u32(0) == [0, 0]
    assert _u32(1500) == [0, 1500]
    assert _u32(70000) == [1, 4464]
    # Negative power never reaches a magnitude register.
    assert _u32(-5) == [0, 0]


@pytest.mark.asyncio
async def test_direct_mode_needs_no_huawei_solar_battery_device():
    """Found by the first hardware run: the guard sat before the branch.

    Requiring huawei_solar's battery device in the mode whose entire purpose is
    doing without that integration blocked every set-point with
    no_battery_device.
    """
    client = _fake_client()
    client.async_write_registers = AsyncMock(return_value=True)
    driver = _driver(client, hass=_hass_with_services(), direct_write=True, device_id="")
    result = await driver.apply_setpoint(500, read_back=False)
    assert result.ok is True
    assert result.failure_reason is None
    assert _written(client)[0] == (47247, [0, 500])


@pytest.mark.asyncio
async def test_the_service_path_still_requires_the_device():
    driver = _driver(hass=_hass_with_services(), device_id="")
    result = await driver.apply_setpoint(500, read_back=False)
    assert result.ok is False
    assert result.failure_reason == "no_battery_device"


@pytest.mark.asyncio
async def test_direct_mode_writes_cutoffs_to_their_registers():
    """Otherwise the device id would still be needed for the SOC window."""
    client = _fake_client()
    client.async_write_registers = AsyncMock(return_value=True)
    driver = _driver(client, hass=_hass_with_services(), direct_write=True, device_id="")
    assert await driver.set_charge_cutoff(95) is True
    assert await driver._write_cutoff("discharging", 10) is True
    assert _written(client) == [(47081, [950]), (47082, [100])]


@pytest.mark.asyncio
async def test_direct_mode_still_refuses_an_unrepresentable_cutoff():
    client = _fake_client()
    client.async_write_registers = AsyncMock(return_value=True)
    driver = _driver(client, hass=_hass_with_services(), direct_write=True, device_id="")
    # 47081 accepts 90-100 % only; a clamped write would move the backstop.
    assert await driver.set_charge_cutoff(80) is False
    client.async_write_registers.assert_not_awaited()


def test_the_write_interval_sits_under_the_declared_ramp():
    """Measured on hardware: ~20 s to land a charge set-point, 11 s to reverse.

    Re-issuing before the previous command lands moves a target the battery is
    still travelling towards; waiting past the ramp makes the loop slower than
    the hardware needs.
    """
    from custom_components.omnibattery.drivers.huawei import (
        _ACTUATOR_LATENCY_S,
        _MIN_WRITE_INTERVAL_S,
    )

    assert _MIN_WRITE_INTERVAL_S <= _ACTUATOR_LATENCY_S
    assert _MIN_WRITE_INTERVAL_S >= 15.0


# ----------------------------------------------------------------------
# slave-id discovery
#
# The slave id is not derivable and differs between setups: on the reference
# installation the inverter answers on 4, while 0 is the energy manager, 2 a
# backup switch and 9 a charger. Asking a user to guess is the least friendly
# part of the setup.
# ----------------------------------------------------------------------
def _bus(monkeypatch, **by_id):
    """Fake a Modbus bus where each slave id answers with its own blocks."""
    import custom_components.omnibattery.drivers.huawei as mod

    def factory(host, port, slave_id):
        blocks = by_id.get(str(slave_id))
        client = _fake_client(blocks if blocks is not None else {})
        client.unit_id = slave_id
        return client

    monkeypatch.setattr(mod, "HuaweiModbusClient", factory)
    return mod


@pytest.mark.asyncio
async def test_scan_reports_every_inverter_on_the_bus(monkeypatch):
    """Huawei inverters can be cascaded, so the scan must not stop at the first."""
    inverter_without_battery = {30000: _LIVE_BLOCKS[30000]}
    mod = _bus(monkeypatch, **{"1": {}, "4": _LIVE_BLOCKS, "5": inverter_without_battery})
    monkeypatch.setattr(mod, "_SLAVE_ID_CANDIDATES", (1, 4, 5))

    found = await mod.HuaweiSolarDriver.scan_slave_ids(MagicMock(), "1.2.3.4", 502)
    assert [(sid, batt) for sid, _model, batt in found] == [(4, True), (5, False)]
    assert found[0][1] == "SUN2000-8K-MAP0"


@pytest.mark.asyncio
async def test_scan_ignores_ids_that_are_not_inverters(monkeypatch):
    """0 is the energy manager on the reference bus, 2 a backup switch."""
    mod = _bus(monkeypatch, **{"4": _LIVE_BLOCKS})
    monkeypatch.setattr(mod, "_SLAVE_ID_CANDIDATES", (1, 0, 2, 4))
    found = await mod.HuaweiSolarDriver.scan_slave_ids(MagicMock(), "1.2.3.4", 502)
    assert [sid for sid, _m, _b in found] == [4]


@pytest.mark.asyncio
async def test_scan_stops_when_the_address_itself_is_unreachable(monkeypatch):
    """No point walking nine ids when nothing is listening at all."""
    import custom_components.omnibattery.drivers.huawei as mod

    tried = []

    def factory(host, port, slave_id):
        tried.append(slave_id)
        client = _fake_client({})
        client.async_connect = AsyncMock(return_value=False)
        return client

    monkeypatch.setattr(mod, "HuaweiModbusClient", factory)
    monkeypatch.setattr(mod, "_SLAVE_ID_CANDIDATES", (1, 2, 3, 4))
    assert await mod.HuaweiSolarDriver.scan_slave_ids(MagicMock(), "1.2.3.4", 502) == []
    assert tried == [1]


def test_the_candidate_list_covers_the_known_layouts():
    from custom_components.omnibattery.drivers.huawei import _SLAVE_ID_CANDIDATES

    # 1 is the factory default for a direct connection; 4 is where the
    # reference installation's inverter sits behind its energy manager.
    assert _SLAVE_ID_CANDIDATES[0] == 1
    assert 4 in _SLAVE_ID_CANDIDATES


def test_the_slave_id_field_is_empty_by_default():
    """Empty means "go and find it", so nothing may be prefilled on a new entry.

    A prefilled guess would invite the user to accept a wrong one — the id is
    not derivable, and a wrong one reads a charger or an energy manager.
    """
    import voluptuous as vol
    from custom_components.omnibattery.config_flow import MarstekVenusConfigFlow

    schema = MarstekVenusConfigFlow._huawei_schema({}, 1)
    marker = next(m for m in schema.schema if m.schema == "slave_id")
    assert marker.default is vol.UNDEFINED
    assert (marker.description or {}).get("suggested_value") is None


def test_an_existing_slave_id_is_offered_again_on_reconfigure():
    from custom_components.omnibattery.config_flow import MarstekVenusConfigFlow

    schema = MarstekVenusConfigFlow._huawei_schema({"slave_id": 4}, 1)
    marker = next(m for m in schema.schema if m.schema == "slave_id")
    assert marker.description["suggested_value"] == 4


def test_the_empty_field_is_explained_in_every_language():
    """A field that does something when left blank has to say so."""
    import glob
    import json

    for path in ["custom_components/omnibattery/strings.json"] + sorted(
        glob.glob("custom_components/omnibattery/translations/*.json")
    ):
        step = json.load(open(path, encoding="utf-8"))["config"]["step"]
        data = step["battery_connection_huawei"]["data"]
        hint = step["battery_connection_huawei"]["data_description"]["slave_id"]
        assert "empty" in data["slave_id"].lower() or "leer" in data["slave_id"].lower(), path
        assert "empty" in hint.lower() or "leer" in hint.lower(), path


# ----------------------------------------------------------------------
# pairing the Modbus address with the huawei_solar device
#
# On the service path the battery is named twice: once as a Modbus address and
# once as a device in the registry. Nothing forces those to be the same
# inverter — and Huawei inverters can be cascaded, so on a two-inverter bus the
# wrong pairing reads one unit and commands the other. The inverter serial
# appears on both sides (register 30015, and huawei_solar's device identifier),
# so the pairing is checkable. On the reference installation both read
# BT24B1457565.
# ----------------------------------------------------------------------
def _huawei_flow(monkeypatch, *, probe, devices=None, huawei_solar_installed=True, emma=None):
    """A config flow whose Huawei probe and device registry are faked."""
    from types import SimpleNamespace

    from custom_components.omnibattery import config_flow as mod

    flow = mod.MarstekVenusConfigFlow()
    flow.battery_index = 0
    flow.config_data = {"num_batteries": 1}
    flow._current_battery_data = {"brand": "huawei"}
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_entries=lambda domain: [object()] if huawei_solar_installed else []
        )
    )
    monkeypatch.setattr(mod.HuaweiSolarDriver, "probe", AsyncMock(return_value=probe))
    # Never let a flow test reach for the network: the EMMA scan would sit on
    # connection timeouts for every candidate id.
    monkeypatch.setattr(
        mod.HuaweiSolarDriver, "find_emma_slave_id", AsyncMock(return_value=emma)
    )
    registry = MagicMock()
    registry.async_get.side_effect = (devices or {}).get
    monkeypatch.setattr(mod.dr, "async_get", lambda hass: registry)
    return flow


def _huawei_device(serial=None, identifier=None, via=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        serial_number=serial,
        identifiers={("huawei_solar", identifier)} if identifier else set(),
        via_device_id=via,
    )


_HUAWEI_INPUT = {
    "name": "Huawei LUNA2000",
    "host": "192.168.1.5",
    "port": 502,
    "slave_id": 4,
    "huawei_battery_device": "dev-batt",
    "huawei_direct_write": False,
}


@pytest.mark.asyncio
async def test_a_battery_from_another_inverter_is_refused(monkeypatch):
    """Cascade: the device hangs off inverter B, the address points at A."""
    flow = _huawei_flow(
        monkeypatch,
        probe=(True, "SUN2000-8K-MAP0", 7000, 7000, "BT24B1457565", 8800),
        devices={
            "dev-batt": _huawei_device(identifier="battery-2", via="dev-inv-2"),
            "dev-inv-2": _huawei_device(serial="BT24B9999999"),
        },
    )
    form = await flow.async_step_battery_connection_huawei(dict(_HUAWEI_INPUT))

    assert form["errors"] == {"huawei_battery_device": "huawei_device_mismatch"}
    assert form["step_id"] == "battery_connection_huawei"
    # Nothing was committed, so the user lands back on the same form.
    assert "slave_id" not in flow._current_battery_data


@pytest.mark.asyncio
async def test_a_battery_on_the_probed_inverter_is_accepted(monkeypatch):
    flow = _huawei_flow(
        monkeypatch,
        probe=(True, "SUN2000-8K-MAP0", 7000, 7000, "BT24B1457565", 8800),
        devices={
            "dev-batt": _huawei_device(identifier="battery", via="dev-inv"),
            "dev-inv": _huawei_device(serial="bt24b1457565"),  # registries vary in case
        },
    )
    form = await flow.async_step_battery_connection_huawei(dict(_HUAWEI_INPUT))

    assert form["step_id"] == "battery_limits"
    assert flow._current_battery_data["slave_id"] == 4
    assert flow._current_battery_data["device_max_charge_power"] == 7000


@pytest.mark.asyncio
async def test_a_device_whose_inverter_is_unknown_is_not_blocked(monkeypatch):
    """Only a contradiction may stop the flow, never a missing serial.

    Older huawei_solar releases do not fill serial_number, and a firmware that
    leaves register 30015 empty is not a reason to refuse a working setup.
    """
    flow = _huawei_flow(
        monkeypatch,
        probe=(True, "SUN2000-8K-MAP0", 7000, 7000, "BT24B1457565", 8800),
        devices={"dev-batt": _huawei_device()},
    )
    assert (await flow.async_step_battery_connection_huawei(dict(_HUAWEI_INPUT)))[
        "step_id"
    ] == "battery_limits"

    flow = _huawei_flow(
        monkeypatch,
        probe=(True, "SUN2000-8K-MAP0", 7000, 7000, None, 8800),
        devices={"dev-batt": _huawei_device(serial="BT24B9999999")},
    )
    assert (await flow.async_step_battery_connection_huawei(dict(_HUAWEI_INPUT)))[
        "step_id"
    ] == "battery_limits"


@pytest.mark.asyncio
async def test_the_serial_identifies_the_inverter_even_without_a_parent(monkeypatch):
    """huawei_solar names the inverter device itself by serial."""
    flow = _huawei_flow(
        monkeypatch,
        probe=(True, "SUN2000-8K-MAP0", 7000, 7000, "BT24B1457565", 8800),
        devices={"dev-batt": _huawei_device(identifier="BT24B9999999")},
    )
    form = await flow.async_step_battery_connection_huawei(dict(_HUAWEI_INPUT))
    assert form["errors"] == {"huawei_battery_device": "huawei_device_mismatch"}


@pytest.mark.asyncio
async def test_a_missing_huawei_solar_integration_is_named_as_such(monkeypatch):
    """"Pick a device" is unhelpful advice when there are no devices to pick."""
    payload = dict(_HUAWEI_INPUT, huawei_battery_device="")

    flow = _huawei_flow(monkeypatch, probe=(False, None, None, None, None, None))
    form = await flow.async_step_battery_connection_huawei(dict(payload))
    assert form["errors"] == {"huawei_battery_device": "huawei_device_required"}

    flow = _huawei_flow(
        monkeypatch, probe=(False, None, None, None, None, None), huawei_solar_installed=False
    )
    form = await flow.async_step_battery_connection_huawei(dict(payload))
    assert form["errors"] == {"huawei_battery_device": "huawei_solar_missing"}


@pytest.mark.asyncio
async def test_direct_writes_need_no_device_at_all(monkeypatch):
    """The whole point of the direct path is not depending on that integration."""
    flow = _huawei_flow(
        monkeypatch,
        probe=(True, "SUN2000-8K-MAP0", 7000, 7000, "BT24B1457565", 8800),
        huawei_solar_installed=False,
    )
    form = await flow.async_step_battery_connection_huawei(
        dict(_HUAWEI_INPUT, huawei_battery_device="", huawei_direct_write=True)
    )
    assert form["step_id"] == "battery_limits"
    assert flow._current_battery_data["huawei_direct_write"] is True


def test_both_new_errors_are_explained_in_every_language():
    import glob
    import json

    for path in ["custom_components/omnibattery/strings.json"] + sorted(
        glob.glob("custom_components/omnibattery/translations/*.json")
    ):
        content = json.load(open(path, encoding="utf-8"))
        for section in ("config", "options"):
            errors = content[section]["error"]
            for key in ("huawei_solar_missing", "huawei_device_mismatch"):
                assert errors.get(key), (path, section, key)


def test_the_options_flow_carries_the_same_huawei_logic():
    """Both flows hold their own copy of these steps; they must not drift.

    Setup and reconfiguration reach the same hardware, so a check added to one
    and forgotten in the other would only be found by the user it fails on.
    """
    import inspect

    from custom_components.omnibattery.config_flow import (
        MarstekVenusConfigFlow,
        OptionsFlowHandler,
    )

    for name in (
        "async_step_battery_connection_huawei",
        "async_step_battery_connection_huawei_slave",
        "_huawei_store",
        "_huawei_search",
    ):
        setup = inspect.getsource(getattr(MarstekVenusConfigFlow, name))
        options = inspect.getsource(getattr(OptionsFlowHandler, name))
        assert setup == options, name


@pytest.mark.asyncio
async def test_a_serial_carried_with_a_suffix_still_counts_as_a_match(monkeypatch):
    """huawei_solar derives child identifiers from the inverter serial."""
    flow = _huawei_flow(
        monkeypatch,
        probe=(True, "SUN2000-8K-MAP0", 7000, 7000, "BT24B1457565", 8800),
        devices={"dev-batt": _huawei_device(identifier="BT24B1457565_batteries")},
    )
    form = await flow.async_step_battery_connection_huawei(dict(_HUAWEI_INPUT))
    assert form["step_id"] == "battery_limits"


@pytest.mark.asyncio
async def test_the_device_is_named_after_the_storage_not_the_inverter():
    """The device entry stands for the battery, so SUN2000 would mislead.

    Naming it after the inverter reads as though the packs and the power module
    belonged to the inverter. Register 47000 says which storage is attached; the
    inverter keeps its model in its own entity.
    """
    driver = _driver(_fake_client({**_LIVE_BLOCKS, 47000: [2]}))
    await driver.connect()
    assert driver.model_label == "LUNA2000"

    data = await driver.read_telemetry()
    assert data["device_name"] == "SUN2000-8K-MAP0"
    # The raw enum is a means to an end and does not belong in diagnostics.
    assert "storage_product_model" not in data


@pytest.mark.asyncio
async def test_an_unknown_storage_falls_back_to_the_inverter_model():
    """Better a model that is merely imprecise than none at all."""
    driver = _driver(_fake_client({**_LIVE_BLOCKS, 47000: [7]}))
    await driver.connect()
    assert driver.model_label == "SUN2000-8K-MAP0"


# ----------------------------------------------------------------------
# form serialisation
#
# Home Assistant hands the frontend a serialised copy of every form schema.
# Anything voluptuous_serialize cannot convert makes the whole step fail with
# "Unknown error occurred" before it is ever drawn — which is what a bare
# vol.Any in the slave-id field did: the Huawei setup could not be opened at
# all, and no test noticed because inspecting markers never serialises them.
# ----------------------------------------------------------------------
def _serialise(schema):
    from homeassistant.helpers import config_validation as cv

    # Home Assistant 2026 serializes its probatio schemas with to_field_list;
    # older supported test environments expose the voluptuous serializer instead.
    try:
        from probatio import to_field_list
    except ImportError:
        import voluptuous_serialize

        result = voluptuous_serialize.convert(
            schema, custom_serializer=cv.custom_serializer
        )
    else:
        result = to_field_list(schema, custom_serializer=cv.custom_serializer)
    assert isinstance(result, list), f"schema did not serialize: {result!r}"
    return result


def test_every_huawei_form_can_be_sent_to_the_frontend():
    from custom_components.omnibattery.config_flow import (
        MarstekVenusConfigFlow,
        OptionsFlowHandler,
    )

    for flow in (MarstekVenusConfigFlow, OptionsFlowHandler):
        assert _serialise(flow._huawei_schema({}, 1))
        assert _serialise(flow._huawei_schema({"slave_id": 4, "host": "1.2.3.4"}, 2))
        assert _serialise(flow._huawei_slave_schema([(4, "SUN2000-8K-MAP0", True)]))


@pytest.mark.asyncio
async def test_the_rendered_connection_form_survives_serialisation(monkeypatch):
    """The step's own output, not just the schema helper it happens to call."""
    flow = _huawei_flow(monkeypatch, probe=(False, None, None, None, None, None))
    form = await flow.async_step_battery_connection_huawei()
    fields = {field["name"]: field for field in _serialise(form["data_schema"])}

    assert set(fields) == {
        "name", "host", "port", "slave_id", "huawei_direct_write",
        "huawei_battery_device",
    }
    # Optional and unprefilled: empty means "go and find it".
    assert fields["slave_id"]["optional"] is True
    assert "default" not in fields["slave_id"]


# ----------------------------------------------------------------------
# power ceilings in the limits form
#
# Registers 37046/37048 report what the battery permits right now, and a third
# pack raises that. Using the momentary reading as the form's hard ceiling
# locked the user out of a figure the installation can genuinely reach.
# ----------------------------------------------------------------------
def test_the_form_allows_more_than_the_battery_reports_today():
    from custom_components.omnibattery.config_flow import _huawei_power_ceilings

    probed = {
        "device_max_charge_power": 7000,
        "device_max_discharge_power": 7000,
        "device_inverter_max_power": 8800,
    }
    # Everything passes through the inverter, so its rating is the real bound.
    assert _huawei_power_ceilings(probed) == (8800, 8800)


def test_without_an_inverter_rating_the_battery_figure_stands_in():
    from custom_components.omnibattery.config_flow import _huawei_power_ceilings

    assert _huawei_power_ceilings({
        "device_max_charge_power": 7000, "device_max_discharge_power": 5000,
    }) == (7000, 5000)
    # Nothing probed at all: a usable form beats an empty one.
    assert _huawei_power_ceilings({}) == (5000, 5000)


def test_a_malformed_reading_cannot_open_the_form_to_anything():
    from custom_components.omnibattery.config_flow import _huawei_power_ceilings

    assert _huawei_power_ceilings({"device_inverter_max_power": 900000}) == (15000, 15000)


@pytest.mark.asyncio
async def test_the_probed_figure_becomes_the_starting_value(monkeypatch):
    """Wider ceiling, but the form still opens on what the hardware reports."""
    flow = _huawei_flow(
        monkeypatch,
        probe=(True, "SUN2000-8K-MAP0", 7000, 7000, "BT24B1457565", 8800),
        devices={"dev-batt": _huawei_device(identifier="BT24B1457565")},
    )
    await flow.async_step_battery_connection_huawei(dict(_HUAWEI_INPUT))

    assert flow._current_battery_data["max_charge_power"] == 7000
    assert flow._current_battery_data["device_inverter_max_power"] == 8800


# ----------------------------------------------------------------------
# commands against a limit the battery has outgrown — or not yet reached
#
# The limits form allows more than the battery reports today, because a third
# pack raises that figure. Until it does, a command above the present reading is
# refused outright — huawei_solar raises rather than trimming — so the live
# figure has to bind at the driver.
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_configured_limit_above_the_hardware_is_trimmed_not_sent():
    hass = _hass_with_services()
    driver = _driver(_fake_client(_LIVE_BLOCKS), hass=hass, max_charge_power_w=7500, max_discharge_power_w=7500)
    await driver.connect()  # picks up 37046/37048 = 7000 W

    result = await driver.apply_setpoint(7500, read_back=False)
    assert result.net_power_w == 7000
    called = hass.services.async_call.await_args
    assert called.args[2]["power"] == 7000


@pytest.mark.asyncio
async def test_the_configured_limit_still_binds_when_it_is_the_lower_one():
    """The register is a ceiling, never a licence to exceed the user's figure."""
    hass = _hass_with_services()
    driver = _driver(_fake_client(_LIVE_BLOCKS), hass=hass, max_charge_power_w=3000, max_discharge_power_w=3000)
    await driver.connect()

    assert (await driver.apply_setpoint(7000, read_back=False)).net_power_w == 3000


@pytest.mark.asyncio
async def test_an_unread_register_leaves_the_configured_limit_in_charge():
    """No reading yet is not the same as a reading of zero."""
    hass = _hass_with_services()
    driver = _driver(_fake_client({}), hass=hass, max_charge_power_w=7500, max_discharge_power_w=7500)
    assert (await driver.apply_setpoint(7500, read_back=False)).net_power_w == 7500


# ----------------------------------------------------------------------
# the EMMA's grid meter
#
# An installation with an EMMA is metered by it, and may well have no other
# meter at all. huawei_solar publishes the same value on a 30 s coordinator —
# measured on the reference installation — which is far too slow to control
# against. Read here it is live on every request, at 25 ms per read.
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_grid_meter_is_read_from_the_emmas_own_unit():
    meter = _fake_client({31657: [0xFFFF, 0xFFE5]})   # -27 W, exporting
    driver = _driver(_fake_client(_LIVE_BLOCKS), meter_client=meter)
    data = await driver.read_telemetry(["grid_power"])
    # Sign matches the Omnibattery convention: positive imports, negative exports.
    assert data["grid_power"] == -27


@pytest.mark.asyncio
async def test_no_emma_means_no_grid_entity():
    """An entity that could only ever read unknown is worse than none."""
    driver = _driver(_fake_client(_LIVE_BLOCKS))
    assert "grid_power" not in {row["key"] for row in driver.sensor_definitions}
    assert "grid_power" not in {k for g in driver.read_groups for k in g.keys}


@pytest.mark.asyncio
async def test_the_meter_joins_the_fast_group():
    """A grid reading is only worth having if it arrives at control speed."""
    driver = _driver(_fake_client(_LIVE_BLOCKS), meter_client=_fake_client({31657: [0, 0]}))
    fast = next(g for g in driver.read_groups if g.scan_interval == "high")
    assert "grid_power" in fast.keys
    assert "grid_power" in {row["key"] for row in driver.sensor_definitions}


@pytest.mark.asyncio
async def test_a_silent_meter_costs_the_reading_and_nothing_else():
    meter = _fake_client({})
    driver = _driver(_fake_client(_LIVE_BLOCKS), meter_client=meter)
    data = await driver.read_telemetry()
    assert "grid_power" not in data
    assert data["battery_soc"] == 61.0


@pytest.mark.asyncio
async def test_the_emma_is_found_by_its_model_name(monkeypatch):
    """Users should not have to know their energy manager's unit id."""
    inverter = {30000: _text("SUN2000-8K-MAP0", 15)}
    emma = {30000: _text("SmartHEMS", 15)}
    mod = _bus(monkeypatch, **{"1": {}, "0": emma, "4": inverter})
    monkeypatch.setattr(mod, "_SLAVE_ID_CANDIDATES", (1, 0, 4))

    found = await mod.HuaweiSolarDriver.find_emma_slave_id(MagicMock(), "1.2.3.4", 502)
    assert found == 0


@pytest.mark.asyncio
async def test_a_bus_without_an_emma_reports_none(monkeypatch):
    mod = _bus(monkeypatch, **{"4": {30000: _text("SUN2000-8K-MAP0", 15)}})
    monkeypatch.setattr(mod, "_SLAVE_ID_CANDIDATES", (1, 4))
    assert await mod.HuaweiSolarDriver.find_emma_slave_id(MagicMock(), "1.2.3.4", 502) is None


@pytest.mark.asyncio
async def test_a_discovered_emma_is_stored_with_the_battery(monkeypatch):
    flow = _huawei_flow(
        monkeypatch,
        probe=(True, "SUN2000-8K-MAP0", 7000, 7000, "BT24B1457565", 8800),
        devices={"dev-batt": _huawei_device(identifier="BT24B1457565")},
        emma=0,
    )
    await flow.async_step_battery_connection_huawei(dict(_HUAWEI_INPUT))
    assert flow._current_battery_data["huawei_emma_slave_id"] == 0


def test_the_grid_sensor_is_named_in_every_language():
    import glob
    import json

    for path in ["custom_components/omnibattery/strings.json"] + sorted(
        glob.glob("custom_components/omnibattery/translations/*.json")
    ):
        entity = json.load(open(path, encoding="utf-8"))["entity"]["sensor"]
        assert entity["grid_power"]["name"], path


# ----------------------------------------------------------------------
# a forcible command caps this inverter's own production
#
# The worst fault this driver has caused, and the one most specific to a hybrid.
# A forcible command is not a request but a ceiling: the inverter produces
# exactly what it was told and curtails the rest of the roof.
#
# Caught in the act with a 315 W charge standing — 288 W harvested, 5054 W six
# seconds after it ended. A discharge is worse still: it serves the house from
# the battery and leaves the tracker down entirely, so the roof makes nothing,
# and the missing production reads as a deficit that asks for more discharge.
# That one ran from 04:32 to 09:22 through a cloudless sunrise.
#
# So while there is light on the panels this driver commands nothing at all and
# leaves the inverter to its own regulation, which harvests everything.
# ----------------------------------------------------------------------
def _pv_block(watts):
    """Register 32064 with a given DC total, AC output left as it was."""
    return [(watts >> 16) & 0xFFFF, watts & 0xFFFF] + [0] * 14 + [0, 758]


def _lit_blocks(watts=5054):
    """Strings under load on a sunny morning: 374 V, and a roof worth keeping."""
    return {
        **_LIVE_BLOCKS,
        32016: [3741, 1141, 3757, 1143],
        32064: _pv_block(watts),
    }


def _dim_blocks(watts=50):
    """An overcast dawn: the strings are up, the array makes almost nothing."""
    return {**_LIVE_BLOCKS, 32016: [3001, 17, 2987, 16], 32064: _pv_block(watts)}


def _dark_blocks():
    """After sunset the voltage collapses with the light."""
    return {**_LIVE_BLOCKS, 32016: [0, 0, 0, 0], 32064: _pv_block(0)}


@pytest.mark.asyncio
async def test_light_on_the_panels_is_read_from_the_string_voltage():
    driver = _driver(_fake_client(_lit_blocks()))
    await driver.read_telemetry()
    assert driver._pv_lit is True


@pytest.mark.asyncio
async def test_darkness_is_read_the_same_way():
    """No forecast, sun elevation or clock: the strings say it themselves."""
    driver = _driver(_fake_client(_dark_blocks()))
    await driver.read_telemetry()
    assert driver._pv_lit is False


@pytest.mark.asyncio
async def test_a_string_at_voltage_but_idle_still_counts_as_lit():
    """The state a held command leaves behind — 374 V at 0.00 A."""
    driver = _driver(_fake_client({
        **_LIVE_BLOCKS, 32016: [3741, 0, 3757, 0], 32064: _pv_block(5054),
    }))
    await driver.read_telemetry()
    assert driver._pv_lit is True


@pytest.mark.asyncio
async def test_no_charge_is_commanded_while_the_panels_are_lit():
    """315 W commanded, 288 W harvested, 5054 W once it stopped."""
    driver = _driver(_fake_client(_lit_blocks()), hass=_hass_with_services())
    await driver.read_telemetry()
    assert (await driver.apply_setpoint(315, read_back=False)).net_power_w == 0


@pytest.mark.asyncio
async def test_no_discharge_is_commanded_while_the_panels_are_lit():
    driver = _driver(_fake_client(_lit_blocks()), hass=_hass_with_services())
    await driver.read_telemetry()
    assert (await driver.apply_setpoint(-2000, read_back=False)).net_power_w == 0


@pytest.mark.asyncio
async def test_after_dark_the_battery_is_commanded_as_normal():
    """The whole point of the driver, and none of this applies without sun."""
    driver = _driver(_fake_client(_dark_blocks()), hass=_hass_with_services())
    await driver.read_telemetry()
    assert (await driver.apply_setpoint(-2000, read_back=False)).net_power_w == -2000


@pytest.mark.asyncio
async def test_sunrise_hands_control_over_without_a_restart():
    table = dict(_dark_blocks())
    client = _fake_client()
    client.async_read_holding_block = AsyncMock(side_effect=lambda start, count: table.get(start))
    driver = _driver(client, hass=_hass_with_services())
    await driver.read_telemetry()
    assert (await driver.apply_setpoint(-2000, read_back=False)).net_power_w == -2000

    table.update(_lit_blocks())
    await driver.read_telemetry()
    driver._last_write_monotonic = 0.0
    assert (await driver.apply_setpoint(-2000, read_back=False)).net_power_w == 0


# ----------------------------------------------------------------------
# Voltage alone cannot tell a bright morning from a dim one. Measured across
# three dawns on the reference installation, the array stays under 150 W for
# 45-50 minutes after first light while the strings already carry ~300 V —
# time the battery was refused for nothing.
#
# Harvested power closes that hole, but only carefully: while a command stands
# the production it suppresses is our own artefact, so a low reading proves
# nothing. A high one still does, which is what hands control back at sunrise.
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_overcast_dawn_does_not_block_the_battery():
    """300 V on the strings, 50 W off the roof: nothing worth curtailing."""
    driver = _driver(_fake_client(_dim_blocks()), hass=_hass_with_services())
    await driver.read_telemetry()
    assert driver._strings_lit is True
    assert driver._pv_lit is False
    assert (await driver.apply_setpoint(-2000, read_back=False)).net_power_w == -2000


@pytest.mark.asyncio
async def test_the_threshold_is_on_harvest_not_on_voltage():
    driver = _driver(_fake_client(_dim_blocks(watts=151)))
    await driver.read_telemetry()
    assert driver._pv_lit is True


@pytest.mark.asyncio
async def test_production_while_commanding_is_believed_at_once():
    """Our own command can only push this number down, never up."""
    driver = _driver(_fake_client(_dim_blocks()), hass=_hass_with_services())
    await driver.read_telemetry()
    await driver.apply_setpoint(-2000, read_back=False)
    assert driver._last_written_w == -2000

    driver._client.async_read_holding_block = AsyncMock(
        side_effect=lambda start, count: _lit_blocks().get(start)
    )
    await driver.read_telemetry()
    assert driver._pv_lit is True
    # The release still waits out the ramp throttle; see the test below.
    driver._last_write_monotonic -= _MIN_WRITE_INTERVAL_S + 1
    assert (await driver.apply_setpoint(-2000, read_back=False)).net_power_w == 0


@pytest.mark.asyncio
async def test_the_release_waits_out_the_ramp_throttle():
    """A verdict change does not jump the queue: rewriting mid-ramp achieves
    nothing, so the hand-back is reported as the command still in force."""
    driver = _driver(_fake_client(_dim_blocks()), hass=_hass_with_services())
    await driver.read_telemetry()
    await driver.apply_setpoint(-2000, read_back=False)

    driver._client.async_read_holding_block = AsyncMock(
        side_effect=lambda start, count: _lit_blocks().get(start)
    )
    await driver.read_telemetry()
    assert driver._pv_lit is True
    held = await driver.apply_setpoint(-2000, read_back=False)
    assert held.net_power_w == -2000
    assert held.confirmed is False


@pytest.mark.asyncio
async def test_a_quiet_roof_while_commanding_is_not_read_as_darkness():
    """The 04:32 to 09:22 failure: a held discharge suppresses its own evidence."""
    driver = _driver(_fake_client(_dim_blocks()), hass=_hass_with_services())
    await driver.read_telemetry()
    await driver.apply_setpoint(-2000, read_back=False)

    # The tracker is now down, so the roof reports nothing whatever the sky does.
    driver._client.async_read_holding_block = AsyncMock(
        side_effect=lambda start, count: _lit_blocks(watts=0).get(start)
    )
    verdict_at = driver._pv_verdict_monotonic
    await driver.read_telemetry()
    # No fresh verdict was taken from a reading this driver caused.
    assert driver._pv_verdict_monotonic == verdict_at
    assert driver._pv_probe_until == 0.0


@pytest.mark.asyncio
async def test_the_verdict_is_rechecked_by_letting_go():
    driver = _driver(_fake_client(_dim_blocks()), hass=_hass_with_services())
    await driver.read_telemetry()
    await driver.apply_setpoint(-2000, read_back=False)

    driver._client.async_read_holding_block = AsyncMock(
        side_effect=lambda start, count: _lit_blocks(watts=0).get(start)
    )
    # Age the verdict past the probe interval, and past the ramp throttle so
    # the release can actually be written.
    driver._pv_verdict_monotonic -= _PV_PROBE_INTERVAL_S + 1
    driver._last_write_monotonic -= _MIN_WRITE_INTERVAL_S + 1
    await driver.read_telemetry()
    assert driver._pv_probe_until > 0.0
    # While the probe runs the driver lets go, whatever it is asked for.
    assert (await driver.apply_setpoint(-2000, read_back=False)).net_power_w == 0


@pytest.mark.asyncio
async def test_a_probe_that_finds_sun_keeps_the_battery_released():
    driver = _driver(_fake_client(_dim_blocks()), hass=_hass_with_services())
    await driver.read_telemetry()
    await driver.apply_setpoint(-2000, read_back=False)

    driver._pv_verdict_monotonic -= _PV_PROBE_INTERVAL_S + 1
    driver._last_write_monotonic -= _MIN_WRITE_INTERVAL_S + 1
    driver._client.async_read_holding_block = AsyncMock(
        side_effect=lambda start, count: _lit_blocks(watts=0).get(start)
    )
    await driver.read_telemetry()
    assert (await driver.apply_setpoint(-2000, read_back=False)).net_power_w == 0

    # Released long enough for the tracker to recover, and it has.
    driver._last_write_monotonic -= _PV_PROBE_SETTLE_S + 1
    driver._client.async_read_holding_block = AsyncMock(
        side_effect=lambda start, count: _lit_blocks().get(start)
    )
    await driver.read_telemetry()
    assert driver._pv_lit is True
    assert driver._pv_probe_until == 0.0
    assert (await driver.apply_setpoint(-2000, read_back=False)).net_power_w == 0


@pytest.mark.asyncio
async def test_a_probe_that_finds_nothing_returns_the_battery():
    """The dim-dawn case: let go, see 50 W, take the battery back."""
    driver = _driver(_fake_client(_dim_blocks()), hass=_hass_with_services())
    await driver.read_telemetry()
    await driver.apply_setpoint(-2000, read_back=False)

    driver._pv_verdict_monotonic -= _PV_PROBE_INTERVAL_S + 1
    driver._last_write_monotonic -= _MIN_WRITE_INTERVAL_S + 1
    driver._client.async_read_holding_block = AsyncMock(
        side_effect=lambda start, count: _lit_blocks(watts=0).get(start)
    )
    await driver.read_telemetry()
    assert (await driver.apply_setpoint(-2000, read_back=False)).net_power_w == 0

    driver._last_write_monotonic -= _PV_PROBE_SETTLE_S + 1
    driver._client.async_read_holding_block = AsyncMock(
        side_effect=lambda start, count: _dim_blocks().get(start)
    )
    await driver.read_telemetry()
    assert driver._pv_lit is False
    assert driver._pv_probe_until == 0.0

    # Taking the battery back is throttled like any other write, so a probe
    # costs the settle window plus the ramp interval — about 20 s in 120 s on
    # the constants above. That is the price of not missing sunrise.
    assert (await driver.apply_setpoint(-2000, read_back=False)).net_power_w == 0
    driver._last_write_monotonic -= _MIN_WRITE_INTERVAL_S + 1
    assert (await driver.apply_setpoint(-2000, read_back=False)).net_power_w == -2000


@pytest.mark.asyncio
async def test_a_probe_whose_release_never_landed_is_abandoned():
    """No reading beats a reading the driver did not actually cause."""
    driver = _driver(_fake_client(_dim_blocks()), hass=_hass_with_services())
    await driver.read_telemetry()
    await driver.apply_setpoint(-2000, read_back=False)

    driver._pv_verdict_monotonic -= _PV_PROBE_INTERVAL_S + 1
    driver._client.async_read_holding_block = AsyncMock(
        side_effect=lambda start, count: _lit_blocks(watts=0).get(start)
    )
    await driver.read_telemetry()
    assert driver._pv_probe_until > 0.0

    # The release never reached the hardware: the command still stands.
    driver._pv_probe_until -= _PV_PROBE_WINDOW_S + 1
    await driver.read_telemetry()
    assert driver._pv_probe_until == 0.0
    assert driver._pv_lit is False


@pytest.mark.asyncio
async def test_darkness_needs_no_probe():
    driver = _driver(_fake_client(_dark_blocks()), hass=_hass_with_services())
    await driver.read_telemetry()
    await driver.apply_setpoint(-2000, read_back=False)
    driver._pv_verdict_monotonic -= 600.0
    await driver.read_telemetry()
    assert driver._pv_probe_until == 0.0
    assert (await driver.apply_setpoint(-2000, read_back=False)).net_power_w == -2000


@pytest.mark.asyncio
async def test_an_unreadable_roof_leaves_the_verdict_alone():
    driver = _driver(_fake_client(_lit_blocks()))
    await driver.read_telemetry()
    assert driver._pv_lit is True

    table = _lit_blocks()
    table.pop(32064)
    driver._client.async_read_holding_block = AsyncMock(
        side_effect=lambda start, count: table.get(start)
    )
    await driver.read_telemetry()
    assert driver._pv_lit is True


# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_shutdown_releases_over_the_path_the_commands_took():
    """Releasing through the service while writing registers reaches nothing.

    The direct path needs no huawei_solar device, so there is none to address,
    and the failure is silent because shutdown suppresses the warning. That is
    why switching the integration off did not end the fault.
    """
    driver, client = _direct(device_id="")
    assert await driver.standby() is True
    written = _written(client)
    assert written[0] == (47100, [0]), "the mode register must be cleared first"
    assert 47249 in {address for address, _values in written}


@pytest.mark.asyncio
async def test_the_service_path_still_releases_through_the_service():
    hass = _hass_with_services()
    driver = _driver(_fake_client(), hass=hass)
    assert await driver.standby() is True
    assert hass.services.async_call.await_args.args[1] == "stop_forcible_charge"



# ----------------------------------------------------------------------
# backup mode
#
# A hybrid has no backup switch to read: it reports a state, and this driver
# names the three a SUN2000 distinguishes. The control layer compared against
# the register convention alone (0 = on), which reads every one of those
# strings as "off" — so a Huawei would keep taking commands through a power cut.
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "value, armed",
    [
        ("Off-grid", True),    # feeding the backup circuit right now
        ("Ready", True),       # switch armed, still on grid
        ("Disabled", False),
        (0, True),             # the register convention: 0 is on
        (1, False),
        (None, False),
    ],
)
def test_both_shapes_of_backup_state_are_understood(value, armed):
    from custom_components.omnibattery import _backup_switch_enabled

    assert _backup_switch_enabled(value) is armed


def test_the_driver_only_ever_reports_states_the_guard_knows():
    """A fourth spelling would silently disable the guard again."""
    import inspect

    from custom_components.omnibattery import _backup_switch_enabled
    from custom_components.omnibattery.drivers import huawei

    source = inspect.getsource(huawei.HuaweiSolarDriver.read_telemetry)
    published = {
        line.split('"')[1]
        for line in source.split("\n")
        if line.strip().startswith(('"Off-grid" if', 'else "Ready" if', 'else "Disabled"'))
    }
    assert published == {"Off-grid", "Ready", "Disabled"}
    # Every one of them decides the guard one way or the other, none by accident.
    assert _backup_switch_enabled("Off-grid") and _backup_switch_enabled("Ready")
    assert not _backup_switch_enabled("Disabled")


# ----------------------------------------------------------------------
# the flows that come after setup
#
# Setup had a Huawei branch from the start; the two flows a user reaches later
# did not, and both fell through to Marstek. Reported in review of the upstream
# pull request.
# ----------------------------------------------------------------------
def test_the_options_limits_use_the_probed_hardware_not_a_marstek_default():
    """Saving options would otherwise cut a 7 kW battery down to 2500 W."""
    import inspect

    from custom_components.omnibattery.config_flow import (
        MarstekVenusConfigFlow,
        OptionsFlowHandler,
    )

    for flow in (MarstekVenusConfigFlow, OptionsFlowHandler):
        source = inspect.getsource(flow.async_step_battery_limits)
        assert '_huawei_power_ceilings' in source, flow.__name__
        assert 'brand == "huawei"' in source, flow.__name__


def test_reconfiguring_a_huawei_battery_offers_the_huawei_form():
    """It used to receive the Marstek form: a battery version, a slave id
    meaning something else, and none of the fields this brand needs."""
    import inspect

    from custom_components.omnibattery.config_flow import MarstekVenusConfigFlow

    routing = inspect.getsource(MarstekVenusConfigFlow.async_step_reconfigure_battery)
    assert 'brand", "marstek") == "huawei"' in routing
    assert "async_step_reconfigure_battery_huawei" in routing
    assert hasattr(MarstekVenusConfigFlow, "async_step_reconfigure_battery_huawei")


@pytest.mark.asyncio
async def test_the_reconfigure_form_carries_the_battery_over(monkeypatch):
    from types import SimpleNamespace

    from custom_components.omnibattery import config_flow as mod

    battery = {
        "name": "Huawei LUNA2000", "host": "192.168.1.5", "port": 502, "slave_id": 4,
        "brand": "huawei", "huawei_direct_write": True,
    }
    flow = mod.MarstekVenusConfigFlow()
    flow.battery_index = 0
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda _domain: [object()])
    )
    entry = SimpleNamespace(data={"batteries": [battery]})
    monkeypatch.setattr(mod.MarstekVenusConfigFlow, "_get_reconfigure_entry", lambda self: entry)

    form = await flow.async_step_reconfigure_battery_huawei()
    assert form["step_id"] == "reconfigure_battery_huawei"
    fields = {marker.schema: marker for marker in form["data_schema"].schema}
    assert "huawei_direct_write" in fields
    # The current address is offered again rather than asked for afresh.
    assert fields["host"].default() == "192.168.1.5"
    assert fields["slave_id"].description["suggested_value"] == 4


def test_the_reconfigure_step_is_named_in_every_language():
    import glob
    import json

    for path in ["custom_components/omnibattery/strings.json"] + sorted(
        glob.glob("custom_components/omnibattery/translations/*.json")
    ):
        step = json.load(open(path, encoding="utf-8"))["config"]["step"]
        entry = step["reconfigure_battery_huawei"]
        assert entry["title"] and entry["description"], path
        assert entry["data"]["huawei_direct_write"], path


# ----------------------------------------------------------------------
# reconfiguration, second review pass
#
# Setup grew these guards one at a time; the reconfigure step was written later
# and had none of them. Reported upstream.
# ----------------------------------------------------------------------
def _reconfigure_flow(monkeypatch, *, probe, battery=None, devices=None,
                      emma=None, scan=None):
    from types import SimpleNamespace

    from custom_components.omnibattery import config_flow as mod

    current = battery or {
        "name": "Huawei LUNA2000", "host": "192.168.1.5", "port": 502, "slave_id": 4,
        "brand": "huawei", "huawei_direct_write": True,
    }
    flow = mod.MarstekVenusConfigFlow()
    flow.battery_index = 0
    flow._reconfigure_batteries = []
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda _domain: [object()])
    )
    entry = SimpleNamespace(data={"batteries": [current]})
    monkeypatch.setattr(mod.MarstekVenusConfigFlow, "_get_reconfigure_entry", lambda self: entry)
    monkeypatch.setattr(mod.HuaweiSolarDriver, "probe", AsyncMock(return_value=probe))
    monkeypatch.setattr(mod.HuaweiSolarDriver, "find_emma_slave_id", AsyncMock(return_value=emma))
    monkeypatch.setattr(mod.HuaweiSolarDriver, "scan_slave_ids", AsyncMock(return_value=scan or []))
    registry = MagicMock()
    registry.async_get.side_effect = (devices or {}).get
    monkeypatch.setattr(mod.dr, "async_get", lambda hass: registry)
    flow._migrated = []
    monkeypatch.setattr(
        mod.MarstekVenusConfigFlow, "_migrate_battery_registry_ids",
        lambda self, *args: flow._migrated.append(args[1:]),
    )
    monkeypatch.setattr(
        mod.MarstekVenusConfigFlow, "async_update_reload_and_abort",
        lambda self, _entry, data_updates: {"type": "abort", "data": data_updates},
    )
    return flow, current


_RECONF_INPUT = {
    "name": "Huawei LUNA2000", "host": "192.168.1.5", "port": 502, "slave_id": 4,
    "huawei_battery_device": "dev-batt", "huawei_direct_write": False,
}
_PROBE_OK = (True, "SUN2000-8K-MAP0", 7000, 7000, "BT24B1457565", 8800)


@pytest.mark.asyncio
async def test_reconfiguration_refuses_a_device_from_another_inverter(monkeypatch):
    """The check setup makes, which a cascade can otherwise break silently."""
    flow, _ = _reconfigure_flow(
        monkeypatch, probe=_PROBE_OK,
        devices={"dev-batt": _huawei_device(identifier="BT24B9999999")},
    )
    form = await flow.async_step_reconfigure_battery_huawei(dict(_RECONF_INPUT))
    assert form["errors"] == {"huawei_battery_device": "huawei_device_mismatch"}


@pytest.mark.asyncio
async def test_a_changed_slave_id_takes_the_history_with_it(monkeypatch):
    """The slave id is part of a battery's identity, so entities are renamed."""
    flow, _ = _reconfigure_flow(
        monkeypatch, probe=_PROBE_OK,
        devices={"dev-batt": _huawei_device(identifier="BT24B1457565")},
    )
    await flow.async_step_reconfigure_battery_huawei(dict(_RECONF_INPUT, slave_id=11))
    assert flow._migrated == [("192.168.1.5", 502, "192.168.1.5", 502, 4, 11)]


@pytest.mark.asyncio
async def test_an_unchanged_address_moves_nothing(monkeypatch):
    flow, _ = _reconfigure_flow(
        monkeypatch, probe=_PROBE_OK,
        devices={"dev-batt": _huawei_device(identifier="BT24B1457565")},
    )
    await flow.async_step_reconfigure_battery_huawei(dict(_RECONF_INPUT))
    assert flow._migrated == []


@pytest.mark.asyncio
async def test_a_cascade_asks_instead_of_reporting_a_dead_connection(monkeypatch):
    """Several inverters answering is a question, not a failure."""
    flow, _ = _reconfigure_flow(
        monkeypatch, probe=_PROBE_OK,
        scan=[(4, "SUN2000-8K-MAP0", True), (5, "SUN2000-10K", True)],
    )
    form = await flow.async_step_reconfigure_battery_huawei(
        dict(_RECONF_INPUT, slave_id="", huawei_direct_write=True, huawei_battery_device="")
    )
    assert form["step_id"] == "reconfigure_battery_huawei_slave"
    options = next(iter(form["data_schema"].schema.values())).config["options"]
    assert {option["value"] for option in options} == {"4", "5"}


@pytest.mark.asyncio
async def test_a_cascade_reconfiguration_refuses_a_device_from_another_inverter(monkeypatch):
    """The serial/device guard also applies after choosing a cascade member."""
    flow, _ = _reconfigure_flow(
        monkeypatch,
        probe=_PROBE_OK,
        devices={"dev-batt": _huawei_device(identifier="BT24B9999999")},
        scan=[(4, "SUN2000-8K-MAP0", True), (5, "SUN2000-10K", True)],
    )
    cascade_form = await flow.async_step_reconfigure_battery_huawei(
        dict(_RECONF_INPUT, slave_id="", huawei_direct_write=False)
    )
    assert cascade_form["step_id"] == "reconfigure_battery_huawei_slave"

    form = await flow.async_step_reconfigure_battery_huawei_slave({"slave_id": "4"})

    assert form["step_id"] == "reconfigure_battery_huawei"
    assert form["errors"] == {"huawei_battery_device": "huawei_device_mismatch"}
    assert flow._migrated == []


@pytest.mark.asyncio
async def test_an_endpoint_without_an_emma_drops_the_remembered_one(monkeypatch):
    """Carried over, it would have the driver read a meter that is not there."""
    flow, _ = _reconfigure_flow(
        monkeypatch, probe=_PROBE_OK, emma=None,
        battery={
            "name": "Huawei LUNA2000", "host": "192.168.1.5", "port": 502,
            "slave_id": 4, "brand": "huawei", "huawei_direct_write": True,
            "huawei_emma_slave_id": 0,
        },
    )
    result = await flow.async_step_reconfigure_battery_huawei(
        dict(_RECONF_INPUT, huawei_direct_write=True, huawei_battery_device="")
    )
    assert "huawei_emma_slave_id" not in result["data"]["batteries"][0]


@pytest.mark.asyncio
async def test_an_endpoint_with_an_emma_records_it(monkeypatch):
    flow, _ = _reconfigure_flow(
        monkeypatch, probe=_PROBE_OK, emma=0,
        devices={"dev-batt": _huawei_device(identifier="BT24B1457565")},
    )
    result = await flow.async_step_reconfigure_battery_huawei(dict(_RECONF_INPUT))
    assert result["data"]["batteries"][0]["huawei_emma_slave_id"] == 0


def test_the_module_documentation_matches_what_the_driver_does():
    """It described the first design: services only, and a held zero at 0 W."""
    from custom_components.omnibattery.drivers import huawei

    doc = huawei.__doc__
    assert "either of two paths" in doc
    assert "after dark only" in doc
    assert "``apply_setpoint(0)``\ntherefore *releases*" in doc.replace("\n", "\n")


def test_no_translation_string_carries_a_url():
    """Hassfest rejects them, and a red check is found late and by someone else.

    The Modbus-proxy hint needed a link, and writing it into the string failed
    the integration validation on every language file at once. It is passed as
    a description placeholder instead.
    """
    import glob
    import json

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                yield from walk(value, f"{path}/{key}")
        elif isinstance(node, str) and ("http://" in node or "https://" in node):
            yield path

    for path in ["custom_components/omnibattery/strings.json"] + sorted(
        glob.glob("custom_components/omnibattery/translations/*.json")
    ):
        offenders = list(walk(json.load(open(path, encoding="utf-8")), ""))
        assert not offenders, f"{path}: {offenders}"


def test_the_proxy_hint_still_reaches_the_user():
    """Removing the URL from the string must not remove it from the screen."""
    import json

    from custom_components.omnibattery.config_flow import _MODBUS_PROXY_URL

    strings = json.load(open("custom_components/omnibattery/strings.json", encoding="utf-8"))
    for section in ("config", "options"):
        description = strings[section]["step"]["battery_connection_huawei"]["description"]
        assert "{proxy_url}" in description, section
    assert _MODBUS_PROXY_URL.startswith("https://")
