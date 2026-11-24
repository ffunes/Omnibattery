"""Key and placeholder parity of the notification catalogue across languages."""
from __future__ import annotations

import importlib
from collections import Counter
from string import Formatter

import pytest

from custom_components.omnibattery.notifications import texts

_PKG = "custom_components.omnibattery.notifications.texts"
OTHERS = ("es", "ca", "de", "fr", "nl")


def _mod(lang):
    return importlib.import_module(f"{_PKG}.{lang}")


def _markers(text):
    """Multiset of (name, format_spec, conversion); positional ones included."""
    return Counter(
        (name, spec, conv)
        for _, name, spec, conv in Formatter().parse(text)
        if name is not None
    )


def _forms(entry):
    return entry.items() if isinstance(entry, dict) else ((None, entry),)


def test_languages_are_exactly_the_six_and_all_load():
    assert set(texts.LANGUAGES) == {"en", *OTHERS}
    assert len(texts.LANGUAGES) == 6
    assert set(texts._TEXTS) == set(texts.LANGUAGES)
    assert set(texts._DAYS) == set(texts.LANGUAGES)


@pytest.mark.parametrize("lang", ("en", *OTHERS))
def test_days_has_seven_non_empty_entries(lang):
    days = _mod(lang).DAYS
    assert len(days) == 7 and all(isinstance(d, str) and d.strip() for d in days), days


@pytest.mark.parametrize("lang", OTHERS)
def test_same_keys_as_english(lang):
    en_keys, keys = set(_mod("en").TEXTS), set(_mod(lang).TEXTS)
    assert keys == en_keys, (
        f"{lang}: missing {sorted(en_keys - keys)}, extra {sorted(keys - en_keys)}"
    )


@pytest.mark.parametrize("lang", OTHERS)
def test_same_shape_as_english(lang):
    en, tr = _mod("en").TEXTS, _mod(lang).TEXTS
    bad = []
    for key in en.keys() & tr.keys():
        if isinstance(en[key], dict):
            ok = isinstance(tr[key], dict) and set(tr[key]) == {"one", "other"}
        else:
            ok = isinstance(tr[key], str)
        if not ok:
            bad.append(f"{key}: en={type(en[key]).__name__} {lang}={tr[key]!r}")
    assert not bad, f"{lang}: shape differs from en: {bad}"


@pytest.mark.parametrize("lang", OTHERS)
def test_same_placeholders_as_english(lang):
    en, tr = _mod("en").TEXTS, _mod(lang).TEXTS
    bad = []
    for key in en.keys() & tr.keys():
        en_forms, tr_forms = dict(_forms(en[key])), dict(_forms(tr[key]))
        for form in en_forms.keys() & tr_forms.keys():
            want, got = _markers(en_forms[form]), _markers(tr_forms[form])
            if want != got:
                bad.append(
                    f"{key}[{form}]: missing {dict(want - got)}, extra {dict(got - want)}"
                )
    assert not bad, f"{lang}: placeholder mismatch:\n" + "\n".join(bad)


@pytest.mark.parametrize("lang", ("en", *OTHERS))
def test_no_positional_placeholders(lang):
    bad = [
        f"{key}[{form}]"
        for key, entry in _mod(lang).TEXTS.items()
        for form, s in _forms(entry)
        if any(n == "" or n.isdigit() for n in (m[0] for m in _markers(s)))
    ]
    assert not bad, f"{lang}: positional placeholders in {bad}"
