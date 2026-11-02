# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The seven Cloud Builder operations and their registrar.

All seven are hand-written (``source_kind="typed"``), so they work on a fresh
install with no spec ingest. Reads are ``safe``; every change sets
``requires_approval``. They share one group, ``installer-cb-bringup``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog

from meho_backplane.connectors.vcf_cloud_builder._schemas import (
    ABOUT_PARAMS,
    ABOUT_RESPONSE,
    CHANGE_RESPONSE,
    REPORT_PARAMS,
    REPORT_RESPONSE,
    SDDC_RETRY_PARAMS,
    SDDC_START_PARAMS,
    SDDC_STATUS_PARAMS,
    SDDC_STATUS_RESPONSE,
    VALIDATION_START_PARAMS,
    VALIDATION_STATUS_PARAMS,
    VALIDATION_STATUS_RESPONSE,
)
from meho_backplane.connectors.vcf_cloud_builder.typed_reads import (
    SUBTASK_FAILED,
    SUBTASK_RUNNING,
)
from meho_backplane.connectors.vcf_cloud_builder.typed_writes import (
    SDDC_RETRY_OP_ID,
    SDDC_START_OP_ID,
    SDDC_STATUS_OP_ID,
    VALIDATION_START_OP_ID,
    VALIDATION_STATUS_OP_ID,
)

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "CLOUD_BUILDER_GROUP",
    "CLOUD_BUILDER_TYPED_OPS",
    "CLOUD_BUILDER_WHEN_TO_USE",
    "CloudBuilderTypedOp",
    "register_cloud_builder_typed_operations",
]

_log = structlog.get_logger(__name__)

CLOUD_BUILDER_GROUP: Final = "installer-cb-bringup"
CLOUD_BUILDER_WHEN_TO_USE: Final = (
    "Use on a Cloud Builder appliance to bring up a VCF 5.0, 5.1 or 5.2 management domain. "
    "Reads: installer.cb.about (version), installer.cb.validation.status (a spec check, check "
    "by check, or all checks), installer.cb.sddc.status (a bring-up, task by task, or all "
    "bring-ups), installer.cb.sddc.report (the detail report or the validation report as a "
    "file). Changes, each waiting for approval: installer.cb.validation.start (check a "
    "spec), installer.cb.sddc.start (start the bring-up), installer.cb.sddc.retry (retry a "
    "failed bring-up)."
)

#: Tags of a call that sends a spec: "credential" makes the flight recorder keep
#: no request or answer body, because the sent spec holds the passwords.
_SPEC_TAGS: Final = ("vcf", "installer", "cloud-builder", "bringup", "write", "credential")
_READ_TAGS: Final = ("vcf", "installer", "cloud-builder", "bringup", "read")

_LOST_REPLY = (
    "If the reply is lost, do not call this again blindly: read {status} with no id and look "
    "for a running {kind}. "
)
_ASYNC_HINT = (
    "Call it with async: true, so MEHO keeps the answer (with the job id) under a run handle."
)


@dataclass(frozen=True)
class CloudBuilderTypedOp:
    """One Cloud Builder operation, as registered at start-up."""

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any]
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any]


def _change_instructions(
    *, when_to_call: str, status_op: str, kind: str, preconditions: str
) -> dict[str, Any]:
    return {
        "when_to_call": when_to_call,
        "output_shape": (
            "{status: accepted|already_running|invalid_input|not_reachable|check_failed|"
            "start_failed|start_unknown, job: {kind, id, vendor_state}, status_read: {op_id, "
            "params}, steps: [{step, result, error}], error, guidance}. accepted means the job "
            "started, not that it is done."
        ),
        "next_step": (
            f"Read {status_op} with job.id until the state is no longer running. "
            + _LOST_REPLY.format(status=status_op, kind=kind)
            + _ASYNC_HINT
        ),
        "preconditions": preconditions,
        "result_scalars": {"keys": ["status", "error", "guidance"]},
        "parameter_hints": {
            "spec": "The SddcSpec without any password or licence key.",
            "secrets": (
                "One entry per password or licence key of the spec: its JSON Pointer in the "
                "spec and the Vault path that holds it."
            ),
        },
    }


_ABOUT = CloudBuilderTypedOp(
    op_id="installer.cb.about",
    handler_attr="about",
    summary="Read the Cloud Builder version and the state of its bring-up service.",
    description=(
        "Reads GET /v1/sddcs/about: the bring-up service id, name, version and status. MEHO "
        "uses the same call to detect the version of a Cloud Builder target. Read-only."
    ),
    parameter_schema=ABOUT_PARAMS,
    response_schema=ABOUT_RESPONSE,
    tags=_READ_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": "Call first on a Cloud Builder target: it shows the version.",
        "output_shape": "{id, name, version, status}.",
        "next_step": "Check a spec with installer.cb.validation.start.",
    },
)

