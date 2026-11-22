"""Driver tests for the APsystems EZHI. Run inside the Omnibattery repo.

Uses a fake aiohttp session, so no network and no Home Assistant fixtures.
"""

from __future__ import annotations

import pytest

from custom_components.omnibattery.drivers import apsystems_ezhi as mod
from custom_components.omnibattery.drivers.base import DriverContext

Driver = mod.ApsystemsEzhiDriver

INFO = {"deviceId": "E1234", "devVer": "1.0.3", "batteryCapacity": "5.12"}
# Confirmed on hardware: the EZHI reports positive battery power while charging.
OUT = {"batSoc": "64", "batP": 700, "ogP": 690, "pvP": "120.5", "batTemp": 24, "devTemp": 41}


class FakeResp:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    async def json(self, content_type=None):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def ok(data):
    return FakeResp({"data": data, "message": "SUCCESS"})


class FakeSession:
    closed = False

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(url)

    async def close(self):
        self.closed = True


def make(routes=None, **kw):
    session = FakeSession(routes or {
        "/getDeviceInfo": ok(INFO), "/getOutputData": ok(OUT), "/setPower": ok({})})
    return Driver("10.0.0.5", session=session, **kw), session


def setpoint_calls(session):
    return [c[1] for c in session.calls if c[0].endswith("/setPower")]


def test_from_config_clamps_to_device_envelope():
    ctx = DriverContext(None, "EZHI", "k", 800, 3000, lambda k, v: None, lambda k: None)
    caps = Driver.from_config({"host": "10.0.0.5", "port": 80}, ctx).capabilities
    assert (caps.max_charge_power_w, caps.max_discharge_power_w) == (800, 1200)


def test_identity_and_traits():
    assert Driver.BRAND == "apsystems_ezhi"
    assert Driver.TRAITS.ip_addressed and Driver.TRAITS.fixed_power_envelope
    assert not Driver.TRAITS.supports_voltage_taper


@pytest.mark.asyncio
async def test_connect_and_failure():
    d, _ = make()
    assert await d.connect() is True and d.connected
    d2, _ = make({"/getDeviceInfo": ConnectionError("down")})
    assert await d2.connect() is False and not d2.connected


@pytest.mark.asyncio
async def test_telemetry_contract_sign_and_keys():
    d, _ = make()
    await d.connect()
    t = await d.read_telemetry()
    assert t["battery_soc"] == 64.0
    assert t["battery_power"] == 700.0          # charging is positive in Omnibattery
    assert t["solar_power"] == 120.5 and "pv_power" not in t
    assert t["software_version"] == "1.0.3"
    assert set(await d.read_telemetry(["battery_soc"])) == {"battery_soc"}


@pytest.mark.asyncio
async def test_setpoint_sign_clamp_and_echo():
    d, s = make()
    await d.connect()
    r = await d.apply_setpoint(500)             # charge 500 W
    assert r.ok and r.net_power_w == 500
    assert setpoint_calls(s)[-1] == {"p": -500}  # device: negative = charge
    assert d.net_power_from_data(r.applied) == 500
    r = await d.apply_setpoint(-5000)
    assert r.net_power_w == -1200 and setpoint_calls(s)[-1] == {"p": 1200}


@pytest.mark.asyncio
async def test_setpoint_failure_and_standby():
    d, s = make()
    await d.connect()
    assert await d.standby() is True and setpoint_calls(s)[-1] == {"p": 0}
    s.routes["/setPower"] = ConnectionError("x")
    r = await d.apply_setpoint(100)
    assert not r.ok and r.failure_reason == "setpoint_write_failed"


def test_read_groups_match_sensor_definitions():
    d, _ = make()
    assert {k for g in d.read_groups for k in g.keys} == {x["key"] for x in d.sensor_definitions}
