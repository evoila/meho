# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""#3084 regression tests — installer poll scalars survive JSONFlux reduction.

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

Payload field names are pinned by the vendored ``vcf-installer-9.1`` OpenAPI
(``components.schemas.Validation`` / ``components.schemas.SddcTask``).
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.vcf_installer.typed_ops import INSTALLER_TYPED_OPS
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer

_OPS_BY_ID = {op.op_id: op for op in INSTALLER_TYPED_OPS}

#: The vendor object each op returns, keyed by op_id — the two submit/poll
#: pairs share their response shape, so the expected scalar keys pair up.
_VALIDATION_OPS = ("installer.sddc.spec.validate", "installer.sddc.validation.status")
_SDDC_TASK_OPS = ("installer.sddc.bringup.start", "installer.sddc.bringup.status")


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


def _sddc_task(subtask_count: int, *, with_milestones: bool) -> dict[str, Any]:
    """A vendor ``SddcTask`` with *subtask_count* sub-tasks.

    ``with_milestones=False`` models the early bring-up shape (the 202
    from ``POST /v1/sddcs`` and the first polls) where ``milestones`` is
    not yet populated — the shape whose single real list field made the
    pre-#3084 reduction swallow the task scalars.
    """
    task: dict[str, Any] = {
        "id": "3f9e7d6c-5b4a-4938-8271-6a5b4c3d2e1f",
        "name": "sddc-bringup-lab01",
        "deploymentType": "INITIAL",
        "vcfInstanceName": "lab01",
        "status": "IN_PROGRESS",
        "creationTimestamp": "2026-08-20T14:00:00.000Z",
        "sddcSubTasks": [
            {"name": f"subtask-{i}", "status": "PENDING"} for i in range(subtask_count)
        ],
    }
    if with_milestones:
        task["milestones"] = [{"name": f"milestone-{i}", "status": "PENDING"} for i in range(4)]
    return task


# ---------------------------------------------------------------------------
# Registration metadata — every primitive carries the right hint
# ---------------------------------------------------------------------------


def test_all_four_primitives_register_the_result_scalars_hint() -> None:
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
    the ``id`` the status poll needs. The registered hint pins them.
    """
    reducer = JsonFluxReducer(sample_size=5, sample_byte_budget=4096)
    payload = _sddc_task(60, with_milestones=False)

    for op_id in _SDDC_TASK_OPS:
        reduced, handle = await reducer.reduce(
            dict(payload), None, _registered_scalars_context(op_id)
        )

        assert handle is not None, f"{op_id}: 60 sub-tasks must materialize a handle"
        assert handle.total_rows == 60
        assert reduced["id"] == payload["id"], f"{op_id}: the poll id must survive"
        assert reduced["status"] == "IN_PROGRESS", op_id
        assert reduced["name"] == payload["name"], op_id
        assert reduced["vcfInstanceName"] == "lab01", op_id
        assert reduced["source_key"] == "sddcSubTasks", op_id
        assert "sddcSubTasks" not in reduced, f"{op_id}: sub-tasks must stay reduced"


async def test_bringup_task_with_milestones_is_a_detail_object_passthrough() -> None:
    """A two-list ``SddcTask`` rides the #2113 detail-object exemption.

    When both ``sddcSubTasks`` and ``milestones`` are populated the payload
    is a dict-of-arrays detail object — more than one real list field — so
    it passes through verbatim (scalars trivially intact, both arrays
    retrievable, no handle). Pinned so a future detection change that
    starts reducing one of the two arrays cannot silently drop the other
    or the task scalars.
    """
    reducer = JsonFluxReducer()
    payload = _sddc_task(60, with_milestones=True)

    reduced, handle = await reducer.reduce(
        payload, None, _registered_scalars_context("installer.sddc.bringup.status")
    )

    assert handle is None, "a two-list SddcTask is a detail object, not a collection"
    assert reduced is payload
    assert reduced["id"] == payload["id"]
    assert len(reduced["sddcSubTasks"]) == 60
    assert len(reduced["milestones"]) == 4
