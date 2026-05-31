#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# soak-harness.sh — dual-run soak harness driver (G11.7-T2 #1402).
#
# The reusable per-write-op graduation gate every Phase-C write slice
# (#1398 / #1399 / #1400 and the write Tasks under #1387 / #1388) runs
# an op through before its consumer wrapper is retired. It implements
# the 5-stage parity / state-diff / approval-completeness methodology
# the ops team's retirement scorecard (Phase-C/D gate) and the
# `govc-vs-meho-baseline` (P1–P6) require:
#
#   1. dry-run / read-back parity   — automatable (this driver + verifier)
#   2. dual-run on a disposable     — orchestrated here; effect captured
#      target                         via the shipped READ ops, not stdout
#   3. state diff, not framing      — automatable (verifier)
#   4. audit + broadcast + approval — automatable (verifier)
#      completeness
#   5. bounded live soak            — documented protocol (the runbook)
#
# This driver is the **consumer-extensible** entry point: a connector
# slice supplies the per-op command hooks (how to produce the wrapper
# plan, the MEHO plan, and read back each side's effect) and the driver
# pipes the evidence into `backend/scripts/soak_harness.py`, which owns
# every comparison rule. See `docs/cross-repo/dual-run-soak-harness.md`
# for the methodology, the consumer wiring contract, and the scorecard
# update procedure. The shape extends the consumer's
# `scripts/parity-check-<connector>.sh` (P6) rather than replacing it.
#
# It deliberately performs NO writes itself and builds NO new approval
# infrastructure — it dispatches the ops under test and reads the
# already-shipped audit / broadcast / READ surfaces back (G11.7's
# explicit non-goal: reuse the queue, do not rebuild it).
#
# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
#
#   scripts/soak/soak-harness.sh \
#     --op k8s.scale --connector k8s-1.x \
#     --evidence-dir ./soak-evidence/k8s.scale \
#     [--idempotent] [--soak-clean] [--explained-file path.json]
#
# The driver expects four evidence files in --evidence-dir, produced by
# the consumer's per-op hooks BEFORE this driver runs (the driver does
# not know how to drive an arbitrary connector — that is the consumer's
# `parity-check-<connector>.sh` job). The contract:
#
#   wrapper-plan.json    stage 1 — the wrapper's dry-run / server-preview
#   meho-plan.json       stage 1 — `meho ... --dry-run` output
#   wrapper-state.json   stage 3 — post-op read-back of the wrapper's effect
#   meho-state.json      stage 3 — post-op read-back of MEHO's effect
#   audit-rows.json      stage 4 — array of audit_log rows for the op window
#   broadcast-events.json stage 4 — array of broadcast events for the window
#   meta.json            stage 4 — { "returned_after_decision": bool,
#                                     "decision": "approved"|"rejected" }
#
# When --idempotent is passed the driver also expects:
#   meho-state-2.json    stage 3 — read-back after a SECOND MEHO run
#
# Flags:
#   --op <op_id>            REQUIRED. The op under test (e.g. k8s.scale).
#   --connector <id>        REQUIRED. The connector_id (e.g. k8s-1.x).
#   --evidence-dir <dir>    REQUIRED. Where the evidence files live and
#                           where soak-report.json is written.
#   --idempotent            Run the stage-3 double-run idempotency leg.
#   --soak-clean            Attest stage 5 ran clean (N≥10 / ~2 weeks,
#                           zero unexplained diffs). Only an operator who
#                           has reviewed the live-soak log passes this —
#                           it moves the supported scorecard cell 🟡→✅.
#   --explained-file <path> JSON object mapping a dotted state-diff path
#                           to a rationale string; those divergences are
#                           recorded EXPLAINED, not blocking.
#   -h | --help             Print this help and exit 0.
#
# Exit codes (mirror smoke.sh / install-verify.sh):
#   0  → every automatable stage (1,3,4) passed; the op is at least
#        shadow-ready (🟡), or ✅ when --soak-clean was attested.
#   1  → at least one stage produced a blocker; the op stays ⛔ and its
#        wrapper must NOT be retired. soak-report.json has the detail.
#   2  → usage / environment error (missing args, evidence file absent,
#        python verifier not importable).

set -euo pipefail

# ---------------------------------------------------------------------------
# Output vocabulary (matches scripts/acceptance/smoke.sh)
# ---------------------------------------------------------------------------
PASS_COUNT=0
FAIL_COUNT=0
check_ok()   { PASS_COUNT=$((PASS_COUNT + 1)); printf '[ok]   %s\n' "$1"; }
check_fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf '[FAIL] %s\n' "$1" >&2; }
check_note() { printf '[note] %s\n' "$1"; }
usage_err()  { printf '[usage] %s\n' "$1" >&2; exit 2; }

OP=""
CONNECTOR=""
EVIDENCE_DIR=""
IDEMPOTENT=0
SOAK_CLEAN=0
EXPLAINED_FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --op)             OP="${2:-}"; shift 2 ;;
    --connector)      CONNECTOR="${2:-}"; shift 2 ;;
    --evidence-dir)   EVIDENCE_DIR="${2:-}"; shift 2 ;;
    --explained-file) EXPLAINED_FILE="${2:-}"; shift 2 ;;
    --idempotent)     IDEMPOTENT=1; shift ;;
    --soak-clean)     SOAK_CLEAN=1; shift ;;
    -h|--help)        sed -n '2,90p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)                usage_err "unknown argument: $1" ;;
  esac
done

