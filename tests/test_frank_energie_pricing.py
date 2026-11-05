"""Frank Energie provider contract and downstream pricing regression tests."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.pricing import calculations
from custom_components.omnibattery.pricing.engine import PricingManager


def entry(start="2999-01-01T12:00:00", end="2999-01-01T12:15:00", price=0.245):
    return {"from": start, "till": end, "price": price}


@pytest.mark.parametrize("minutes", [15, 60])
def test_explicit_intervals_and_price_scale(minutes):
    start = datetime(2999, 1, 1, 12)
    slots = calculations.parse_frank_energie_prices({"prices": [entry(start, start + timedelta(minutes=minutes))]})
    assert slots[0].price == pytest.approx(0.245)
    assert slots[0].end - slots[0].start == timedelta(minutes=minutes)


@pytest.mark.parametrize("bad", [None, "bad", {}, entry(price=None), entry(price="nan"), entry(price="inf"), entry(price=True), entry(end=None), entry(end="2999-01-01T11:00:00")])
def test_bad_entries_do_not_discard_valid_prices(bad):
    assert len(calculations.parse_frank_energie_prices({"prices": [bad, entry()]})) == 1


@pytest.mark.parametrize("prices", [None, {}, "[]", 123])
def test_invalid_prices(prices):
    assert calculations.parse_frank_energie_prices({"prices": prices}) == []


def test_sort_deduplicate_and_keep_negative_price():
    early = entry(price=-0.05)
    late = entry("2999-01-01T13:00:00", "2999-01-01T13:15:00", 0)
    slots = calculations.parse_frank_energie_prices({"prices": [late, early, early]})
    assert [s.price for s in slots] == [-0.05, 0]
    selected = calculations.select_cheapest_slots_by_duration(slots, 0.25, None)
    assert selected == [slots[0]]


def test_local_timezone_conversion(monkeypatch):
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", ZoneInfo("Europe/Amsterdam"))
    slot = calculations.parse_frank_energie_prices({"prices": [entry("2026-09-28T10:00:00+00:00", "2026-09-28T10:15:00+00:00")]})[0]
    assert slot.start == datetime(2026, 9, 28, 12)
    assert slot.end == datetime(2026, 9, 28, 12, 15)


def test_live_hourly_prices_attribute(monkeypatch):
    """Real hourly sensor data observed on 2026-10-26, including the clock change."""
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", ZoneInfo("Europe/Amsterdam"))
    prices = [
        {"from": "2026-10-25T00:00:00+02:00", "till": "2026-10-25T01:00:00+02:00", "price": 0.136},
        {"from": "2026-10-25T01:00:00+02:00", "till": "2026-10-25T02:00:00+02:00", "price": 0.133},
        {"from": "2026-10-25T02:00:00+02:00", "till": "2026-10-25T02:00:00+01:00", "price": 0.127},
        {"from": "2026-10-25T02:00:00+01:00", "till": "2026-10-25T03:00:00+01:00", "price": 0.125},
        {"from": "2026-10-27T23:00:00+01:00", "till": "2026-10-28T00:00:00+01:00", "price": 0.393},
    ]
    slots = calculations.parse_prices_for_integration("frank_energie", {"prices": prices, "unit_of_measurement": "€/kWh"})
    assert [s.price for s in slots] == [0.136, 0.133, 0.125, 0.393]
    assert slots[0].start == datetime(2026, 10, 25, 0)
    assert slots[2].end == datetime(2026, 10, 25, 3)
    assert slots[-1].end == datetime(2026, 10, 28, 0)


def test_clock_rollback_never_emits_negative_duration(monkeypatch):
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", ZoneInfo("Europe/Amsterdam"))
    assert calculations.parse_frank_energie_prices({"prices": [entry("2026-10-25T02:45:00+02:00", "2026-10-25T02:00:00+01:00")]}) == []


def test_engine_dispatch_and_current_state():
    controller = SimpleNamespace(price_integration_type="frank_energie", price_sensor="sensor.tariff")
    state = SimpleNamespace(state="0.245", attributes={"prices": [entry()]})
    manager = PricingManager(SimpleNamespace(states=SimpleNamespace(get=lambda _: state)), controller)
    assert manager._get_current_price() == pytest.approx(0.245)
    slots = manager._parse_price_data(horizon_end=datetime(2999, 1, 2))
    assert len(slots) == 1
    assert slots[0].price == pytest.approx(0.245)


def test_shared_dispatch_and_stringified_prices():
    attrs = {"prices": [entry()]}
    assert calculations.parse_prices_for_integration("frank_energie", attrs) == calculations.parse_frank_energie_prices(attrs)
    assert calculations.stringified_price_attrs("frank_energie", {"prices": "[]"}) == ["prices"]


@pytest.mark.parametrize("explicit_export_type", [None, "frank_energie"])
def test_independent_export_curve_uses_shared_dispatch(explicit_export_type):
    controller = SimpleNamespace(
        price_integration_type="frank_energie", price_sensor="sensor.import",
        export_price_sensor="sensor.export", export_price_integration_type=explicit_export_type,
        zonneplan_export_bonus_enabled=False,
        _price_data_status="ok",
    )
    states = {
        "sensor.import": SimpleNamespace(state="0.245", attributes={"prices": [entry()]}),
        "sensor.export": SimpleNamespace(state="0.1", attributes={"prices": [entry(price=0.1)]}),
    }
    manager = PricingManager(SimpleNamespace(states=SimpleNamespace(get=states.get)), controller)
    assert manager.get_future_export_price_slots(datetime(2999, 1, 2))[0].price == pytest.approx(0.1)
    assert controller._price_data_status == "ok"
    assert manager.get_future_price_slots(datetime(2999, 1, 2))[0].price == pytest.approx(0.245)


def test_missing_export_sensor_falls_back_to_import():
    controller = SimpleNamespace(
        price_integration_type="frank_energie",
        price_sensor="sensor.import",
        export_price_sensor=None,
        export_price_integration_type="frank_energie",
        zonneplan_export_bonus_enabled=False,
        _price_data_status="ok",
    )
    state = SimpleNamespace(state="0.245", attributes={"prices": [entry()]})
    manager = PricingManager(SimpleNamespace(states=SimpleNamespace(get=lambda _: state)), controller)
    import_slots = manager.get_future_price_slots(datetime(2999, 1, 2))
    export_slots = manager.get_future_export_price_slots(datetime(2999, 1, 2))
    assert export_slots == import_slots
    assert export_slots[0].price == pytest.approx(0.245)


def test_shared_export_selector_and_validation():
    from custom_components.omnibattery.config_flow import _price_integration_export_options, _validate_price_sensor

    assert "frank_energie" in _price_integration_export_options()
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: SimpleNamespace(attributes={"prices": [entry()]})))
    assert _validate_price_sensor(hass, "sensor.export", "frank_energie", allow_service_cache=False) is None


def test_parse_error_logs_offending_entry(caplog):
    bad = entry(price="invalid-amount")
    with caplog.at_level("DEBUG"):
        assert calculations.parse_frank_energie_prices({"prices": [bad]}) == []
    assert "invalid-amount" in caplog.text


@pytest.mark.parametrize("options", [False, True])
@pytest.mark.parametrize("valid", [False, True])
async def test_setup_and_options_validate_and_save_frank_energie(options, valid):
    from custom_components.omnibattery.config_flow import MarstekVenusConfigFlow, OptionsFlowHandler

    config_entry = SimpleNamespace(entry_id="test", data={}, options={})
    flow = OptionsFlowHandler(config_entry) if options else MarstekVenusConfigFlow()
    if options:
        config_entry.data["price_integration_type"] = "frank_energie"
    else:
        flow.config_data["price_integration_type"] = "frank_energie"
    flow.handler = "test"
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda _: config_entry,
            async_update_entry=lambda *args, **kwargs: None,
            async_reload=AsyncMock(),
        ),
        states=SimpleNamespace(get=lambda _: SimpleNamespace(
            attributes={"prices": [entry()]} if valid else {"prices": [{"garbage": 1}]},
        )),
    )
    form = await flow.async_step_dynamic_pricing_config()
    selector = next(value for marker, value in form["data_schema"].schema.items() if marker.schema == "price_integration_type")
    assert "frank_energie" in selector.config["options"]
    result = await flow.async_step_dynamic_pricing_config(
        {"price_integration_type": "frank_energie", "price_sensor": "sensor.tariff"}
    )
    if valid:
        assert flow.config_data["price_integration_type"] == "frank_energie"
        assert flow.config_data["price_sensor"] == "sensor.tariff"
        assert result.get("errors", {}) == {}
    else:
        assert result["errors"]["price_sensor"] == "no_price_data"
