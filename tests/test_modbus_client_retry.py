"""Retry/backoff and no-self-reconnect behaviour of the Modbus read/write loops.

Exercises ``_read_raw`` and ``async_write_register`` against a scripted fake
pymodbus client (no hardware, no HA). These pin two deliberate properties that
had no coverage: the same-connection retry loop, and that a connection error
does NOT trigger a reconnect from inside the loop (the coordinator owns
reconnection; reconnecting here would storm the v3 single TCP slot, issue #361).

``retry_delay=0`` keeps the backoff sleeps at zero so the tests run instantly.
"""
from __future__ import annotations

import asyncio

import pytest
from pymodbus.exceptions import ConnectionException

from custom_components.omnibattery.infra.modbus_client import (
    MarstekModbusClient,
    _backoff_jitter,
)


class _Result:
    def __init__(self, registers=None, error=False):
        self.registers = registers
        self._error = error

    def isError(self):
        return self._error


class _FakeClient:
    """Scripted pymodbus stand-in: pops a queued result/exception per call."""

    def __init__(self, read_results=None, write_results=None):
        self._reads = list(read_results or [])
        self._writes = list(write_results or [])
        self.read_calls = 0
        self.write_calls = 0
        self.connect_calls = 0
        self.close_calls = 0

    async def read_holding_registers(self, address, count, **kwargs):
        self.read_calls += 1
        item = self._reads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def write_register(self, address, value, **kwargs):
        self.write_calls += 1
        item = self._writes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def connect(self):
        self.connect_calls += 1
        return True

    def close(self):
        self.close_calls += 1


def _client_with_fake(fake: _FakeClient, **kwargs) -> MarstekModbusClient:
    async def _build():
        c = MarstekModbusClient(host="192.168.1.50", port=502, message_wait_ms=0, **kwargs)
        c.client = fake
        c._message_wait_sec = 0.0
        return c
    return asyncio.run(_build())


# ---------------------------------------------------------------- read path
def test_read_success_returns_registers():
    fake = _FakeClient(read_results=[_Result([1, 2])])
    c = _client_with_fake(fake)
    regs = asyncio.run(c._read_raw(0x0000, 2, retry_delay=0))
    assert regs == [1, 2]
    assert fake.read_calls == 1


def test_read_retries_on_connection_error_then_succeeds():
    fake = _FakeClient(read_results=[ConnectionException("boom"), _Result([5])])
    c = _client_with_fake(fake)
    regs = asyncio.run(c._read_raw(0x0010, 1, retry_delay=0))
    assert regs == [5]
    assert fake.read_calls == 2


def test_read_timeout_is_retried():
    fake = _FakeClient(read_results=[asyncio.TimeoutError(), _Result([9])])
    c = _client_with_fake(fake)
    regs = asyncio.run(c._read_raw(0x0010, 1, retry_delay=0))
    assert regs == [9]
    assert fake.read_calls == 2


def test_read_error_result_is_retried():
    fake = _FakeClient(read_results=[_Result(error=True), _Result([7])])
    c = _client_with_fake(fake)
    regs = asyncio.run(c._read_raw(0x0010, 1, retry_delay=0))
    assert regs == [7]
    assert fake.read_calls == 2


def test_read_short_read_is_rejected_and_retried():
    # Every attempt returns one word for a two-word read -> incomplete, never accepted.
    fake = _FakeClient(read_results=[_Result([1]), _Result([1]), _Result([1])])
    c = _client_with_fake(fake)
    regs = asyncio.run(c._read_raw(0x0010, 2, max_retries=3, retry_delay=0))
    assert regs is None
    assert fake.read_calls == 3


def test_read_all_attempts_fail_returns_none():
    fake = _FakeClient(read_results=[ConnectionException()] * 3)
    c = _client_with_fake(fake)
    regs = asyncio.run(c._read_raw(0x0010, 1, max_retries=3, retry_delay=0))
    assert regs is None
    assert fake.read_calls == 3


def test_read_does_not_self_reconnect_on_failure():
    """A failed read must not tear down / rebuild the connection (issue #361)."""
    fake = _FakeClient(read_results=[ConnectionException()] * 3)
    c = _client_with_fake(fake)
    asyncio.run(c._read_raw(0x0010, 1, max_retries=3, retry_delay=0))
    assert fake.connect_calls == 0
    assert fake.close_calls == 0


def test_read_stops_immediately_when_shutting_down():
    fake = _FakeClient(read_results=[ConnectionException(), _Result([1])])
    c = _client_with_fake(fake)
    c._is_shutting_down = True
    regs = asyncio.run(c._read_raw(0x0010, 1, retry_delay=0))
    assert regs is None
    assert fake.read_calls == 1  # no retry once shutting down


def test_read_invalid_register_returns_none_without_io():
    fake = _FakeClient()
    c = _client_with_fake(fake)
    assert asyncio.run(c._read_raw(0x1_0000, 1, retry_delay=0)) is None
    assert fake.read_calls == 0


def test_read_invalid_count_returns_none_without_io():
    fake = _FakeClient()
    c = _client_with_fake(fake)
    assert asyncio.run(c._read_raw(0x0000, 200, retry_delay=0)) is None
    assert fake.read_calls == 0


# --------------------------------------------------------------- write path
def test_write_success_returns_true():
    fake = _FakeClient(write_results=[_Result(error=False)])
    c = _client_with_fake(fake)
    assert asyncio.run(c.async_write_register(0x2000, 1, retry_delay=0)) is True
    assert fake.write_calls == 1


def test_write_retries_on_connection_error_then_succeeds():
    fake = _FakeClient(write_results=[ConnectionException(), _Result(error=False)])
    c = _client_with_fake(fake)
    assert asyncio.run(c.async_write_register(0x2000, 1, retry_delay=0)) is True
    assert fake.write_calls == 2


def test_write_error_result_returns_false_without_retry():
    # A protocol-level error result is a definitive answer, not a comms failure.
    fake = _FakeClient(write_results=[_Result(error=True)])
    c = _client_with_fake(fake)
    assert asyncio.run(c.async_write_register(0x2000, 1, max_retries=3, retry_delay=0)) is False
    assert fake.write_calls == 1


def test_write_all_attempts_fail_returns_false_no_reconnect():
    fake = _FakeClient(write_results=[ConnectionException()] * 3)
    c = _client_with_fake(fake)
    assert asyncio.run(c.async_write_register(0x2000, 1, max_retries=3, retry_delay=0)) is False
    assert fake.write_calls == 3
    assert fake.connect_calls == 0
    assert fake.close_calls == 0


# ------------------------------------------------------------------- jitter
@pytest.mark.parametrize("delay", [0.1, 0.5, 1.0, 5.0])
def test_backoff_jitter_bounded_to_five_percent(delay):
    # Symmetric +/-10% * 0.5 amplitude -> magnitude never exceeds 5% of the delay,
    # so delay + jitter stays strictly positive.
    j = _backoff_jitter(delay)
    assert abs(j) <= 0.05 * delay
    assert delay + j > 0
