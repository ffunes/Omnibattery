"""Async Modbus TCP client for Huawei SUN2000 inverters.

Huawei exposes everything Omnibattery needs through FC03 holding registers, and
accepts set-points through FC16 — the same function code the reference
integration uses for every write, single registers included.

Two Huawei-specific traits this client exists for:

* **Post-connect pause.** A SUN2000 drops the first request if it arrives too
  soon after the TCP handshake. The reference library waits 1500 ms; we do the
  same. Without it the first poll after every reconnect is lost.
* **Single in-flight request.** Huawei cannot cope with pipelined requests, so
  a lock serialises calls and a short cooldown separates them, mirroring the
  reference library's 50 ms pacing.

Verified against a SUN2000-8K-MAP0 (V200R024C00SPC110) reached through a
modbus-proxy in front of an EMMA-A02, slave id 4.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusIOException

from .modbus_client import _detect_slave_kwarg

_LOGGER = logging.getLogger(__name__)

_REQUEST_TIMEOUT_S = 10.0
# The inverter ignores requests issued immediately after the TCP handshake.
_WAIT_ON_CONNECT_S = 1.5
# Pacing between requests. The reference library uses 50 ms, which also covers
# its serial and SDongle transports. Measured over Modbus TCP with nothing else
# on the bus: 800 consecutive reads at 10 ms and at 0 ms pacing, zero failures,
# 3.6 ms median round trip, 6 ms at the 95th percentile. 20 ms keeps roughly a
# threefold margin over that while cutting a full telemetry sweep from ~1.1 s to
# ~0.45 s — which matters because a set-point write queues behind it.
_MESSAGE_WAIT_S = 0.02


def decode_u16(regs: list[int], offset: int = 0) -> int:
    return regs[offset] & 0xFFFF


def decode_i16(regs: list[int], offset: int = 0) -> int:
    value = regs[offset] & 0xFFFF
    return value - 0x10000 if value >= 0x8000 else value


def decode_u32(regs: list[int], offset: int = 0) -> int:
    return ((regs[offset] & 0xFFFF) << 16) | (regs[offset + 1] & 0xFFFF)


def decode_i32(regs: list[int], offset: int = 0) -> int:
    value = decode_u32(regs, offset)
    return value - 0x100000000 if value >= 0x80000000 else value


def decode_string(regs: list[int], offset: int, count: int) -> Optional[str]:
    """Decode a Huawei ASCII string register run, NUL-terminated and padded."""
    raw = bytearray()
    for reg in regs[offset:offset + count]:
        raw.append((reg >> 8) & 0xFF)
        raw.append(reg & 0xFF)
    text = raw.split(b"\x00")[0].decode("ascii", "replace").strip()
    return text or None


class HuaweiModbusClient:
    """Thin async Modbus TCP transport for one Huawei SUN2000 inverter."""

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 1,
        timeout: float = _REQUEST_TIMEOUT_S,
    ) -> None:
        self.host = host
        self.port = port
        self.unit_id = slave_id
        self._timeout = timeout
        self._client: Optional[AsyncModbusTcpClient] = None
        self._slave_kwarg = "slave"
        self._is_shutting_down = False
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        client = self._client
        return bool(client is not None and getattr(client, "connected", False))

    def set_shutting_down(self, value: bool) -> None:
        self._is_shutting_down = bool(value)

    async def async_connect(self) -> bool:
        """Open a fresh connection and observe the post-connect pause."""
        await self.async_close()
        client = AsyncModbusTcpClient(
            host=self.host, port=self.port, timeout=self._timeout
        )
        try:
            connected = await client.connect()
        except Exception as err:
            _LOGGER.error(
                "Huawei Modbus connect failed %s:%s: %s", self.host, self.port, err
            )
            await _close_quietly(client)
            return False
        if not connected:
            _LOGGER.error(
                "Huawei Modbus connect refused %s:%s", self.host, self.port
            )
            await _close_quietly(client)
            return False
        # Skipping this pause costs the first poll after every reconnect.
        await asyncio.sleep(_WAIT_ON_CONNECT_S)
        self._client = client
        self._slave_kwarg = _detect_slave_kwarg(client)
        return True

    async def async_close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await _close_quietly(client)

    async def async_read_holding_block(
        self, start: int, count: int
    ) -> Optional[list[int]]:
        """FC03: read ``count`` contiguous holding registers, or None on failure."""
        if not (0 <= start <= 0xFFFF) or not (1 <= count <= 125):
            _LOGGER.error(
                "Invalid Huawei Modbus read start=%s count=%s", start, count
            )
            return None
        if self._client is None or not self.connected:
            return None

        kwargs = {self._slave_kwarg: self.unit_id}
        async with self._lock:
            try:
                try:
                    result = await asyncio.wait_for(
                        self._client.read_holding_registers(
                            address=start, count=count, **kwargs
                        ),
                        timeout=self._timeout,
                    )
                finally:
                    if not self._is_shutting_down:
                        await asyncio.sleep(_MESSAGE_WAIT_S)
            except (ConnectionException, ModbusIOException, asyncio.TimeoutError):
                if not self._is_shutting_down:
                    _LOGGER.debug(
                        "Huawei Modbus connection error reading %d (count=%d)",
                        start,
                        count,
                    )
                return None
            except Exception as err:
                if not self._is_shutting_down:
                    _LOGGER.exception(
                        "Huawei Modbus exception reading %d: %s", start, err
                    )
                return None

        if result.isError():
            # An unsupported register run is a normal outcome on models that do
            # not implement it, so this stays at debug rather than error.
            if not self._is_shutting_down:
                _LOGGER.debug(
                    "Huawei Modbus read error at %d (count=%d)", start, count
                )
            return None
        regs = getattr(result, "registers", None)
        if regs is None or len(regs) < count:
            if not self._is_shutting_down:
                _LOGGER.warning(
                    "Huawei Modbus incomplete read at %d: got %s expected %d",
                    start,
                    len(regs) if regs else 0,
                    count,
                )
            return None
        return list(regs[:count])


    async def async_write_registers(self, start: int, values: list[int]) -> bool:
        """FC16: write ``values`` to consecutive holding registers from ``start``.

        One function code covers every write this driver makes, because the
        reference integration writes even single registers as a block and the
        inverter is known to accept that shape.
        """
        if not (0 <= start <= 0xFFFF) or not (1 <= len(values) <= 123):
            _LOGGER.error(
                "Invalid Huawei Modbus write start=%s count=%s", start, len(values)
            )
            return False
        if self._client is None or not self.connected:
            return False

        kwargs = {self._slave_kwarg: self.unit_id}
        async with self._lock:
            try:
                try:
                    result = await asyncio.wait_for(
                        self._client.write_registers(
                            address=start, values=list(values), **kwargs
                        ),
                        timeout=self._timeout,
                    )
                finally:
                    if not self._is_shutting_down:
                        await asyncio.sleep(_MESSAGE_WAIT_S)
            except (ConnectionException, ModbusIOException, asyncio.TimeoutError) as err:
                if not self._is_shutting_down:
                    _LOGGER.warning(
                        "Huawei Modbus write to %d failed: %s", start, err
                    )
                return False
            except Exception as err:
                if not self._is_shutting_down:
                    _LOGGER.exception(
                        "Huawei Modbus exception writing %d: %s", start, err
                    )
                return False

        if result.isError():
            if not self._is_shutting_down:
                _LOGGER.warning(
                    "Huawei Modbus write rejected at %d (values=%s)", start, values
                )
            return False
        return True


async def _close_quietly(client) -> None:
    try:
        result = client.close()
        if asyncio.iscoroutine(result):
            await result
    except Exception as err:
        _LOGGER.debug("Error closing Huawei Modbus connection: %s", err)
