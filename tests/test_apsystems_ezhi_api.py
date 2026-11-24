"""Tests for the EZHI API client: sign convention, clamping, parsing, errors.

Runs inside the Omnibattery repo (pytest) and also standalone: the client
only needs a session object with an async-context-manager `.get()`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types

import pytest

try:  # inside the repo
    from custom_components.omnibattery.drivers import apsystems_ezhi_api as api
except ImportError:  # standalone
    if "aiohttp" not in sys.modules:
        try:
            import aiohttp  # noqa: F401
        except ImportError:
            stub = types.ModuleType("aiohttp")
            stub.ClientTimeout = lambda total=None: ("timeout", total)
            sys.modules["aiohttp"] = stub
    path = pathlib.Path(__file__).parents[1] / "drivers" / "apsystems_ezhi_api.py"
    spec = importlib.util.spec_from_file_location("apsystems_ezhi_api", path)
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload, self.status = payload, status

    async def json(self, content_type=None):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected {url}")


def run(coro):
    return asyncio.run(coro)


def ok(data):
    return FakeResp({"data": data, "message": "SUCCESS"})


# ----------------------------------------------------------------- sign/clamp
def test_setpoint_charge_is_negative_on_the_wire():
    # contract +500 = charge -> EZHI wants -500
    assert api.contract_to_setpoint(500) == -500


def test_setpoint_discharge_is_positive_on_the_wire():
    assert api.contract_to_setpoint(-300) == 300


def test_setpoint_clamped_and_rounded():
    assert api.contract_to_setpoint(5000) == -1200
    assert api.contract_to_setpoint(-5000) == 1200
    assert api.contract_to_setpoint(0.4) == 0
    assert isinstance(api.contract_to_setpoint(10.6), int)


def test_confirmed_hardware_conventions():
    """Observed on a real EZHI: telemetry is positive while charging (already the
    Omnibattery convention); /setPower is negative to charge (inverted)."""
    assert api.TELEMETRY_POSITIVE_IS_DISCHARGE is False
    assert api.SETPOINT_POSITIVE_IS_DISCHARGE is True
    assert api.telemetry_to_contract(700) == 700      # charging stays positive
    assert api.telemetry_to_contract(-700) == -700    # discharging stays negative
    assert api.contract_to_setpoint(700) == -700      # charge 700 W -> p=-700
    assert api.contract_to_setpoint(-700) == 700      # discharge 700 W -> p=700


# ------------------------------------------------------------------- parsing
def test_device_info():
    s = FakeSession({"/getDeviceInfo": ok(
        {"deviceId": "E1234", "devVer": "1.0.3", "batteryCapacity": "5.12"})})
    info = run(api.EzhiApiClient(s, "10.0.0.5").get_device_info())
    assert info == {"device_id": "E1234", "firmware": "1.0.3", "capacity_kwh": 5.12}


def test_output_data_converts_sign_and_types():
    s = FakeSession({"/getOutputData": ok({
        "batSoc": "64", "batP": 700, "ogP": 690, "pvP": "120.5",
        "batTemp": 24, "devTemp": 41, "batCTE": 100.5, "batDTE": 90})})
    t = run(api.EzhiApiClient(s, "h").get_output_data())
    assert t["soc"] == 64.0
    assert t["solar_power_w"] == 120.5
    assert t["charged_kwh"] == 100.5 and t["discharged_kwh"] == 90.0
    cell = -1 if api.TELEMETRY_POSITIVE_IS_DISCHARGE else 1
    port = -1 if api.AC_PORT_POSITIVE_IS_DISCHARGE else 1
    assert t["battery_power_w"] == cell * 700
    assert t["ac_power_w"] == port * 690


def test_output_data_missing_fields_are_none():
    s = FakeSession({"/getOutputData": ok({"batSoc": 10})})
    t = run(api.EzhiApiClient(s, "h").get_output_data())
    assert t["soc"] == 10.0
    assert t["battery_power_w"] is None and t["solar_power_w"] is None


def test_unwrapped_payload_is_accepted():
    s = FakeSession({"/getDeviceInfo": FakeResp({"deviceId": "X"})})
    assert run(api.EzhiApiClient(s, "h").get_device_info())["device_id"] == "X"


def test_set_power_sends_inverted_value_and_returns_it():
    s = FakeSession({"/setPower": ok({"p": -500})})
    sent = run(api.EzhiApiClient(s, "h").set_power(500))
    assert sent == -500
    assert s.calls[-1][1] == {"p": -500}


# ------------------------------------------------------------ AC port, /getPower
def test_ac_port_already_uses_the_contract_sign():
    """Official EZHI manual: ogP negative = output power (delivered to the grid), so a
    500 W discharge command reads as -500 W with no inversion."""
    assert api.AC_PORT_POSITIVE_IS_DISCHARGE is False
    assert api.ac_to_contract(-500) == -500
    assert api.ac_to_contract(300) == 300


def test_setpoint_to_contract_is_inverse_of_contract_to_setpoint():
    for watts in (-1200, -500, 0, 1, 500, 1200):
        assert api.setpoint_to_contract(api.contract_to_setpoint(watts)) == watts


def test_get_power_known_field():
    s = FakeSession({"/getPower": ok({"power": "500"})})
    r = run(api.EzhiApiClient(s, "h").get_power())
    assert r == {"wire_w": 500, "setpoint_w": -500}


def test_get_power_single_numeric_field_fallback():
    s = FakeSession({"/getPower": ok({"limit_w": 300, "note": "x"})})
    r = run(api.EzhiApiClient(s, "h").get_power())
    assert r["wire_w"] == 300


def test_get_power_unrecognised_answer_is_none_not_error():
    s = FakeSession({"/getPower": ok({"a": "x", "b": "y"})})
    r = run(api.EzhiApiClient(s, "h").get_power())
    assert r == {"wire_w": None, "setpoint_w": None}


def test_get_power_transport_error_raises():
    s = FakeSession({"/getPower": FakeResp({}, status=500)})
    with pytest.raises(api.EzhiError):
        run(api.EzhiApiClient(s, "h").get_power())


def test_real_capture_parses():
    real = {"batSoc": "29.0", "batSoh": "100", "batTemp": "3.2", "devTemp": "10.2", "pvP": "111", "batP": "111",
            "batCTE": "4.6421", "batDTE": "3.8480", "ogP": "0"}
    s = FakeSession({"/getOutputData": FakeResp({"data": real, "batS": "2", "deviceId": "D", "message": "SUCCESS"})})
    t = run(api.EzhiApiClient(s, "h").get_output_data())
    assert t["battery_power_w"] == 111 and t["ac_power_w"] == 0 and t["solar_power_w"] == 111
    assert t["soh"] == 100.0 and t["battery_temp"] == 3.2 and t["charged_kwh"] == 4.6421 and t["discharged_kwh"] == 3.848


# -------------------------------------------------------------------- errors
@pytest.mark.parametrize("resp", [
    FakeResp({}, status=500),
    FakeResp({"message": "FAILED"}),
    FakeResp(ValueError("not json")),
    FakeResp([1, 2, 3]),
    ConnectionError("down"),
])
def test_errors_become_ezhi_error(resp):
    s = FakeSession({"/getDeviceInfo": resp})
    with pytest.raises(api.EzhiError):
        run(api.EzhiApiClient(s, "h").get_device_info())
