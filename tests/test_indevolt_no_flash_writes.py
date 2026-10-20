"""Flash-write regression suite for the Indevolt integration.

Indevolt R&D: only the realtime tuple 47015 is RAM-backed. Every other writable
point (work mode, reserve, limits, grid charging, ...) lives in flash, which
wears out with each write. Wear comes from frequency, so there are two channels:
control writes 47015 only, and every periodic or automatic path uses it; the
config channel writes three user settings, only on a direct user action. These
tests pin the guards that keep the two apart. Every assertion carries
FLASH_WEAR_MESSAGE.

DO NOT relax these tests without written confirmation from Indevolt.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
from pathlib import Path

import pytest

from custom_components.omnibattery.drivers.indevolt import ControlState, IndevoltDriver
from custom_components.omnibattery.infra import indevolt_client
from custom_components.omnibattery.infra.indevolt_client import (
    ALLOWED_ENDPOINTS,
    CONFIG_WRITE_POINTS,
    RAM_WRITE_POINTS,
    IndevoltClient,
    IndevoltFlashWriteForbidden,
    IndevoltForbiddenEndpoint,
)
from tests.indevolt_fake import (
    FLASH_WEAR_MESSAGE,
    KNOWN_FLASH_POINTS,
    READ_AND_WRITE_POINTS,
    FakeIndevoltDevice,
    VirtualClock,
    connected_driver,
    flash_writes,
)

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "omnibattery"
CLIENT_FILE = PACKAGE / "infra" / "indevolt_client.py"
SETTINGS_FILE = PACKAGE / "drivers" / "indevolt_settings.py"
DRIVER_FILE = PACKAGE / "drivers" / "indevolt.py"


def _client(clock: VirtualClock) -> tuple[IndevoltClient, FakeIndevoltDevice]:
    device = FakeIndevoltDevice(clock)
    return IndevoltClient("192.0.2.10", transport=device, clock=clock, sleep=clock.sleep), device


def test_ram_allowlist_is_exactly_47015():
    assert RAM_WRITE_POINTS == frozenset({47015}), FLASH_WEAR_MESSAGE


def test_config_allowlist_is_exactly_the_three_user_settings():
    """Min SOC (1142), max AC input (1138), max AC output (1147), with their ranges."""
    assert dict(CONFIG_WRITE_POINTS) == {
        1142: (5, 100), 1138: (100, 10800), 1147: (0, 10800),
    }, FLASH_WEAR_MESSAGE
    assert not set(CONFIG_WRITE_POINTS) & RAM_WRITE_POINTS, FLASH_WEAR_MESSAGE


def test_endpoint_allowlist_is_exactly_the_three_rpc_calls():
    assert dict(ALLOWED_ENDPOINTS) == {
        "Sys.GetConfig": "GET",
        "Indevolt.GetData": "POST",
        "Indevolt.SetData": "POST",
    }, FLASH_WEAR_MESSAGE


@pytest.mark.parametrize("point", KNOWN_FLASH_POINTS)
async def test_the_control_channel_refuses_every_known_flash_point(point):
    """Outside set_config no flash point gets through, not even a user setting."""
    clock = VirtualClock()
    client, device = _client(clock)
    for values in ([1, 0, 0], [20]):
        with pytest.raises(IndevoltFlashWriteForbidden):
            await client._submit("Indevolt.SetData", {"f": 16, "t": point, "v": values})
    assert device.requests == [], FLASH_WEAR_MESSAGE


@pytest.mark.parametrize("point", sorted(set(KNOWN_FLASH_POINTS) - set(CONFIG_WRITE_POINTS)))
async def test_the_config_channel_refuses_every_other_flash_point(point):
    clock = VirtualClock()
    client, device = _client(clock)
    with pytest.raises(IndevoltFlashWriteForbidden):
        await client.set_config(point, 1)
    token = indevolt_client._CONFIG_WRITE.set(True)
    try:
        with pytest.raises(IndevoltFlashWriteForbidden):
            await client._submit("Indevolt.SetData", {"f": 16, "t": point, "v": [1]})
    finally:
        indevolt_client._CONFIG_WRITE.reset(token)
    assert device.requests == [], FLASH_WEAR_MESSAGE


async def test_the_config_channel_cannot_write_the_realtime_tuple():
    clock = VirtualClock()
    client, device = _client(clock)
    with pytest.raises(IndevoltFlashWriteForbidden):
        await client.set_config(47015, 0)
    token = indevolt_client._CONFIG_WRITE.set(True)
    try:
        with pytest.raises(IndevoltFlashWriteForbidden):
            await client._submit("Indevolt.SetData", {"f": 16, "t": 47015, "v": [0, 0, 0]})
    finally:
        indevolt_client._CONFIG_WRITE.reset(token)
    assert device.requests == [], FLASH_WEAR_MESSAGE


@pytest.mark.parametrize("point, value", [(1142, 4), (1142, 101), (1138, 99), (1147, -1),
                                          (1147, 10801), (1142, 20.0), (1142, True)])
async def test_config_values_outside_their_range_are_refused(point, value):
    clock = VirtualClock()
    client, device = _client(clock)
    with pytest.raises(ValueError):
        await client.set_config(point, value)
    token = indevolt_client._CONFIG_WRITE.set(True)
    try:
        with pytest.raises(IndevoltFlashWriteForbidden):
            await client._submit("Indevolt.SetData", {"f": 16, "t": point, "v": [value, 0]})
    finally:
        indevolt_client._CONFIG_WRITE.reset(token)
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
    """No Indevolt file names a flash point, except the client and the settings
    module, which may name only the three user settings. The points that are also
    read have their own rule below; 1 and 4 are plain numbers too."""
    writable_only = set(KNOWN_FLASH_POINTS) - READ_AND_WRITE_POINTS - {1, 4}
    offenders = []
    for path in _source_files():
        if "indevolt" not in path.name or path == CLIENT_FILE:
            continue
        allowed = set(CONFIG_WRITE_POINTS) if path == SETTINGS_FILE else set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Constant) and type(node.value) is int
                    and node.value in writable_only - allowed):
                offenders.append(f"{path.name}:{node.lineno} {node.value}")
    assert offenders == [], f"{FLASH_WEAR_MESSAGE}\n{offenders}"


def test_points_that_are_also_written_are_named_only_where_reads_are_defined():
    """A point with the same number for reading and writing (READ_AND_WRITE_POINTS)
    is named only in the module-level definitions of the driver and the settings
    module: the read tiers and point constants. Code inside a function refers to
    it through those names, so no function can put the number in a request."""
    offenders = []
    for path in _source_files():
        if "indevolt" not in path.name or path == CLIENT_FILE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        inside: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                inside.update(id(n) for n in ast.walk(node))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and type(node.value) is int
                    and node.value in READ_AND_WRITE_POINTS
                    and (path not in (DRIVER_FILE, SETTINGS_FILE) or id(node) in inside)):
                offenders.append(f"{path.name}:{node.lineno} {node.value}")
    assert offenders == [], f"{FLASH_WEAR_MESSAGE}\n{offenders}"


@pytest.mark.parametrize("point", sorted(READ_AND_WRITE_POINTS))
async def test_a_driver_reads_but_never_writes_the_points_that_are_also_written(point):
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock)
    driver = await connected_driver(clock, device)
    await driver.apply_setpoint(800)
    await clock.run(400)  # every read tier comes round, the limits tier twice
    reads = {p for r in device.requests if r.endpoint == "Indevolt.GetData" for p in r.config["t"]}
    assert point in reads
    assert flash_writes(device) == [], FLASH_WEAR_MESSAGE
    await driver.close()


def test_client_has_no_generic_write_method():
    public = {name for name, _ in inspect.getmembers(IndevoltClient) if not name.startswith("_")}
    assert public <= {
        "get_config", "get_data", "set_realtime", "stop", "exchange", "close",
        "realtime_config", "config_write", "set_config", "slot_s", "closed",
    }, f"{FLASH_WEAR_MESSAGE}\nUnexpected client API: {sorted(public)}"


def _calls(path: Path, attribute: str) -> list[tuple[str, int]]:
    """(enclosing function, line) of every ``.attribute(...)`` call in ``path``."""
    found = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(function):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == attribute):
                found.append((function.name, node.lineno))
    return found


def test_only_user_intent_reaches_the_config_channel():
    """set_config is called only by the settings sync's write step, and the sync is
    asked to write only from write_control (number entities) and apply_config
    (setup after an options-flow save). The scheduler and control path never."""
    callers = {(p.name, f) for p in _source_files() for f, _ in _calls(p, "set_config")}
    assert callers == {("indevolt_settings.py", "_write")}, f"{FLASH_WEAR_MESSAGE}\n{callers}"
    requests = {f for f, _ in _calls(DRIVER_FILE, "request")}
    assert requests == {"write_control"}, f"{FLASH_WEAR_MESSAGE}\n{requests}"
    applies = {f for f, _ in _calls(DRIVER_FILE, "apply_config")}
    assert applies <= {"apply_config"}, f"{FLASH_WEAR_MESSAGE}\n{applies}"
    inside_settings = {f for f, _ in _calls(SETTINGS_FILE, "request")}
    assert inside_settings <= {"apply_config"}, f"{FLASH_WEAR_MESSAGE}\n{inside_settings}"
    for path in _source_files():
        if path not in (CLIENT_FILE, SETTINGS_FILE, DRIVER_FILE) and "indevolt" in path.name:
            assert not _calls(path, "set_config"), f"{FLASH_WEAR_MESSAGE}\n{path.name}"


async def test_periodic_paths_never_write_flash():
    """Hours of control, target refreshes, a weekly-full-charge cutoff, a restart and
    a setup that passes unchanged settings: not one flash write."""
    clock = VirtualClock()
    device = FakeIndevoltDevice(clock, soc=50.0)
    store: dict[str, object] = {}

    def build_kwargs():
        return {"persist": store.__setitem__, "stored": store.get}

    driver = await connected_driver(clock, device, **build_kwargs())
    for step in range(2 * 3600 // 5):
        await driver.apply_setpoint((-1) ** (step // 120) * 900)
        await clock.run(5)
    assert await driver.set_charge_cutoff(100) is False
    assert await driver.apply_config(max_soc_pct=100, min_soc_pct=store["min_soc"],
                                     max_charge_power_w=store["max_charge_power"],
                                     max_discharge_power_w=store["max_discharge_power"])
    await clock.run(120)
    await driver.close()
    driver = await connected_driver(clock, device, **build_kwargs())
    assert await driver.apply_config(max_soc_pct=90, min_soc_pct=15,
                                     max_charge_power_w=2400, max_discharge_power_w=2400)
    await driver.apply_setpoint(-700)
    await clock.run(600)
    assert driver.control_state is ControlState.OWNED
    assert flash_writes(device) == [], FLASH_WEAR_MESSAGE
    await driver.close()


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
