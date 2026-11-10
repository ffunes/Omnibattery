"""A refused Anker connection closes its client (no leak per reconnect)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import custom_components.omnibattery.infra.anker_modbus_client as anker_client


def _client_factory(connect_result):
    created = []

    def factory(**_kwargs):
        client = MagicMock()
        client.connect = AsyncMock(return_value=connect_result)
        client.close = MagicMock(return_value=None)
        created.append(client)
        return client

    return factory, created


def test_a_refused_connection_closes_its_client(monkeypatch):
    factory, created = _client_factory(False)
    monkeypatch.setattr(anker_client, "AsyncModbusTcpClient", factory)
    client = anker_client.AnkerModbusClient("1.2.3.4", 502, 1)

    assert asyncio.run(client.async_connect()) is False

    created[0].close.assert_called_once()
    assert client.connected is False


def test_a_successful_connection_keeps_its_client(monkeypatch):
    factory, created = _client_factory(True)
    monkeypatch.setattr(anker_client, "AsyncModbusTcpClient", factory)
    client = anker_client.AnkerModbusClient("1.2.3.4", 502, 1)

    assert asyncio.run(client.async_connect()) is True

    created[0].close.assert_not_called()