[[ -n "$OP" ]]           || usage_err "--op is required"
[[ -n "$CONNECTOR" ]]    || usage_err "--connector is required"
[[ -n "$EVIDENCE_DIR" ]] || usage_err "--evidence-dir is required"
[[ -d "$EVIDENCE_DIR" ]] || usage_err "evidence dir not found: $EVIDENCE_DIR"

command -v jq >/dev/null 2>&1 || usage_err "jq not on PATH"

# Resolve the backend dir so the verifier imports with pythonpath=["."].
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
[[ -d "$BACKEND_DIR" ]] || usage_err "backend dir not found at $BACKEND_DIR"

require_file() {
  [[ -f "$EVIDENCE_DIR/$1" ]] || usage_err "missing evidence file: $EVIDENCE_DIR/$1 (see --help for the contract)"
}
require_file wrapper-plan.json
require_file meho-plan.json
require_file wrapper-state.json
require_file meho-state.json
require_file audit-rows.json
require_file broadcast-events.json
require_file meta.json
if [[ "$IDEMPOTENT" -eq 1 ]]; then
  require_file meho-state-2.json
fi

REPORT="$EVIDENCE_DIR/soak-report.json"

# ---------------------------------------------------------------------------
# Run the verifier. All comparison logic lives in soak_harness.py; this
# driver only marshals the evidence files into it and renders the verdict.
# A heredoc Python program keeps the consumer's install surface to "have
# the backend venv" — no extra entry-point packaging.
# ---------------------------------------------------------------------------
printf '== soak harness: op=%s connector=%s ==\n' "$OP" "$CONNECTOR"

set +e
(
  cd "$BACKEND_DIR" && \
  OP="$OP" CONNECTOR="$CONNECTOR" EVIDENCE_DIR="$EVIDENCE_DIR" \
  IDEMPOTENT="$IDEMPOTENT" SOAK_CLEAN="$SOAK_CLEAN" \
  EXPLAINED_FILE="$EXPLAINED_FILE" REPORT="$REPORT" \
  uv run python - <<'PY'
import json
import os
from pathlib import Path

from scripts.soak_harness import (
    SoakReport,
    assert_approval_completeness,
    idempotency_drift,
    parity_diff,
    scorecard_cell,
    state_diff,
)

ev = Path(os.environ["EVIDENCE_DIR"])
op = os.environ["OP"]
connector = os.environ["CONNECTOR"]
idempotent = os.environ["IDEMPOTENT"] == "1"
soak_clean = os.environ["SOAK_CLEAN"] == "1"
explained_file = os.environ["EXPLAINED_FILE"]


def load(name: str):
    return json.loads((ev / name).read_text())


explained = json.loads(Path(explained_file).read_text()) if explained_file else {}
meta = load("meta.json")

report = SoakReport(op_id=op, connector_id=connector)
report.stages.append(parity_diff(load("wrapper-plan.json"), load("meho-plan.json")))
report.stages.append(
    state_diff(load("wrapper-state.json"), load("meho-state.json"), explained=explained)
)
if idempotent:
    report.stages.append(idempotency_drift(load("meho-state.json"), load("meho-state-2.json")))
report.stages.append(
    assert_approval_completeness(
        op,
        audit_rows=load("audit-rows.json"),
        broadcast_events=load("broadcast-events.json"),
        returned_after_decision=bool(meta.get("returned_after_decision", False)),
        decision=meta.get("decision", "approved"),
    )
)

cell = scorecard_cell(report, soak_clean=soak_clean)
out = report.to_dict()
out["supported_scorecard_cell"] = cell.value
out["soak_clean_attested"] = soak_clean
Path(os.environ["REPORT"]).write_text(json.dumps(out, indent=2, sort_keys=True))

# Emit a compact per-stage summary on stdout for the shell layer to render.
for stage in report.stages:
    blockers = [f for f in stage.findings if f.get("severity") == "blocker"]
    status = "ok" if stage.passed else "FAIL"
    print(f"STAGE\t{stage.stage}\t{status}\t{stage.name}\t{len(blockers)}")
print(f"CELL\t{cell.value}")
PY
)
VERIFIER_RC=$?
set -e

if [[ "$VERIFIER_RC" -ne 0 ]]; then
  check_fail "verifier failed to run (rc=$VERIFIER_RC) — check the backend venv and evidence shapes"
  exit 2
fi

# The verifier wrote soak-report.json; re-read it as the source of truth for
# the exit decision (the stdout summary is for humans).
HAS_BLOCKER="$(jq -r '.has_blocker' "$REPORT")"
ALL_PASSED="$(jq -r '.all_passed' "$REPORT")"
CELL="$(jq -r '.supported_scorecard_cell' "$REPORT")"

# Process substitution (not a pipe) so the PASS_COUNT/FAIL_COUNT increments
# land in this shell rather than a subshell that exits before the summary.
while IFS=$'\t' read -r n p name; do
  if [[ "$p" == "true" ]]; then check_ok "stage $n — $name"; else check_fail "stage $n — $name"; fi
done < <(jq -r '.stages[] | "\(.stage)\t\(.passed)\t\(.name)"' "$REPORT")

case "$CELL" in
  blocked) check_note "supported scorecard cell: ⛔ blocked (a stage produced a blocker; wrapper stays)" ;;
  shadow)  check_note "supported scorecard cell: 🟡 shadow (stages 1–4 clean; run the stage-5 live soak next)" ;;
  ready)   check_note "supported scorecard cell: ✅ ready (clean soak attested; wrapper may be retired)" ;;
esac
check_note "report: $REPORT"

printf '== %d ok, %d failed ==\n' "$PASS_COUNT" "$FAIL_COUNT"
if [[ "$HAS_BLOCKER" == "true" || "$ALL_PASSED" != "true" ]]; then
  exit 1
fi
exit 0
