# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`scripts.ci.run_eval_gate`.

The CI gate is the load-bearing "MEHO ≥ baseline" check that gates
every backend PR (Initiative #373 retire-criterion #4). The previous
shape returned exit 0 when the baseline file was missing — a silent
gate-bypass surface. These tests pin the corrected fail-loud
behaviour so a future regression is caught at PR time.

The tests invoke the script as a subprocess so the entry-point
behaviour (argparse + ``return 1``) is exercised end-to-end.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path


def _repo_root(start: Path) -> Path:
    """Walk up from the test file to the repo root (where scripts/ lives)."""
    here = start.resolve()
    for parent in (here, *here.parents):
        if (parent / "scripts" / "ci" / "run_eval_gate.py").exists():
            return parent
    raise RuntimeError("could not find repo root containing scripts/ci/run_eval_gate.py")


REPO_ROOT = _repo_root(Path(__file__))
GATE_SCRIPT = REPO_ROOT / "scripts" / "ci" / "run_eval_gate.py"


def _run_gate(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Invoke run_eval_gate.py from a chosen cwd; capture stdout/stderr.

    Set PYTHONPATH so the script's ``from meho_backplane...`` imports
    resolve against the backend source tree, same as the CI step's
    ``working-directory: backend`` + ``uv run python ../scripts/...``.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "backend" / "src")
    return subprocess.run(
        [sys.executable, str(GATE_SCRIPT), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_run_eval_gate_fails_loud_when_baseline_missing(tmp_path: Path) -> None:
    """Missing ``--compare-baseline`` file → exit 1 + clear stderr message.

    Pre-fix the script returned 0 with a "Skipping regression check"
    note — a silent gate-bypass. The fix asserts the gate fails-closed
    so renaming / deleting / path-shadowing the baseline file blocks
    merge until corrected.
    """
    missing = tmp_path / "no-such-baseline.json"
    assert not missing.exists()

    result = _run_gate("--compare-baseline", str(missing), cwd=tmp_path)

    assert result.returncode == 1, (
        f"expected exit 1 on missing baseline; got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "GATE FAILED" in result.stderr
    assert "baseline missing" in result.stderr


def test_run_eval_gate_passes_with_valid_baseline(tmp_path: Path) -> None:
    """Save → compare round-trip against a fresh baseline returns 0.

    Smoke-checks the happy path so the fail-loud guard doesn't
    accidentally regress the gate when the baseline is present.
    """
    baseline_path = tmp_path / "baseline.json"

    save = _run_gate("--save-baseline", str(baseline_path), cwd=tmp_path)
    assert save.returncode == 0, (
        f"save expected exit 0; got {save.returncode}.\n"
        f"stdout: {save.stdout}\nstderr: {save.stderr}"
    )
    assert baseline_path.exists()

    # The baseline content is a valid EvalResult JSON.
    body = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert "overall_verdict" in body
    assert "surfaces" in body

    compare = _run_gate("--compare-baseline", str(baseline_path), cwd=tmp_path)
    assert compare.returncode == 0, (
        f"compare against self expected exit 0; got {compare.returncode}.\n"
        f"stdout: {compare.stdout}\nstderr: {compare.stderr}"
    )
    assert "GATE PASSED" in compare.stdout


def test_run_eval_gate_docstring_mentions_missing_baseline_exit_code() -> None:
    """The script's docstring documents the missing-baseline exit code.

    Light contract test: the module docstring is the operator-facing
    spec; the change introduced a fourth case (baseline file missing)
    that previously was not in the exit-code list. Pin the docstring
    update so a future contributor can't quietly remove it.
    """
    source = GATE_SCRIPT.read_text(encoding="utf-8")
    # Look at the first chunk (the module docstring): from the first
    # ``"""`` to the second.
    first = source.find('"""')
    second = source.find('"""', first + 3)
    head = source[first : second + 3]
    head_lower = textwrap.dedent(head).lower()
    # Variants: "baseline file missing" (initial wording) or
    # "missing-baseline branch" (cross-reference to the code). Either
    # is acceptable as long as the docstring acknowledges the
    # fail-loud case.
    assert (
        "baseline file missing" in head_lower
        or "missing-baseline" in head_lower
        or "baseline missing" in head_lower
    )
