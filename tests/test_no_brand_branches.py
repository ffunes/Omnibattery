"""Guardian: shared code must not know brand names or model versions (T1.3).

Goal of the "everything in drivers/" refactor: adding a brand touches only
``drivers/``.  This test scans every ``.py`` file of the integration except
``drivers/`` (plus ``frontend/*.js``) and reports every place that names a
brand or a model, compares/looks up ``brand`` or ``battery_version``, or
imports a brand module.  Today's violations live in
``tests/guard_allowlists/no_brand_branches.txt``; the list may only SHRINK.
When it is empty (except the permanent exceptions below) the refactor is done.

Findings (one per occurrence, "path:symbol:kind:detail")
--------------------------------------------------------
literal     a str constant (also the literal parts of an f-string) containing a
            brand or model token, case-insensitive.  detail = the token.
identifier  a name containing a brand token: def/class/argument names,
            ``Name.id``, ``Attribute.attr``, keyword names in calls, import
            names/aliases.  detail = the FULL identifier (more useful than the
            bare token when removing entries).
compare     a ``Compare`` using ==, !=, in, not in where an operand is the
            brand or the version (``X.brand``, ``getattr(X, "brand", ..)``, the
            name ``brand``, ``X.get("brand")``, ``X["brand"]``; same for
            ``battery_version``, plus the name ``CONF_BATTERY_VERSION``).
            detail = ``ast.unparse`` of the comparison, whitespace-collapsed
            and cut at 120 chars.
lookup      a subscript or ``.get(...)`` whose index/key is one of those
            brand/version VALUE expressions (``TABLE[battery_version]``,
            ``TABLE.get(self.brand, ..)``).  detail = ``ast.unparse``.
import      an import of ``drivers.<module>`` other than ``base``/``registry``
            (``from .drivers.marstek import X``; ``from .drivers import X``
            counts as ``drivers.X`` since X is then a driver module or a
            re-exported class) or of a brand transport client of ``infra/``.
            detail = ``drivers.<module>`` / ``infra.<client>``.
js          a brand/model token in ``frontend/*.js``, comments stripped.

Brand tokens (substring, case-insensitive; used for identifiers and literals):
    marstek zendure esphome anker sessy indevolt huawei hoymiles
Model tokens (literals and JS only), as words (not glued to letters, so
"lunar", "dilemma", "avenue" do not match; digits/underscores/hyphens do):
    venus solarflow solarbank sun2000 luna2000 luna emma hyper hypertmp
    ms-a2/msa2 2400ac/"2400 ac" hub2000 e1600
(taken from the models that appear in drivers/ and the shared code today.)

Ignored: comments (the AST drops them), docstrings (any statement that is only
a str constant) and every argument of a ``<x>.debug/info/warning/error/
exception/critical/log(...)`` call.

Permanent exceptions (plan 3.5, row 49, D5) are RULES below, not allowlist
entries.  Nothing from the panel is a permanent exception yet: every JS
finding is allowlisted until T8.1, where D5 decides.

Allowlist file: one line per occurrence, sorted, blank lines and ``#``
comments allowed.  Compared as a multiset.  To regenerate (only to REMOVE
lines after a refactor step; review that the diff only deletes):

    OMNIBATTERY_UPDATE_ALLOWLIST=1 pytest tests/test_no_brand_branches.py
"""

from __future__ import annotations

import ast
import os
import re

from tests import _guard_common
from tests._guard_common import PACKAGE, UPDATE_ENV, diff_against_allowlist, shared_python_files

ALLOWLIST_FILE = _guard_common.ALLOWLIST_DIR / "no_brand_branches.txt"

BRANDS = ("marstek", "zendure", "esphome", "anker", "sessy", "indevolt", "huawei", "hoymiles")
BRAND_RE = re.compile("|".join(BRANDS), re.IGNORECASE)
# Brand OR model.  The lookarounds forbid letters on both sides so "lunar" or
# "dilemma" never match, while "venus_a", "sun2000", "2400ac_pro" do.
_MODELS = (
    r"venus", r"solarflow", r"solarbank", r"sun2000", r"luna2000", r"luna", r"emma",
    r"hyper", r"hypertmp", r"ms-?a2", r"2400 ?ac", r"hub2000", r"e1600",
)
TOKEN_RE = re.compile(
    "(?:" + "|".join(BRANDS) + ")|(?<![a-z])(?:" + "|".join(_MODELS) + ")(?![a-z])",
    re.IGNORECASE,
)

