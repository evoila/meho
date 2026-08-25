# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""#3084 / #3122 regression tests — installer poll reduction stays drivable.

Proven live against a real VCF Installer 9.0.2 appliance: the first governed
``installer.sddc.spec.validate`` returned a JSONFlux-reduced ``result`` that
carried *only* the reduction bookkeeping (``row_count`` / ``total`` /
``sample_rows_returned`` / ``sample_bytes`` / ``source_key``) — the vendor
``Validation``'s top-level ``id`` / ``executionStatus`` / ``resultStatus``
were swallowed by the reduction, so the validation ``id`` needed to drive
``installer.sddc.validation.status`` had to be recovered out-of-band and the
submit → poll loop was undrivable through MEHO alone.

The fix is the opt-in ``result_scalars`` reduction hint (registered under
``llm_instructions``, threaded dispatcher → reducer like ``result_ordering``
/ ``pagination_hint``). These tests close the loop between the *registered*
hint metadata (:data:`INSTALLER_TYPED_OPS`) and the reducer behavior against
vendor-shaped payloads — the hint each test feeds the reducer is read off
the op tuple, never re-typed, so a drift between registration and test is
structurally impossible.

#3122 extends this to the ``SddcTask`` bring-up poll. Live, the vendor
``SddcTask`` carries BOTH ``sddcSubTasks[]`` (~260 rows) and a populated
``milestones[]``: two list fields, so the reducer's #2113 multi-list
detail-object exemption passed the whole document through UNREDUCED on every
150 s poll — the ``result_scalars`` hint never even fired. The fix is the
opt-in ``result_digest`` hint: it names ``sddcSubTasks`` as THE collection to
reduce (overriding the exemption) and drives an inline digest — per-``status``
counts, IN_PROGRESS names, and FAILED / COMPLETED_WITH_FAILURE sub-tasks kept
WHOLE with their ``errors[]`` so the restart-from-failed flow never drills in.
These tests read both hints off the op tuple (never re-typed), so a drift
between registration and behavior is structurally impossible.

Payload field names are pinned by the vendored ``vcf-installer-9.1`` OpenAPI
(``components.schemas.Validation`` / ``components.schemas.SddcTask`` /
``components.schemas.SddcSubTask``).
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.vcf_installer.typed_ops import INSTALLER_TYPED_OPS
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer

_OPS_BY_ID = {op.op_id: op for op in INSTALLER_TYPED_OPS}

#: The vendor object each op returns, keyed by op_id — the two submit/poll
#: pairs share their response shape, so the expected scalar keys pair up.
_VALIDATION_OPS = ("installer.sddc.spec.validate", "installer.sddc.validation.status")
_SDDC_TASK_OPS = (
    "installer.sddc.bringup.start",
    "installer.sddc.bringup.retry",
    "installer.sddc.bringup.status",
)


def _registered_scalars_context(op_id: str) -> dict[str, Any]:
    """The reducer context a dispatch of *op_id* carries, from live metadata.

    Reads ``llm_instructions["result_scalars"]`` off the registered op tuple
    — exactly the dict ``dispatcher._result_scalars_from_descriptor`` lifts
    from the persisted descriptor row at dispatch time.
    """
    instructions = _OPS_BY_ID[op_id].llm_instructions
    assert instructions is not None, f"{op_id} must register llm_instructions"
    hint = instructions.get("result_scalars")
    assert isinstance(hint, dict), f"{op_id} must register a result_scalars hint (#3084)"
    return {"op_id": op_id, "result_scalars": hint}


def _registered_reducer_context(op_id: str) -> dict[str, Any]:
    """The FULL reducer context a dispatch of *op_id* carries, from live metadata.

    Lifts both ``result_scalars`` (#3084) and ``result_digest`` (#3122) off the
    registered op tuple — exactly the two dicts the dispatcher's
    ``_result_scalars_from_descriptor`` / ``_result_digest_from_descriptor``
    forward from the persisted descriptor at dispatch time. Reading them off the
    tuple (never re-typing) keeps registration and test in lockstep.
    """
    instructions = _OPS_BY_ID[op_id].llm_instructions
    assert instructions is not None, f"{op_id} must register llm_instructions"
    context: dict[str, Any] = {"op_id": op_id}
    scalars = instructions.get("result_scalars")
    if isinstance(scalars, dict):
        context["result_scalars"] = scalars
    digest = instructions.get("result_digest")
    if isinstance(digest, dict):
        context["result_digest"] = digest
    return context


