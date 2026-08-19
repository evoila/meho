# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op registrar for :class:`Vrops8Connector` (legacy vROps 8.x, #3067).

The 8.x impl reuses the modern sibling's **audited typed read set verbatim** —
the four ops in
:data:`~meho_backplane.connectors.vcf_operations.typed_ops.VROPS_TYPED_OPS`
(``vrops.liveness`` / ``vrops.alert.list`` / ``vrops.resource.query`` /
``vrops.resource.stats``) hit the identical ``/suite-api/api/*`` surface on 8.x,
and their handlers are inherited on :class:`Vrops8Connector`. So this registrar
upserts the *same op_ids* under the 8.x connector's own
``(vrops, 8.0, vrops-vrops8)`` triple rather than re-authoring them.

Two invariants make the reuse safe:

* **op_ids are shared on purpose.** ``endpoint_descriptor`` rows are keyed by
  ``(product, version, impl_id, op_id)``, so ``vrops.liveness`` on
  ``vrops-vrops8-8.0`` is a distinct row from the same op_id on
  ``vrops-rest-9.0`` — the logical operation is the same, dispatched to whichever
  impl the target resolves to.
* **group_keys are scoped per impl.** ``_resolve_or_create_group`` looks up an
  :class:`~meho_backplane.db.models.OperationGroup` by
  ``(product, version, impl_id, group_key)``, so reusing the modern group_keys
  lands distinct group rows for this impl — never colliding with the 9.x rows.

This mirrors the modern
:func:`~meho_backplane.connectors.vcf_operations.typed_ops.register_vcf_operations_typed_operations`
registrar exactly, differing only in the target class (:class:`Vrops8Connector`)
it resolves handlers and the registration triple from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = ["register_vrops8_typed_operations"]

_log = structlog.get_logger(__name__)


async def register_vrops8_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the modern vROps typed read set under the ``vrops-vrops8`` triple.

    Queued onto the lifespan-driven registrar list via
    :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
    in this package's ``__init__``; the runner
    (:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`)
    invokes it after
    :func:`~meho_backplane.connectors.registry._eager_import_connectors` has
    walked every connector subpackage, so the descriptor rows land before the
    first dispatch — the 8.x read core dispatches on a fresh boot with zero
    catalog ingest. Idempotent across pod restarts. Mirrors the modern
    ``register_vcf_operations_typed_operations`` registrar.

    The ``embedding_service`` keyword-only parameter is the runner contract:
    :func:`run_typed_op_registrars` passes the process-wide
    :class:`~meho_backplane.retrieval.embedding.EmbeddingService` (or a
    chassis-test stub) to every registrar, so each must accept the kwarg; it is
    forwarded to :func:`register_typed_operation` (which falls back to the
    process-wide singleton when ``None``).
    """
    # Lazy import: the operations package pulls in the embedding pipeline
    # (ONNX runtime + model), which pure connector/handler unit tests should not
    # pay. Lifespan callers have it warmed by the time this runs.
    from meho_backplane.connectors.vcf_operations.typed_ops import (
        VROPS_TYPED_OPS,
        VROPS_TYPED_WHEN_TO_USE_BY_GROUP,
    )
    from meho_backplane.connectors.vrops8.connector import Vrops8Connector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in VROPS_TYPED_OPS:
        handler = getattr(Vrops8Connector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"Vrops8Connector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = (
            None if op.group_key is None else VROPS_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        )
        if op.group_key is not None and when_to_use is None:
            raise ValueError(
                f"Vrops8Connector typed op {op.op_id!r} declares group_key="
                f"{op.group_key!r} but no curated when_to_use exists for that key. "
                f"Add an entry to VROPS_TYPED_WHEN_TO_USE_BY_GROUP."
            )
        await register_typed_operation(
            product=Vrops8Connector.product,
            version=Vrops8Connector.version,
            impl_id=Vrops8Connector.impl_id,
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
        "vrops8_typed_operations_registered",
        count=len(VROPS_TYPED_OPS),
        product=Vrops8Connector.product,
        version=Vrops8Connector.version,
        impl_id=Vrops8Connector.impl_id,
    )