# Brand transport clients that live in infra/ today (plan row 28, D1).
INFRA_CLIENTS = frozenset({
    "modbus_client", "huawei_modbus_client", "anker_modbus_client",
    "indevolt_client", "indevolt_notify",
})
# The only driver modules shared code may import (registry does not exist yet).
# "setup" is the shared per-brand config flow module (DriverSetup, flow context);
# it imports neither config_flow nor registry, so brand flows may import it.
ALLOWED_DRIVER_MODULES = frozenset({"base", "registry", "setup"})

LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})

# ---------------------------------------------------------------------------
# Permanent exceptions (plan 3.5, row 49, D5)
# ---------------------------------------------------------------------------

# Migration code must name the old brand/domain: it reads data written before
# the rebrand.
EXEMPT_FILES = frozenset({"domain_migration.py", "migration_flow.py"})

# In __init__.py the migration functions (and whatever is inside them).
_INIT_EXEMPT_FUNCTION_RE = re.compile(
    r"^(?:async_migrate_entry|_async_migrate_.*|_migrate_.*"
    r"|_dismiss_legacy_active_balance_notifications)$"
)

# Row 49: the solar-profile fingerprint stores battery_version; changing it
# would reset the learned profile ("frozen").
EXEMPT_SYMBOLS = frozenset({("tracking/solar_profile.py", "configuration_fingerprint")})

# Stable identifiers that cannot be renamed without orphaning user data.
# Every ``marstek_venus*`` / ``marstek-venus*`` string is one of:
#   marstek_venus_system_*      unique_id of the system entities (72 of them)
#   marstek_venus_system        device-registry identifier of the system device
#   marstek_venus_energy_manager / marstek_venus   old domain / old entity
#                               registry platform (infra/entity_naming.py)
#   marstek_venus_              NOTIFICATION_ID_PREFIX (persistent notification ids)
#   marstek-venus-panel         the web component registered by __init__.py
# and ``marstek-panel`` is the panel URL/file name.  Matched text is removed
# from the literal before the brand/model scan.
STABLE_ID_RE = re.compile(r"marstek[_-]venus[a-z0-9_-]*|marstek-panel", re.IGNORECASE)
# Class names MarstekVenus* (MarstekVenusDataUpdateCoordinator, MarstekVenusNumber,
# ...) and references to them are stable: HA / pickled-free code references them
# by name and renaming is out of scope (plan section 7).
STABLE_NAME_PREFIX = "MarstekVenus"


# ---------------------------------------------------------------------------
# Python scanner
# ---------------------------------------------------------------------------

def _is_str_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _key_name(node: ast.AST) -> str | None:
    """'brand'/'battery_version' if node is that key (string or CONF_ constant)."""
    if _is_str_const(node) and node.value in ("brand", "battery_version"):
        return node.value
    if isinstance(node, ast.Name) and node.id == "CONF_BATTERY_VERSION":
        return "battery_version"
    return None


def _is_value_expr(node: ast.AST) -> bool:
    """True if node EVALUATES to the brand or the battery version."""
    if isinstance(node, ast.Name):
        return node.id in ("brand", "battery_version")
    if isinstance(node, ast.Attribute):
        return node.attr in ("brand", "battery_version")
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name) and f.id == "getattr" and len(node.args) >= 2:
            return _key_name(node.args[1]) in ("brand", "battery_version") and _is_str_const(node.args[1])
        if isinstance(f, ast.Attribute) and f.attr == "get" and node.args:
            return _key_name(node.args[0]) is not None
        return False
    if isinstance(node, ast.Subscript):
        return _key_name(node.slice) is not None
    return False


