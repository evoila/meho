# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""FastAPI route handlers for the operator console.

Initiative #337 (G10.0 Frontend chassis), Task #866 (T5). The chassis
ships the umbrella :func:`build_router` that aggregates:

* :mod:`~meho_backplane.ui.routes.dashboard` -- ``GET /ui/`` --
  authenticated landing page with the 3x2 surface card grid, the
  HTMX SSE last-5-events snippet, and the version + readiness card.
* :mod:`~meho_backplane.ui.routes.memory` -- the memory surface
  (G10.4-T1 #877): ``/ui/memory`` list, ``/ui/memory/<scope>/<slug>``
  detail + edit-in-place + delete, ``/ui/memory/tags`` autocomplete.
* :mod:`~meho_backplane.ui.routes.connectors` -- the connectors
  surface (G10.3-T1 #873): ``/ui/connectors`` targets list
  (sortable / filterable), ``/ui/connectors/<name>`` per-target
  detail (full row + fingerprint card + recent-ops SSE-live +
  available-operations matrix), ``POST /ui/connectors/<name>/probe``
  tenant_admin re-probe.
* :mod:`~meho_backplane.ui.routes.agents` -- the agents console
  surface (G10.8-T1 #1825): ``GET /ui/agents`` definitions list,
  ``GET /ui/agents/<name>`` per-agent detail, and the
  tenant_admin-gated create / edit / enable-disable / delete write
  routes (``/ui/agents/create``, ``/ui/agents/<name>/edit``,
  ``/ui/agents/<name>/toggle``, ``/ui/agents/<name>/delete``). The
  write routes delegate to ``AgentDefinitionService`` in-process so the
  UI and REST surfaces share one validation + identity-ref-check +
  persist code path.
* :mod:`~meho_backplane.ui.routes.agents.grants` -- the agent permission
  grants surface (G10.8-T5 #1832): ``GET /ui/agents/grants`` table,
  ``GET /ui/agents/grants/<grant_id>`` per-grant detail, and the
  create / elevate (time-bounded) / revoke write routes
  (``/ui/agents/grants/create``, ``/ui/agents/grants/elevate``,
  ``/ui/agents/grants/<grant_id>/revoke``). The **whole** surface --
  reads included -- is tenant_admin, because a grant listing reveals the
  tenant's least-privilege posture. Delegates to ``AgentGrantService``
  in-process.
* :mod:`~meho_backplane.ui.routes.agents.runs` -- the agent-runs read
  surface (G10.8-T3 #1830): ``GET /ui/agents/runs`` cross-agent run list
  (``status`` + ``work_ref`` filters, full-page / HTMX-fragment) and
  ``GET /ui/agents/runs/{handle}`` per-run detail (poll-after-the-fact
  while non-terminal, static once terminal). Operator-readable; reuses the
  in-process ``AgentInvoker`` ``list_runs`` / ``poll`` read path. Its
  router is included **before** ``build_agents_router`` so the literal
  ``/ui/agents/runs`` segment is not bound as ``/ui/agents/{name}``.
* :mod:`~meho_backplane.ui.routes.kb` -- ``GET /ui/kb``,
  ``POST /ui/kb/search``, ``GET /ui/kb/<slug>``,
  ``GET /ui/kb/<slug>/preview`` -- KB read surface (G10.2-T1 #870).
* :mod:`~meho_backplane.ui.routes.corpus` -- ``GET /ui/corpus``,
  ``POST /ui/corpus/search`` -- docs-corpus page: collection picker
  (default-if-one) + ask-the-corpus + cited chunks, reusing the
  ``search_docs`` + ``doc_collections`` backends (G10.7-T1 #1777).
* :mod:`~meho_backplane.ui.routes.runbooks` -- ``GET /ui/runbooks``,
  ``GET /ui/runbooks/list`` (HTMX filter partial),
  ``GET /ui/runbooks/<slug>`` -- runbooks read surface, catalog +
  opacity-floor-aware template detail (G10.6-T1 #1382).
* :mod:`~meho_backplane.ui.routes.approvals` -- ``GET /ui/approvals/badge``,
  ``GET /ui/approvals`` (content-negotiated: full-page console on a normal
  navigation, pending panel fragment on the bell's ``HX-Request``),
  ``GET /ui/approvals/list`` (status-filterable decision-history partial,
  G10.8-T #1827), ``GET /ui/approvals/<id>``,
  ``POST /ui/approvals/<id>/approve`` + ``.../reject`` -- the approvals
  bell/badge + approve/deny modal + full-page history over a session BFF
  that calls the ``approval_queue`` service in-process (G10.7-T3 #1778,
  G10.8-T #1827).
* :mod:`~meho_backplane.ui.routes.stubs` -- now empty. All seven
  surfaces (broadcast #867, topology #880, memory #877, connectors
  #873, kb #870, runbooks #1382, approvals #1778) ship real routers;
  no ``/ui/{slug}`` placeholder remains.

Auth surfaces (``/ui/auth/login``, ``/ui/auth/callback``,
``/ui/auth/logout``) live under
:mod:`meho_backplane.ui.auth.routes` and are aggregated separately;
T5's :func:`meho_backplane.main` ``include_router`` block mounts both
routers.

The router factory pattern (rather than module-level constants)
mirrors :func:`meho_backplane.ui.auth.routes.build_router` so a
test app can construct multiple parallel routers without sharing
route state -- handy for the chassis smoke test's "minimal app"
fixture that wires UI middleware + UI router without dragging the
full backplane app in.
"""

from __future__ import annotations

from fastapi import APIRouter

from meho_backplane.ui.routes.agents import build_agents_router
from meho_backplane.ui.routes.agents.grants import build_agent_grants_router
from meho_backplane.ui.routes.agents.runs import build_runs_router
from meho_backplane.ui.routes.approvals import build_approvals_router
from meho_backplane.ui.routes.broadcast import build_router as build_broadcast_router
from meho_backplane.ui.routes.connectors import build_router as build_connectors_router
from meho_backplane.ui.routes.corpus import build_corpus_router
from meho_backplane.ui.routes.dashboard import build_dashboard_router
from meho_backplane.ui.routes.kb import build_kb_router
from meho_backplane.ui.routes.memory import build_memory_router
from meho_backplane.ui.routes.operations import build_operations_router
from meho_backplane.ui.routes.runbooks import build_runbooks_router
from meho_backplane.ui.routes.scheduler import build_scheduler_router
from meho_backplane.ui.routes.stubs import build_stubs_router
from meho_backplane.ui.routes.topology import build_router as build_topology_router

__all__ = [
    "build_agent_grants_router",
    "build_agents_router",
    "build_approvals_router",
    "build_broadcast_router",
    "build_connectors_router",
    "build_corpus_router",
    "build_dashboard_router",
    "build_kb_router",
    "build_memory_router",
    "build_operations_router",
    "build_router",
    "build_runbooks_router",
    "build_runs_router",
    "build_scheduler_router",
    "build_stubs_router",
    "build_topology_router",
]


def build_router() -> APIRouter:
    """Aggregate the dashboard + surface routers (broadcast … runbooks).

    Order matters: FastAPI matches by registration order, so a
    surface Initiative's real router is included **before** the
    stubs aggregate to win the first-match-wins path lookup. The
    dashboard ``/ui/`` route does not collide with any surface
    sub-path; broadcast lands ``/ui/broadcast`` + ``/ui/broadcast/stream``,
    topology lands ``/ui/topology`` + ``/ui/topology/node/{id}``,
    memory lands ``/ui/memory`` + ``/ui/memory/{scope}/{slug}``,
    connectors lands ``/ui/connectors`` + ``/ui/connectors/{name}`` +
    ``POST /ui/connectors/{name}/probe``, kb lands ``/ui/kb`` +
    ``/ui/kb/{slug}`` (+ search / preview), and runbooks lands
    ``/ui/runbooks`` + ``/ui/runbooks/list`` + ``/ui/runbooks/{slug}``
    -- each owning its path. ``/ui/runbooks/list`` is registered before
    ``/ui/runbooks/{slug}`` inside that router so the literal segment is
    not bound as a slug; approvals applies the same discipline with
    ``/ui/approvals/badge`` ahead of ``/ui/approvals/{request_id}``. All
    seven surfaces now ship real routers, so the stubs aggregate is empty;
    it is still included for symmetry and to keep the retirement pattern's
    seam in place.
    """
    router = APIRouter()
    router.include_router(build_dashboard_router())
    # Surface routers ahead of stubs -- their concrete paths win
    # the match against the stubs' placeholder ``/ui/{slug}``.
    router.include_router(build_broadcast_router())
    router.include_router(build_topology_router())
    router.include_router(build_memory_router())
    router.include_router(build_connectors_router())
    # Agent-grants surface ahead of the agents surface: the literal
    # ``/ui/agents/grants`` path must win the first-match-wins lookup
    # against the agents surface's ``/ui/agents/{name}`` (which would
    # otherwise bind ``name="grants"``). Inside the grants router the
    # literal ``create`` / ``elevate`` routes register before the
    # ``{grant_id}`` detail route for the same reason (G10.8-T5 #1832).
    router.include_router(build_agent_grants_router())
    # Agent-runs read surface (G10.8-T3 #1830) before the agents-definition
    # router: ``/ui/agents/runs`` + ``/ui/agents/runs/{handle}`` are literal-
    # prefixed under ``/ui/agents`` and MUST win the first-match-wins lookup
    # against the definition surface's ``/ui/agents/{name}`` (which would
    # otherwise bind ``"runs"`` as a definition name).
    router.include_router(build_runs_router())
    router.include_router(build_agents_router())
    router.include_router(build_kb_router())
    router.include_router(build_corpus_router())
    router.include_router(build_runbooks_router())
    router.include_router(build_approvals_router())
    # Scheduler lands ``/ui/scheduler`` + ``/ui/scheduler/{trigger_id}`` +
    # the literal ``/ui/scheduler/create`` + ``/ui/scheduler/validate-cron``
    # + ``/ui/scheduler/{id}/cancel`` write routes (G10.8-T6 #1826); the
    # literal-prefix routes register before the ``{trigger_id}`` detail
    # route inside that router so ``"create"`` is never bound as an id.
    router.include_router(build_scheduler_router())
    # Operations launcher (G10.9-T1 #1879): ``/ui/operations`` +
    # ``/ui/operations/search`` + ``/ui/operations/descriptor/{id}``. The
    # only ``{param}`` route sits under the distinct
    # ``/ui/operations/descriptor/`` prefix, so the literal ``search``
    # segment can never bind as a descriptor id; registered before the
    # stubs aggregate so its concrete paths win the first-match-wins lookup.
    router.include_router(build_operations_router())
    router.include_router(build_stubs_router())
    return router
