"""Local HTTP client for the APsystems EZHI hybrid microinverter.

Pure transport + unit/sign conversion, no Home Assistant or Omnibattery
imports, so it can be unit-tested on its own.

Prerequisite on the device: enable "Local Mode" in the AP EasyPower app.
Endpoints (HTTP, port 80):

    GET /getDeviceInfo          deviceId, devVer, batteryCapacity (kWh)
    GET /getOutputData          batSoc, batP, ogP, pvP, batTemp, devTemp,
                                batCTE, batDTE (kWh counters)
    GET /getPower               current on-grid setpoint ("power limit setting")
    GET /setPower?p=<watts>     net power setpoint, -1200..1200 W; only acted on
                                in the Local system mode, answers SUCCESS in all
"""

from __future__ import annotations

from typing import Any

MAX_POWER_W = 1200

# --- SIGN CONVENTIONS ------------------------------------------------------
# Omnibattery driver contract: +W = charging the battery, -W = discharging.
#
# These two flags are the ONLY place the EZHI's own convention is encoded.
# Confirmed on real hardware: the EZHI reports battery power as POSITIVE while
# charging (so telemetry is already in the Omnibattery convention), and /setPower
# treats a negative value as charge (so setpoints are inverted).
TELEMETRY_POSITIVE_IS_DISCHARGE = False
SETPOINT_POSITIVE_IS_DISCHARGE = True
# On-grid AC port (ogP): the EZHI local API manual says "negative numbers indicate
# output power" (power delivered to the grid), and its sample payload shows
# ogP -438 together with batP +136 (charging). So the port already uses the
# Omnibattery convention (negative = discharge/delivering): no inversion.
# Check on hardware: during a discharge command "AC delivered power" is negative.
AC_PORT_POSITIVE_IS_DISCHARGE = False


class EzhiError(Exception):
    """Any failure talking to the EZHI (transport, HTTP status, API error)."""


def telemetry_to_contract(raw_watts: float) -> float:
    """Convert an EZHI telemetry power value to Omnibattery's +charge/-discharge."""
    return -raw_watts if TELEMETRY_POSITIVE_IS_DISCHARGE else raw_watts


def ac_to_contract(raw_watts: float) -> float:
    """Convert the EZHI on-grid port power to +charge/-discharge."""
    return -raw_watts if AC_PORT_POSITIVE_IS_DISCHARGE else raw_watts


def setpoint_to_contract(wire_watts: float) -> float:
    """Inverse of contract_to_setpoint (without clamping)."""
    return -wire_watts if SETPOINT_POSITIVE_IS_DISCHARGE else wire_watts


# The manual's /getPower answer is {"data": {"power": "800"}} (W, the on-grid setting).
# If a firmware names it differently, a single numeric field is used.
_SETPOINT_KEYS = ("power",)


def _extract_setpoint(data: dict[str, Any]) -> float | None:
    for key in _SETPOINT_KEYS:
        if key in data and _num(data[key]) is not None:
            return _num(data[key])
    numeric = [_num(v) for v in data.values() if not isinstance(v, (dict, list, bool))]
    numeric = [v for v in numeric if v is not None]
    return numeric[0] if len(numeric) == 1 else None


def contract_to_setpoint(net_power_w: float) -> int:
    """Convert Omnibattery's +charge/-discharge request to the EZHI /setPower value.

    Clamped to the device's +/-1200 W range and rounded to whole watts.
    """
    watts = -net_power_w if SETPOINT_POSITIVE_IS_DISCHARGE else net_power_w
    return int(round(max(-MAX_POWER_W, min(MAX_POWER_W, watts))))


def _num(value: Any, default: float | None = None) -> float | None:
    """The API returns numbers as numbers or strings; be tolerant."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class EzhiApiClient:
    """Thin async client.

    `session` is an aiohttp.ClientSession, or a zero-argument callable returning
    one (the driver passes its `_ensure_session`, so a recreated session is
    picked up on the next request).
    """

    def __init__(self, session: Any, host: str, port: int = 80, timeout: float = 10.0):
        self._session = session
        self._base = f"http://{host}:{port}"
        self._timeout = timeout

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        import aiohttp  # lazy: keeps this module importable without aiohttp

        url = f"{self._base}{path}"
        session = self._session() if callable(self._session) else self._session
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=self._timeout)
            ) as resp:
                if resp.status != 200:
                    raise EzhiError(f"{path}: HTTP {resp.status}")
                payload = await resp.json(content_type=None)
        except EzhiError:
            raise
        except Exception as err:  # aiohttp.ClientError, TimeoutError, bad JSON
            raise EzhiError(f"{path}: {err}") from err

        if not isinstance(payload, dict):
            raise EzhiError(f"{path}: unexpected payload {type(payload).__name__}")
        message = payload.get("message")
        if message not in (None, "SUCCESS"):
            raise EzhiError(f"{path}: device answered {message!r}")
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise EzhiError(f"{path}: missing data object")
        return data

    async def get_device_info(self) -> dict[str, Any]:
        """Return {'device_id', 'firmware', 'capacity_kwh'} (any may be None)."""
        data = await self._get("/getDeviceInfo")
        return {
            "device_id": data.get("deviceId"),
            "firmware": data.get("devVer"),
            "capacity_kwh": _num(data.get("batteryCapacity")),
        }

    async def get_output_data(self) -> dict[str, Any]:
        """Return live telemetry already converted to the Omnibattery contract.

        Keys: soc (%), soh (% state of health), battery_power_w (cell power, +charge/-discharge),
        ac_power_w (on-grid AC port power, same sign convention; this is what
        /setPower controls), solar_power_w (>=0), battery_temp,
        device_temp, charged_kwh, discharged_kwh. Missing values are None.
        """
        d = await self._get("/getOutputData")
        bat_p = _num(d.get("batP"))
        og_p = _num(d.get("ogP"))
        return {
            "soc": _num(d.get("batSoc")),
            "soh": _num(d.get("batSoh")),
            "battery_power_w": None if bat_p is None else telemetry_to_contract(bat_p),
            "ac_power_w": None if og_p is None else ac_to_contract(og_p),
            "solar_power_w": _num(d.get("pvP")),
            "battery_temp": _num(d.get("batTemp")),
            "device_temp": _num(d.get("devTemp")),
            "charged_kwh": _num(d.get("batCTE")),
            "discharged_kwh": _num(d.get("batDTE")),
        }

    async def get_power(self) -> dict[str, Any]:
        """Read /getPower. Returns {'wire_w', 'setpoint_w'}: the device's own value
        and the same value in the Omnibattery convention; both None when the
        answer has no recognisable setpoint field (transport errors raise)."""
        wire = _extract_setpoint(await self._get("/getPower"))
        return {
            "wire_w": None if wire is None else int(round(wire)),
            "setpoint_w": None if wire is None else int(round(setpoint_to_contract(wire))),
        }

    async def set_power(self, net_power_w: float) -> int:
        """Send a setpoint given in the Omnibattery contract. Returns watts sent."""
        sent = contract_to_setpoint(net_power_w)
        await self._get("/setPower", params={"p": sent})
        return sent
