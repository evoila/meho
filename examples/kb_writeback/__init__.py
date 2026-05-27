# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Closed-loop kb write-back reference sample (R3 of G11.6 #807).

The pattern this package demonstrates is:

1. An **investigation agent** -- an :class:`AgentDefinition` running in
   MEHO's in-process loop (G11.1) -- examines a piece of operator
   context (a fault signature, a strange config, a recurring alert)
   and produces a structured ``Finding``.
2. The harness persists that finding to the tenant's knowledge base
   (G4) via :meth:`KbService.create_entry`.
3. A later run -- a follow-up agent, a different operator's query, or
   even the same agent on a related signature -- retrieves the
   finding through :meth:`KbService.search_entries` so the team's
   accumulated reasoning becomes part of the next loop's context.

Why this lives in ``examples/`` and not in MEHO's runtime
=========================================================

This sample is *composition* on top of two shipped primitives
(:mod:`meho_backplane.kb`, :mod:`meho_backplane.agent`). It is **not**
new MEHO surface -- the kb service does the write, the agent runtime
does the run, and the consumer's harness glues them together with a
prompt and a tenant id. The sample documents the glue so every
consumer doesn't reinvent it.

Where the CI gate lives
=======================

The integration exercise that proves the loop closes against a real
``pgvector`` container lives in
:mod:`tests.integration.test_examples_kb_writeback`. It imports this
package by absolute path (the example tree is outside ``backend/``'s
package root by design -- consumers see ``examples/`` at the repo
root, not under ``backend/``), seeds an investigation, asserts the
finding is retrievable by terms drawn from its body, and verifies
that a search over a sibling tenant returns no results.

See :doc:`docs/codebase/examples-kb-writeback` for the wider walk
through the pattern, and :doc:`docs/codebase/kb` for the underlying
:class:`KbService` API surface.
"""

from __future__ import annotations
