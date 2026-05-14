# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho_backplane.operations`` — dispatcher-facing operation surfaces.

The G0.6 substrate (#388) and G0.7 ingestion pipeline (#389) both
live here. G0.6 ships the underlying ``endpoint_descriptor`` +
``operation_group`` tables (T1 #392), the :class:`Connector` ABC
registry-v2 metadata (T3 #394), and :func:`register_typed_operation`
— the async helper typed connectors call at init time to populate
the tables (T4 #395). G0.7 ships the operator review-queue state
machine that gates ingested (auto-derived) connectors before any
of their operations reach agents (T4 #402).

Sub-modules:

* :mod:`.typed_register` — :func:`register_typed_operation` and its
  :class:`HandlerRefError` for typed-connector init-time
  registration (re-exported at the package level for convenience).
* :mod:`.ingest` — G0.7 spec-ingestion pipeline. Today: the
  :class:`~meho_backplane.operations.ingest.ReviewService`
  state machine (T4 #402). Later: the OpenAPI parser (T1 #401),
  the bulk-upsert helper (T2 #403), and the LLM-grouping pass
  (T3 #404).

The dispatcher (G0.6-T5, #396) reads ``endpoint_descriptor`` rows
directly via the ORM; the meta-tools (T8, #399) hit the same
surface via the retrieval helpers in
:mod:`meho_backplane.operations.search` (G0.6-T6 / T7 territory).
"""

from meho_backplane.operations.typed_register import (
    HandlerRefError,
    TypedOpHandler,
    register_typed_operation,
)

__all__ = [
    "HandlerRefError",
    "TypedOpHandler",
    "register_typed_operation",
]
