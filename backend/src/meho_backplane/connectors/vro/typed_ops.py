# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op registrar for :class:`VroConnector`.

The op metadata lives in :mod:`meho_backplane.connectors.vro.read_ops`,
:mod:`meho_backplane.connectors.vro.library_ops` and
:mod:`meho_backplane.connectors.vro.write_ops`.
The dataclass + tuple + registrar shape mirrors
:mod:`meho_backplane.connectors.hcx.typed_ops`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from meho_backplane.connectors.vro.library_ops import VRO_LIBRARY_OPS
from meho_backplane.connectors.vro.op_types import (
    GROUP_EXECUTIONS,
    GROUP_LIBRARY,
    GROUP_SERVER,
    GROUP_WORKFLOWS,
    VroTypedOp,
)
from meho_backplane.connectors.vro.read_ops import VRO_READ_OPS
from meho_backplane.connectors.vro.write_ops import VRO_WRITE_OPS

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "VRO_TYPED_OPS",
    "VRO_TYPED_WHEN_TO_USE_BY_GROUP",
    "VroTypedOp",
    "register_vro_typed_operations",
]

#: Curated ``when_to_use`` text per group. Says which reads and changes the
#: group holds; it makes no claim about the safety of the whole group.
VRO_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    GROUP_SERVER: (
        "Use to read the Orchestrator server itself: its version, build and API "
        "version (vro.about) and the health of its services (vro.health). Two reads."
    ),
    GROUP_WORKFLOWS: (
        "Use to find Orchestrator workflows by name or category (vro.workflow.list), "
        "to read one workflow with its inputs and outputs (vro.workflow.get), and to "
        "run a workflow with inputs (vro.workflow.run). Two reads and one change; a "
        "person runs the change after a preview and an approval."
    ),
    GROUP_EXECUTIONS: (
        "Use to look at the runs of a workflow: list the runs with state, start, end "
        "and user (vro.execution.list), read one run with its inputs, outputs and "
        "error (vro.execution.get), and read the log lines of one run "
        "(vro.execution.logs). Also cancel a run that has not ended "
        "(vro.execution.cancel). Three reads and one change that waits for approval."
    ),
    GROUP_LIBRARY: (
        "Use to see the content that workflows use: actions (scripts) by module "
        "(vro.action.list), configuration elements and their attributes "
        "(vro.config_element.list), and installed packages with their versions "
        "(vro.package.list). Three reads."
    ),
}

VRO_TYPED_OPS: tuple[VroTypedOp, ...] = VRO_READ_OPS + VRO_LIBRARY_OPS + VRO_WRITE_OPS


async def register_vro_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`VRO_TYPED_OPS` into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list in this package's
    ``__init__``; the runner passes the process-wide embedding service
    (or a test stub), which is forwarded to ``register_typed_operation``.
    """
    from meho_backplane.connectors.vro.connector import VroConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in VRO_TYPED_OPS:
        handler = getattr(VroConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"VroConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = (
            None if op.group_key is None else VRO_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        )
        if op.group_key is not None and when_to_use is None:
            raise ValueError(
                f"VroConnector typed op {op.op_id!r} declares group_key={op.group_key!r} "
                "but no curated when_to_use exists for that key."
            )
        await register_typed_operation(
            product=VroConnector.product,
            version=VroConnector.version,
            impl_id=VroConnector.impl_id,
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
