"""EnergyZero provider contract and downstream pricing regression tests."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.pricing import calculations, energyzero
from custom_components.omnibattery.pricing.engine import PricingManager


def entry(start="2999-01-01T12:00:00", end="2999-01-01T12:15:00", price=0.245):
    return {"start": start, "end": end, "price": price}


@pytest.mark.parametrize("minutes", [15, 60])
def test_explicit_intervals_and_price_scale(minutes):
    start = datetime(2999, 1, 1, 12)
    slots = calculations.parse_energyzero_prices(
        {"prices": [entry(start.isoformat(), (start + timedelta(minutes=minutes)).isoformat())]}
    )
    assert slots[0].price == pytest.approx(0.245)
    assert slots[0].end - slots[0].start == timedelta(minutes=minutes)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "bad",
        {},
        entry(price=None),
        entry(price="nan"),
        entry(price="inf"),
        entry(price=True),
        entry(end=None),
        entry(end="2999-01-01T11:00:00"),
    ],
)
def test_bad_entries_do_not_discard_valid_prices(bad):
    assert len(calculations.parse_energyzero_prices({"prices": [bad, entry()]})) == 1


@pytest.mark.parametrize("prices", [None, {}, "[]", 123])
def test_invalid_prices(prices):
    assert calculations.parse_energyzero_prices({"prices": prices}) == []


def test_sort_deduplicate_and_preserve_negative_prices():
    early = entry(price=-0.05)
    late = entry("2999-01-01T13:00:00", "2999-01-01T13:15:00", 0)
    slots = calculations.parse_energyzero_prices({"prices": [late, early, early]})
    assert [slot.price for slot in slots] == [-0.05, 0]
    assert calculations.select_cheapest_slots_by_duration(slots, 0.25, None) == [slots[0]]


def test_local_timezone_conversion_and_clock_rollback(monkeypatch):
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", ZoneInfo("Europe/Amsterdam"))
    slot = calculations.parse_energyzero_prices(
        {"prices": [entry("2026-09-10T15:00:00Z", "2026-09-10T15:15:00Z")]}
    )[0]
    assert slot.start == datetime(2026, 9, 10, 17)
    assert slot.end == datetime(2026, 9, 10, 17, 15)
    assert calculations.parse_energyzero_prices(
        {"prices": [entry("2026-10-25T02:45:00+02:00", "2026-10-25T02:00:00+01:00")]}
    ) == []


def test_source_resolution_uses_selected_electricity_price_type_and_interval(monkeypatch):
    market = SimpleNamespace(
        platform="energyzero",
        config_entry_id="entry-id",
        unique_id="entry-id_today_energy_current_hour_price",
    )
    all_in = SimpleNamespace(
        platform="energyzero",
        config_entry_id="entry-id",
        unique_id="entry-id_today_energy_all_in_current_price",
    )
    entities = {"sensor.market": market, "sensor.all_in": all_in}
    monkeypatch.setattr(
        energyzero.er,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=entities.get),
    )
    entry_record = SimpleNamespace(options={"electricity_price_interval": "quarter_hourly"})
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_get_entry=lambda _entry_id: entry_record),
        services=SimpleNamespace(
            has_service=lambda domain, service: (domain, service)
            == ("energyzero", "get_energy_prices")
        ),
    )

    assert energyzero.resolve_energyzero_source(hass, "sensor.market") == energyzero.EnergyZeroSource(
        "entry-id", "market", "quarter"
    )
    assert energyzero.resolve_energyzero_source(hass, "sensor.all_in").price_type == "all_in"
    assert energyzero.is_energyzero_sensor(hass, "sensor.market")

    from custom_components.omnibattery.config_flow import (
        _price_integration_export_options,
        _price_integration_options,
        _validate_price_sensor,
    )

    hass.states = SimpleNamespace(
        get=lambda _entity_id: SimpleNamespace(attributes={})
    )
    assert "energyzero" in _price_integration_options()
    assert "energyzero" in _price_integration_export_options()
    assert _validate_price_sensor(hass, "sensor.market", "energyzero") is None


class _FakeEnergyZeroServices:
    def __init__(self, fail_tomorrow=False, fail_all=False):
        self.calls = []
        self.fail_tomorrow = fail_tomorrow
        self.fail_all = fail_all

    def has_service(self, domain, service):
        return domain == "energyzero" and service == "get_energy_prices"

    async def async_call(self, domain, service, data, blocking=True, return_response=True):
        self.calls.append((domain, service, data))
        if self.fail_all or (self.fail_tomorrow and len(self.calls) == 2):
            raise RuntimeError("prices not published")
        return {"prices": [entry()]}


@pytest.mark.parametrize("fail_tomorrow", [False, True])
def test_service_refresh_requests_both_days_and_keeps_today(fail_tomorrow):
    source = energyzero.EnergyZeroSource("entry-id", "market", "quarter")
    services = _FakeEnergyZeroServices(fail_tomorrow)
    controller = SimpleNamespace(
        price_sensor="sensor.energyzero_today_energy_current_price",
        export_price_sensor=None,
        _energyzero_price_slots={},
        _energyzero_prices_fetched_at={},
    )
    manager = PricingManager(SimpleNamespace(services=services), controller)
    manager._energyzero_source = lambda _entity_id: source

    asyncio.run(manager._maybe_refresh_energyzero_prices(force=True))

    assert len(services.calls) == 2
    assert [call[2]["price_type"] for call in services.calls] == ["market", "market"]
    assert [call[2]["interval"] for call in services.calls] == ["quarter", "quarter"]
    assert len(manager._energyzero_price_slots(controller.price_sensor)) == 1


@pytest.mark.parametrize("sensor_state", ["0.2", "unavailable"])
@pytest.mark.parametrize("explicit_export_type", [None, "energyzero"])
def test_independent_energyzero_export_curve_uses_cached_service_slots(
    explicit_export_type, sensor_state
):
    import_source = energyzero.EnergyZeroSource("entry-id", "market", "hour")
    export_source = energyzero.EnergyZeroSource("entry-id", "all_in", "hour")
    import_key = ("entry-id", "market", "hour")
    export_key = ("entry-id", "all_in", "hour")
    controller = SimpleNamespace(
        price_integration_type="energyzero",
        price_sensor="sensor.import",
        export_price_sensor="sensor.export",
        export_price_integration_type=explicit_export_type,
        _price_data_status="ok",
        _energyzero_price_slots={
            import_key: calculations.parse_energyzero_prices(
                {"prices": [entry(price=0.245)]}
            ),
            export_key: calculations.parse_energyzero_prices(
                {"prices": [entry(price=0.1)]}
            ),
        },
        _energyzero_prices_fetched_at={},
    )
    state = SimpleNamespace(state=sensor_state, attributes={})
    hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda _entity_id: state),
        services=SimpleNamespace(has_service=lambda *_args: True),
    )
    manager = PricingManager(hass, controller)
    manager._energyzero_source = (
        lambda entity_id: import_source if entity_id == "sensor.import" else export_source
    )

    horizon_end = datetime(3000, 1, 1)
    assert manager.get_future_export_price_slots(horizon_end)[0].price == pytest.approx(0.1)
    assert manager.get_future_price_slots(horizon_end)[0].price == pytest.approx(0.245)
    assert controller._price_data_status.startswith("ok")

def test_total_service_failure_warns_and_caches_nothing(caplog):
    source = energyzero.EnergyZeroSource("entry-id", "market", "hour")
    services = _FakeEnergyZeroServices(fail_all=True)
    controller = SimpleNamespace(
        price_sensor="sensor.energyzero_today_energy_current_price",
        export_price_sensor=None,
        _energyzero_price_slots={},
        _energyzero_prices_fetched_at={},
    )
    manager = PricingManager(SimpleNamespace(services=services), controller)
    manager._energyzero_source = lambda _entity_id: source

    asyncio.run(manager._maybe_refresh_energyzero_prices(force=True))

    assert manager._energyzero_price_slots(controller.price_sensor) == []
    assert "returned no usable slots" in caplog.text


def test_config_flow_reports_missing_energyzero_service():
    from custom_components.omnibattery.config_flow import _validate_price_sensor

    hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda _entity_id: SimpleNamespace(attributes={})),
        services=SimpleNamespace(has_service=lambda *_args: False),
    )
    assert _validate_price_sensor(hass, "sensor.market", "energyzero") == "energyzero_unavailable"
