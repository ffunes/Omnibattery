"""Helpers shared by the two "everything in drivers/" guardians (T1.3, T1.4).

Allowlist mechanics only: one line per occurrence, compared as a multiset,
regenerated with ``OMNIBATTERY_UPDATE_ALLOWLIST=1``.  Behaviour is exactly what
``test_no_brand_branches`` had before it was moved here.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Iterator

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "omnibattery"
ALLOWLIST_DIR = Path(__file__).resolve().parent / "guard_allowlists"
UPDATE_ENV = "OMNIBATTERY_UPDATE_ALLOWLIST"


def shared_python_files() -> Iterator[tuple[str, Path]]:
    """(path relative to the package, path) of every shared ``.py`` (no ``drivers/``)."""
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel.split("/")[0] == "drivers" or "__pycache__" in rel:
            continue
        yield rel, path


def read_allowlist(allowlist_file: Path) -> list[str]:
    if not allowlist_file.exists():
        return []
    lines = []
    for raw in allowlist_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def write_allowlist(allowlist_file: Path, header: str, found: list[str]) -> None:
    allowlist_file.parent.mkdir(parents=True, exist_ok=True)
    allowlist_file.write_text(header + "\n".join(sorted(found)) + "\n", encoding="utf-8", newline="\n")


def diff_against_allowlist(found: list[str], allowed: list[str]) -> tuple[list[str], list[str]]:
    """(new findings not allowed, obsolete allowlist entries), as exact lines."""
    f, a = Counter(found), Counter(allowed)
    return sorted((f - a).elements()), sorted((a - f).elements())
