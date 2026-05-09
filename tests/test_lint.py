"""Lint gate: every Python file in the repo must pass flake8.

Runs flake8 as a subprocess (rather than importing its API) so the
behaviour matches what a developer sees from `python -m flake8` on the
command line, and so any flake8 plugin/config picked up via .flake8 is
honoured automatically.

Skipped — not failed — if flake8 isn't installed, so a contributor who
ran `pip install -r requirements.txt` (without the dev extras) doesn't
hit a confusing missing-dependency error.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LINT_TARGETS = ("secdigest", "tests")


def _flake8_available() -> bool:
    """True if `python -m flake8` is callable. We prefer the module form
    over the bare `flake8` script so the test works regardless of whether
    the script directory is on PATH (it often isn't with --user installs)."""
    if shutil.which("flake8"):
        return True
    probe = subprocess.run(
        [sys.executable, "-m", "flake8", "--version"],
        capture_output=True,
    )
    return probe.returncode == 0


@pytest.mark.skipif(not _flake8_available(),
                    reason="flake8 not installed; install requirements-dev.txt")
def test_flake8_clean():
    """The codebase must lint clean under the project's .flake8 config.

    On failure, flake8's stdout (one violation per line) is included in the
    pytest output so the contributor sees exactly what to fix without
    re-running the linter manually.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "flake8", *LINT_TARGETS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(
            "flake8 reported violations:\n\n"
            + (proc.stdout or "(no stdout)")
            + (("\n\nstderr:\n" + proc.stderr) if proc.stderr.strip() else "")
        )
