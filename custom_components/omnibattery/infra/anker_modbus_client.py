"""Async Modbus TCP client for Anker SOLIX Solarbank Max AC.

Reads use only FC03 (holding) and FC04 (input) as defined by Anker's official
``batch_read_ranges``. Writes target holding addresses with FC06 (uint16) and
FC16 (int32), matching Anker's official HA client.

INT32 word order is big-endian (high word first), same as
:func:`decode_registers`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusIOException

from .modbus_client import _detect_slave_kwarg, decode_registers

_LOGGER = logging.getLogger(__name__)

_REQUEST_TIMEOUT_S = 10.0
_MESSAGE_WAIT_S = 0.05


def encode_int32(value: int) -> list[int]:
    """Encode a signed 32-bit value as two Modbus registers (big-endian words)."""
    if value < 0:
        value += 0x100000000
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]


class AnkerModbusClient:
    """Thin async Modbus TCP transport for Anker Solarbank devices."""

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

    @property
    def connected(self) -> bool:
        client = self._client
        return bool(client is not None and getattr(client, "connected", False))

    def set_shutting_down(self, value: bool) -> None:
        self._is_shutting_down = bool(value)

    async def async_connect(self) -> bool:
        """Open a fresh TCP connection. Returns True on success."""
        await self.async_close()
        client = AsyncModbusTcpClient(
            host=self.host,
            port=self.port,
            timeout=self._timeout,
        )
        try:
            connected = await client.connect()
        except Exception as err:
            _LOGGER.error(
                "Anker Modbus connect failed %s:%s: %s",
                self.host,
                self.port,
                err,
            )
            try:
                close_result = client.close()
                if asyncio.iscoroutine(close_result):
                    await close_result
            except Exception:
                pass
            return False
        if not connected:
            _LOGGER.error(
                "Anker Modbus connect refused %s:%s", self.host, self.port
            )
            return False
        self._client = client
        self._slave_kwarg = _detect_slave_kwarg(client)
        return True

    async def async_close(self) -> None:
        """Close the TCP connection if open."""
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            result = client.close()
            if asyncio.iscoroutine(result):
                await result
        except Exception as err:
            _LOGGER.debug("Error closing Anker Modbus connection: %s", err)

    async def _pace(self) -> None:
        if self._is_shutting_down:
            return
        await asyncio.sleep(_MESSAGE_WAIT_S)

    async def _read_raw(
        self,
        *,
        address: int,
        count: int,
        register_type: str,
    ) -> Optional[list[int]]:
        """Read ``count`` registers via FC03 (holding) or FC04 (input)."""
        if register_type not in ("holding", "input"):
            raise ValueError(f"Unsupported register_type: {register_type}")
        if not (0 <= address <= 0xFFFF) or not (1 <= count <= 125):
            _LOGGER.error(
                "Invalid Anker Modbus read address=%s count=%s", address, count
            )
            return None
        if self._client is None or not self.connected:
            return None

        kwargs = {self._slave_kwarg: self.unit_id}
        try:
            try:
                if register_type == "holding":
                    result = await asyncio.wait_for(
                        self._client.read_holding_registers(
                            address=address, count=count, **kwargs
                        ),
                        timeout=self._timeout,
                    )
                else:
                    result = await asyncio.wait_for(
                        self._client.read_input_registers(
                            address=address, count=count, **kwargs
                        ),
                        timeout=self._timeout,
                    )
            finally:
                await self._pace()

            if result.isError():
                if not self._is_shutting_down:
                    _LOGGER.error(
                        "Anker Modbus %s read error at %d (count=%d)",
                        register_type,
                        address,
                        count,
                    )
                return None
            regs = getattr(result, "registers", None)
            if regs is None or len(regs) < count:
                if not self._is_shutting_down:
                    _LOGGER.warning(
                        "Anker Modbus incomplete %s read at %d: got %s expected %d",
                        register_type,
                        address,
                        len(regs) if regs else 0,
                        count,
                    )
                return None
            return list(regs[:count])
        except (ConnectionException, ModbusIOException, asyncio.TimeoutError):
            if not self._is_shutting_down:
                _LOGGER.debug(
                    "Anker Modbus connection error reading %s %d",
                    register_type,
                    address,
                )
            return None
        except Exception as err:
            if not self._is_shutting_down:
                _LOGGER.exception(
                    "Anker Modbus exception reading %s %d: %s",
                    register_type,
                    address,
                    err,
                )
            return None

    async def async_read_input_block(
        self, start: int, count: int
    ) -> Optional[list[int]]:
        """FC04: read contiguous input registers."""
        return await self._read_raw(
            address=start, count=count, register_type="input"
        )

    async def async_read_holding_block(
        self, start: int, count: int
    ) -> Optional[list[int]]:
        """FC03: read contiguous holding registers."""
        return await self._read_raw(
            address=start, count=count, register_type="holding"
        )

    async def async_read_input_register(
        self,
        address: int,
        data_type: str = "uint16",
        count: Optional[int] = None,
    ):
        """FC04: read and decode one typed value from input registers."""
        if count is None:
            count = 2 if data_type in ("int32", "uint32") else 1
        regs = await self.async_read_input_block(address, count)
        if regs is None:
            return None
        return decode_registers(regs, data_type)

    async def async_read_holding_register(
        self,
        address: int,
        data_type: str = "uint16",
        count: Optional[int] = None,
    ):
        """FC03: read and decode one typed value from holding registers."""
        if count is None:
            count = 2 if data_type in ("int32", "uint32") else 1
        regs = await self.async_read_holding_block(address, count)
        if regs is None:
            return None
        return decode_registers(regs, data_type)

    async def async_write_register(self, address: int, value: int) -> bool:
        """FC06: write a single uint16 holding register."""
        if self._client is None or not self.connected:
            return False
        kwargs = {self._slave_kwarg: self.unit_id}
        try:
            try:
                result = await asyncio.wait_for(
                    self._client.write_register(
                        address=address,
                        value=int(value) & 0xFFFF,
                        **kwargs,
                    ),
                    timeout=self._timeout,
                )
            finally:
                await self._pace()
            return not result.isError()
        except (ConnectionException, ModbusIOException, asyncio.TimeoutError):
            if not self._is_shutting_down:
                _LOGGER.debug(
                    "Anker Modbus connection error writing register %d", address
                )
            return False
        except Exception as err:
            if not self._is_shutting_down:
                _LOGGER.exception(
                    "Anker Modbus exception writing register %d: %s",
                    address,
                    err,
                )
            return False

    async def async_write_registers_int32(self, address: int, value: int) -> bool:
        """FC16: write a signed INT32 as two holding registers (big-endian)."""
        if self._client is None or not self.connected:
            return False
        words = encode_int32(int(value))
        kwargs = {self._slave_kwarg: self.unit_id}
        try:
            try:
                result = await asyncio.wait_for(
                    self._client.write_registers(
                        address=address, values=words, **kwargs
                    ),
                    timeout=self._timeout,
                )
            finally:
                await self._pace()
            return not result.isError()
        except (ConnectionException, ModbusIOException, asyncio.TimeoutError):
            if not self._is_shutting_down:
                _LOGGER.debug(
                    "Anker Modbus connection error writing INT32 at %d", address
                )
            return False
        except Exception as err:
            if not self._is_shutting_down:
                _LOGGER.exception(
                    "Anker Modbus exception writing INT32 at %d: %s",
                    address,
                    err,
                )
            return False
