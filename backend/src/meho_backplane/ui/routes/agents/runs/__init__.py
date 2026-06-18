# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent-runs UI routes: cross-agent run list + per-run detail/poll.

Initiative #1824 (G10.8 Agents console), Task #1830 (T3). This surface is
the console face of the agent run-history read path
(``api/v1/agent_runs.py`` -- ``GET /api/v1/agents/runs`` +
``GET /api/v1/agents/runs/{handle}``): an operator could only see run
history / poll a run's durable status from the CLI / REST, never the
console. This task adds the read-only ``/ui/agents/runs`` list + detail on
the established HTMX/Jinja chassis, hung off the ``/ui/agents`` scaffold
(#1825).

Module layout
-------------

* :mod:`~meho_backplane.ui.routes.agents.runs.list_view` -- ``GET
  /ui/agents/runs``. One handler serves the full page (browser nav) and the
  table-rows fragment (HTMX filter swap). Filters: ``status`` (closed enum)
  + ``work_ref`` (exact match). Operator-readable.
* :mod:`~meho_backplane.ui.routes.agents.runs.detail` -- ``GET
  /ui/agents/runs/{handle}``. The full run state; the status panel polls
  while the run is non-terminal and renders statically once terminal.
  Operator-readable.
* :mod:`~meho_backplane.ui.routes.agents.runs.views` -- row-to-view
  projection + status-badge + UTC coercion shared by both handlers.
* :mod:`~meho_backplane.ui.routes.agents.runs.operator` -- the read-path
  operator lift (synthesised tenant-scoped ``OPERATOR`` for the invoker
  call) + the re-exported role probe.

Why a session BFF and not the Bearer ``/api/v1/agents/runs`` routes
-------------------------------------------------------------------

The REST run routes are Bearer-gated over a verified JWT; a browser
carrying only the BFF session cookie cannot authenticate them. So this
surface calls the in-process
:class:`~meho_backplane.agent.invocation.AgentInvoker` (the same
``list_runs`` / ``poll`` the REST + MCP + CLI surfaces share) -- the same
console-surface pattern the connectors / scheduler / approvals surfaces
use. Tenant isolation is enforced by the invoker on the operator's
``tenant_id``; a cross-tenant run is invisible (list) or 404 (detail).

Registration order is **load-bearing** at the umbrella
:func:`~meho_backplane.ui.routes.build_router`: this router is included
**before** :func:`~meho_backplane.ui.routes.agents.build_agents_router` so
the literal ``/ui/agents/runs`` + ``/ui/agents/runs/{handle}`` routes win
the first-match-wins lookup against the definition surface's
``/ui/agents/{name}`` (which would otherwise bind ``"runs"`` as a name).

Scope (Initiative #1824): reads only. Invoking / streaming a run is the run
console (T2 #1829); cancelling a run is T8/T9. The list / detail never
expose ``system_prompt`` / ``toolset`` / approval params -- the invoker's
summary + poll projections carry none of them.
"""

from __future__ import annotations

from fastapi import APIRouter

from meho_backplane.ui.routes.agents.runs.detail import build_runs_detail_router
from meho_backplane.ui.routes.agents.runs.list_view import build_runs_list_router

__all__ = ["build_runs_router"]


def build_runs_router() -> APIRouter:
    """Aggregate the agent-runs UI routes into one ``/ui/agents/runs*`` router.

    Factory function (chassis convention) so a test app can construct
    parallel routers without sharing route state.

    Registration order within this router is **load-bearing**: the literal
    list route (``/ui/agents/runs``) registers before the parametrised
    detail route (``/ui/agents/runs/{handle}``) so the bare ``runs`` segment
    is matched as the list rather than captured as a ``{handle}``. (The
    ``uuid.UUID`` path type on ``{handle}`` already 422s a non-UUID segment,
    so this ordering is belt-and-suspenders, but it mirrors every other
    surface router's literal-before-parametrised discipline.)
    """
    router = APIRouter()
    router.include_router(build_runs_list_router())
    router.include_router(build_runs_detail_router())
    return router
