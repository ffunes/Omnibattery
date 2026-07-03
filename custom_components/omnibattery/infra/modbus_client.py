"""
Helper module for Modbus TCP communication using pymodbus.
Provides an abstraction for reading and writing registers from
a Marstek Venus battery system asynchronously.
"""

from pymodbus.client import AsyncModbusTcpClient, AsyncModbusSerialClient
from pymodbus.exceptions import ConnectionException, ModbusIOException
import asyncio
import inspect
import time
from typing import Optional

import logging

from ..const import DEBUG_RAW_MODBUS_READS, SERIAL_BAUDRATE

_LOGGER = logging.getLogger(__name__)


def _backoff_jitter(delay: float) -> float:
    """Return a symmetric +/-10% jitter for a retry backoff delay.

    Entropy comes from time.monotonic() rather than
    asyncio.get_event_loop().time(); the latter reaches into loop internals and
    is a deprecated access pattern. Precision is irrelevant: the jitter only
    de-syncs concurrent retriers so they don't all wake on the same tick.
    """
    return delay * 0.1 * (0.5 - time.monotonic() % 1)


def _marstek_v3_packet_correction(sending: bool, data: bytes) -> bytes:
    """Fix malformed Modbus exception responses from Marstek v3 firmware.

    The v3 firmware incorrectly sets the MBAP length byte to 4 instead of 3
    in exception responses. This causes pymodbus to wait for an extra byte
    that never arrives, resulting in long timeouts.

    Exception response structure (9 bytes):
      [0-1] Transaction ID, [2-3] Protocol ID, [4-5] Length (should be 3),
      [6] Unit ID, [7] Function code (bit 7=1 for exception), [8] Exception code
    """
    if not sending and len(data) == 9 and data[5] == 4 and (data[7] & 0x80) == 0x80:
        return data[0:5] + b'\x03' + data[6:]
    return data


def _detect_slave_kwarg(client) -> str:
    """Return the keyword pymodbus uses to address the slave/unit id.

    pymodbus renamed this parameter from ``slave`` to ``device_id`` across 3.x
    releases. Inspect the live method signature so we pass the right one
    regardless of the version Home Assistant bundles.
    """
    try:
        params = inspect.signature(client.read_holding_registers).parameters
        if "device_id" in params:
            return "device_id"
    except (ValueError, TypeError):
        pass
    return "slave"


def decode_registers(regs, data_type: str = "uint16", bit_index: Optional[int] = None):
    """Interpret a list of Modbus register words as a typed value.

    Shared by single-register reads and block reads (which slice the block
    buffer per field). Returns the decoded value, or None if ``regs`` is too
    short for the requested type. Raises ValueError for an unsupported
    data_type or an invalid bit_index.
    """
    if not regs:
        return None

    if data_type == "int16":
        val = regs[0]
        return val - 0x10000 if val >= 0x8000 else val

    elif data_type == "uint16":
        return regs[0]

    elif data_type == "int32":
        if len(regs) < 2:
            return None
        val = (regs[0] << 16) | regs[1]
        return val - 0x100000000 if val >= 0x80000000 else val

    elif data_type == "uint32":
        if len(regs) < 2:
            return None
        return (regs[0] << 16) | regs[1]

    elif data_type == "uint48":
        if len(regs) < 3:
            return None
        return (regs[0] << 32) | (regs[1] << 16) | regs[2]

    elif data_type == "uint64":
        if len(regs) < 4:
            return None
        return (regs[0] << 48) | (regs[1] << 32) | (regs[2] << 16) | regs[3]

    elif data_type == "char":
        byte_array = bytearray()
        for reg in regs:
            byte_array.append((reg >> 8) & 0xFF)
            byte_array.append(reg & 0xFF)
        text = byte_array.decode("ascii", errors="ignore").rstrip('\x00')
        return "".join(char for char in text if char.isprintable())

    elif data_type == "bit":
        if bit_index is None or not (0 <= bit_index < 16):
            raise ValueError("bit_index must be between 0 and 15 for bit data_type")
        return bool((regs[0] >> bit_index) & 1)

    else:
        raise ValueError(f"Unsupported data_type: {data_type}")


