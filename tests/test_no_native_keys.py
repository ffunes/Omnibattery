"""Guardian: shared code must not read a brand's native keys or codes (T1.4).

Second guardian of the "everything in drivers/" refactor (plan section 1,
criterion 2).  ``test_no_brand_branches`` cannot see leaks that name no brand:
a Zendure key such as ``soc_set`` or Marstek's inverter-state code table are
"brand knowledge" even though no brand word appears.  This test scans the same
scope (every ``.py`` of the integration except ``drivers/``, Python only: the
panel is out of scope, decision D5), with the same permanent exceptions (plan
3.5: migrations), the same ignores (comments, docstrings, every argument of a
log call) and the same allowlist mechanics.  Allowlist:
``tests/guard_allowlists/no_native_keys.txt``; it may only SHRINK.

Findings (one per occurrence, ``path:symbol:kind:detail``)
----------------------------------------------------------
key          a str constant exactly equal to a native key (``NATIVE_KEYS``).
             detail = the key.  Rows: #22 ``soc_set``; #23 ``inverse_max_power``;
             #35 ``alarm_status`` / ``fault_status``; #41 ``soc_status``;
             #44 ``soc_set``.  (No other native key is cited by those rows.)
data_key     a READ of ``min_soc`` from the polled data (#22): ``X.data.get(
             "min_soc")``, ``X.data["min_soc"]``, ``"min_soc" in X.data`` and the
             same with a bare name ``data``.  ``min_soc`` is also a plain config
             key, so it is not a ``key`` finding: heuristic = the data source is
             an attribute ``.data``, a name ``data`` or ``getattr(x, "data", ..)``
             (also behind ``or``), and the expression it hangs from does NOT
             contain ``entry`` (``entry.data``, ``self.config_entry.data``).
             ``battery_config``, ``user_input``, ``merged``... are never sources.
             Writes (``X.data["min_soc"] = v``) are not reads.  detail =
             ``ast.unparse`` of the expression.
state_code   a comparison (==, !=, in, not in, <, <=, >, >=) between something
             derived from ``inverter_state`` and a "code": a numeric literal, a
             tuple/set/list of codes, or an UPPER_SNAKE constant name (the code
             then lives in ``const/``: ``NORMAL_BALANCE_RECAL_INVERTER_STANDBY``).
             "Derived" = the expression text contains ``inverter_state``, or it
             uses a local name assigned in the same function from such an
             expression, or a parameter that the same module passes such an
             expression to (``_inverter_in_ac_bypass(data.get("inverter_state"))``
             taints its ``value`` parameter).  One level only; calls across
             modules are not followed.  detail = ``ast.unparse`` of the compare.
backup_code  the same derivation for the key ``backup_function`` (#40,
             ``_backup_switch_enabled``), compared with ANY literal (number,
             str, tuple/set/list of literals) or UPPER_SNAKE constant.
alarm_table  any reference (definition, import, use, attribute) to
             ``ALARM_BIT_DESCRIPTIONS`` / ``FAULT_BIT_DESCRIPTIONS`` (#35).
             Star-imports are not references.  detail = the name.

Regenerate (only to REMOVE lines after a refactor step; check the diff only
deletes):

    OMNIBATTERY_UPDATE_ALLOWLIST=1 pytest tests/test_no_native_keys.py
"""

from __future__ import annotations

import ast
import os
import re

from tests import _guard_common
from tests._guard_common import diff_against_allowlist, shared_python_files
from tests.test_no_brand_branches import (
    EXEMPT_FILES,
    LOG_METHODS,
    UPDATE_ENV,
    _Scanner as _BrandScanner,
    _is_str_const,
    _short,
)

ALLOWLIST_FILE = _guard_common.ALLOWLIST_DIR / "no_native_keys.txt"

NATIVE_KEYS = frozenset({
    "soc_set", "inverse_max_power", "soc_status", "alarm_status", "fault_status",
})
ALARM_TABLES = frozenset({"ALARM_BIT_DESCRIPTIONS", "FAULT_BIT_DESCRIPTIONS"})

# finding kind -> text that marks an expression as derived from the native key
STATE_MARK = "inverter_state"
BACKUP_MARK = "backup_function"
_MARKS = {"state_code": STATE_MARK, "backup_code": BACKUP_MARK}

_UPPER_NAME_RE = re.compile(r"^_*[A-Z][A-Z0-9_]*$")
_COMPARE_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn, ast.Lt, ast.LtE, ast.Gt, ast.GtE)


# ---------------------------------------------------------------------------
# Expression classifiers
# ---------------------------------------------------------------------------

