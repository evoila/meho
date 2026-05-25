# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent-definition storage + admin CRUD (G11.1-T2, #809) and
permission grant management (G11.2-T6, #819).

Under Initiatives #802 (P1 agent runtime) and #803 (P3 agent
identity + RBAC). This package owns the tenant-scoped CRUD surfaces
for both the agent-definition record and the agent-permission grant
records:

* :mod:`meho_backplane.agents.schemas` — create / update / read models
  for agent definitions.
* :mod:`meho_backplane.agents.service` — stateless CRUD service for
  agent definitions.
* :mod:`meho_backplane.agents.grant_schemas` — create / read schemas
  for permission grants.
* :mod:`meho_backplane.agents.grants` — stateless grant/revoke/list
  service.
* :mod:`meho_backplane.agents.grant_expiry` — background sweeper that
  removes expired time-bounded elevation grants.

The REST routes, MCP verbs, and CLI verbs are thin shells over these
services. Writes are gated to ``tenant_admin``; reads to ``operator``.
"""

from meho_backplane.agents.grant_expiry import (
    start_grant_expiry_sweeper,
    stop_grant_expiry_sweeper,
)

__all__ = [
    "start_grant_expiry_sweeper",
    "stop_grant_expiry_sweeper",
]
