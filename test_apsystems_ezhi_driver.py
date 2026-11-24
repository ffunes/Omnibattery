"""Driver tests for the APsystems EZHI. Run inside the Omnibattery repo.

Fake aiohttp session: no network and no Home Assistant fixtures. The scenario
is the one from the review: PV 900 W, discharge command 500 W, AC port delivers
500 W, the cell is charging 400 W.
"""

from __future__ import annotations

import pytest

from custom_components.omnibattery.drivers import apsystems_ezhi as mod
from custom_components.omnibattery.drivers.base import DriverContext

Driver = mod.ApsystemsEzhiDriver
AC_KEY = mod.DELIVERED_AC_POWER_KEY

INFO = {"deviceId": "E1234", "devVer": "1.0.3", "batteryCapacity": "5.12"}
# device convention (official manual): batP positive = charging; ogP negative = output
OUT = {"batSoc": "64", "batP": 400, "ogP": -500, "pvP": "900", "batTemp": 24, "devTemp": 41}


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


def routes(**over):
    r = {"/getDeviceInfo": ok(INFO), "/getOutputData": ok(OUT),
         "/setPower": ok({}), "/getPower": ok({"power": "500"})}
    r.update(over)
    return r


def make(rt=None):
    session = FakeSession(rt if rt is not None else routes())
    return Driver("10.0.0.5", session=session), session


def wire(session):
    return [c[1] for c in session.calls if c[0].endswith("/setPower")]


def outage(session):
    for key in list(session.routes):
        session.routes[key] = ConnectionError("down")


def test_identity_and_envelope():
    assert Driver.BRAND == "apsystems_ezhi"
    assert Driver.TRAITS.ip_addressed and Driver.TRAITS.fixed_power_envelope
    ctx = DriverContext(None, "EZHI", "k", 800, 3000, lambda k, v: None, lambda k: None)
    caps = Driver.from_config({"host": "10.0.0.5", "port": 80}, ctx).capabilities
    assert (caps.max_charge_power_w, caps.max_discharge_power_w) == (800, 1200)


@pytest.mark.asyncio
async def test_connect_and_failure():
    d, _ = make()
    assert await d.connect() is True and d.connected
    d2, _ = make(routes(**{"/getDeviceInfo": ConnectionError("down")}))
    assert await d2.connect() is False


# review 1: AC port power is published and used as delivered power
@pytest.mark.asyncio
async def test_ac_port_power_published_and_in_control_keys():
    d, _ = make()
    await d.connect()
    t = await d.read_telemetry()
    assert t["battery_power"] == 400.0      # cell: charging
    assert t[AC_KEY] == -500.0              # port: delivering 500 W = discharge
    assert t["solar_power"] == 900.0
    assert {"power_setpoint", AC_KEY, "solar_power"} <= set(d.control_dependency_keys)


@pytest.mark.asyncio
async def test_setpoint_result_reports_ac_port_as_delivered():
    d, _ = make()
    await d.connect()
    r = await d.apply_setpoint(-500)
    assert r.battery_power_w == -500
    assert r.applied[AC_KEY] == -500 and r.applied["battery_power"] == 400


# review 2: an unreachable device must not look alive
@pytest.mark.asyncio
async def test_outage_returns_empty_for_every_group():
    d, s = make()
    await d.connect()
    await d.apply_setpoint(-500)
    outage(s)
    for _ in range(12):
        assert await d.read_telemetry() == {}
        for group in d.read_groups:
            assert await d.read_telemetry(list(group.keys)) == {}
        assert await d.read_telemetry(["power_setpoint"]) == {}
        assert await d.read_telemetry(["software_version"]) == {}


@pytest.mark.asyncio
async def test_remembered_only_groups_still_hit_the_device():
    d, s = make()
    await d.connect()
    n = len(s.calls)
    await d.read_telemetry(["software_version"])
    assert len(s.calls) == n + 1 and s.calls[-1][0].endswith("/getDeviceInfo")
    await d.read_telemetry(["power_setpoint"])
    assert s.calls[-1][0].endswith("/getPower")


# review 3: solar is read by default
def test_solar_enabled_by_default():
    d, _ = make()
    solar = next(x for x in d.sensor_definitions if x["key"] == "solar_power")
    assert solar["enabled_by_default"] is True and d.capabilities.has_solar_telemetry


# review 4: confirm against /getPower
@pytest.mark.asyncio
async def test_confirmed_when_getpower_echoes_command():
    d, s = make()
    await d.connect()
    r = await d.apply_setpoint(-500)                  # device wire value +500
    assert wire(s)[-1] == {"p": 500}
    assert r.ok and r.confirmed and r.exact and r.applied["power_setpoint"] == -500
    assert d.net_power_from_data(r.applied) == -500
    assert await d.read_telemetry(["power_setpoint"]) == {"power_setpoint": -500}


@pytest.mark.asyncio
@pytest.mark.parametrize("getpower", [
    ok({"power": "0"}),                 # device reports another value
    ok({"a": "x", "b": "y"}),              # no recognisable field
    ConnectionError("down"),               # request fails
])
async def test_unconfirmed_otherwise(getpower):
    d, _ = make(routes(**{"/getPower": getpower}))
    await d.connect()
    r = await d.apply_setpoint(-500)
    assert r.ok and not r.confirmed


