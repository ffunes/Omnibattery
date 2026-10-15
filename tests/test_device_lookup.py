"""Device lookups avoid the async_get_device call Home Assistant 2026.10 deprecates."""

from types import SimpleNamespace

from custom_components.omnibattery.infra.device_lookup import get_entry_device


def test_uses_the_per_entry_lookup_when_available():
    device = SimpleNamespace(id="dev1")
    calls = []

    def deprecated(**kwargs):
        raise AssertionError("async_get_device is deprecated")

    def by_identifier(identifier, config_entry_id):
        calls.append((identifier, config_entry_id))
        return device

    registry = SimpleNamespace(
        async_get_device=deprecated, async_get_device_by_identifier=by_identifier
    )
    assert get_entry_device(registry, ("omnibattery", "10.0.0.1_502"), "entry1") is device
    assert calls == [(("omnibattery", "10.0.0.1_502"), "entry1")]


def test_falls_back_on_releases_without_the_per_entry_lookup():
    device = SimpleNamespace(id="dev1")
    registry = SimpleNamespace(
        async_get_device=lambda identifiers: device
        if identifiers == {("omnibattery", "10.0.0.1_502")}
        else None
    )
    assert get_entry_device(registry, ("omnibattery", "10.0.0.1_502"), "entry1") is device
    assert get_entry_device(registry, ("omnibattery", "other"), "entry1") is None