class MarstekModbusClient:
    """
    Wrapper for pymodbus AsyncModbusTcpClient with helper methods
    for async reading/writing and interpreting common data types.
    """

    def __init__(self, host: str, port: int = 502, message_wait_ms: int = 50, timeout: int = 10, is_v3: bool = False, slave_id: int = 1, serial_port: Optional[str] = None):
        """
        Initialize Modbus client with host, port, message wait time, and timeout.

        Args:
            host (str): IP address or hostname of Modbus server.
            port (int): TCP port number.
            message_wait_ms (int): Delay in ms between Modbus messages.
            timeout (int): Connection timeout in seconds.
            is_v3 (bool): If True, enable v3 firmware packet correction.
            slave_id (int): Modbus slave/unit id to address (default 1).
            serial_port (Optional[str]): Serial device path (e.g. "/dev/ttyUSB0").
                When set, communication uses Modbus RTU over serial instead of
                TCP and ``host``/``port`` are ignored for the link (discussion
                #350). None = Modbus TCP (default, unchanged behaviour).
        """
        self.host = host
        self.port = port

        # Store constructor params for creating fresh client instances on reconnect
        self._host = host
        self._port = port
        self._timeout = timeout
        self._is_v3 = is_v3
        self._serial_port = serial_port
        # pymodbus has no inter-message delay for TCP (message_wait_milliseconds
        # is a Home Assistant modbus-integration concept that pymodbus never
        # implemented), so we enforce the spacing ourselves after every request.
        self._message_wait_sec = max(0.0, message_wait_ms / 1000.0)

        self.client = self._make_client()

        # v3 packet correction repairs the TCP MBAP length byte; RTU framing has
        # no MBAP header (CRC instead), so it only applies to the TCP transport.
        if is_v3 and serial_port is None:
            self.client.trace_packet = _marstek_v3_packet_correction

        self.unit_id = slave_id  # Modbus slave/unit id for this battery
        self._slave_kwarg = _detect_slave_kwarg(self.client)  # "slave" or "device_id"
        self._is_shutting_down = False  # Flag to suppress errors during shutdown

    def _make_client(self):
        """Build a fresh pymodbus async client for this connection.

        Auto-reconnect is disabled: we manage reconnection ourselves by creating
        fresh client instances, which avoids pymodbus's internal reconnect_delay
        growing up to 300s. retries=0 because our _read_raw loop already owns
        retries+backoff; pymodbus's default 3 internal retries each fire a NEW
        transaction_id, which surfaces as the "transaction_id mismatch" cascade on
        the weak v3 MCU (issue #361).

        Serial uses RTU framing at a fixed 115200 8N1 — the rate Marstek's RS485
        link runs at (discussion #350); these are hardware-fixed, not tunable.
        """
        if self._serial_port is not None:
            return AsyncModbusSerialClient(
                port=self._serial_port,
                baudrate=SERIAL_BAUDRATE,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=self._timeout,
                retries=0,
            )
        return AsyncModbusTcpClient(
            host=self._host,
            port=self._port,
            timeout=self._timeout,
            retries=0,
            reconnect_delay=0,
            reconnect_delay_max=0,
        )

    def set_shutting_down(self, value: bool) -> None:
        """
        Set the shutdown flag to suppress error logging during integration unload.

        Args:
            value (bool): True to suppress errors, False for normal operation.
        """
        self._is_shutting_down = value

    @property
    def connected(self) -> bool:
        """Return whether the client is currently connected."""
        return self.client is not None and self.client.connected

    async def async_connect(self) -> bool:
        """
        Connect asynchronously to the Modbus TCP server.

        Always creates a fresh AsyncModbusTcpClient instance to avoid reusing
        internal buffers/state that may be left in an inconsistent state after
        network interruptions. This also resets pymodbus's internal reconnect
        delay which can grow up to 300 seconds after repeated failures.

        Returns:
            bool: True if connection succeeded, False otherwise.
        """
        try:
            # Close and discard existing client to release the battery's
            # single TCP connection slot and avoid half-open connections
            if self.client is not None:
                try:
                    result = self.client.close()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    _LOGGER.debug("Error closing stale Modbus client before reconnect: %s", e)

                # v3 firmware exposes a single TCP slot and does not free it
                # instantly on close. Reopening immediately gets refused, so the
                # battery never recovers. Give it time to release the old socket
                # before we open a new one. Serial has no socket slot, so skip it.
                if self._is_v3 and self._serial_port is None:
                    await asyncio.sleep(1.0)

            # Create a fresh client instance (no corrupted state, no backoff)
            self.client = self._make_client()

            # Restore v3 packet correction (TCP transport only — see __init__)
            if self._is_v3 and self._serial_port is None:
                self.client.trace_packet = _marstek_v3_packet_correction

            connected = await self.client.connect()

            if connected:
                await asyncio.sleep(0.2)  # Wait for connection to stabilize
                _LOGGER.info(
                    "Connected to Modbus server at %s:%s with unit %s",
                    self.host,
                    self.port,
                    self.unit_id,
                )
                return True
            else:
                if not self._is_shutting_down:
                    _LOGGER.warning(
                        "Failed to connect to Modbus server at %s:%s with unit %s",
                        self.host,
                        self.port,
                        self.unit_id,
                    )
                return False
        except Exception as e:
            if not self._is_shutting_down:
                _LOGGER.error(
                    "Exception connecting to Modbus server at %s:%s: %s",
                    self.host,
                    self.port,
                    e,
                )
            return False

    async def async_close(self) -> None:
        """
        Close the Modbus TCP connection safely (handles sync or async close).
        """
        if self.client is None:
            return
        try:
            result = self.client.close()
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            _LOGGER.debug("Error closing Modbus connection: %s", e)
        finally:
            # Drop the reference so the next async_connect() always builds a
            # fresh client instead of reusing a torn-down transport.
            self.client = None

    async def _read_raw(
        self,
        register: int,
        count: int,
        max_retries: int = 3,
        retry_delay: float = 0.1,
        sensor_key: Optional[str] = None,
    ) -> Optional[list]:
        """Read ``count`` holding registers with retries, returning raw words.

        Shared read+retry machinery for both single-register reads and block
        reads. Returns the list of register words (length ``count``) or None on
        failure. Callers decode the words with :func:`decode_registers`.
        """
        if not (0 <= register <= 0xFFFF):
            _LOGGER.error(
                "Invalid register address: %d (0x%04X). Must be 0-65535.",
                register,
                register,
            )
            return None

        if not (1 <= count <= 125):  # Modbus spec limit
            _LOGGER.error(
                "Invalid register count: %d. Must be between 1 and 125.",
                count,
            )
            return None

        attempt = 0
        current_retry_delay = retry_delay

        while attempt < max_retries:
            # Skip connection check - let pymodbus handle connection issues
            # This avoids problems with incorrect connection state reporting

            try:
                try:
                    result = await asyncio.wait_for(
                        self.client.read_holding_registers(address=register, count=count, **{self._slave_kwarg: self.unit_id}),
                        timeout=self._timeout,
                    )
                finally:
                    # Inter-message spacing: v3 firmware needs time between
                    # frames (MESSAGE_WAIT_MS) or it stops responding.
                    if self._message_wait_sec and not self._is_shutting_down:
                        await asyncio.sleep(self._message_wait_sec)
                if result.isError():
                    if not self._is_shutting_down:
                        _LOGGER.error(
                            "Modbus read error at register %d (0x%04X) on attempt %d",
                            register,
                            register,
                            attempt + 1,
                        )
                elif not hasattr(result, "registers") or result.registers is None or len(result.registers) < count:
                    if not self._is_shutting_down:
                        _LOGGER.warning(
                            "Incomplete data received at register %d (0x%04X) on attempt %d: expected %d registers, got %s",
                            register,
                            register,
                            attempt + 1,
                            count,
                            len(result.registers) if result.registers else 0,
                        )
                else:
                    regs = result.registers
                    if DEBUG_RAW_MODBUS_READS:
                        _LOGGER.debug(
                            "Modbus read %s: register=%d/0x%04X count=%s raw=%s",
                            sensor_key or "unknown",
                            register,
                            register,
                            count,
                            regs,
                        )
                    return regs

            except (ConnectionException, ModbusIOException, asyncio.TimeoutError):
                if self._is_shutting_down:
                    return None
                # Do NOT reconnect from here. The battery (v3 firmware especially)
                # holds a single TCP slot and refuses new connections until it has
                # released the old one. Tearing down and reopening on every failed
                # read creates a reconnect storm the battery never recovers from.
                # Fail this read and fall through to the same-connection retry; the
                # coordinator's health monitor owns reconnection after N failed cycles.
                _LOGGER.debug("Connection error reading register %d (0x%04X)", register, register)

            except Exception as e:
                if not self._is_shutting_down:
                    _LOGGER.exception("Exception during Modbus read at register %d (0x%04X) on attempt %d: %s", register, register, attempt + 1, e)

            # During shutdown, don't retry or reconnect - exit immediately to release the connection
            if self._is_shutting_down:
                return None

            attempt += 1
            if attempt < max_retries:
                # Exponential backoff with jitter
                await asyncio.sleep(current_retry_delay + _backoff_jitter(current_retry_delay))
                current_retry_delay = min(current_retry_delay * 2, 5.0)  # Cap at 5 seconds

        _LOGGER.debug(
            "Failed to read register %d (0x%04X) after %d attempts",
            register,
            register,
            max_retries,
        )
        return None

    async def async_read_register(
        self,
        register: int,
        data_type: str = "uint16",
        count: Optional[int] = None,
        bit_index: Optional[int] = None,
        sensor_key: Optional[str] = None,
        max_retries: int = 3,
        retry_delay: float = 0.1,
    ):
        """
        Robustly read registers and interpret the data asynchronously with retries.

        Args:
            register (int): Register address to read from.
            data_type (str): Data type for interpretation, e.g. 'int16', 'int32', 'char', 'bit'.
            count (Optional[int]): Number of registers to read (default depends on data_type).
            bit_index (Optional[int]): Bit position for 'bit' data type (0-15).
            sensor_key (Optional[str]): Sensor key for logging.
            max_retries (int): Maximum number of read attempts.
            retry_delay (float): Delay in seconds between retries.

        Returns:
            int, str, bool, or None: Interpreted value or None on error.
        """
        if count is None:
            count = 2 if data_type in ["int32", "uint32"] else 1

        regs = await self._read_raw(
            register,
            count,
            max_retries=max_retries,
            retry_delay=retry_delay,
            sensor_key=sensor_key,
        )
        if regs is None:
            return None
        return decode_registers(regs, data_type, bit_index)

    async def async_read_block(
        self,
        start: int,
        count: int,
        max_retries: int = 3,
        retry_delay: float = 0.1,
        block_key: Optional[str] = None,
    ) -> Optional[list]:
        """Read a contiguous span of holding registers in a single request.

        Returns the raw list of register words (length ``count``) or None on
        failure. Callers slice the buffer per field and decode each with
        :func:`decode_registers`. Used to cut request count on the weak v3 MCU
        (issue #361).
        """
        return await self._read_raw(
            start,
            count,
            max_retries=max_retries,
            retry_delay=retry_delay,
            sensor_key=block_key,
        )

    async def async_write_register(self, register: int, value: int, max_retries: int = 3, retry_delay: float = 0.1) -> bool:
        """
        Write a single value to a Modbus holding register asynchronously.

        Args:
            register (int): Register address to write to.
            value (int): Value to write.

        Returns:
            bool: True if write was successful, False otherwise.
        """
        attempt = 0
        current_retry_delay = retry_delay
        
        while attempt < max_retries:
            # Skip connection check for write operations too
            # Let pymodbus handle connection issues

            try:
                if DEBUG_RAW_MODBUS_READS:
                    _LOGGER.debug("Modbus write: register=%d/0x%04X value=%s", register, register, value)
                try:
                    result = await asyncio.wait_for(
                        self.client.write_register(address=register, value=value, **{self._slave_kwarg: self.unit_id}),
                        timeout=self._timeout,
                    )
                finally:
                    # Inter-message spacing (see async_read_register).
                    if self._message_wait_sec and not self._is_shutting_down:
                        await asyncio.sleep(self._message_wait_sec)
                return not result.isError()

            except (ConnectionException, ModbusIOException, asyncio.TimeoutError):
                if self._is_shutting_down:
                    return False
                # Do NOT reconnect from here (see async_read_register). A fresh TCP
                # connection per failed write is what makes the v3 single-slot
                # firmware go permanently unresponsive. Fall through to the
                # same-connection retry; the coordinator owns reconnection.
                _LOGGER.debug("Connection error writing register %d (0x%04X)", register, register)

            except Exception as e:
                if not self._is_shutting_down:
                    _LOGGER.exception("Exception during modbus write at register %d (0x%04X) on attempt %d: %s", register, register, attempt + 1, e)

            # During shutdown, don't retry or reconnect - exit immediately to release the connection
            if self._is_shutting_down:
                return False

            attempt += 1
            if attempt < max_retries:
                # Exponential backoff with jitter
                await asyncio.sleep(current_retry_delay + _backoff_jitter(current_retry_delay))
                current_retry_delay = min(current_retry_delay * 2, 5.0)  # Cap at 5 seconds

        _LOGGER.debug(
            "Failed to write register %d (0x%04X) after %d attempts",
            register,
            register,
            max_retries,
        )
        return False
