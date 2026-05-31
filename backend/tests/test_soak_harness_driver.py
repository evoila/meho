# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end test for the soak-harness shell driver (G11.7-T2 #1402).

Drives ``scripts/soak/soak-harness.sh`` as a subprocess against the
committed example evidence fixtures so the driver — argument parsing,
evidence-file contract, exit codes, and the report it writes — is
CI-verified, not just the Python verifier underneath it. Mirrors how
``test_ci_check_release_body_paths.py`` exercises a repo shell/CLI
helper via subprocess rather than re-implementing its logic.

Skips cleanly when ``jq`` is unavailable (the driver requires it; the
sandbox may not have it) so the gate is honest about not having run
rather than vacuously green.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DRIVER = _REPO_ROOT / "scripts" / "soak" / "soak-harness.sh"
_EXAMPLE = _REPO_ROOT / "scripts" / "soak" / "examples" / "k8s.scale"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="soak-harness.sh requires jq + bash; not present in this sandbox",
)


def _run(evidence_dir: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(_DRIVER),
            "--op",
            "k8s.scale",
            "--connector",
            "k8s-1.x",
            "--evidence-dir",
            str(evidence_dir),
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _seed(tmp_path: Path) -> Path:
    ev = tmp_path / "evidence"
    ev.mkdir()
    for f in _EXAMPLE.glob("*.json"):
        shutil.copy(f, ev / f.name)
    return ev


def test_driver_exists_and_is_executable() -> None:
    assert _DRIVER.exists()
    assert _EXAMPLE.is_dir()


def test_clean_evidence_exits_zero_and_reports_shadow(tmp_path: Path) -> None:
    ev = _seed(tmp_path)
    proc = _run(ev)
    assert proc.returncode == 0, proc.stderr
    report = json.loads((ev / "soak-report.json").read_text())
    assert report["all_passed"] is True
    assert report["has_blocker"] is False
    assert report["supported_scorecard_cell"] == "shadow"


def test_soak_clean_flag_promotes_to_ready(tmp_path: Path) -> None:
    ev = _seed(tmp_path)
    proc = _run(ev, "--soak-clean")
    assert proc.returncode == 0, proc.stderr
    report = json.loads((ev / "soak-report.json").read_text())
    assert report["supported_scorecard_cell"] == "ready"


def test_plan_divergence_exits_one_and_blocks(tmp_path: Path) -> None:
    ev = _seed(tmp_path)
    # Make the MEHO plan semantically diverge from the wrapper plan.
    meho_plan = json.loads((ev / "meho-plan.json").read_text())
    meho_plan["spec"]["replicas"] = 99
    (ev / "meho-plan.json").write_text(json.dumps(meho_plan))
    proc = _run(ev)
    assert proc.returncode == 1, proc.stdout
    report = json.loads((ev / "soak-report.json").read_text())
    assert report["has_blocker"] is True
    assert report["supported_scorecard_cell"] == "blocked"


def test_missing_evidence_file_exits_two(tmp_path: Path) -> None:
    ev = _seed(tmp_path)
    (ev / "meta.json").unlink()
    proc = _run(ev)
    assert proc.returncode == 2, proc.stdout


def test_missing_required_arg_exits_two(tmp_path: Path) -> None:
    ev = _seed(tmp_path)
    proc = subprocess.run(
        ["bash", str(_DRIVER), "--connector", "k8s-1.x", "--evidence-dir", str(ev)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