def _is_data_source(node: ast.AST) -> bool:
    """Polled data (not config): ``.data``, ``data``, ``getattr(x, "data", ..)``."""
    if isinstance(node, ast.Name):
        return node.id == "data"
    if isinstance(node, ast.Attribute):
        return node.attr == "data" and "entry" not in ast.unparse(node.value).lower()
    if isinstance(node, ast.BoolOp):
        return any(_is_data_source(v) for v in node.values)
    if isinstance(node, ast.Call):
        f = node.func
        return (
            isinstance(f, ast.Name) and f.id == "getattr" and len(node.args) >= 2
            and _is_str_const(node.args[1]) and node.args[1].value == "data"
            and "entry" not in ast.unparse(node.args[0]).lower()
        )
    return False


def _is_min_soc(node: ast.AST) -> bool:
    return _is_str_const(node) and node.value == "min_soc"


def _is_code_name(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return bool(_UPPER_NAME_RE.match(node.id))
    if isinstance(node, ast.Attribute):
        return bool(_UPPER_NAME_RE.match(node.attr))
    return False


def _is_literal(node: ast.AST, *, numeric_only: bool) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _is_literal(node.operand, numeric_only=numeric_only)
    if isinstance(node, ast.Constant):
        if node.value is None:
            return False
        if isinstance(node.value, bool):
            return not numeric_only
        if isinstance(node.value, (int, float)):
            return True
        return not numeric_only
    if isinstance(node, (ast.Tuple, ast.Set, ast.List)):
        return bool(node.elts) and all(
            _is_literal(e, numeric_only=numeric_only) or _is_code_name(e) for e in node.elts
        )
    return False


def _is_code(node: ast.AST, kind: str) -> bool:
    return _is_code_name(node) or _is_literal(node, numeric_only=(kind == "state_code"))


# ---------------------------------------------------------------------------
# Derivation pre-pass (per module)
# ---------------------------------------------------------------------------

def _functions(tree: ast.AST):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _local_marks(func: ast.AST, mark: str) -> set[str]:
    """Names assigned in ``func`` from an expression whose text contains ``mark``."""
    out: set[str] = set()
    for n in ast.walk(func):
        if isinstance(n, ast.Assign):
            targets, value = n.targets, n.value
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)) and n.value is not None:
            targets, value = [n.target], n.value
        elif isinstance(n, ast.NamedExpr):
            targets, value = [n.target], n.value
        else:
            continue
        if mark in ast.unparse(value):
            for t in targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        out.add(sub.id)
    return out


def _derived(node: ast.AST, mark: str, names: set[str]) -> bool:
    if mark in ast.unparse(node):
        return True
    return any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(node))


def _tainted_params(tree: ast.AST) -> dict[str, dict[str, set[str]]]:
    """function name -> kind -> parameter names that receive a derived argument."""
    # callee name -> kind -> (argument positions, keyword names)
    calls: dict[str, dict[str, tuple[set[int], set[str]]]] = {}

    def record(call: ast.Call, locals_by_kind: dict[str, set[str]]) -> None:
        f = call.func
        callee = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if callee is None:
            return
        for kind, mark in _MARKS.items():
            pos, kws = calls.setdefault(callee, {}).setdefault(kind, (set(), set()))
            for i, a in enumerate(call.args):
                if _derived(a, mark, locals_by_kind[kind]):
                    pos.add(i)
            for k in call.keywords:
                if k.arg and _derived(k.value, mark, locals_by_kind[kind]):
                    kws.add(k.arg)

    no_locals = {kind: set() for kind in _MARKS}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            record(n, no_locals)  # textual marker only
    for func in _functions(tree):
        locs = {kind: _local_marks(func, mark) for kind, mark in _MARKS.items()}
        for n in ast.walk(func):
            if isinstance(n, ast.Call):
                record(n, locs)

    out: dict[str, dict[str, set[str]]] = {}
    for func in _functions(tree):
        params = [a.arg for a in (*func.args.posonlyargs, *func.args.args)]
        if params and params[0] in ("self", "cls"):
            params = params[1:]
        named = set(params) | {a.arg for a in func.args.kwonlyargs}
        for kind, (pos, kws) in calls.get(func.name, {}).items():
            tainted = {params[i] for i in pos if i < len(params)} | (kws & named)
            if tainted:
                out.setdefault(func.name, {}).setdefault(kind, set()).update(tainted)
    return out


# ---------------------------------------------------------------------------
# Scanner (reuses the scope/symbol/exemption machinery of the brand guardian)
# ---------------------------------------------------------------------------