def _is_compare_operand(node: ast.AST) -> bool:
    return _is_value_expr(node) or (
        isinstance(node, ast.Name) and node.id == "CONF_BATTERY_VERSION"
    )


def _short(node: ast.AST) -> str:
    return re.sub(r"\s+", " ", ast.unparse(node)).strip()[:120]


class _Scanner(ast.NodeVisitor):
    def __init__(self, relpath: str):
        self.relpath = relpath
        self.scope: list[str] = []
        self.found: list[str] = []

    # -- helpers ----------------------------------------------------------
    def _symbol(self) -> str:
        return ".".join(self.scope) or "<module>"

    def _add(self, kind: str, detail: str) -> None:
        self.found.append(f"{self.relpath}:{self._symbol()}:{kind}:{detail}")

    def _identifier(self, name: str | None) -> None:
        if not name or name.startswith(STABLE_NAME_PREFIX) or name.lstrip("_").startswith(STABLE_NAME_PREFIX):
            return
        if BRAND_RE.search(name):
            self._add("identifier", name)

    def _exempt_scope(self, name: str) -> bool:
        if (self.relpath, name) in EXEMPT_SYMBOLS:
            return True
        return self.relpath == "__init__.py" and bool(_INIT_EXEMPT_FUNCTION_RE.match(name))

    # -- scopes -----------------------------------------------------------
    def _visit_scope(self, node, is_function: bool) -> None:
        if self._exempt_scope(node.name):
            return
        self._identifier(node.name)
        for d in node.decorator_list:
            self.visit(d)
        if not is_function:
            for b in node.bases:
                self.visit(b)
            for k in node.keywords:
                self.visit(k)
        self.scope.append(node.name)
        if is_function:
            self.visit(node.args)
            if node.returns:
                self.visit(node.returns)
        for stmt in node.body:
            self.visit(stmt)
        self.scope.pop()

    def visit_FunctionDef(self, node):
        self._visit_scope(node, True)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._visit_scope(node, False)

    # -- identifiers ------------------------------------------------------
    def visit_arg(self, node):
        self._identifier(node.arg)
        if node.annotation:
            self.visit(node.annotation)

    def visit_Name(self, node):
        self._identifier(node.id)

    def visit_Attribute(self, node):
        self._identifier(node.attr)
        self.visit(node.value)

    def visit_keyword(self, node):
        self._identifier(node.arg)
        self.visit(node.value)

    # -- literals ---------------------------------------------------------
    def visit_Constant(self, node):
        if isinstance(node.value, str):
            text = STABLE_ID_RE.sub(" ", node.value)
            for m in TOKEN_RE.finditer(text):
                self._add("literal", m.group(0).lower())

    def visit_Expr(self, node):
        if _is_str_const(node.value):  # docstring / bare string statement
            return
        self.generic_visit(node)

    # -- calls, compares, lookups ----------------------------------------
    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in LOG_METHODS:
            self.visit(f)  # arguments of a log call are ignored
            return
        if (
            isinstance(f, ast.Attribute) and f.attr == "get" and node.args
            and _is_value_expr(node.args[0])
        ):
            self._add("lookup", _short(node))
        self.generic_visit(node)

    def visit_Subscript(self, node):
        if _is_value_expr(node.slice):
            self._add("lookup", _short(node))
        self.generic_visit(node)

    def visit_Compare(self, node):
        ops_ok = any(isinstance(o, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)) for o in node.ops)
        if ops_ok and any(_is_compare_operand(n) for n in (node.left, *node.comparators)):
            self._add("compare", _short(node))
        self.generic_visit(node)

    # -- imports ----------------------------------------------------------
    def _import_findings(self, parts: list[str], alias_names: list[str]) -> None:
        if "drivers" in parts:
            rest = parts[parts.index("drivers") + 1:]
            if rest:
                if rest[0] not in ALLOWED_DRIVER_MODULES:
                    self._add("import", f"drivers.{rest[0]}")
            else:
                for n in alias_names:
                    if n not in ALLOWED_DRIVER_MODULES and n != "*":
                        self._add("import", f"drivers.{n}")
        for p in parts:
            if p in INFRA_CLIENTS:
                self._add("import", f"infra.{p}")
        if not parts or parts[-1] == "infra":
            for n in alias_names:
                if n in INFRA_CLIENTS:
                    self._add("import", f"infra.{n}")

    def visit_Import(self, node):
        for a in node.names:
            self._import_findings(a.name.split("."), [])
            self._identifier(a.asname)

    def visit_ImportFrom(self, node):
        parts = node.module.split(".") if node.module else []
        self._import_findings(parts, [a.name for a in node.names])
        for a in node.names:
            self._identifier(a.name)
            self._identifier(a.asname)


