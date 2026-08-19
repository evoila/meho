# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata + registrar for :class:`InstallerConnector`.

The Installer's bring-up *status poll* (``GET /v1/sddcs/{id}`` → ``SddcTask``)
ships as a typed op (``source_kind="typed"``) so it dispatches on a fresh boot
with zero catalog ingest. The op body lives in
:mod:`meho_backplane.connectors.vcf_installer.typed_reads`; a thin bound-method
shim on :class:`InstallerConnector` exposes it under the ``handler_attr`` name so
the dispatcher's ``import_handler`` walk recovers the callable.

The governed bring-up *write* (validate → deploy → poll) is a separate
``dangerous`` + ``requires_approval`` composite (a following increment), not a
typed op — the highest-blast-radius write in the product must carry the preview
+ sub-op-policy machinery a raw op cannot. The dataclass + tuple + registrar
shape mirrors :mod:`meho_backplane.connectors.sddc_manager.typed_ops`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import structlog

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "INSTALLER_TYPED_OPS",
    "INSTALLER_TYPED_WHEN_TO_USE_BY_GROUP",
    "InstallerTypedOp",
    "register_installer_typed_operations",
]

_log = structlog.get_logger(__name__)

_GROUP_BRINGUP = "installer-bringup"


@dataclass(frozen=True)
class InstallerTypedOp:
    """Metadata for one Installer typed op registered at lifespan startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts. ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.vcf_installer.connector.InstallerConnector`
    exposing the async handler. Mirrors
    :class:`~meho_backplane.connectors.sddc_manager.typed_ops.SddcTypedOp`.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: Curated ``when_to_use`` blurb per typed-op group.
INSTALLER_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_BRINGUP: (
        "Use to track a VCF management-domain bring-up the Installer is running: "
        "installer.sddc.status polls one bring-up task by id for its lifecycle "
        "state (IN_PROGRESS / COMPLETED_WITH_SUCCESS / COMPLETED_WITH_FAILURE) "
        "and per-stage sub-tasks. The right group while a bring-up submitted "
        "through the governed installer.composite.sddc.bringup deploy is in "
        "flight, or when triaging one that failed. Read-only."
    ),
}


def _instructions(
    *, when_to_use: str, output_shape: str, parameter_hints: dict[str, str]
) -> dict[str, Any]:
    return {
        "when_to_use": when_to_use,
        "output_shape": output_shape,
        "parameter_hints": parameter_hints,
    }


_SDDC_STATUS = InstallerTypedOp(
    op_id="installer.sddc.status",
    handler_attr="sddc_status",
    summary="Lifecycle status of one VCF bring-up task.",
    description=(
        "Reads one VCF management-domain bring-up task via GET /v1/sddcs/{id} — "
        "the SddcTask object whose top-level status carries the "
        "IN_PROGRESS / COMPLETED_WITH_SUCCESS / COMPLETED_WITH_FAILURE lifecycle "
        "state, plus sddcSubTasks[] and milestones[] for per-stage progress. "
        "Requires the bring-up id returned by the governed deploy. The poll an "
        "operator (or the automation add-on) runs while a bring-up is in flight "
        "or to triage a failed one. Works with zero catalog ingest. "
        "safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "description": "The bring-up (SDDC) task id returned by the deploy.",
            },
        },
        "required": ["id"],
        "additionalProperties": False,
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_BRINGUP,
    tags=("read-only", "vcf", "installer", "bringup", "status"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call with a bring-up id to check whether a VCF management-domain "
            "deployment has completed, is still running, or failed."
        ),
        output_shape=(
            "{id, name, status, vcfInstanceName, sddcSubTasks: [...], "
            "milestones: [...]}. Surface the top-level status and any failed "
            "sub-task."
        ),
        parameter_hints={"id": "The bring-up (SDDC) task id from the deploy."},
    ),
)


#: The typed ops :class:`InstallerConnector` registers at lifespan startup.
INSTALLER_TYPED_OPS: tuple[InstallerTypedOp, ...] = (_SDDC_STATUS,)


async def register_installer_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`INSTALLER_TYPED_OPS` into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list via
    :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
    in this package's ``__init__``. Mirrors
    :func:`~meho_backplane.connectors.sddc_manager.typed_ops.register_sddc_typed_operations`.
    """
    from meho_backplane.connectors.vcf_installer.connector import InstallerConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in INSTALLER_TYPED_OPS:
        handler = getattr(InstallerConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"InstallerConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = (
            None if op.group_key is None else INSTALLER_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        )
        if op.group_key is not None and when_to_use is None:
            raise ValueError(
                f"InstallerConnector typed op {op.op_id!r} declares "
                f"group_key={op.group_key!r} but no curated when_to_use exists for that key."
            )
        await register_typed_operation(
            product=InstallerConnector.product,
            version=InstallerConnector.version,
            impl_id=InstallerConnector.impl_id,
            op_id=op.op_id,
            handler=handler,
            summary=op.summary,
            description=op.description,
            parameter_schema=op.parameter_schema,
            response_schema=op.response_schema,
            group_key=op.group_key,
            when_to_use=when_to_use,
            tags=list(op.tags),
            safety_level=op.safety_level,
            requires_approval=op.requires_approval,
            llm_instructions=op.llm_instructions,
            embedding_service=embedding_service,
        )
    _log.info(
        "installer_typed_operations_registered",
        count=len(INSTALLER_TYPED_OPS),
        product=InstallerConnector.product,
        version=InstallerConnector.version,
        impl_id=InstallerConnector.impl_id,
    )