class _NativeScanner(_BrandScanner):
    def __init__(self, relpath: str, tree: ast.AST):
        super().__init__(relpath)
        self._taint = _tainted_params(tree)
        self._marks: list[dict[str, set[str]]] = []

    # Brand-token identifiers do not apply here: only the alarm tables do.
    def _identifier(self, name: str | None) -> None:
        if name in ALARM_TABLES:
            self._add("alarm_table", name)

    def _names(self, kind: str) -> set[str]:
        out: set[str] = set()
        for marks in self._marks:
            out |= marks[kind]
        return out

    # -- scopes: remember which names are derived inside each function ------
    def visit_FunctionDef(self, node):
        marks = {
            kind: _local_marks(node, mark) | self._taint.get(node.name, {}).get(kind, set())
            for kind, mark in _MARKS.items()
        }
        self._marks.append(marks)
        try:
            super().visit_FunctionDef(node)
        finally:
            self._marks.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    # -- key / data_key ------------------------------------------------------
    def visit_Constant(self, node):
        if isinstance(node.value, str) and node.value in NATIVE_KEYS:
            self._add("key", node.value)

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in LOG_METHODS:
            self.visit(f)  # arguments of a log call are ignored
            return
        if (
            isinstance(f, ast.Attribute) and f.attr == "get" and node.args
            and _is_min_soc(node.args[0]) and _is_data_source(f.value)
        ):
            self._add("data_key", _short(node))
        self.generic_visit(node)

    def visit_Subscript(self, node):
        if (
            isinstance(node.ctx, ast.Load)
            and _is_min_soc(node.slice) and _is_data_source(node.value)
        ):
            self._add("data_key", _short(node))
        self.generic_visit(node)

    # -- state_code / backup_code / membership data_key ----------------------
    def visit_Compare(self, node):
        operands = [node.left, *node.comparators]
        for op, a, b in zip(node.ops, operands, operands[1:]):
            if not isinstance(op, _COMPARE_OPS):
                continue
            if isinstance(op, (ast.In, ast.NotIn)) and _is_min_soc(a) and _is_data_source(b):
                self._add("data_key", _short(node))
                break
            hit = False
            for kind, mark in _MARKS.items():
                names = self._names(kind)
                if any(_derived(x, mark, names) and _is_code(y, kind) for x, y in ((a, b), (b, a))):
                    self._add(kind, _short(node))
                    hit = True
                    break
            if hit:
                break
        self.generic_visit(node)

    # -- imports: only the alarm tables matter -------------------------------
    def visit_Import(self, node):
        for a in node.names:
            self._identifier(a.name.split(".")[-1])
            self._identifier(a.asname)

    def visit_ImportFrom(self, node):
        for a in node.names:
            self._identifier(a.name)
            self._identifier(a.asname)


def scan_python_source(source: str, relpath: str) -> list[str]:
    """Findings for one module's source; ``relpath`` is relative to the package."""
    if relpath in EXEMPT_FILES:
        return []
    tree = ast.parse(source)
    scanner = _NativeScanner(relpath, tree)
    scanner.visit(tree)
    return scanner.found


# ---------------------------------------------------------------------------
# Package scan + allowlist
# ---------------------------------------------------------------------------

def scan_package() -> list[str]:
    found: list[str] = []
    for rel, path in shared_python_files():
        found += scan_python_source(path.read_text(encoding="utf-8"), rel)
    return sorted(found)


_HEADER = (
    "# Native-key allowlist (tests/test_no_native_keys.py). One line per\n"
    "# occurrence, format path:symbol:kind:detail. It may only SHRINK: delete lines\n"
    "# as the refactor removes the code that produces them. Regenerate with\n"
    "# OMNIBATTERY_UPDATE_ALLOWLIST=1 and check that the diff only deletes.\n"
)


def test_no_new_native_keys():
    found = scan_package()
    if os.environ.get(UPDATE_ENV) == "1":
        _guard_common.write_allowlist(ALLOWLIST_FILE, _HEADER, found)
        return
    new, obsolete = diff_against_allowlist(found, _guard_common.read_allowlist(ALLOWLIST_FILE))
    problems = []
    if new:
        problems.append(
            "Brand-native keys/codes read by shared code (let the driver translate "
            f"them, do NOT add them to the allowlist). {len(new)} new finding(s):\n  "
            + "\n  ".join(new)
        )
    if obsolete:
        problems.append(
            "Allowlist entries that no longer occur: delete these lines from "
            f"{ALLOWLIST_FILE.name} ({len(obsolete)}):\n  " + "\n  ".join(obsolete)
        )
    assert not problems, "\n\n".join(problems)
