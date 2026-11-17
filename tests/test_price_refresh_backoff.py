"""Failed price-service refreshes back off instead of retrying every cycle.

The control loop calls the refresh on every 2.5 s cycle, inside the control
lock. A failing Tibber / Nord Pool / EnergyZero call was retried on every one
of them (with a WARNING each time), and a hung call stalled battery control.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

import custom_components.omnibattery.pricing.engine as engine_module
from custom_components.omnibattery.const import PRICE_INTEGRATION_TIBBER
from custom_components.omnibattery.pricing.engine import PricingManager


class _Services:
    def __init__(self, outcome):
        self.calls = 0
        self.outcome = outcome

    def has_service(self, domain, service):
        return domain == "tibber"

    async def async_call(self, domain, service, data, blocking=True, return_response=True):
        self.calls += 1
        result = self.outcome()
        if isinstance(result, Exception):
            raise result
        return result


def _tibber_payload():
    return {
        "prices": {
            "home": [
                {"start_time": "2026-11-09T10:00:00+01:00", "price": 0.2},
                {"start_time": "2026-11-09T11:00:00+01:00", "price": 0.3},
            ]
        }
    }


@pytest.fixture
def clock(monkeypatch):
    state = {"t": 1000.0}
    monkeypatch.setattr(engine_module, "monotonic", lambda: state["t"])
    return state


def _manager(services):
    controller = SimpleNamespace(
        price_integration_type=PRICE_INTEGRATION_TIBBER,
        _tibber_price_slots=[],
        _tibber_prices_fetched_at=None,
    )
    return PricingManager(SimpleNamespace(services=services), controller), controller


def test_failed_refresh_is_not_retried_every_cycle(clock):
    services = _Services(lambda: RuntimeError("cloud down"))
    manager, _ = _manager(services)

    for _ in range(10):  # ten control cycles inside the first minute
        asyncio.run(manager._maybe_refresh_tibber_prices())

    assert services.calls == 1


def test_backoff_grows_and_retries_after_it_expires(clock):
    services = _Services(lambda: RuntimeError("cloud down"))
    manager, _ = _manager(services)

    asyncio.run(manager._maybe_refresh_tibber_prices())
    clock["t"] += 61
    asyncio.run(manager._maybe_refresh_tibber_prices())
    assert services.calls == 2

    clock["t"] += 61  # second failure waits 5 min, not 1
    asyncio.run(manager._maybe_refresh_tibber_prices())
    assert services.calls == 2
    clock["t"] += 300
    asyncio.run(manager._maybe_refresh_tibber_prices())
    assert services.calls == 3


def test_empty_response_also_backs_off(clock):
    services = _Services(lambda: {"prices": {}})
    manager, _ = _manager(services)

    asyncio.run(manager._maybe_refresh_tibber_prices())
    asyncio.run(manager._maybe_refresh_tibber_prices())

    assert services.calls == 1


def test_forced_refresh_ignores_the_backoff(clock):
    services = _Services(lambda: RuntimeError("cloud down"))
    manager, _ = _manager(services)

    asyncio.run(manager._maybe_refresh_tibber_prices())
    asyncio.run(manager._maybe_refresh_tibber_prices(force=True))

    assert services.calls == 2


def test_success_clears_the_backoff(clock):
    outcomes = iter([RuntimeError("blip"), _tibber_payload()])
    services = _Services(lambda: next(outcomes))
    manager, controller = _manager(services)

    asyncio.run(manager._maybe_refresh_tibber_prices())
    clock["t"] += 61
    asyncio.run(manager._maybe_refresh_tibber_prices())

    assert controller._tibber_price_slots
    assert manager._refresh_backoff_active("tibber") is False


def test_a_hung_service_call_times_out(clock, monkeypatch):
    monkeypatch.setattr(PricingManager, "_REFRESH_TIMEOUT_S", 0.01)

    class _Hung(_Services):
        async def async_call(self, *args, **kwargs):
            self.calls += 1
            await asyncio.sleep(5)

    services = _Hung(None)
    manager, _ = _manager(services)

    started = datetime.now()
    asyncio.run(manager._maybe_refresh_tibber_prices())

    assert (datetime.now() - started).total_seconds() < 2
    assert manager._refresh_backoff_active("tibber") is True
