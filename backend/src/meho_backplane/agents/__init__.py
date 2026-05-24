# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent-definition storage + admin CRUD (G11.1-T2, #809).

Under Initiative #802 (the P1 agent runtime). This package owns the
first-class, tenant-scoped :class:`~meho_backplane.db.models.AgentDefinition`
record and the CRUD surface that manages it:

* :mod:`meho_backplane.agents.schemas` -- the Pydantic v2 create / update
  / read models (all ``extra="forbid"``) and the logical model-tier enum.
* :mod:`meho_backplane.agents.service` -- the stateless, tenant-scoped
  CRUD service (session-per-method), mirroring the
  :class:`~meho_backplane.memory.service.MemoryService` /
  :class:`~meho_backplane.kb.service.KbService` precedents.

The REST routes (:mod:`meho_backplane.api.v1.agents`), MCP verbs
(:mod:`meho_backplane.mcp.tools.agents`), and the Go CLI verbs
(``cli/internal/cmd/agent``) are thin shells over this service. Writes
are gated to ``tenant_admin``; reads to ``operator``. Running a
definition (T1 #808 / T4 #811) and resolving its toolset against the
identity's permissions (T3 #810) are out of scope here -- this package
stores and manages the record only.
"""
