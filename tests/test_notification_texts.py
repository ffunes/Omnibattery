"""The notification text catalogue: loading, English fallback, plurals."""
from __future__ import annotations

from string import Formatter
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.notifications import texts
from custom_components.omnibattery.notifications.texts import en


def _hass(lang):
    return SimpleNamespace(config=SimpleNamespace(language=lang))


def _forms(entry):
    return entry.values() if isinstance(entry, dict) else (entry,)


def test_english_is_loaded():
    assert "en" in texts.LANGUAGES
    assert texts._TEXTS["en"] is en.TEXTS
    assert len(en.DAYS) == 7
    assert texts.day_name("en", 0) == "Mon"
    assert texts.day_name("en", 6) == "Sun"


def test_unsupported_language_and_missing_config_fall_back_to_english():
    assert texts.language(_hass("pt-BR")) == "en"
    assert texts.language(object()) == "en"
    assert texts.language(SimpleNamespace(config=SimpleNamespace(language=None))) == "en"
    assert texts.text(_hass("pt-BR"), "plan.horizon.until_midnight") == "until midnight"
    assert texts.text(object(), "plan.horizon.until_midnight") == "until midnight"


def test_language_uses_the_base_code_only_when_loaded(monkeypatch):
    monkeypatch.setitem(texts._TEXTS, "es", {})
    assert texts.language(_hass("es-ES")) == "es"
    assert texts.language(_hass("ES")) == "es"
    monkeypatch.delitem(texts._TEXTS, "es")
    assert texts.language(_hass("es-ES")) == "en"


def test_missing_key_in_a_language_falls_back_to_english(monkeypatch):
    monkeypatch.setitem(texts._TEXTS, "es", {"plan.solar.tomorrow": "Sol de mañana"})
    assert texts.translate("es", "plan.solar.tomorrow") == "Sol de mañana"
    assert texts.translate("es", "plan.horizon.until_midnight") == "until midnight"
    assert texts.translate("xx", "plan.horizon.until_midnight") == "until midnight"


def test_values_are_formatted():
    assert texts.translate("en", "plan.battery", soc=54.4, usable=3.456) == (
        "🔋 Battery: 54% (3.46 kWh usable)"
    )


def test_english_plurals():
    assert texts.translate("en", "plan.pricing.slots", count=1) == "1 slot"
    assert texts.translate("en", "plan.pricing.slots", count=2) == "2 slots"
    assert texts.translate("en", "plan.pricing.slots", count=0) == "0 slots"


def test_french_zero_is_singular(monkeypatch):
    monkeypatch.setitem(
        texts._TEXTS, "fr", {"k": {"one": "{count} un", "other": "{count} autres"}}
    )
    assert [texts.translate("fr", "k", count=n) for n in (0, 1, 2)] == [
        "0 un", "1 un", "2 autres",
    ]