def _validation(check_count: int) -> dict[str, Any]:
    """A vendor ``Validation`` object with *check_count* checks."""
    return {
        "id": "6d1c8a4e-42d7-4f1b-9c58-0f8a2b3c4d5e",
        "description": "SDDC specification validation",
        "executionStatus": "IN_PROGRESS",
        "resultStatus": "UNKNOWN",
        "validationChecks": [
            {"description": f"check-{i}", "resultStatus": "SUCCEEDED"} for i in range(check_count)
        ],
    }


def _sddc_task(
    subtask_count: int,
    *,
    with_milestones: bool,
    active: int = 0,
    failed: int = 0,
) -> dict[str, Any]:
    """A vendor ``SddcTask`` with *subtask_count* sub-tasks.

    ``with_milestones=False`` models the early bring-up shape (the 202
    from ``POST /v1/sddcs`` and the first polls) where ``milestones`` is
    not yet populated — the shape whose single real list field made the
    pre-#3084 reduction swallow the task scalars. ``with_milestones=True``
    is the live multi-hour poll shape: two populated list fields, the #3122
    defect where the reducer's multi-list detail-object exemption passed the
    whole document through UNREDUCED.

    The first *active* sub-tasks are ``IN_PROGRESS``, the next *failed* are
    ``FAILED`` with a nested ``errors[]``, and the remainder are
    ``COMPLETED_WITH_SUCCESS`` — the mix the digest must summarize inline
    (names for active, whole rows for failed).
    """
    subtasks: list[dict[str, Any]] = []
    for i in range(subtask_count):
        if i < active:
            subtasks.append({"name": f"subtask-{i}", "status": "IN_PROGRESS"})
        elif i < active + failed:
            subtasks.append(
                {
                    "name": f"subtask-{i}",
                    "status": "FAILED",
                    "errors": [
                        {
                            "errorCode": "INVENTORY_INTERNAL_SERVER_ERROR",
                            "message": f"sub-task {i} boom",
                        }
                    ],
                }
            )
        else:
            subtasks.append({"name": f"subtask-{i}", "status": "COMPLETED_WITH_SUCCESS"})
    task: dict[str, Any] = {
        "id": "3f9e7d6c-5b4a-4938-8271-6a5b4c3d2e1f",
        "name": "sddc-bringup-lab01",
        "deploymentType": "INITIAL",
        "vcfInstanceName": "lab01",
        "status": "IN_PROGRESS",
        "creationTimestamp": "2026-08-20T14:00:00.000Z",
        "sddcSubTasks": subtasks,
    }
    if with_milestones:
        task["milestones"] = [{"name": f"milestone-{i}", "status": "PENDING"} for i in range(4)]
    return task


# ---------------------------------------------------------------------------
# Registration metadata — every primitive carries the right hint
# ---------------------------------------------------------------------------


def test_all_primitives_register_the_result_scalars_hint() -> None:
    """The hint is registered per response shape, poll key first.

    ``id`` must be listed for every op — it is the poll key the live
    defect swallowed — and the lifecycle/verdict scalars must match the
    vendor object the op returns.
    """
    for op_id in _VALIDATION_OPS:
        keys = _registered_scalars_context(op_id)["result_scalars"]["keys"]
        assert keys == ["id", "description", "executionStatus", "resultStatus"], op_id
    for op_id in _SDDC_TASK_OPS:
        keys = _registered_scalars_context(op_id)["result_scalars"]["keys"]
        assert keys == [
            "id",
            "name",
            "status",
            "deploymentType",
            "vcfInstanceName",
            "creationTimestamp",
        ], op_id


def test_sddc_task_ops_register_the_result_digest_hint() -> None:
    """#3122: every SddcTask op names sddcSubTasks + the active/failed states.

    The digest hint is what overrides the multi-list detail-object exemption
    (so a live two-list task reduces) and drives the inline digest. The
    validation ops must NOT carry it — their single-list validationChecks
    shape needs no override.
    """
    for op_id in _SDDC_TASK_OPS:
        instructions = _OPS_BY_ID[op_id].llm_instructions
        assert instructions is not None
        digest = instructions.get("result_digest")
        assert isinstance(digest, dict), f"{op_id} must register a result_digest hint (#3122)"
        assert digest["collection"] == "sddcSubTasks", op_id
        assert digest["status_field"] == "status", op_id
        assert digest["active_states"] == ["IN_PROGRESS"], op_id
        assert digest["failed_states"] == ["FAILED", "COMPLETED_WITH_FAILURE"], op_id
    for op_id in _VALIDATION_OPS:
        instructions = _OPS_BY_ID[op_id].llm_instructions
        assert instructions is not None
        assert "result_digest" not in instructions, f"{op_id} must not carry a digest hint"


