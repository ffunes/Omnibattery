"""The v3 exception frame, end to end against a fake battery.

Marstek's v3-family firmware answers a read of an unmapped register with nine
bytes whose MBAP length field says 4 where the protocol requires 3. pymodbus
frames the reply as 7 + (length - 1) and waits for a tenth byte that never
comes, so a rejection costs a full timeout - times the internal retries - on a
device that answered in milliseconds. ``_marstek_v3_packet_correction`` repairs
the field on the way in.

Tested through a socket rather than by calling the hook, because the bug this
guards against was not in the hook: it was correct and never called. Only a
read that really goes over a connection says whether pymodbus uses it.
"""
from __future__ import annotations

import asyncio
import struct
import time

import pytest

from custom_components.omnibattery.infra import modbus_client as modbus_client_module
from custom_components.omnibattery.infra.modbus_client import MarstekModbusClient

MAPPED = 30200
UNMAPPED = 39000
TIMEOUT_S = 1


def _broken_device(split: bool):
    """A battery that rejects UNMAPPED with the firmware's wrong length field.

    ``split`` delivers the nine bytes in two writes, which is what a busy
    network does and what decides whether the correction may test the length
    of one segment or has to survive reassembly.
    """

    class Device(asyncio.Protocol):
        def connection_made(self, transport):
            self.transport = transport
            self.buffer = b""

        def data_received(self, data):
            self.buffer += data
            while len(self.buffer) >= 12:
                tid, _pid, _len, unit, fc, start, qty = struct.unpack(">HHHBBHH", self.buffer[:12])
                self.buffer = self.buffer[12:]
                if start == MAPPED:
                    payload = struct.pack(">H", 0x1234)
                    self.transport.write(
                        struct.pack(">HHHBBB", tid, 0, 3 + len(payload), unit, fc, len(payload)) + payload
                    )
                    continue
                frame = struct.pack(">HHHBBB", tid, 0, 4, unit, fc | 0x80, 2)  # length 4, nine bytes
                if split:
                    self.transport.write(frame[:6])
                    self.transport.write(frame[6:])
                else:
                    self.transport.write(frame)

    return Device


async def _time_a_rejection(monkeypatch, *, is_v3: bool, split: bool = False) -> tuple[float, object]:
    monkeypatch.setattr(modbus_client_module, "_PYMODBUS_RETRIES", 0)  # one attempt, short test
    loop = asyncio.get_running_loop()
    server = await loop.create_server(_broken_device(split), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    client = MarstekModbusClient("127.0.0.1", port, message_wait_ms=0, timeout=TIMEOUT_S, is_v3=is_v3)
    try:
        assert await client.async_connect()
        assert await client.async_read_register(register=MAPPED, data_type="uint16") == 0x1234
        started = time.monotonic()
        value = await client.async_read_register(register=UNMAPPED, data_type="uint16")
        elapsed = time.monotonic() - started
        # The connection has to stay usable: the nine bytes must not be left in
        # the buffer for the next reply to be glued onto.
        assert await client.async_read_register(register=MAPPED, data_type="uint16") == 0x1234
        return elapsed, value
    finally:
        client.client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("split", [False, True], ids=["one segment", "two segments"])
async def test_v3_rejection_costs_no_timeout(monkeypatch, split):
    elapsed, value = await _time_a_rejection(monkeypatch, is_v3=True, split=split)
    assert value is None  # a rejection is still a rejection
    assert elapsed < TIMEOUT_S / 2, f"rejection took {elapsed:.2f}s, the correction is not being called"


async def test_without_the_correction_the_same_read_waits_out_the_timeout(monkeypatch):
    """Why the correction exists - and a warning if pymodbus ever fixes this."""
    elapsed, value = await _time_a_rejection(monkeypatch, is_v3=False)
    assert value is None
    assert elapsed >= TIMEOUT_S