def test_day_name_falls_back_to_english(monkeypatch):
    monkeypatch.setitem(texts._DAYS, "es", ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"))
    assert texts.day_name("es", 2) == "Mié"
    assert texts.day_name("xx", 2) == "Wed"


def _sample(field, spec):
    if field == "count":
        return 2
    return 1.5 if spec else "x"


@pytest.mark.parametrize("key", sorted(en.TEXTS))
def test_every_english_text_formats(key):
    for form in _forms(en.TEXTS[key]):
        values = {}
        for _lit, field, spec, _conv in Formatter().parse(form):
            if field is None:
                continue
            assert field and field.isidentifier(), f"{key}: positional marker {field!r}"
            values[field] = _sample(field, spec)
        form.format(**values)
    if isinstance(en.TEXTS[key], dict):
        assert set(en.TEXTS[key]) == {"one", "other"}


# --- task 4.3: the other emitters read the catalogue -------------------------

_ES = {
    "switch.override.title": "Carga predictiva: desactivada",
    "switch.manual.title": "Modo manual: activo",
    "alarm.title.fault": "FALLO: {device}",
    "alarm.new_faults": "Nuevos fallos:",
    "balance.title": "{icon} Celdas: {name}",
    "balance.high": "Delta: {delta:.0f} mV alto.",
    "weekly.title": "Carga semanal: poco sol",
}


def _es(monkeypatch):
    monkeypatch.setitem(texts._TEXTS, "es", _ES)


def _hass_es():
    return SimpleNamespace(config=SimpleNamespace(language="es"))


def test_switch_notices_read_the_catalogue(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from custom_components.omnibattery import switch as switch_mod

    _es(monkeypatch)
    hass = _hass_es()
    hass.config_entries = MagicMock()
    notify = AsyncMock()
    with patch.object(switch_mod, "async_notify", notify):
        override = switch_mod.PredictiveChargingSwitch.__new__(switch_mod.PredictiveChargingSwitch)
        override.hass = hass
        override.entry = SimpleNamespace(data={})
        override.controller = MagicMock(grid_charging_active=False)
        override.async_write_ha_state = MagicMock()
        asyncio.run(override.async_turn_off())
        manual = switch_mod.ManualModeSwitch.__new__(switch_mod.ManualModeSwitch)
        manual.hass = hass
        manual.entry = SimpleNamespace(data={})
        manual.controller = SimpleNamespace(coordinators=[])
        manual.async_write_ha_state = MagicMock()
        asyncio.run(manual.async_turn_on())
    (first, second) = [c.args for c in notify.call_args_list]
    assert first[3] == "Carga predictiva: desactivada"
    # Not in the Spanish stub: falls back to the exact English text.
    assert first[4] == en.TEXTS["switch.override.disabled"]
    assert second[3] == "Modo manual: activo"
    assert second[4] == en.TEXTS["switch.manual.message"]


def test_alarm_notice_reads_the_catalogue_and_keeps_driver_labels(monkeypatch):
    import asyncio

    from custom_components.omnibattery.drivers.base import DriverAlarm
    from custom_components.omnibattery.infra.alarm_notifier import AlarmNotifier

    _es(monkeypatch)
    calls = []

    class _Services:
        async def async_call(self, domain, service, data):
            calls.append(data)

    hass = _hass_es()
    hass.services = _Services()
    alarm = DriverAlarm(kind="fault", code=1, description="Over temperature")
    asyncio.run(AlarmNotifier(hass, "Bat 1").check([alarm]))
    (data,) = calls
    assert data["title"] == "FALLO: Bat 1"
    assert "Nuevos fallos:" in data["message"]
    assert "Over temperature" in data["message"]
    # Not in the stub: English.
    assert data["message"].startswith(en.TEXTS["alarm.header.fault"].format(device="Bat 1"))


def test_balance_notice_reads_the_catalogue(monkeypatch):
    import asyncio

    from custom_components.omnibattery.drivers.base import BalanceBaseline
    from custom_components.omnibattery.tracking.balance_monitor import BalanceMonitor

    _es(monkeypatch)
    sent = []

    async def _notify(notification_id, title, message):
        sent.append((title, message))

    monitor = BalanceMonitor.__new__(BalanceMonitor)
    monitor._hass = _hass_es()
    monitor._notify = _notify
    monitor._controller = SimpleNamespace(
        _create_entry_background_task=lambda coro, name: asyncio.run(coro)
    )
    baseline = BalanceBaseline(offset_mv=0, yellow_mv=10, orange_mv=20, red_mv=30)
    bat = {"consecutive_red": 0}
    issues: list[str] = []
    status, severe = monitor._evaluate(50.0, bat, issues, baseline)
    monitor._maybe_notify("host", "Bat 1", bat, issues, severe)
    assert status == "red"
    assert sent == [("⚠️ Celdas: Bat 1", "• Delta: 50 mV alto.")]


def test_weekly_notice_reads_the_catalogue(monkeypatch):
    from unittest.mock import MagicMock, patch

    from tests.test_weekly_solar_outlook import _BATTERY, _GATEWAY_PN, _NOON, _MOD, _setup

    _es(monkeypatch)
    manager, ctrl = _setup()
    ctrl.hass = _hass_es()
    notify = MagicMock()
    forecast = SimpleNamespace(source="remaining", remaining_kwh=2.5)
    with (
        patch(f"{_MOD}.dt_util.now", return_value=_NOON),
        patch(f"{_MOD}.read_remaining_solar_kwh", return_value=forecast),
        patch(_GATEWAY_PN, notify),
    ):
        manager.update_solar_outlook([_BATTERY])
    kwargs = notify.async_create.call_args.kwargs
    assert kwargs["title"] == "Carga semanal: poco sol"
    body = notify.async_create.call_args.args[1]
    assert body == en.TEXTS["weekly.not_enough_solar"].format(net=1.5, needed=2.0)
