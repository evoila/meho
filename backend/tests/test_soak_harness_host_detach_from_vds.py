# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Soak-harness wiring for ``vmware.composite.host.detach_from_vds`` (G3.16-T3 #1416).

The headline retirement of Initiative #1400: ``host.detach_from_vds`` is
the one VCF workflow ``govc`` cannot express (it has no
``host.detach-from-vds`` verb — the consumer's
``scripts/host-detach-from-vds.py`` exists precisely because govc could
not), so it is the highest-value composite to soak before its wrapper is
deleted in Phase D.

This test drives the committed
``scripts/soak/examples/host.detach_from_vds`` evidence bundle through
the shipped ``scripts/soak/soak-harness.sh`` driver as a subprocess —
the same end-to-end shape ``test_soak_harness_driver.py`` uses for
``k8s.scale`` — and asserts the harness processes the bundle and emits
the expected scorecard cell. The decision core itself is already
exhaustively unit-tested in ``test_scripts_soak_harness.py``; this test
proves the *host.detach_from_vds bundle* is well-formed and grades
exactly as the runbook (``docs/cross-repo/dual-run-soak-harness.md``,
host.detach_from_vds section) documents.

It is deterministic and has **no** live-lab dependency — the real
holodeck dual-run (stage 5) is an operator protocol the runbook owns;
this test exercises stages 1, 3, and 4 against the static bundle.

Skips cleanly when ``jq`` / ``bash`` are unavailable (the driver
requires them) so the gate is honest about not having run rather than
vacuously green.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DRIVER = _REPO_ROOT / "scripts" / "soak" / "soak-harness.sh"
_EXAMPLE = _REPO_ROOT / "scripts" / "soak" / "examples" / "host.detach_from_vds"
_OP = "vmware.composite.host.detach_from_vds"
_CONNECTOR = "vmware-rest-8.x"

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
            _OP,
            "--connector",
            _CONNECTOR,
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


def test_bundle_exists() -> None:
    assert _EXAMPLE.is_dir()
    for name in (
        "wrapper-plan.json",
        "meho-plan.json",
        "wrapper-state.json",
        "meho-state.json",
        "audit-rows.json",
        "broadcast-events.json",
        "meta.json",
    ):
        assert (_EXAMPLE / name).is_file(), f"missing evidence file {name}"


def test_clean_bundle_grades_shadow(tmp_path: Path) -> None:
    """Stages 1/3/4 clean + no live-soak attestation → 🟡 SHADOW, exit 0.

    SHADOW (not READY) is the correct cell after a single harness run:
    the automatable stages pass, so the op is ready to *enter* the
    bounded live soak (stage 5) — it is not retirement-ready until the
    operator re-runs with ``--soak-clean`` after a clean soak window.
    """
    ev = _seed(tmp_path)
    proc = _run(ev)
    assert proc.returncode == 0, proc.stderr
    report = json.loads((ev / "soak-report.json").read_text())
    assert report["op_id"] == _OP
    assert report["connector_id"] == _CONNECTOR
    assert report["all_passed"] is True
    assert report["has_blocker"] is False
    assert report["supported_scorecard_cell"] == "shadow"
    # The two governance-bracketing approval rows + the single dispatch
    # row + single write broadcast are what stage 4 asserts; the bundle
    # carries exactly that shape, so stage 4 must pass with zero blockers.
    stage4 = next(s for s in report["stages"] if s["stage"] == 4)
    assert stage4["passed"] is True
    assert [f for f in stage4["findings"] if f["severity"] == "blocker"] == []


def test_soak_clean_flag_promotes_to_ready(tmp_path: Path) -> None:
    """With the operator's stage-5 attestation, the bundle grades ✅ READY.

    Mirrors the runbook's 🟡→✅ transition: only an operator who has
    reviewed a clean live-soak log passes ``--soak-clean``, which is the
    sole path to the READY cell that makes the wrapper deletable.
    """
    ev = _seed(tmp_path)
    proc = _run(ev, "--soak-clean")
    assert proc.returncode == 0, proc.stderr
    report = json.loads((ev / "soak-report.json").read_text())
    assert report["supported_scorecard_cell"] == "ready"
    assert report["soak_clean_attested"] is True


def test_nic_migration_divergence_blocks(tmp_path: Path) -> None:
    """A MEHO plan that migrates a NIC to a different fallback network is a
    blocker → ⛔ BLOCKED, exit 1, wrapper must NOT be retired.

    This is the exact failure class the harness exists to catch: a
    silently diverging plan that would leave a VM stranded on the wrong
    network after the host loses DVS connectivity.
    """
    ev = _seed(tmp_path)
    meho_plan = json.loads((ev / "meho-plan.json").read_text())
    meho_plan["planned_nic_migrations"][0]["to"] = "network-WRONG"
    (ev / "meho-plan.json").write_text(json.dumps(meho_plan))
    proc = _run(ev)
    assert proc.returncode == 1, proc.stdout
    report = json.loads((ev / "soak-report.json").read_text())
    assert report["has_blocker"] is True
    assert report["supported_scorecard_cell"] == "blocked"


def test_missing_dispatch_audit_row_blocks(tmp_path: Path) -> None:
    """Dropping the ``path == op_id`` dispatch audit row trips the stage-4
    durable-write-record clause → ⛔ BLOCKED.

    Proves the bundle exercises the #817 governance invariant for this
    op, not just the plan/state legs.
    """
    ev = _seed(tmp_path)
    rows = json.loads((ev / "audit-rows.json").read_text())
    rows = [r for r in rows if r["path"] != _OP]
    (ev / "audit-rows.json").write_text(json.dumps(rows))
    proc = _run(ev)
    assert proc.returncode == 1, proc.stdout
    report = json.loads((ev / "soak-report.json").read_text())
    assert report["has_blocker"] is True
    assert report["supported_scorecard_cell"] == "blocked"