def scan_python_source(source: str, relpath: str) -> list[str]:
    """Findings for one module's source; ``relpath`` is relative to the package."""
    if relpath in EXEMPT_FILES:
        return []
    scanner = _Scanner(relpath)
    scanner.visit(ast.parse(source))
    return scanner.found


# ---------------------------------------------------------------------------
# JS scanner
# ---------------------------------------------------------------------------

def _strip_js_comments(src: str) -> str:
    """Drop ``//`` and ``/* */`` comments, leaving string literals untouched.

    A single left-to-right pass: a plain regex gets fooled by ``/*`` inside a
    ``//`` comment (the panel has one, which swallowed 240 KB) and by ``//``
    inside URL strings.  Regex literals are not parsed (none in the panel
    contains a quote that would unbalance this).
    """
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        two = src[i:i + 2]
        if two == "//":
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif two == "/*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif ch in "'\"`":
            j = i + 1
            while j < n and src[j] != ch:
                j += 2 if src[j] == "\\" else 1
            out.append(src[i:j + 1])
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def scan_js_source(source: str, relpath: str) -> list[str]:
    """Brand/model tokens in a JS file, comments stripped."""
    text = _strip_js_comments(source)
    # Same stable identifiers as in Python: panel element/file names, the
    # marstek_venus_* entity ids and MarstekVenus* class names.
    text = STABLE_ID_RE.sub(" ", text)
    text = re.sub(STABLE_NAME_PREFIX + r"\w*", " ", text)
    return [f"{relpath}:<js>:js:{m.group(0).lower()}" for m in TOKEN_RE.finditer(text)]


# ---------------------------------------------------------------------------
# Package scan + allowlist
# ---------------------------------------------------------------------------

def scan_package() -> list[str]:
    found: list[str] = []
    for rel, path in shared_python_files():
        found += scan_python_source(path.read_text(encoding="utf-8"), rel)
    for path in sorted((PACKAGE / "frontend").glob("*.js")):
        rel = path.relative_to(PACKAGE).as_posix()
        found += scan_js_source(path.read_text(encoding="utf-8"), rel)
    return sorted(found)


def read_allowlist() -> list[str]:
    return _guard_common.read_allowlist(ALLOWLIST_FILE)


_HEADER = (
    "# Brand-branch allowlist (tests/test_no_brand_branches.py). One line per\n"
    "# occurrence, format path:symbol:kind:detail. It may only SHRINK: delete lines\n"
    "# as the refactor removes the code that produces them. Regenerate with\n"
    "# OMNIBATTERY_UPDATE_ALLOWLIST=1 and check that the diff only deletes.\n"
)


def write_allowlist(found: list[str]) -> None:
    _guard_common.write_allowlist(ALLOWLIST_FILE, _HEADER, found)


def test_no_new_brand_branches():
    found = scan_package()
    if os.environ.get(UPDATE_ENV) == "1":
        write_allowlist(found)
        return
    new, obsolete = diff_against_allowlist(found, read_allowlist())
    problems = []
    if new:
        problems.append(
            "Brand/model knowledge in shared code (move it into drivers/, do NOT "
            f"add it to the allowlist). {len(new)} new finding(s):\n  " + "\n  ".join(new)
        )
    if obsolete:
        problems.append(
            "Allowlist entries that no longer occur: delete these lines from "
            f"{ALLOWLIST_FILE.name} ({len(obsolete)}):\n  " + "\n  ".join(obsolete)
        )
    assert not problems, "\n\n".join(problems)
