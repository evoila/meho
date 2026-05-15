# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho_backplane.operations`` — dispatcher-facing operation surfaces.

The G0.6 substrate (#388) and G0.7 ingestion pipeline (#389) both
live here. G0.6 ships the underlying ``endpoint_descriptor`` +
``operation_group`` tables (T1 #392), the :class:`Connector` ABC
registry-v2 metadata (T3 #394), :func:`register_typed_operation`
— the async helper typed connectors call at init time to populate
the tables (T4 #395) — and :func:`dispatch` (T5 #396), the single
entry point every operation flows through. G0.7 ships the OpenAPI
parser (T1 #401) and the operator review-queue state machine that
gates ingested (auto-derived) connectors before any of their
operations reach agents (T4 #402).

Sub-modules:

* :mod:`.dispatcher` — :func:`dispatch` and the orchestration of the
  eight-phase pipeline (parse → lookup → validate → policy → resolve
  → branch → reduce → audit+broadcast). Hosts the
  ``parent_audit_id_var`` contextvar (populated by composite-recursion
  T7 #398), and the module-level reducer slot
  (:func:`set_default_reducer`) that T6 (#397) will register the
  real ``Reducer`` against.
* :mod:`.composite` — :class:`DispatchChild` Protocol +
  :func:`get_dispatch_child` factory + :data:`composite_depth_var`
  contextvar + :class:`CompositeRecursionLimitExceeded` for the
  composite-operation recursion infrastructure (T7 #398). The
  dispatcher's ``source_kind='composite'`` branch builds a
  ``DispatchChild`` callable via :func:`get_dispatch_child` and
  hands it to the composite handler in place of raw
  :func:`dispatch`; the callable owns the audit-tree linkage +
  bounded-depth guard so handlers read as plain business logic.
* :mod:`.reducer` — the v0.2 :class:`PassThroughReducer` stub +
  :class:`Reducer` Protocol + :class:`ResultHandle`. T6 (#397) will
  ship the full reducer implementation; the dispatcher already
  invokes the reducer slot so today's pass-through gets swapped in
  cleanly later.
* :mod:`.typed_register` — :func:`register_typed_operation` and its
  G3.1-T4 (#504) sibling :func:`register_composite_operation` for
  typed/composite connector init-time registration, plus
  :class:`HandlerRefError` (closure/lambda/partial rejection) and
  :class:`HandlerSignatureError` (composite-handler signature
  validation). Re-exported at the package level for convenience.
* :mod:`.ingest` — G0.7 spec-ingestion pipeline. Today:
  :func:`~meho_backplane.operations.ingest.parse_openapi` (T1 #401)
  + the :class:`~meho_backplane.operations.ingest.ReviewService`
  state machine (T4 #402). Later: the bulk-upsert helper (T2 #403)
  and the LLM-grouping pass (T3 #404).

The dispatcher reads ``endpoint_descriptor`` rows directly via the
ORM; the meta-tools (T8, #399) will hit the same surface via the
retrieval helpers in :mod:`meho_backplane.operations.search`
(G0.6-T6 / T7 territory).
"""

from meho_backplane.operations.composite import (
    CompositeRecursionLimitExceeded,
    DispatchChild,
)
from meho_backplane.operations.dispatcher import (
    Dispatcher,
    compute_params_hash,
    dispatch,
    import_handler,
    parent_audit_id_var,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations.reducer import (
    PassThroughReducer,
    Reducer,
    ResultHandle,
)
from meho_backplane.operations.typed_register import (
    CompositeOpHandler,
    HandlerRefError,
    HandlerSignatureError,
    TypedOpHandler,
    clear_typed_op_registrars,
    register_composite_operation,
    register_typed_op_registrar,
    register_typed_operation,
    run_typed_op_registrars,
)

__all__ = [
    "CompositeOpHandler",
    "CompositeRecursionLimitExceeded",
    "DispatchChild",
    "Dispatcher",
    "HandlerRefError",
    "HandlerSignatureError",
    "PassThroughReducer",
    "Reducer",
    "ResultHandle",
    "TypedOpHandler",
    "clear_typed_op_registrars",
    "compute_params_hash",
    "dispatch",
    "import_handler",
    "parent_audit_id_var",
    "register_composite_operation",
    "register_typed_op_registrar",
    "register_typed_operation",
    "reset_dispatcher_caches",
    "run_typed_op_registrars",
    "set_default_reducer",
]
