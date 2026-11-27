"""Fingerprint of the code the planner sweep covers (.githooks/pre-merge-commit).

A passing sweep writes it to the git dir; the hook skips the sweep when the
merged index has the same fingerprint. ``python tests/sweep_stamp.py`` prints
the index's and exits 0 when it matches the last pass.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

# The hook's trigger paths.
PATHS = (
    "custom_components/omnibattery/__init__.py",
    "custom_components/omnibattery/pricing",
    "custom_components/omnibattery/control",
    "tests/scenarios",
    "tests/test_predictive_sweep.py",
    "tests/test_predictive_scenarios.py",
    "tests/sweep_known.json",
)
ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, input=stdin, capture_output=True,
                          text=True, check=True).stdout


def fingerprint(index: bool) -> str:
    """The index's blobs (a merge being committed) or the working tree's."""
    if index:
        lines = [f"{line.split()[1]} {line.split(chr(9), 1)[1]}"
                 for line in _git("ls-files", "-s", "--", *PATHS).splitlines()]
    else:
        files = _git("ls-files", "--", *PATHS).splitlines()
        blobs = _git("hash-object", "--stdin-paths", stdin="\n".join(files) + "\n").split()
        lines = [f"{b} {f}" for b, f in zip(blobs, files)]
    return hashlib.sha1("\n".join(lines).encode()).hexdigest()


def stamp_file() -> Path:
    return ROOT / _git("rev-parse", "--git-common-dir").strip() / "sweep-passed"


def record() -> None:
    stamp_file().write_text(fingerprint(index=False))


if __name__ == "__main__":
    last = stamp_file().read_text().strip() if stamp_file().exists() else None
    sys.exit(0 if last == fingerprint(index=True) else 1)
