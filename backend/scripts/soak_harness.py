# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dual-run soak harness verifier (G11.7-T2 #1402).

The reusable, per-write-op **graduation gate** that every Phase-C write
slice (#1398 / #1399 / #1400 and the write Tasks under #1387 / #1388)
runs an op through before its consumer wrapper is retired. The shell
driver ``scripts/soak/soak-harness.sh`` orchestrates the live legs
(dispatch the op, read back state via the shipped READ ops, query the
audit feed); this module is the **decision core** it pipes evidence
into — the part that must be deterministic, unit-tested, and the same
across every connector.

Why a Python module and not just shell
======================================

Three of the five soak stages reduce to a comparison that is fiddly to
get right in ``jq`` and easy to get *subtly* wrong (the field-evidence
failure the Task cites — ``meho-drove-the-op-and-the-connector-broke``
— was exactly a subtle, unflagged diff). The comparison rules live
here, in one place, with tests:

* **Stage 1 — dry-run / read-back parity.** Does the MEHO op resolve
  the *same target + params + plan* the wrapper would? Implemented as
  :func:`parity_diff`, which normalises both sides before comparing so
  a cosmetic spelling difference (key order, MEHO's reduced envelope)
  does not read as a divergence — only a *semantic* one does.
* **Stage 3 — state diff, not framing.** Read post-op state back via
  the already-shipped READ ops and compare wrapper-effect vs
  MEHO-effect, ignoring the *cosmetic* fields that always differ
  (timestamps, generated UIDs, resourceVersion). Implemented as
  :func:`state_diff` + :func:`idempotency_drift`.
* **Stage 4 — audit + broadcast + approval completeness.** The
  #817 invariant: one MEHO write must produce exactly one audit row,
  exactly one broadcast event, and — being ``dangerous`` +
  ``requires_approval`` — exactly the **two synchronous approval audit
  rows** (``approval.request`` + ``approval.decision``), with the op
  not returning until the decision row commits. Implemented as
  :func:`assert_approval_completeness` over rows read back from
  ``audit_log`` and the captured broadcast feed.

Stages 2 (dual-run on a disposable target) and 5 (bounded live soak)
are operational protocols the shell driver and the runbook own; this
module supplies the comparison primitives they feed evidence into and
the :class:`StageResult` / :class:`SoakReport` shapes the driver emits
as JSON.

This module imports only the backplane's already-shipped redaction
classifier (:func:`~meho_backplane.broadcast.events.classify_op`) and
the approval-row path constants — it builds **no** new queue, table,
or dispatch machinery (that is #1397's explicit non-goal, inherited).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from meho_backplane.broadcast.events import classify_op
from scripts.soak_models import ScorecardCell, Severity, SoakReport, StageResult

__all__ = [
    "COSMETIC_KEYS",
    "REDACTED_OP_CLASSES",
    "ScorecardCell",
    "Severity",
    "SoakReport",
    "StageResult",
    "assert_approval_completeness",
    "idempotency_drift",
    "normalise",
    "op_is_redacted",
    "parity_diff",
    "scorecard_cell",
    "state_diff",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Fields that always differ between two otherwise-identical effects and
#: carry no semantic meaning. Stripped recursively before a state diff so
#: a fresh ``creationTimestamp`` / ``uid`` / ``resourceVersion`` does not
#: read as a real divergence. Connectors extend this set per-op via the
#: ``extra_cosmetic`` argument rather than mutating the module global.
#:
#: The names cover the common shapes across the connectors that graduate
#: through this harness: Kubernetes object metadata
#: (``resourceVersion`` / ``uid`` / ``creationTimestamp`` /
#: ``generation`` / ``managedFields``), VCF/vSphere task envelopes
#: (``taskId`` / ``startTime`` / ``endTime``), and Vault lease metadata
#: (``lease_id`` / ``lease_duration`` / ``request_id``).
COSMETIC_KEYS: frozenset[str] = frozenset(
    {
        # Kubernetes object metadata
        "resourceVersion",
        "uid",
        "creationTimestamp",
        "generation",
        "managedFields",
        "selfLink",
        # VCF / vSphere task envelope
        "taskId",
        "task_id",
        "startTime",
        "endTime",
        # Vault lease metadata
        "lease_id",
        "lease_duration",
        "request_id",
        # Generic audit/trace noise
        "timestamp",
        "occurred_at",
        "duration_ms",
    }
)

#: Op-sensitivity classes whose broadcast payload must collapse to the
#: aggregate-only view (no params, no response secret). Mirrors the
#: sensitive set the broadcast resolver enforces (G6.1 + G11.7-T1 #1401)
#: — the harness asserts membership rather than re-deriving it, so a
#: future class added to the backplane is picked up by re-reading
#: :func:`classify_op` output against this set.
REDACTED_OP_CLASSES: frozenset[str] = frozenset(
    {
        "credential_read",
        "credential_mint",
        "credential_write",
        "audit_query",
    }
)

#: The two audit-row paths a ``requires_approval`` write must emit, in
#: order, per the #817 synchronous-approval invariant.
_APPROVAL_REQUEST_PATH = "approval.request"
_APPROVAL_DECISION_PATH = "approval.decision"


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def normalise(value: Any, *, extra_cosmetic: Iterable[str] = ()) -> Any:
    """Return *value* with cosmetic noise stripped and ordering made stable.

    Recursively:

    * drops any mapping key in :data:`COSMETIC_KEYS` (plus *extra_cosmetic*),
    * sorts list elements that are themselves comparable scalars so a
      read-back that returns the same set in a different order does not
      read as a diff (lists of mappings keep their order — element
      identity there is positional),
    * leaves scalars untouched.

    The result is a value safe to compare with ``==`` for semantic
    equivalence. Comparing the *normalised* forms is what makes a
    state diff "not framing": two effects that differ only in a fresh
    ``uid`` or a reordered label set compare equal.
    """
    cosmetic = COSMETIC_KEYS | frozenset(extra_cosmetic)
    return _normalise(value, cosmetic)


def _normalise(value: Any, cosmetic: frozenset[str]) -> Any:
    if isinstance(value, Mapping):
        return {k: _normalise(v, cosmetic) for k, v in sorted(value.items()) if k not in cosmetic}
    if isinstance(value, (list, tuple)):
        items = [_normalise(v, cosmetic) for v in value]
        if items and all(isinstance(i, (str, int, float, bool)) for i in items):
            # Scalar list → order-insensitive. Sort by JSON form to give a
            # total order across mixed scalar types without a TypeError.
            return sorted(items, key=lambda i: json.dumps(i, sort_keys=True))
        return items
    return value


# ---------------------------------------------------------------------------
# Stage 1 — dry-run / read-back parity
# ---------------------------------------------------------------------------


def parity_diff(
    wrapper_plan: Mapping[str, Any],
    meho_plan: Mapping[str, Any],
    *,
    extra_cosmetic: Iterable[str] = (),
) -> StageResult:
    """Stage 1: prove the MEHO op resolves the same plan as the wrapper.

    Both *wrapper_plan* and *meho_plan* are the dry-run / server-preview
    outputs each side produces for the **same** operator intent
    (``kubectl apply --dry-run=server`` vs ``meho k8s apply
    --dry-run``; a VCF task-preview / DRS recommendation). They are
    normalised (:func:`normalise`) before comparison so MEHO's reduced
    envelope and cosmetic field differences do not register.

    A non-empty diff after normalisation is a **blocker** — the Task's
    "diverging plans fail before anything writes" rule. The finding
    records the normalised diff so the operator sees exactly which
    semantic field diverged.
    """
    w = normalise(dict(wrapper_plan), extra_cosmetic=extra_cosmetic)
    m = normalise(dict(meho_plan), extra_cosmetic=extra_cosmetic)
    diff = _semantic_diff(w, m)
    findings: list[dict[str, Any]] = []
    if diff:
        findings.append(
            {
                "severity": Severity.BLOCKER.value,
                "kind": "plan_divergence",
                "detail": "MEHO dry-run plan diverges from the wrapper plan",
                "diff": diff,
            }
        )
    return StageResult(stage=1, name="dry-run/read-back parity", passed=not diff, findings=findings)


# ---------------------------------------------------------------------------
# Stage 3 — state diff + idempotency
# ---------------------------------------------------------------------------


def state_diff(
    wrapper_state: Mapping[str, Any],
    meho_state: Mapping[str, Any],
    *,
    extra_cosmetic: Iterable[str] = (),
    explained: Mapping[str, str] | None = None,
) -> StageResult:
    """Stage 3: compare post-op read-back state, cosmetic noise removed.

    *wrapper_state* and *meho_state* are the state each side leaves
    behind, read back through the **already-shipped READ ops** (not the
    write op's own return framing — the Task's "state diff, not
    framing"). Cosmetic fields are stripped; any remaining divergence is
    a blocker **unless** its dotted key path is present in *explained*
    (a divergence the operator has recorded a rationale for, e.g. MEHO
    deliberately omits a deprecated annotation the wrapper still sets).

    Explained divergences stay visible in the report at
    :attr:`Severity.EXPLAINED` so a reviewer can audit the rationale,
    but they do not pin the scorecard cell.
    """
    explained = explained or {}
    w = normalise(dict(wrapper_state), extra_cosmetic=extra_cosmetic)
    m = normalise(dict(meho_state), extra_cosmetic=extra_cosmetic)
    diff = _semantic_diff(w, m)
    findings: list[dict[str, Any]] = []
    has_blocker = False
    for path, delta in diff.items():
        if path in explained:
            findings.append(
                {
                    "severity": Severity.EXPLAINED.value,
                    "kind": "state_divergence",
                    "path": path,
                    "delta": delta,
                    "rationale": explained[path],
                }
            )
        else:
            has_blocker = True
            findings.append(
                {
                    "severity": Severity.BLOCKER.value,
                    "kind": "state_divergence",
                    "path": path,
                    "delta": delta,
                }
            )
    return StageResult(
        stage=3,
        name="state diff (post-op read-back)",
        passed=not has_blocker,
        findings=findings,
    )


def idempotency_drift(
    first_state: Mapping[str, Any],
    second_state: Mapping[str, Any],
    *,
    extra_cosmetic: Iterable[str] = (),
) -> StageResult:
    """Stage 3 (idempotency leg): run an idempotent op twice, prove no drift.

    For ops the Task names idempotent (``snapshot.revert``, ``kv.put``
    with the same value), running the MEHO op a second time must leave
    state identical to the first run once cosmetic noise is removed. A
    non-empty diff means the op is *not* idempotent in MEHO even though
    its wrapper is — a blocker.
    """
    a = normalise(dict(first_state), extra_cosmetic=extra_cosmetic)
    b = normalise(dict(second_state), extra_cosmetic=extra_cosmetic)
    diff = _semantic_diff(a, b)
    findings: list[dict[str, Any]] = []
    if diff:
        findings.append(
            {
                "severity": Severity.BLOCKER.value,
                "kind": "idempotency_drift",
                "detail": "second run of an idempotent op drifted from the first",
                "diff": diff,
            }
        )
    return StageResult(
        stage=3,
        name="idempotency (double-run)",
        passed=not diff,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Stage 4 — audit + broadcast + approval completeness
# ---------------------------------------------------------------------------


def op_is_redacted(op_id: str) -> bool:
    """Return ``True`` if *op_id* classifies into a redacted (aggregate-only)
    broadcast class.

    Delegates to the backplane's own :func:`classify_op` so the harness
    tracks the shipped classification (including the ``credential_write``
    / ``credential_mint`` classes #1401 added) rather than re-listing
    op-ids the harness would have to keep in sync by hand.
    """
    return classify_op(op_id) in REDACTED_OP_CLASSES


def assert_approval_completeness(
    op_id: str,
    *,
    audit_rows: Sequence[Mapping[str, Any]],
    broadcast_events: Sequence[Mapping[str, Any]],
    returned_after_decision: bool,
    decision: str = "approved",
) -> StageResult:
    """Stage 4: assert the governance-completeness invariant for one write.

    The #817 invariant a ``dangerous`` + ``requires_approval`` write must
    satisfy, checked against rows the driver read back from ``audit_log``
    and the captured broadcast feed:

    1. **Exactly one** ``approval.request`` audit row (the synchronous
       row :func:`create_pending_request` writes alongside the pending
       request).
    2. **Exactly one** ``approval.decision`` audit row (the row
       ``approve_request`` / ``reject_request`` writes).
    3. The op did **not** return before the decision row committed
       (*returned_after_decision* — the driver proves this by ordering
       the dispatch return against the decision-row timestamp).
    4. **Exactly one** ``audit_log`` row whose ``path`` equals *op_id* —
       the single dispatch audit row the dispatcher writes for the
       executed write (``_audit.py`` writes it with ``path=op_id`` once
       the op runs). This is the durable write-record half of the #817
       invariant the approval rows bracket. A rejected decision never
       executes, so it produces **zero** such rows.
    5. **Exactly one** non-approval broadcast event for the op
       (the publish-on-write hook's single event). Approval-lifecycle
       broadcast events (``approval.*``) are counted separately and not
       required to be exactly one.
    6. If *op_id* classifies as a redacted class
       (:func:`op_is_redacted`), **no** broadcast event for the op may
       carry a ``params`` key — the credential must never reach the
       feed.

    Each failed sub-check is a blocker. A rejected decision
    (``decision="rejected"``) relaxes checks 4 and 5: a rejected op
    never executes, so it produces no dispatch audit row and no
    write-effect broadcast — both checks then require *zero* op rows /
    events instead of one.
    """
    findings: list[dict[str, Any]] = []
    findings += _check_approval_audit_rows(audit_rows, returned_after_decision)
    findings += _check_write_audit_row(op_id, audit_rows, decision)
    findings += _check_op_broadcast(op_id, broadcast_events, decision)

    passed = not any(f["severity"] == Severity.BLOCKER.value for f in findings)
    return StageResult(
        stage=4,
        name="audit + broadcast + approval completeness",
        passed=passed,
        findings=findings,
    )


def _check_approval_audit_rows(
    audit_rows: Sequence[Mapping[str, Any]], returned_after_decision: bool
) -> list[dict[str, Any]]:
    """The two-row + synchronous-decision half of the stage-4 invariant."""
    findings: list[dict[str, Any]] = []
    request_rows = [r for r in audit_rows if r.get("path") == _APPROVAL_REQUEST_PATH]
    decision_rows = [r for r in audit_rows if r.get("path") == _APPROVAL_DECISION_PATH]

    if len(request_rows) != 1:
        findings.append(
            _blocker(
                "approval_request_row_count",
                f"expected exactly 1 {_APPROVAL_REQUEST_PATH} audit row, got {len(request_rows)}",
            )
        )
    if len(decision_rows) != 1:
        findings.append(
            _blocker(
                "approval_decision_row_count",
                f"expected exactly 1 {_APPROVAL_DECISION_PATH} audit row, got {len(decision_rows)}",
            )
        )
    if not returned_after_decision:
        findings.append(
            _blocker(
                "premature_return",
                "the op returned before its approval.decision row committed "
                "(synchronous-decision invariant violated)",
            )
        )
    return findings


def _check_write_audit_row(
    op_id: str, audit_rows: Sequence[Mapping[str, Any]], decision: str
) -> list[dict[str, Any]]:
    """The single dispatch audit row half of the stage-4 invariant.

    The dispatcher writes exactly one ``audit_log`` row whose ``path``
    equals the op-id when the op executes (``_audit.py``). The two
    ``approval.*`` rows bracket it; this row is the durable record that
    the write itself ran. A rejected decision never executes, so the
    expected count is zero.
    """
    findings: list[dict[str, Any]] = []
    write_rows = [r for r in audit_rows if r.get("path") == op_id]
    expected = 0 if decision == "rejected" else 1
    if len(write_rows) != expected:
        findings.append(
            _blocker(
                "write_audit_row_count",
                f"expected exactly {expected} audit_log row with path == {op_id!r} "
                f"(decision={decision}), got {len(write_rows)}",
            )
        )
    return findings


def _check_op_broadcast(
    op_id: str, broadcast_events: Sequence[Mapping[str, Any]], decision: str
) -> list[dict[str, Any]]:
    """The single-broadcast + no-credential-leak half of the stage-4 invariant."""
    findings: list[dict[str, Any]] = []
    op_events = [e for e in broadcast_events if e.get("op_id") == op_id]
    approval_events = [
        e for e in broadcast_events if str(e.get("op_id", "")).startswith("approval.")
    ]
    expected_op_events = 0 if decision == "rejected" else 1
    if len(op_events) != expected_op_events:
        findings.append(
            _blocker(
                "broadcast_event_count",
                f"expected exactly {expected_op_events} broadcast event(s) for {op_id} "
                f"(decision={decision}), got {len(op_events)}",
            )
        )

    if op_is_redacted(op_id):
        leaking = [e for e in op_events if "params" in (e.get("payload") or {})]
        if leaking:
            findings.append(
                _blocker(
                    "credential_leak",
                    f"{op_id} is a redacted op-class but a broadcast event carried "
                    "a `params` key — the credential reached the feed",
                )
            )

    # Surface the approval-lifecycle event count as INFO so the report is
    # self-describing without making it a gate (the two audit rows are the
    # durable record; the broadcast is fail-open by design).
    findings.append(
        {
            "severity": Severity.INFO.value,
            "kind": "approval_broadcast_count",
            "detail": f"{len(approval_events)} approval.* broadcast event(s) observed",
        }
    )
    return findings


# ---------------------------------------------------------------------------
# Scorecard cell derivation
# ---------------------------------------------------------------------------


def scorecard_cell(report: SoakReport, *, soak_clean: bool) -> ScorecardCell:
    """Map a :class:`SoakReport` to the write-column cell it supports.

    * Any blocker in stages 1-4 → ``BLOCKED`` (⛔): the automatable gate
      did not pass, the op is not even shadow-ready.
    * Stages 1-4 clean but *soak_clean* is ``False`` → ``SHADOW`` (🟡):
      the op is ready to enter the bounded live soak (stage 5) but has
      not completed it with zero unexplained diffs.
    * Stages 1-4 clean and *soak_clean* ``True`` → ``READY`` (✅).

    *soak_clean* is the human attestation that stage 5 ran for the
    bounded window (~2 weeks / N≥10 invocations) with zero unexplained
    diffs and zero governance gaps — the harness cannot derive it from a
    single run, so the runbook's documented procedure supplies it.

    An **empty report** (no stages ran) fails closed to ``BLOCKED``:
    ``all([])`` is vacuously ``True``, so without this guard a degenerate
    report with zero evidence would read as a pass and promote an op the
    harness never actually exercised. No evidence is a gap, not a pass.
    """
    if not report.stages or report.has_blocker or not report.all_passed:
        return ScorecardCell.BLOCKED
    if not soak_clean:
        return ScorecardCell.SHADOW
    return ScorecardCell.READY


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _blocker(kind: str, detail: str) -> dict[str, Any]:
    return {"severity": Severity.BLOCKER.value, "kind": kind, "detail": detail}


def _semantic_diff(a: Any, b: Any, *, prefix: str = "") -> dict[str, Any]:
    """Return the dotted-path → ``{wrapper, meho}`` diff between two
    already-normalised values. Empty dict means semantic equivalence."""
    diff: dict[str, Any] = {}
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        for key in sorted(set(a) | set(b)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in a:
                diff[path] = {"wrapper": _MISSING, "meho": b[key]}
            elif key not in b:
                diff[path] = {"wrapper": a[key], "meho": _MISSING}
            else:
                diff.update(_semantic_diff(a[key], b[key], prefix=path))
        return diff
    if a != b:
        diff[prefix or "<root>"] = {"wrapper": a, "meho": b}
    return diff


_MISSING = "<absent>"