# ---------------------------------------------------------------------------
# Validation — spec.validate / validation.status
# ---------------------------------------------------------------------------


async def test_validate_scalars_survive_when_checks_exceed_threshold() -> None:
    """AC 1+2: over-threshold ``validationChecks`` reduce; the poll keys stay.

    The exact live-defect shape: 60 checks clear the 50-row threshold, the
    checks materialize into a handle with drill-in, and — with the
    registered hint — ``id`` / ``executionStatus`` / ``resultStatus`` /
    ``description`` are top-level result fields the caller can poll with.
    """
    reducer = JsonFluxReducer(sample_size=5, sample_byte_budget=4096)
    payload = _validation(60)

    for op_id in _VALIDATION_OPS:
        reduced, handle = await reducer.reduce(
            dict(payload), None, _registered_scalars_context(op_id)
        )

        assert handle is not None, f"{op_id}: 60 checks must materialize a handle"
        assert handle.total_rows == 60
        assert handle.fetch_more is not None, f"{op_id}: drill-in envelope must ship"
        assert reduced["id"] == payload["id"], f"{op_id}: the poll id must survive"
        assert reduced["executionStatus"] == "IN_PROGRESS", op_id
        assert reduced["resultStatus"] == "UNKNOWN", op_id
        assert reduced["description"] == payload["description"], op_id
        assert reduced["row_count"] == 60, op_id
        assert reduced["source_key"] == "validationChecks", op_id
        assert "validationChecks" not in reduced, f"{op_id}: checks must stay reduced"


async def test_validate_small_check_set_passes_through_verbatim() -> None:
    """AC 4 (the "exactly when" bound): under threshold nothing changes.

    A 14-check validation (the live appliance's count) is under the 50-row
    threshold and within the byte bound, so the whole ``Validation``
    passes through verbatim — scalars trivially present, no handle. The
    hint must not force a reduction that the thresholds don't."""
    reducer = JsonFluxReducer()
    payload = _validation(14)

    reduced, handle = await reducer.reduce(
        payload, None, _registered_scalars_context("installer.sddc.spec.validate")
    )

    assert handle is None, "an under-threshold validation must not materialize"
    assert reduced is payload, "pass-through must return the exact vendor object"


# ---------------------------------------------------------------------------
# SddcTask — bringup.start / bringup.status
# ---------------------------------------------------------------------------


async def test_bringup_task_scalars_survive_single_list_reduction() -> None:
    """AC 3: ``SddcTask.id`` / ``status`` survive an ``sddcSubTasks`` reduction.

    The early bring-up shape — ``milestones`` not yet populated — has
    exactly one real list field, so ``_detect_collection`` reduces
    ``sddcSubTasks`` and (pre-#3084) swallowed the task scalars, including
    the ``id`` the status poll needs. The registered hints pin them, and
    (#3122) the digest rides inline.
    """
    reducer = JsonFluxReducer(sample_size=5, sample_byte_budget=4096)
    payload = _sddc_task(60, with_milestones=False, active=2, failed=1)

    for op_id in _SDDC_TASK_OPS:
        reduced, handle = await reducer.reduce(
            dict(payload), None, _registered_reducer_context(op_id)
        )

        assert handle is not None, f"{op_id}: 60 sub-tasks must materialize a handle"
        assert handle.total_rows == 60
        assert reduced["id"] == payload["id"], f"{op_id}: the poll id must survive"
        assert reduced["status"] == "IN_PROGRESS", op_id
        assert reduced["name"] == payload["name"], op_id
        assert reduced["vcfInstanceName"] == "lab01", op_id
        assert reduced["source_key"] == "sddcSubTasks", op_id
        assert "sddcSubTasks" not in reduced, f"{op_id}: sub-tasks must stay reduced"
        # #3122 digest rides inline: counts sum to the full set, active names
        # are listed, failures carry their errors[].
        assert reduced["status_counts"] == {
            "IN_PROGRESS": 2,
            "FAILED": 1,
            "COMPLETED_WITH_SUCCESS": 57,
        }, op_id
        assert sum(reduced["status_counts"].values()) == 60, op_id
        assert reduced["in_progress"] == ["subtask-0", "subtask-1"], op_id
        assert [row["name"] for row in reduced["failed"]] == ["subtask-2"], op_id
        assert reduced["failed"][0]["errors"][0]["errorCode"] == (
            "INVENTORY_INTERNAL_SERVER_ERROR"
        ), op_id


