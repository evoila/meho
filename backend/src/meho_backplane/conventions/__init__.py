# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tenant conventions module (Layer 1 server-side rules).

Initiative #229 (G7.1 Tenant conventions + Layer 2 starter). The
package collects the helpers shared across the convention surfaces:

* :mod:`.schemas` -- Pydantic request/response models + the token-
  budget heuristic T2 (#314) uses for write-time over-budget
  rejection and T4 (#316) reuses for preamble packing.

T1 (#313) shipped the schema; T2 (this module's first consumer) ships
the API surface; T3 (#315) layers CLI verbs; T4 layers the session-
preamble assembler that reads through this package's budget
heuristic. T5 (#317) seeds rows for the ``rdc-internal`` tenant.

The package boundary is deliberate: the budget heuristic must agree
across the write-time 422 (T2) and the read-time priority-ranked
packer (T4) -- a divergence between the two would let a write pass
the API only to be silently dropped at every future preamble
assembly. Sharing the helper through a small import surface keeps
the two sites grep-aligned (one ``DEFAULT_MAX_PREAMBLE_TOKENS``
constant + one ``estimate_tokens`` function).
"""

from meho_backplane.conventions.schemas import (
    DEFAULT_MAX_PREAMBLE_TOKENS,
    Convention,
    ConventionCreate,
    ConventionHistoryEntry,
    ConventionKind,
    ConventionListResponse,
    ConventionSummary,
    ConventionUpdate,
    estimate_tokens,
)

__all__ = [
    "DEFAULT_MAX_PREAMBLE_TOKENS",
    "Convention",
    "ConventionCreate",
    "ConventionHistoryEntry",
    "ConventionKind",
    "ConventionListResponse",
    "ConventionSummary",
    "ConventionUpdate",
    "estimate_tokens",
]
