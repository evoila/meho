# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op registrar for :class:`Vrli8Connector` (legacy vRLI 8.x, #3068).

The 8.x impl reuses the modern sibling's typed read op verbatim —
:data:`~meho_backplane.connectors.vcf_logs.typed_ops.VRLI_TYPED_OPS` (today just
``vrli.event.query``) issues the identical ``GET /api/v2/events/{constraints}``
query on 8.x (the v2 API is stable since vRLI 8.0), and the handler is inherited
on :class:`Vrli8Connector`. So this registrar upserts the *same op_id* under the
8.x connector's own ``(vrli, 8.0, vrli-vrli8)`` triple rather than re-authoring
it.

The reuse is safe for the same two reasons as the vROps 8.x registrar:
``endpoint_descriptor`` rows are keyed by ``(product, version, impl_id, op_id)``
(so the op_id is shared but the rows are distinct), and ``OperationGroup`` rows
are scoped by ``(product, version, impl_id, group_key)`` (so reusing the modern
group_key lands a distinct group row for this impl).

Note the modern vRLI connector types only ``vrli.event.query``; the rest of its
browse surface (hosts / content-packs / alerts / fields) is **generic-ingested**
from an internally-authored ``vcf-logs-9.0/openapi.yaml``, not a VMware download.
8.x has no committable spec to ingest, so the 8.x impl's surface is the inherited
typed op — a wider typed inventory surface is a documented follow-up on #3068,
not shipped speculatively.

Mirrors the modern
:func:`~meho_backplane.connectors.vcf_logs.typed_ops.register_vrli_typed_operations`
registrar exactly, differing only in the target class (:class:`Vrli8Connector`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = ["register_vrli8_typed_operations"]

_log = structlog.get_logger(__name__)


async def register_vrli8_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the modern vRLI typed read op(s) under the ``vrli-vrli8`` triple.

    Queued onto the lifespan-driven registrar list via
    :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
    in this package's ``__init__``; the runner
    (:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`)
    invokes it after
    :func:`~meho_backplane.connectors.registry._eager_import_connectors` has
    walked every connector subpackage, so the descriptor row lands before the
    first dispatch — the 8.x read op dispatches on a fresh boot with zero catalog
    ingest. Idempotent across pod restarts. Mirrors the modern
    ``register_vrli_typed_operations`` registrar.

    The ``embedding_service`` keyword-only parameter is the runner contract:
    :func:`run_typed_op_registrars` passes the process-wide
    :class:`~meho_backplane.retrieval.embedding.EmbeddingService` (or a
    chassis-test stub) to every registrar, so each must accept the kwarg; it is
    forwarded to :func:`register_typed_operation`.
    """
    # Lazy import: the operations package pulls in the embedding pipeline
    # (ONNX runtime + model), which pure connector/handler unit tests should not
    # pay. Lifespan callers have it warmed by the time this runs.
    from meho_backplane.connectors.vcf_logs.typed_ops import (
        VRLI_TYPED_OPS,
        VRLI_TYPED_WHEN_TO_USE_BY_GROUP,
    )
    from meho_backplane.connectors.vrli8.connector import Vrli8Connector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in VRLI_TYPED_OPS:
        handler = getattr(Vrli8Connector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"Vrli8Connector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = (
            None if op.group_key is None else VRLI_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        )
        if op.group_key is not None and when_to_use is None:
            raise ValueError(
                f"Vrli8Connector typed op {op.op_id!r} declares group_key="
                f"{op.group_key!r} but no curated when_to_use exists for that key. "
                f"Add an entry to VRLI_TYPED_WHEN_TO_USE_BY_GROUP."
            )
        await register_typed_operation(
            product=Vrli8Connector.product,
            version=Vrli8Connector.version,
            impl_id=Vrli8Connector.impl_id,
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
        "vrli8_typed_operations_registered",
        count=len(VRLI_TYPED_OPS),
        product=Vrli8Connector.product,
        version=Vrli8Connector.version,
        impl_id=Vrli8Connector.impl_id,
    )