async def test_bringup_task_with_milestones_reduces_and_digests_the_live_gap() -> None:
    """#3122 root-cause fix: a two-list ``SddcTask`` now REDUCES, not passes through.

    Live, the vendor ``SddcTask`` carries BOTH ``sddcSubTasks[]`` (~260 rows)
    and a populated ``milestones[]`` — two real list fields, so the reducer's
    #2113 multi-list detail-object exemption used to pass the whole document
    through with ``handle=None`` (the defect this issue fixes: tens of KB every
    poll, the ``result_scalars`` hint never firing). The ``result_digest``
    hint names ``sddcSubTasks`` as THE collection, so it reduces despite the
    sibling ``milestones``. The full array goes behind the handle; the inline
    summary keeps the task scalars AND the sub-task digest — including every
    failure WITH its ``errors[]`` so restart-from-failed needs no drill-in.
    """
    reducer = JsonFluxReducer(sample_size=5, sample_byte_budget=4096)
    # 260 sub-tasks: 3 in progress, 2 failed (with errors), 255 succeeded.
    payload = _sddc_task(260, with_milestones=True, active=3, failed=2)

    for op_id in _SDDC_TASK_OPS:
        reduced, handle = await reducer.reduce(
            dict(payload), None, _registered_reducer_context(op_id)
        )

        assert handle is not None, f"{op_id}: the two-list live task MUST reduce (#3122)"
        assert handle.total_rows == 260, op_id
        assert handle.fetch_more is not None, f"{op_id}: drill-in envelope must ship"
        # Task scalars survive for the poll loop (#3084).
        assert reduced["id"] == payload["id"], op_id
        assert reduced["status"] == "IN_PROGRESS", op_id
        assert reduced["creationTimestamp"] == "2026-08-20T14:00:00.000Z", op_id
        # The reduced collection is sddcSubTasks; the full array is NOT inline.
        assert reduced["source_key"] == "sddcSubTasks", op_id
        assert "sddcSubTasks" not in reduced, op_id
        # Digest: per-state counts over the whole set, active names, failures.
        assert reduced["status_counts"] == {
            "IN_PROGRESS": 3,
            "FAILED": 2,
            "COMPLETED_WITH_SUCCESS": 255,
        }, op_id
        assert reduced["in_progress"] == ["subtask-0", "subtask-1", "subtask-2"], op_id
        assert [row["name"] for row in reduced["failed"]] == ["subtask-3", "subtask-4"], op_id
        for failed_row in reduced["failed"]:
            assert failed_row["status"] == "FAILED", op_id
            assert failed_row["errors"][0]["message"], f"{op_id}: failure keeps its errors[]"


async def test_two_list_task_without_digest_hint_stays_exempt() -> None:
    """The #2113 exemption is intact for ops that don't register a digest hint.

    The #3122 fix is strictly opt-in: it is the ``result_digest`` hint that
    overrides the multi-list detail-object exemption, NOT a blanket detection
    change. Fed a scalars-only context (no digest), the same two-list task
    still passes through verbatim — proving the generic exemption other
    connectors rely on (k8s.pod.info) is untouched.
    """
    reducer = JsonFluxReducer()
    payload = _sddc_task(60, with_milestones=True, active=3, failed=2)

    reduced, handle = await reducer.reduce(
        payload, None, _registered_scalars_context("installer.sddc.bringup.status")
    )

    assert handle is None, "without the digest hint a two-list task is still exempt"
    assert reduced is payload
    assert len(reduced["sddcSubTasks"]) == 60
    assert len(reduced["milestones"]) == 4


async def test_small_two_list_task_passes_through_even_with_digest_hint() -> None:
    """The digest hint must not force a reduction the thresholds don't warrant.

    An early poll with a handful of sub-tasks (under the 50-row threshold and
    within the byte bound) passes through verbatim even though the digest hint
    names ``sddcSubTasks`` — the hint changes WHICH collection reduces, never
    WHETHER the thresholds are met. Both arrays and the task scalars stay
    inline; no handle.
    """
    reducer = JsonFluxReducer()
    payload = _sddc_task(5, with_milestones=True, active=1)

    reduced, handle = await reducer.reduce(
        payload, None, _registered_reducer_context("installer.sddc.bringup.status")
    )

    assert handle is None, "an under-threshold task must not materialize"
    assert reduced is payload, "pass-through must return the exact vendor object"
    assert "status_counts" not in reduced, "no digest when nothing reduced"
