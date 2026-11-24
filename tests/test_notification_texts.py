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