_VALIDATION_START = CloudBuilderTypedOp(
    op_id=VALIDATION_START_OP_ID,
    handler_attr="validation_start",
    summary="Check a VCF 5.x bring-up spec on Cloud Builder (deploys nothing).",
    description=(
        "Sends the spec to POST /v1/sddcs/validations. Passwords come from Vault references. "
        "Returns at once with the validation id; it does not wait for the result. Starts "
        "nothing when a validation already runs."
    ),
    parameter_schema=VALIDATION_START_PARAMS,
    response_schema=CHANGE_RESPONSE,
    tags=_SPEC_TAGS,
    safety_level="caution",
    requires_approval=True,
    llm_instructions=_change_instructions(
        when_to_call="Call with a full bring-up spec before installer.cb.sddc.start.",
        status_op=VALIDATION_STATUS_OP_ID,
        kind="validation",
        preconditions=(
            "The Vault paths in secrets exist and the caller may read them. A person must "
            "approve the call before it runs."
        ),
    ),
)

_VALIDATION_STATUS = CloudBuilderTypedOp(
    op_id=VALIDATION_STATUS_OP_ID,
    handler_attr="validation_status",
    summary="Read a Cloud Builder spec validation check by check, or list all validations.",
    description=(
        "With an id, reads GET /v1/sddcs/validations/{id}: the state, the result and each "
        "check with its error. Without an id, lists every validation (GET "
        "/v1/sddcs/validations). Read-only."
    ),
    parameter_schema=VALIDATION_STATUS_PARAMS,
    response_schema=VALIDATION_STATUS_RESPONSE,
    tags=_READ_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call after installer.cb.validation.start, with its job.id, until the state is "
            "no longer running. Call without an id to find a validation whose id you lost."
        ),
        "output_shape": (
            "{id, description, state: running|succeeded|failed|cancelled|unknown|"
            "not_reachable, vendor_state, result, checks_total, checks_finished, "
            "checks_failed, error, checks: [{description, severity, result, error}], "
            "checked_at}. Without an id: {validations: [{id, description, state, "
            "vendor_state, result}], checked_at}. not_reachable means Cloud Builder did not "
            "answer, not that the validation failed."
        ),
        "next_step": (
            "When the state is succeeded, start the bring-up with installer.cb.sddc.start. "
            "When it is failed, fix what the failed checks name and check again. "
            "installer.cb.sddc.report with validation_id gives the report as a file."
        ),
        "result_scalars": {
            "keys": [
                "id",
                "state",
                "vendor_state",
                "result",
                "checks_failed",
                "error",
                "reason",
            ]
        },
        "result_digest": {
            "collection": "checks",
            "status_field": "result",
            "name_field": "description",
            "active_states": ["IN_PROGRESS"],
            "failed_states": ["FAILED"],
        },
    },
)

_SDDC_START = CloudBuilderTypedOp(
    op_id=SDDC_START_OP_ID,
    handler_attr="sddc_start",
    summary="Start a VCF 5.x management domain bring-up on Cloud Builder from a checked spec.",
    description=(
        "Sends the spec to POST /v1/sddcs. Passwords come from Vault references. Returns as "
        "soon as Cloud Builder accepted the bring-up, with its id; the bring-up runs for hours. "
        "Starts nothing when a bring-up already runs."
    ),
    parameter_schema=SDDC_START_PARAMS,
    response_schema=CHANGE_RESPONSE,
    tags=_SPEC_TAGS,
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=_change_instructions(
        when_to_call=(
            "Call after installer.cb.validation.status says succeeded for the same spec."
        ),
        status_op=SDDC_STATUS_OP_ID,
        kind="bringup",
        preconditions=(
            "The same spec passed installer.cb.validation.start. The ESXi hosts are ready. "
            "A person must approve the call before it runs; an agent also needs a grant."
        ),
    ),
)

