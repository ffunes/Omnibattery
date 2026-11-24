"""Notification texts per language, with English as the fallback."""
from __future__ import annotations

import importlib

LANGUAGES = ("en", "es", "ca", "de", "fr", "nl")

# ponytail: loaded at import, never at run time: Home Assistant warns about
# import_module inside the event loop. A language without a file is skipped.
_TEXTS: dict[str, dict] = {}
_DAYS: dict[str, tuple[str, ...]] = {}
for _lang in LANGUAGES:
    try:
        _module = importlib.import_module(f".{_lang}", __name__)
    except ModuleNotFoundError:
        continue
    _TEXTS[_lang] = _module.TEXTS
    _DAYS[_lang] = _module.DAYS


def language(hass) -> str:
    """The server language when it has a catalogue, else ``en``."""
    lang = str(getattr(getattr(hass, "config", None), "language", None) or "en")
    lang = lang.split("-")[0].lower()
    return lang if lang in _TEXTS else "en"


def translate(lang: str, key: str, /, **values) -> str:
    """The text for ``key`` in ``lang`` (English if missing), formatted."""
    entry = _TEXTS.get(lang, {}).get(key)
    if entry is None:
        entry = _TEXTS["en"][key]
    if isinstance(entry, dict):
        count = values["count"]
        one = count in (0, 1) if lang == "fr" else count == 1
        entry = entry["one" if one else "other"]
    return entry.format(**values)


def text(hass, key: str, /, **values) -> str:
    """The text for ``key`` in the server language."""
    return translate(language(hass), key, **values)


def day_name(lang: str, weekday: int) -> str:
    """Short day name, Monday = 0."""
    return _DAYS.get(lang, _DAYS["en"])[weekday]
