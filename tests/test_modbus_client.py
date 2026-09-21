"""Unit tests for the Modbus register decoder and the v3 block-read table.

No hardware and no Home Assistant: ``decode_registers`` is a pure function and
the block table is plain data. These pin the refactor that split decoding out of
``async_read_register`` so block reads can reuse it (issue #361), and guard the
block offsets against the real v3 register addresses.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.omnibattery.infra.modbus_client import (
    decode_registers,
    MarstekModbusClient,
    _marstek_v3_packet_correction,
)
from pymodbus.client import AsyncModbusTcpClient, AsyncModbusSerialClient
from custom_components.omnibattery.const import (
    REGISTER_BLOCKS_V3,
    REGISTER_BLOCKS_V2,
    SENSOR_DEFINITIONS_V3,
    NUMBER_DEFINITIONS_V3,
    SELECT_DEFINITIONS_V3,
    SWITCH_DEFINITIONS_V3,
    BINARY_SENSOR_DEFINITIONS_V3,
    SENSOR_DEFINITIONS_VA,
    NUMBER_DEFINITIONS_VA,
    SELECT_DEFINITIONS_VA,
    NUMBER_DEFINITIONS_VD,
    SELECT_DEFINITIONS_VD,
    SENSOR_DEFINITIONS,
    NUMBER_DEFINITIONS,
    SELECT_DEFINITIONS,
    SWITCH_DEFINITIONS,
    BINARY_SENSOR_DEFINITIONS,
    SCAN_INTERVAL,
)


# ----------------------------------------------------------------------
# decode_registers
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "regs, data_type, expected",
    [
        ([0x0000], "uint16", 0),
        ([0xFFFF], "uint16", 65535),
        ([0x0005], "int16", 5),
        ([0x8000], "int16", -32768),
        ([0xFFFF], "int16", -1),
        ([0x0001, 0x0000], "uint32", 0x00010000),
        ([0x0001, 0x0000], "int32", 65536),
        ([0xFFFF, 0xFFFF], "int32", -1),
        ([0x0001, 0x0000, 0x0000], "uint48", 0x000100000000),
        ([0x0001, 0x0000, 0x0000, 0x0000], "uint64", 0x0001000000000000),
        ([0x4142, 0x4300], "char", "ABC"),
    ],
)
def test_decode_scalar_types(regs, data_type, expected):
    assert decode_registers(regs, data_type) == expected


@pytest.mark.parametrize(
    "value, bit_index, expected",
    [(0b0000, 0, False), (0b0001, 0, True), (0b0100, 2, True), (0b0100, 1, False)],
)
def test_decode_bit(value, bit_index, expected):
    assert decode_registers([value], "bit", bit_index) is expected


def test_decode_empty_returns_none():
    assert decode_registers([], "uint16") is None


@pytest.mark.parametrize("data_type", ["int32", "uint32", "uint48", "uint64"])
def test_decode_too_short_returns_none(data_type):
    # Only one word given for a multi-word type.
    assert decode_registers([0x0001], data_type) is None


def test_decode_bad_bit_index_raises():
    with pytest.raises(ValueError):
        decode_registers([0x0001], "bit", 16)


def test_decode_unsupported_type_raises():
    with pytest.raises(ValueError):
        decode_registers([0x0001], "float32")


# ----------------------------------------------------------------------
# REGISTER_BLOCKS integrity (all versions that use block reads)
# ----------------------------------------------------------------------
def _register_by_key(*def_lists):
    by_key = {}
    for defs in def_lists:
        for defn in defs:
            by_key[defn["key"]] = defn
    return by_key


# v3, vA and vD all read the v3 block table; vA/vD must therefore map every
# block member to the same register address as v3 (they share that map).
_V3_DEFS = (
    SENSOR_DEFINITIONS_V3,
    NUMBER_DEFINITIONS_V3,
    SELECT_DEFINITIONS_V3,
    SWITCH_DEFINITIONS_V3,
    BINARY_SENSOR_DEFINITIONS_V3,
)
_VA_DEFS = (
    SENSOR_DEFINITIONS_VA,
    NUMBER_DEFINITIONS_VA,
    SELECT_DEFINITIONS_VA,
    SWITCH_DEFINITIONS_V3,
    BINARY_SENSOR_DEFINITIONS_V3,
)
_VD_DEFS = (
    SENSOR_DEFINITIONS_VA,  # vD shares the vA sensor map
    NUMBER_DEFINITIONS_VD,
    SELECT_DEFINITIONS_VD,
    SWITCH_DEFINITIONS_V3,
    BINARY_SENSOR_DEFINITIONS_V3,
)
_V2_DEFS = (
    SENSOR_DEFINITIONS,
    NUMBER_DEFINITIONS,
    SELECT_DEFINITIONS,
    SWITCH_DEFINITIONS,
    BINARY_SENSOR_DEFINITIONS,
)


@pytest.mark.parametrize(
    "blocks, def_lists",
    [
        (REGISTER_BLOCKS_V3, _V3_DEFS),
        (REGISTER_BLOCKS_V3, _VA_DEFS),
        (REGISTER_BLOCKS_V3, _VD_DEFS),
        (REGISTER_BLOCKS_V2, _V2_DEFS),
    ],
)
def test_block_members_match_real_register_addresses(blocks, def_lists):
    """Each member's offset must land on its real register address.

    block.start + member.offset == the address declared on the entity, and the
    slice must stay inside the block. A wrong offset would silently feed the
    wrong word to a sensor. Checked against every version that reads the table
    (v3/vA/vD share the v3 table, so all three must agree on the addresses).
    """
    by_key = _register_by_key(*def_lists)
    for block in blocks:
        assert block["scan_interval"] in SCAN_INTERVAL
        for member in block["members"]:
            defn = by_key.get(member["key"])
            assert defn is not None, f"unknown key {member['key']}"
            assert defn["register"] == block["start"] + member["offset"]
            assert member["offset"] + member["count"] <= block["count"]
            assert member["data_type"] == defn.get("data_type", "uint16")


@pytest.mark.parametrize("blocks", [REGISTER_BLOCKS_V3, REGISTER_BLOCKS_V2])
def test_blocks_are_gapless_contiguous(blocks):
    """No padding: span length equals the registers actually consumed."""
    for block in blocks:
        consumed = sum(m["count"] for m in block["members"])
        assert consumed == block["count"]


def test_block_members_are_not_total_increasing():
    """Block decoding skips the per-register backward-jump guard, so no
    total_increasing energy counter may be served by a block."""
    for blocks, def_lists in (
        (REGISTER_BLOCKS_V3, _V3_DEFS),
        (REGISTER_BLOCKS_V2, _V2_DEFS),
    ):
        by_key = _register_by_key(*def_lists)
        for block in blocks:
            for member in block["members"]:
                defn = by_key.get(member["key"], {})
                assert defn.get("state_class") != "total_increasing"


# ----------------------------------------------------------------------
# Transport selection: TCP vs serial / Modbus RTU (discussion #350)
# ----------------------------------------------------------------------
def _make_client(**kwargs):
    """Build a client inside an event loop (pymodbus needs a running loop)."""
    async def _build():
        return MarstekModbusClient(**kwargs)
    return asyncio.run(_build())


def test_default_transport_is_tcp():
    """No serial_port -> TCP client, unchanged behaviour."""
    c = _make_client(host="192.168.1.50", port=502)
    assert isinstance(c.client, AsyncModbusTcpClient)


def test_serial_port_selects_serial_client():
    """serial_port set -> RTU serial client built instead of TCP."""
    c = _make_client(host="/dev/ttyUSB0", port=502, serial_port="/dev/ttyUSB0")
    assert isinstance(c.client, AsyncModbusSerialClient)


def test_serial_skips_v3_packet_correction():
    """The v3 MBAP fix is TCP-framing only; serial must not install it."""
    serial = _make_client(host="/dev/ttyUSB0", port=502, is_v3=True, serial_port="/dev/ttyUSB0")
    tcp = _make_client(host="192.168.1.50", port=502, is_v3=True)
    assert getattr(serial.client, "ctx", None) is None or (
        serial.client.ctx.trace_packet is not _marstek_v3_packet_correction
    )
    assert tcp.client.ctx.trace_packet is _marstek_v3_packet_correction


def test_v3_packet_correction_reaches_the_transaction_manager():
    """Where pymodbus reads the hook, not where it is convenient to set it.

    pymodbus takes trace_packet as a constructor argument and hands it to its
    TransactionManager. Assigning client.trace_packet afterwards leaves the
    manager on its own dummy hook, so the correction was installed on every v3
    connection and called on none of them - and a test that only looked at the
    client attribute saw nothing wrong.
    """
    tcp = _make_client(host="192.168.1.50", port=502, is_v3=True)
    plain = _make_client(host="192.168.1.50", port=502, is_v3=False)
    assert tcp.client.ctx.trace_packet is _marstek_v3_packet_correction
    assert plain.client.ctx.trace_packet is not _marstek_v3_packet_correction
