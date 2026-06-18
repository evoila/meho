# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent-grants UI surface: list + detail + create / elevate / revoke.

Initiative #1824 (G10.8 Agents console), Task #1832 (T5). Layers the
agent permission-grant governance surface onto the ``/ui/agents``
console: a tenant_admin-only table of which principal may run which op
pattern with which verdict, plus create / elevate (time-bounded) /
revoke. The whole surface -- reads included -- is tenant_admin, because
a grant listing reveals the tenant's least-privilege posture (the same
gate the REST surface :mod:`meho_backplane.api.v1.agent_grants` applies
to every route).

Module layout:

* :mod:`~meho_backplane.ui.routes.agents.grants.routes` -- the request
  handlers (path / method / dependency wiring).
* :mod:`~meho_backplane.ui.routes.agents.grants.views` -- the read
  renders (table + detail) and the row projections, including the
  verdict-badge colour mapping.
* :mod:`~meho_backplane.ui.routes.agents.grants.forms` -- the write
  renders (create / elevate / revoke modals) + submit handlers.
  Delegates to :class:`~meho_backplane.agents.grants.AgentGrantService`
  in-process so the UI write and the REST write share one validation +
  persist code path.
* :mod:`~meho_backplane.ui.routes.agents.grants.operator` -- the
  all-paths tenant_admin gate (reuses the shared operator lift the rest
  of the agents console ships).

The umbrella :func:`build_agent_grants_router` is mounted **before**
:func:`~meho_backplane.ui.routes.agents.build_agents_router` in
:func:`~meho_backplane.ui.routes.build_router` so the literal
``/ui/agents/grants`` path wins the first-match-wins lookup against the
agents surface's ``/ui/agents/{name}`` (which would otherwise bind
``name="grants"``).
"""

from __future__ import annotations

from meho_backplane.ui.routes.agents.grants.routes import build_agent_grants_router

__all__ = ["build_agent_grants_router"]
