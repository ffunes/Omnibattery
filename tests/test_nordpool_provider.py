"""Tests for official-vs-HACS Nord Pool source detection."""
from types import SimpleNamespace

from custom_components.omnibattery.pricing import nordpool


class _Services:
    def has_service(self, domain, service):
        return domain == "nordpool" and service == "get_prices_for_date"


def test_resolve_official_source_uses_entity_config_entry_and_device_area(monkeypatch):
    entity = SimpleNamespace(
        platform="nordpool",
        config_entry_id="entry-id",
        device_id="device-id",
    )
    device = SimpleNamespace(identifiers={("nordpool", "DE-LU")})
    monkeypatch.setattr(
        nordpool.er,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=lambda _entity_id: entity),
    )
    monkeypatch.setattr(
        nordpool.dr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=lambda _device_id: device),
    )
    hass = SimpleNamespace(services=_Services())

    source = nordpool.resolve_official_nordpool_source(
        hass,
        "sensor.nord_pool_de_lu_current_price",
    )

    assert source == nordpool.OfficialNordPoolSource("entry-id", "DE-LU")
    assert nordpool.is_official_nordpool_sensor(
        hass,
        "sensor.nord_pool_de_lu_current_price",
        {},
    )


def test_hacs_raw_today_takes_precedence_without_registry_lookup(monkeypatch):
    monkeypatch.setattr(
        nordpool.er,
        "async_get",
        lambda _hass: (_ for _ in ()).throw(AssertionError("registry should not be read")),
    )
    hass = SimpleNamespace(services=_Services())

    assert not nordpool.is_official_nordpool_sensor(
        hass,
        "sensor.nordpool_kwh_es_eur",
        {"raw_today": []},
    )