# regression
@pytest.mark.asyncio
async def test_setpoint_clamp_wire_sign_standby_and_failure():
    d, s = make()
    await d.connect()
    await d.apply_setpoint(500)
    assert wire(s)[-1] == {"p": -500}                 # device: negative = charge
    assert (await d.apply_setpoint(-5000)).net_power_w == -1200
    assert await d.standby() is True and wire(s)[-1] == {"p": 0}
    s.routes["/setPower"] = ConnectionError("x")
    r = await d.apply_setpoint(100)
    assert not r.ok and r.failure_reason == "setpoint_write_failed"


def test_read_groups_match_sensor_definitions():
    d, _ = make()
    assert {k for g in d.read_groups for k in g.keys} == {x["key"] for x in d.sensor_definitions}


@pytest.mark.asyncio
async def test_temperatures_are_enabled_and_polled_with_power():
    d, _ = make()
    await d.connect()
    t = await d.read_telemetry(["internal_temperature", "device_temperature"])
    assert t == {"internal_temperature": 24.0, "device_temperature": 41.0}
    defs = {x["key"]: x for x in d.sensor_definitions}
    assert defs["internal_temperature"]["enabled_by_default"] is True
    assert defs["device_temperature"]["enabled_by_default"] is False
    high = next(g for g in d.read_groups if g.scan_interval == "high")
    assert {"internal_temperature", "device_temperature"} <= set(high.keys)


REAL = {"batSoc": "29.0", "batSoh": "100", "batTemp": "3.2", "devTemp": "10.2", "pvP": "111", "pvTE": "5.2850",
        "batP": "111", "batCTE": "4.6421", "batDTE": "3.8480", "ogP": "0", "ogOTE": "3.8711", "ogITE": "0.9196",
        "ofgP": "0", "ofgOTE": "0.0196", "ofgITE": "0.0248"}


@pytest.mark.asyncio
async def test_real_capture_pv_charging_the_cell():
    """Captured from a real EZHI: PV 111 W charging the battery, nothing to the grid."""
    payload = FakeResp({"data": REAL, "batS": "2", "deviceId": "D", "message": "SUCCESS"})
    d, _ = make(routes(**{"/getOutputData": payload}))
    await d.connect()
    t = await d.read_telemetry()
    assert t["battery_soc"] == 29.0 and t["battery_soh"] == 100.0 and t["battery_power"] == 111.0 and t["solar_power"] == 111.0
    assert t[AC_KEY] == 0 and t["internal_temperature"] == 3.2 and t["device_temperature"] == 10.2
    assert t["total_charging_energy"] == 4.6421 and t["total_discharging_energy"] == 3.848
    caps = d.capabilities
    assert caps.has_energy_counters and not caps.has_daily_energy_counters and not caps.lifetime_counters_ac
    assert not caps.independent_power_planes


# --- review: real discharge-with-PV capture --------------------------------------
# Values from the real capture posted in the PR (only the fields the test needs).
REAL_DISCHARGE_WITH_PV = {"pvP": "54", "batP": "-144", "ogP": "-198"}


@pytest.mark.asyncio
async def test_real_capture_discharge_with_pv():
    """PV 54 W plus the cell discharging 144 W is 198 W delivered to the grid."""
    payload = FakeResp({"data": REAL_DISCHARGE_WITH_PV, "message": "SUCCESS"})
    d, _ = make(routes(**{"/getOutputData": payload}))
    await d.connect()
    t = await d.read_telemetry()
    assert t["battery_power"] == -144.0     # cell discharging (negative = discharge)
    assert t[AC_KEY] == -198.0              # AC port delivering 198 W
    assert t["solar_power"] == 54.0
    # energy balance of the capture: PV + cell output = port output
    assert t["solar_power"] - t["battery_power"] == -t[AC_KEY]


# --- review: device offline over several polls -----------------------------------
async def _poll(driver) -> bool:
    """One coordinator cycle: read every group. A poll counts as failed when no
    group returned any data; this mirrors the rule the review describes ("any
    non-empty group counts as a good read"). If the repo has a coordinator test
    fixture, drive the real failure counter with it instead of this helper."""
    results = [await driver.read_telemetry(list(g.keys)) for g in driver.read_groups]
    return any(results)


@pytest.mark.asyncio
async def test_device_offline_over_several_polls_raises_failure_counter(monkeypatch):
    monkeypatch.setattr(mod, "_RETRY_DELAY_S", 0)
    d, s = make()
    await d.connect()                       # firmware is now cached
    await d.apply_setpoint(-198)            # a setpoint is now remembered
    assert await _poll(d) is True           # healthy: counter stays at 0

    failures = 0
    outage(s)
    for poll_number in range(1, 7):         # six consecutive polls offline
        assert await d.read_telemetry() == {}
        for group in d.read_groups:
            assert await d.read_telemetry(list(group.keys)) == {}
        if not await _poll(d):
            failures += 1
        assert failures == poll_number      # rises on every poll, never stalls

    s.routes.update(routes())               # device comes back
    assert await _poll(d) is True
