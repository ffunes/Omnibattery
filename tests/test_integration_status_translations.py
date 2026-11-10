"""Every Integration Status state is translated, not left in English (#824)."""

import json
from pathlib import Path

import pytest

TRANSLATIONS = Path(__file__).parent.parent / "custom_components" / "omnibattery" / "translations"

# "Standby" is the word Dutch and German use as well.
SAME_AS_ENGLISH = {"balanced"}


def _states(lang):
    data = json.loads((TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8"))
    return data["entity"]["sensor"]["integration_status"]["state"]


@pytest.mark.parametrize("lang", ["ca", "de", "es", "fr", "nl"])
def test_integration_status_states_are_translated(lang):
    english = _states("en")
    translated = _states(lang)

    untranslated = sorted(
        key
        for key, text in english.items()
        if key not in SAME_AS_ENGLISH and translated.get(key) == text
    )

    assert set(translated) == set(english)
    assert untranslated == []
