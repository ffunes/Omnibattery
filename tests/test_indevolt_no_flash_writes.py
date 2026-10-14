"""Flash-write regression suite for the Indevolt integration.

Indevolt R&D: only the realtime tuple 47015 is RAM-backed. Every other writable
point (work mode, reserve, limits, grid charging, ...) lives in flash, which
wears out with each write. These tests pin the guards that keep Omnibattery away
from flash. Every assertion carries FLASH_WEAR_MESSAGE.

DO NOT relax these tests without written confirmation from Indevolt.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
from pathlib import Path

import pytest

from custom_components.omnibattery.drivers.indevolt import IndevoltDriver
from custom_components.omnibattery.infra import indevolt_client
from custom_components.omnibattery.infra.indevolt_client import (
    ALLOWED_ENDPOINTS,
    RAM_WRITE_POINTS,
    IndevoltClient,
    IndevoltFlashWriteForbidden,
    IndevoltForbiddenEndpoint,
)
from tests.indevolt_fake import (
    FLASH_WEAR_MESSAGE,
    KNOWN_FLASH_POINTS,
    FakeIndevoltDevice,
    VirtualClock,
    flash_writes,
)

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "omnibattery"
CLIENT_FILE = PACKAGE / "infra" / "indevolt_client.py"


def _client(clock: VirtualClock) -> tuple[IndevoltClient, FakeIndevoltDevice]:
    device = FakeIndevoltDevice(clock)
    return IndevoltClient("192.0.2.10", transport=device, clock=clock, sleep=clock.sleep), device


def test_ram_allowlist_is_exactly_47015():
    assert RAM_WRITE_POINTS == frozenset({47015}), FLASH_WEAR_MESSAGE


def test_endpoint_allowlist_is_exactly_the_three_rpc_calls():
    assert dict(ALLOWED_ENDPOINTS) == {
        "Sys.GetConfig": "GET",
        "Indevolt.GetData": "POST",
        "Indevolt.SetData": "POST",
    }, FLASH_WEAR_MESSAGE


@pytest.mark.parametrize("point", KNOWN_FLASH_POINTS)
async def test_submit_refuses_every_known_flash_point(point):
    clock = VirtualClock()
    client, device = _client(clock)
    with pytest.raises(IndevoltFlashWriteForbidden):
        await client._submit("Indevolt.SetData", {"f": 16, "t": point, "v": [1, 0, 0]})
    assert device.requests == [], FLASH_WEAR_MESSAGE


@pytest.mark.parametrize(
    "config",
    [
        {"f": 16, "t": "47015", "v": [0, 0, 0]},  # string point
        {"f": 16, "t": 47015, "v": [0, 0, 0], "extra": 1},
        {"f": 17, "t": 47015, "v": [0, 0, 0]},
        {"f": 16, "t": 47015, "v": [0, 0]},
        {"f": 16, "t": 47015, "v": 4},
        {"f": 16, "t": [47015, 47005], "v": [0, 0, 0]},
        None,
    ],
)
async def test_submit_refuses_malformed_writes(config):
    clock = VirtualClock()
    client, device = _client(clock)
    with pytest.raises(IndevoltFlashWriteForbidden):
        await client._submit("Indevolt.SetData", config)
    assert device.requests == [], FLASH_WEAR_MESSAGE


@pytest.mark.parametrize(
    "endpoint", ["Indevolt.SetConfig", "Sys.SetConfig", "Sys.Reboot", "Indevolt.SetMode", ""]
)
async def test_submit_refuses_endpoints_outside_the_allowlist(endpoint):
    clock = VirtualClock()
    client, device = _client(clock)
    with pytest.raises(IndevoltForbiddenEndpoint):
        await client._submit(endpoint, {"t": 47005, "v": [1]})
    assert device.requests == [], FLASH_WEAR_MESSAGE


async def test_guard_errors_are_not_transport_errors():
    """A tripped guard must never be swallowed by a transport error handler."""
    assert not issubclass(IndevoltFlashWriteForbidden, indevolt_client.IndevoltError), FLASH_WEAR_MESSAGE
    assert not issubclass(IndevoltForbiddenEndpoint, indevolt_client.IndevoltError), FLASH_WEAR_MESSAGE


async def test_only_the_realtime_tuple_reaches_the_device():
    clock = VirtualClock()
    client, device = _client(clock)
    task = asyncio.ensure_future(client.set_realtime(2, 500, 40))
    await clock.run(1)
    assert await task
    task = asyncio.ensure_future(client.stop())
    await clock.run(6)
    assert await task
    assert device.writes == [(47015, [2, 500, 40]), (47015, [0, 0, 0])], FLASH_WEAR_MESSAGE
    assert flash_writes(device) == [], FLASH_WEAR_MESSAGE


@pytest.mark.parametrize("values", [(4, 0, 0), (1, 20000, 50), (2, 500, 3), (1, -1, 50), (0, 0, 5)])
def test_realtime_values_outside_the_documented_tuple_are_refused(values):
    with pytest.raises(ValueError):
        IndevoltClient.realtime_config(*values)


def _source_files():
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_setdata_outside_the_client():
    """Only indevolt_client.py may name SetData or the realtime write point."""
    offenders = []
    for path in _source_files():
        if path == CLIENT_FILE:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "SetData" in node.value or "set_data" in node.value:
                    offenders.append(f"{path.name}:{node.lineno} {node.value!r}")
            elif isinstance(node, (ast.Name, ast.Attribute)):
                name = node.id if isinstance(node, ast.Name) else node.attr
                if name in ("set_data", "SetData"):
                    offenders.append(f"{path.name}:{node.lineno} {name}")
            elif isinstance(node, ast.Constant) and node.value == 47015:
                offenders.append(f"{path.name}:{node.lineno} 47015")
    assert offenders == [], f"{FLASH_WEAR_MESSAGE}\n{offenders}"


def test_indevolt_files_name_no_writable_point():
    """No Indevolt file outside the client mentions a writable (flash) point."""
    writable_only = set(KNOWN_FLASH_POINTS) - {11010, 2618, 4}  # 11010/2618 are also read
    offenders = []
    for path in _source_files():
        if "indevolt" not in path.name or path == CLIENT_FILE:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and type(node.value) is int and node.value in writable_only:
                offenders.append(f"{path.name}:{node.lineno} {node.value}")
    assert offenders == [], f"{FLASH_WEAR_MESSAGE}\n{offenders}"


def test_client_has_no_generic_write_method():
    public = {name for name, _ in inspect.getmembers(IndevoltClient) if not name.startswith("_")}
    assert public <= {
        "get_config", "get_data", "set_realtime", "stop", "exchange", "close",
        "realtime_config", "slot_s", "closed",
    }, f"{FLASH_WEAR_MESSAGE}\nUnexpected client API: {sorted(public)}"


async def test_driver_offers_no_way_to_change_work_mode():
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, energy_mode=1)
    driver = IndevoltDriver("192.0.2.11", transport=device, clock=clock, sleep=clock.sleep)
    names = {name for name, _ in inspect.getmembers(IndevoltDriver) if not name.startswith("_")}
    mode_writers = {n for n in names if re.search(r"(set|write|change|switch|select).*mode", n)}
    assert not mode_writers, f"{FLASH_WEAR_MESSAGE}\n{mode_writers}"
    for key in ("energy_mode", "work_mode", "working_mode", "47005", "backup_soc", "min_soc",
                "max_soc", "feedin_power_limit", "grid_charging_allowed"):
        assert await driver.write_control(key, 4) is False, FLASH_WEAR_MESSAGE
    assert await driver.apply_config(max_soc_pct=90, min_soc_pct=20, max_charge_power_w=1000,
                                     max_discharge_power_w=1000) is True
    assert await driver.set_charge_cutoff(100) is False
    assert device.requests == [], FLASH_WEAR_MESSAGE