_SDDC_STATUS = CloudBuilderTypedOp(
    op_id=SDDC_STATUS_OP_ID,
    handler_attr="sddc_status",
    summary="Read a Cloud Builder bring-up task by task, or list all bring-ups.",
    description=(
        "With an id, reads GET /v1/sddcs/{id}: the state, the progress, each task and the "
        "first failed task with the vendor's error. Without an id, lists every bring-up "
        "(GET /v1/sddcs). Read-only."
    ),
    parameter_schema=SDDC_STATUS_PARAMS,
    response_schema=SDDC_STATUS_RESPONSE,
    tags=_READ_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call after installer.cb.sddc.start or installer.cb.sddc.retry, with job.id, "
            "every few minutes until the state is no longer running. Call without an id to "
            "find a bring-up whose id you lost."
        ),
        "output_shape": (
            "{id, name, state: running|succeeded|failed|rolled_back|unknown|not_reachable, "
            "vendor_state, created_at, tasks_total, tasks_done, tasks_running, tasks_failed, "
            "failed_task, error, tasks: [{name, description, status, updated_at, error}], "
            "checked_at}. Without an id: {bringups: [{id, name, state, vendor_state, "
            "created_at}], checked_at}. not_reachable means Cloud Builder did not answer, not "
            "that the bring-up failed."
        ),
        "next_step": (
            "When the state is failed, fix what error names, then call installer.cb.sddc.retry "
            "with the same id. installer.cb.sddc.report gives the detail report as a file."
        ),
        "result_scalars": {
            "keys": [
                "id",
                "name",
                "state",
                "vendor_state",
                "tasks_total",
                "tasks_done",
                "tasks_failed",
                "failed_task",
                "error",
                "reason",
            ]
        },
        "result_digest": {
            "collection": "tasks",
            "status_field": "status",
            "name_field": "name",
            "active_states": sorted(SUBTASK_RUNNING),
            "failed_states": sorted(SUBTASK_FAILED),
        },
    },
)

_SDDC_RETRY = CloudBuilderTypedOp(
    op_id=SDDC_RETRY_OP_ID,
    handler_attr="sddc_retry",
    summary="Retry a failed Cloud Builder bring-up from its failed task.",
    description=(
        "Sends PATCH /v1/sddcs/{id}, with a fixed spec or without one (then Cloud Builder "
        "uses the stored spec). Returns as soon as Cloud Builder accepted the retry. Starts "
        "nothing when the bring-up still runs."
    ),
    parameter_schema=SDDC_RETRY_PARAMS,
    response_schema=CHANGE_RESPONSE,
    tags=_SPEC_TAGS,
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=_change_instructions(
        when_to_call=("Call when installer.cb.sddc.status says failed and the cause is fixed."),
        status_op=SDDC_STATUS_OP_ID,
        kind="bringup",
        preconditions=(
            "The bring-up failed (installer.cb.sddc.status). A person must approve the call "
            "before it runs; an agent also needs a grant."
        ),
    ),
)

_SDDC_REPORT = CloudBuilderTypedOp(
    op_id="installer.cb.sddc.report",
    handler_attr="sddc_report",
    summary="Get the bring-up detail report or a validation report of Cloud Builder as a file.",
    description=(
        "Reads GET /v1/sddcs/{id}/detail-report (pdf or csv) or "
        "GET /v1/sddcs/validations/{validationId}/report (pdf). MEHO keeps the file behind a "
        "result handle and returns its size and SHA-256. Read-only."
    ),
    parameter_schema=REPORT_PARAMS,
    response_schema=REPORT_RESPONSE,
    tags=_READ_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call when a person wants the bring-up report or the validation report as a file."
        ),
        "output_shape": (
            "A result handle to {report, id, format, content_type, size_bytes, sha256, "
            "encoding: base64, content}. Read content with result_query and the path /content."
        ),
        "next_step": "Give the handle to the person, or decode content from base64.",
        "result_document": {"always": True},
    },
)

#: The operations, in the order a bring-up uses them.
CLOUD_BUILDER_TYPED_OPS: Final[tuple[CloudBuilderTypedOp, ...]] = (
    _ABOUT,
    _VALIDATION_START,
    _VALIDATION_STATUS,
    _SDDC_START,
    _SDDC_STATUS,
    _SDDC_RETRY,
    _SDDC_REPORT,
)


async def register_cloud_builder_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Write every operation of :data:`CLOUD_BUILDER_TYPED_OPS` into ``endpoint_descriptor``."""
    from meho_backplane.connectors.vcf_cloud_builder.connector import CloudBuilderConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in CLOUD_BUILDER_TYPED_OPS:
        handler = getattr(CloudBuilderConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"CloudBuilderConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        await register_typed_operation(
            product=CloudBuilderConnector.product,
            version=CloudBuilderConnector.version,
            impl_id=CloudBuilderConnector.impl_id,
            op_id=op.op_id,
            handler=handler,
            summary=op.summary,
            description=op.description,
            parameter_schema=op.parameter_schema,
            response_schema=op.response_schema,
            group_key=CLOUD_BUILDER_GROUP,
            when_to_use=CLOUD_BUILDER_WHEN_TO_USE,
            tags=list(op.tags),
            safety_level=op.safety_level,
            requires_approval=op.requires_approval,
            llm_instructions=op.llm_instructions,
            embedding_service=embedding_service,
        )
    _log.info(
        "cloud_builder_typed_operations_registered",
        count=len(CLOUD_BUILDER_TYPED_OPS),
        impl_id=CloudBuilderConnector.impl_id,
    )
