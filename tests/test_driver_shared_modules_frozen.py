"""Brand knowledge inside the shared modules of ``drivers/`` is frozen and listed.

``tests/test_no_brand_branches.py`` skips all of ``drivers/``: a brand's own files
may know their brand. The modules every brand shares (``base.py`` and any other
module that is not a brand's own file) may not, yet the refactor had to keep a
few behaviours that today follow the stored Marstek ``battery_version`` for any
brand (stale keys left by a brand change, plan section 8). Those live in
``base.py`` and are listed in ``guard_allowlists/drivers_shared_frozen.txt`` with
the section 8 row each one freezes. The list may only shrink.

Scanned: brand/model findings (same scanner as the brand guardian) plus string
literals naming a Marstek version (``SUPPORTED_VERSIONS``) or a brand's own
connection key (``serial_port``…), and references to ``*_VERSIONS`` constants. Not scanned: a brand's own files (name starts with a
registered brand), ``registry.py`` (the one registration point) and
``__init__.py`` (re-exports for tests, row #30).
"""
from __future__ import annotations

import ast
import os
import re
from collections import Counter

from custom_components.omnibattery.const import SUPPORTED_VERSIONS
from custom_components.omnibattery.drivers.registry import DRIVERS
from tests import _guard_common
from tests._guard_common import PACKAGE, UPDATE_ENV
from tests.test_no_brand_branches import _Scanner, scan_python_source

FROZEN_FILE = _guard_common.ALLOWLIST_DIR / "drivers_shared_frozen.txt"
_VERSIONS = frozenset(SUPPORTED_VERSIONS)
# Config keys that belong to one brand's connection (CONF_SERIAL_PORT: Marstek RTU).
_BRAND_CONFIG_KEYS = frozenset({"serial_port", "rs485_gateway", "ems_version"})
_VERSIONS_NAME_RE = re.compile(r"^[A-Z_]*VERSIONS$")


def shared_driver_modules():
    for path in sorted((PACKAGE / "drivers").glob("*.py")):
        stem = path.stem
        if stem in ("registry", "__init__") or any(stem.startswith(b) for b in DRIVERS):
            continue
        yield path


class _VersionScanner(_Scanner):
    """Adds version literals and *_VERSIONS references to the brand findings."""

    def visit_Constant(self, node):  # noqa: N802
        if isinstance(node.value, str) and node.value in _VERSIONS:
            self._add("version", node.value)
        if isinstance(node.value, str) and node.value in _BRAND_CONFIG_KEYS:
            self._add("version", f"config_key {node.value}")

    def visit_Name(self, node):  # noqa: N802
        if _VERSIONS_NAME_RE.match(node.id):
            self._add("version", node.id)

    def visit_alias(self, node):  # noqa: N802
        if _VERSIONS_NAME_RE.match(node.name):
            self._add("version", node.name)


def scan() -> list[str]:
    found: list[str] = []
    for path in shared_driver_modules():
        rel = path.relative_to(PACKAGE).as_posix()
        source = path.read_text(encoding="utf-8")
        found += scan_python_source(source, rel)
        scanner = _VersionScanner(rel)
        scanner.visit(ast.parse(source))
        found += [f for f in scanner.found if ":version:" in f]
    return sorted(found)


def read_frozen() -> list[str]:
    entries = []
    for raw in FROZEN_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.split("  #", 1)[0].strip()
        if line and not line.startswith("#"):
            entries.append(line)
    return entries


def test_shared_driver_modules_only_hold_the_frozen_list():
    found = scan()
    if os.environ.get(UPDATE_ENV) == "1" and not FROZEN_FILE.exists():
        FROZEN_FILE.write_text("\n".join(found) + "\n", encoding="utf-8", newline="\n")
        return
    f, a = Counter(found), Counter(read_frozen())
    new, gone = sorted((f - a).elements()), sorted((a - f).elements())
    assert not new, "Brand/version knowledge in a shared drivers/ module:\n  " + "\n  ".join(new)
    assert not gone, f"Delete these lines from {FROZEN_FILE.name}:\n  " + "\n  ".join(gone)
