# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-stage tests for the dual-run soak harness verifier (G11.7-T2 #1402).

The harness is the graduation gate every Phase-C write slice runs an op
through before its wrapper is retired, so each of the four automatable
stages gets its own test class — both the pass path and the
divergence/gap path that must pin the scorecard cell. The ``scripts``
package is on ``sys.path`` via ``pythonpath = ["."]`` (pyproject), the
same import path ``test_scripts_annotate_github_write_ops.py`` uses.
"""

from __future__ import annotations

from scripts.soak_harness import (
    REDACTED_OP_CLASSES,
    ScorecardCell,
    Severity,
    SoakReport,
    StageResult,
    assert_approval_completeness,
    idempotency_drift,
    normalise,
    op_is_redacted,
    parity_diff,
    scorecard_cell,
    state_diff,
)

# ---------------------------------------------------------------------------
# normalise
# ---------------------------------------------------------------------------


class TestNormalise:
    def test_strips_cosmetic_keys_recursively(self) -> None:
        raw = {
            "metadata": {
                "name": "web",
                "uid": "abc-123",
                "creationTimestamp": "2026-05-31T00:00:00Z",
                "resourceVersion": "9921",
            },
            "spec": {"replicas": 3},
        }
        out = normalise(raw)
        assert out == {"metadata": {"name": "web"}, "spec": {"replicas": 3}}

    def test_scalar_lists_compare_order_insensitive(self) -> None:
        assert normalise({"x": [3, 1, 2]}) == normalise({"x": [2, 3, 1]})

    def test_lists_of_mappings_keep_position(self) -> None:
        # Element identity in a list of objects is positional — do not sort.
        a = normalise({"c": [{"k": "a"}, {"k": "b"}]})
        b = normalise({"c": [{"k": "b"}, {"k": "a"}]})
        assert a != b

    def test_extra_cosmetic_extends_the_global_set(self) -> None:
        raw = {"name": "x", "myGeneratedToken": "tok-9"}
        assert normalise(raw, extra_cosmetic=["myGeneratedToken"]) == {"name": "x"}


# ---------------------------------------------------------------------------
# Stage 1 — dry-run / read-back parity
# ---------------------------------------------------------------------------


class TestStage1Parity:
    def test_identical_plans_pass(self) -> None:
        wrapper = {"kind": "Deployment", "spec": {"replicas": 3}, "resourceVersion": "1"}
        meho = {"kind": "Deployment", "spec": {"replicas": 3}, "resourceVersion": "2"}
        res = parity_diff(wrapper, meho)
        assert res.passed
        assert res.blockers == []

    def test_semantic_divergence_is_a_blocker(self) -> None:
        wrapper = {"spec": {"replicas": 3}}
        meho = {"spec": {"replicas": 5}}
        res = parity_diff(wrapper, meho)
        assert not res.passed
        assert len(res.blockers) == 1
        assert "spec.replicas" in res.blockers[0]["diff"]

    def test_meho_reduced_envelope_does_not_register(self) -> None:
        # MEHO omits a cosmetic field the wrapper includes — not a divergence.
        wrapper = {"spec": {"replicas": 3}, "managedFields": [{"manager": "kubectl"}]}
        meho = {"spec": {"replicas": 3}}
        assert parity_diff(wrapper, meho).passed


# ---------------------------------------------------------------------------
# Stage 3 — state diff + idempotency
# ---------------------------------------------------------------------------


class TestStage3StateDiff:
    def test_cosmetic_only_difference_passes(self) -> None:
        wrapper = {"replicas": 3, "uid": "w-1", "creationTimestamp": "t1"}
        meho = {"replicas": 3, "uid": "m-2", "creationTimestamp": "t2"}
        assert state_diff(wrapper, meho).passed

    def test_semantic_divergence_blocks(self) -> None:
        res = state_diff({"replicas": 3}, {"replicas": 4})
        assert not res.passed
        assert res.blockers[0]["path"] == "replicas"

    def test_explained_divergence_does_not_block(self) -> None:
        res = state_diff(
            {"annotations": {"legacy": "y"}, "replicas": 3},
            {"replicas": 3},
            explained={"annotations": "MEHO intentionally drops the deprecated legacy annotation"},
        )
        assert res.passed
        assert res.findings[0]["severity"] == Severity.EXPLAINED.value
        assert res.blockers == []

    def test_idempotent_double_run_no_drift_passes(self) -> None:
        first = {"data": {"k": "v"}, "resourceVersion": "10"}
        second = {"data": {"k": "v"}, "resourceVersion": "11"}
        assert idempotency_drift(first, second).passed

    def test_idempotency_drift_blocks(self) -> None:
        res = idempotency_drift({"count": 1}, {"count": 2})
        assert not res.passed
        assert res.blockers


# ---------------------------------------------------------------------------
# Stage 4 — audit + broadcast + approval completeness
# ---------------------------------------------------------------------------


def _request_row() -> dict[str, str]:
    return {"path": "approval.request", "operator_sub": "operator:alice"}


def _decision_row() -> dict[str, str]:
    return {"path": "approval.decision", "operator_sub": "operator:bob"}


class TestStage4ApprovalCompleteness:
    def test_clean_dangerous_write_passes(self) -> None:
        res = assert_approval_completeness(
            "k8s.scale",
            audit_rows=[_request_row(), _decision_row()],
            broadcast_events=[
                {"op_id": "approval.request"},
                {"op_id": "approval.decision"},
                {"op_id": "k8s.scale", "payload": {"op_class": "write", "result_status": "ok"}},
            ],
            returned_after_decision=True,
        )
        assert res.passed, res.findings

    def test_missing_decision_row_blocks(self) -> None:
        res = assert_approval_completeness(
            "k8s.scale",
            audit_rows=[_request_row()],
            broadcast_events=[{"op_id": "k8s.scale", "payload": {}}],
            returned_after_decision=True,
        )
        assert not res.passed
        kinds = {f["kind"] for f in res.blockers}
        assert "approval_decision_row_count" in kinds

    def test_duplicate_request_row_blocks(self) -> None:
        res = assert_approval_completeness(
            "k8s.scale",
            audit_rows=[_request_row(), _request_row(), _decision_row()],
            broadcast_events=[{"op_id": "k8s.scale", "payload": {}}],
            returned_after_decision=True,
        )
        assert not res.passed
        assert any(f["kind"] == "approval_request_row_count" for f in res.blockers)

    def test_premature_return_blocks(self) -> None:
        res = assert_approval_completeness(
            "k8s.scale",
            audit_rows=[_request_row(), _decision_row()],
            broadcast_events=[{"op_id": "k8s.scale", "payload": {}}],
            returned_after_decision=False,
        )
        assert not res.passed
        assert any(f["kind"] == "premature_return" for f in res.blockers)

    def test_two_broadcast_events_for_op_blocks(self) -> None:
        res = assert_approval_completeness(
            "k8s.scale",
            audit_rows=[_request_row(), _decision_row()],
            broadcast_events=[
                {"op_id": "k8s.scale", "payload": {}},
                {"op_id": "k8s.scale", "payload": {}},
            ],
            returned_after_decision=True,
        )
        assert not res.passed
        assert any(f["kind"] == "broadcast_event_count" for f in res.blockers)

    def test_rejected_op_requires_zero_op_broadcasts(self) -> None:
        # A rejected op never executes → zero write-effect broadcasts is correct.
        res = assert_approval_completeness(
            "k8s.scale",
            audit_rows=[_request_row(), _decision_row()],
            broadcast_events=[{"op_id": "approval.decision"}],
            returned_after_decision=True,
            decision="rejected",
        )
        assert res.passed, res.findings

    def test_credential_write_leak_in_broadcast_blocks(self) -> None:
        # k8s.secret.create classifies credential_write (#1401) — a params
        # key on its broadcast event means the secret reached the feed.
        res = assert_approval_completeness(
            "k8s.secret.create",
            audit_rows=[_request_row(), _decision_row()],
            broadcast_events=[
                {
                    "op_id": "k8s.secret.create",
                    "payload": {"op_class": "credential_write", "params": {"data": "s3cret"}},
                },
            ],
            returned_after_decision=True,
        )
        assert not res.passed
        assert any(f["kind"] == "credential_leak" for f in res.blockers)

    def test_credential_write_aggregate_only_passes(self) -> None:
        res = assert_approval_completeness(
            "k8s.secret.create",
            audit_rows=[_request_row(), _decision_row()],
            broadcast_events=[
                {
                    "op_id": "k8s.secret.create",
                    "payload": {"op_class": "credential_write", "result_status": "ok"},
                },
            ],
            returned_after_decision=True,
        )
        assert res.passed, res.findings


# ---------------------------------------------------------------------------
# op-class redaction tracks the shipped classifier
# ---------------------------------------------------------------------------


class TestRedactionClassification:
    def test_known_redacted_ops(self) -> None:
        # These are the classes #1401 wired into classify_op.
        assert op_is_redacted("vault.kv.read")  # credential_read
        assert op_is_redacted("harbor.robot.create")  # credential_mint
        assert op_is_redacted("k8s.secret.create")  # credential_write
        assert op_is_redacted("vault.auth.userpass.write")  # credential_write
        assert op_is_redacted("audit.query")  # audit_query

    def test_plain_write_is_not_redacted(self) -> None:
        assert not op_is_redacted("k8s.scale")

    def test_redacted_set_matches_module_constant(self) -> None:
        # Guard against the harness drifting from its own documented set.
        assert (
            frozenset({"credential_read", "credential_mint", "credential_write", "audit_query"})
            == REDACTED_OP_CLASSES
        )


# ---------------------------------------------------------------------------
# scorecard cell derivation
# ---------------------------------------------------------------------------


def _report(*stages: StageResult) -> SoakReport:
    return SoakReport(op_id="k8s.scale", connector_id="k8s-1.x", stages=list(stages))


class TestScorecardCell:
    def test_blocker_pins_to_blocked(self) -> None:
        blocked_stage = StageResult(
            stage=1,
            name="x",
            passed=False,
            findings=[{"severity": Severity.BLOCKER.value, "kind": "k", "detail": "d"}],
        )
        cell = scorecard_cell(_report(blocked_stage), soak_clean=True)
        assert cell is ScorecardCell.BLOCKED

    def test_clean_stages_without_soak_is_shadow(self) -> None:
        ok = StageResult(stage=1, name="x", passed=True)
        assert scorecard_cell(_report(ok), soak_clean=False) is ScorecardCell.SHADOW

    def test_clean_stages_with_clean_soak_is_ready(self) -> None:
        ok = StageResult(stage=1, name="x", passed=True)
        assert scorecard_cell(_report(ok), soak_clean=True) is ScorecardCell.READY


# ---------------------------------------------------------------------------
# Report serialisation (the shell driver emits this)
# ---------------------------------------------------------------------------


class TestReportSerialisation:
    def test_to_json_round_trips(self) -> None:
        import json

        report = _report(
            parity_diff({"spec": {"replicas": 3}}, {"spec": {"replicas": 3}}),
            state_diff({"replicas": 3}, {"replicas": 3}),
        )
        parsed = json.loads(report.to_json())
        assert parsed["op_id"] == "k8s.scale"
        assert parsed["all_passed"] is True
        assert parsed["has_blocker"] is False
        assert len(parsed["stages"]) == 2
