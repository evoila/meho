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

from meho_backplane.ui.routes.account import build_account_router
from meho_backplane.ui.routes.agents import build_agents_router
from meho_backplane.ui.routes.agents.grants import build_agent_grants_router
from meho_backplane.ui.routes.agents.runs import build_runs_router
from meho_backplane.ui.routes.approvals import build_approvals_router
from meho_backplane.ui.routes.audit import build_audit_router
from meho_backplane.ui.routes.broadcast import build_router as build_broadcast_router
from meho_backplane.ui.routes.connectors import build_router as build_connectors_router
from meho_backplane.ui.routes.conventions import build_conventions_router
from meho_backplane.ui.routes.corpus import build_corpus_router
from meho_backplane.ui.routes.dashboard import build_dashboard_router
from meho_backplane.ui.routes.kb import build_kb_router
from meho_backplane.ui.routes.keycloak import build_keycloak_router
from meho_backplane.ui.routes.memory import build_memory_router
from meho_backplane.ui.routes.operations import build_operations_router
from meho_backplane.ui.routes.retrieval import build_retrieval_router
from meho_backplane.ui.routes.runbooks import build_runbooks_router
from meho_backplane.ui.routes.scheduler import build_scheduler_router
from meho_backplane.ui.routes.stubs import build_stubs_router
from meho_backplane.ui.routes.topology import build_router as build_topology_router
from meho_backplane.ui.routes.vault import (
    build_vault_router,
    build_vault_status_router,
    build_vault_writes_router,
)

__all__ = [
    "build_account_router",
    "build_agent_grants_router",
    "build_agents_router",
    "build_approvals_router",
    "build_audit_router",
    "build_broadcast_router",
    "build_connectors_router",
    "build_conventions_router",
    "build_corpus_router",
    "build_dashboard_router",
    "build_kb_router",
    "build_keycloak_router",
    "build_memory_router",
    "build_operations_router",
    "build_retrieval_router",
    "build_router",
    "build_runbooks_router",
    "build_runs_router",
    "build_scheduler_router",
    "build_stubs_router",
    "build_topology_router",
    "build_vault_router",
    "build_vault_status_router",
    "build_vault_writes_router",
]


# A flat router-registration aggregator: every console surface contributes
# exactly one ``router.include_router(...)`` call plus an inline
# first-match-wins ordering rationale; the length is the count of console
# surfaces this function wires (now incl. the keycloak realm browser and the
# vault KV browser), not per-function complexity. The single registration
# ORDER is load-bearing (the docstring documents why literal-before-param +
# real-before-stubs ordering must hold), so splitting it into phase helpers
# would fracture that one ordered sequence for no readability gain.
# code-quality-allow: function-size -- registration aggregator, see above.
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
    # Conventions console (G10.12-T1 #1895): ``/ui/conventions`` list +
    # always-on preamble token-budget banner + ``/ui/conventions/{slug}``
    # detail. The literal ``/ui/conventions`` list route is registered
    # before the ``{slug}`` detail route inside that router so T2's
    # static-prefix write routes (``/create`` + ``/preview``) drop in
    # without binding a literal as a slug; included before the stubs
    # aggregate so its concrete paths win the first-match-wins lookup.
    router.include_router(build_conventions_router())
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
    # Retrieval diagnostics & quality console (G10.14-T1 #1888): the anchor
    # surface for ``/ui/retrieval`` + ``POST /ui/retrieval/diagnostics``. The
    # only state-changing route is the literal ``/diagnostics`` POST -- there
    # is no ``/ui/retrieval/{param}`` route yet (the T2/T3 tabs are
    # client-side panels on the one page), but the literal-before-param
    # ordering is pinned inside ``build_retrieval_router`` for when one lands.
    # Registered before the stubs aggregate so its concrete paths win the
    # first-match-wins lookup against the placeholder ``/ui/{slug}``.
    router.include_router(build_retrieval_router())
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
    # Keycloak console (Initiative #1943, G10.x-T1 #1959): the read-only realm
    # browser -- ``/ui/keycloak`` (realm-config card + client list +
    # client-scope list) + ``/ui/keycloak/clients/{client_uuid}`` (per-client
    # detail). All surfaces dispatch the curated ``keycloak.*`` read ops
    # in-process through ``call_operation`` against the PINNED
    # ``connector_id="keycloak-admin-26.x"`` (never the bare ``keycloak``
    # slug). The only ``{param}`` route sits under the distinct
    # ``/ui/keycloak/clients/`` prefix, so a future literal ``/ui/keycloak/users``
    # (T2) registered before any ``{param}`` route binds first; included before
    # the stubs aggregate so its concrete paths win the first-match-wins lookup.
    router.include_router(build_keycloak_router())
    # Audit-query forensic console (G10.15-T1 #1944): ``/ui/audit`` (filter
    # form + first result page) + ``/ui/audit/results`` (filter-submit +
    # forward-cursor "Load more" fragment). Reads dispatch the
    # ``audit_query.query_audit`` substrate in-process, tenant-scoped from the
    # session. Inside the router the literal ``/ui/audit/results`` is
    # registered before the ``/ui/audit`` page route (and ahead of any future
    # ``{param}`` route T2/T3 add) so the literal ``results`` segment is never
    # bound as a slug; registered before the stubs aggregate so its concrete
    # paths win the first-match-wins lookup against the placeholder
    # ``/ui/{slug}``.
    router.include_router(build_audit_router())
    # Account surface (G10.11-T1 #1892): ``/ui/account`` page + the two
    # session-revoke POSTs. The literal ``/ui/account/sessions/revoke-others``
    # is registered before the parametrised
    # ``/ui/account/sessions/{session_id}/revoke`` inside that router so the
    # literal ``revoke-others`` segment is never bound as a ``session_id``.
    # Operator-tier (self-service own identity + own sessions only); no
    # ``{slug}`` route, so no shadowing concern against the stubs aggregate,
    # but registered before it for consistency with the other surfaces.
    router.include_router(build_account_router())
    # Vault / secrets console KV browser (G10.18-T1 #1956): ``/ui/vault`` +
    # ``/ui/vault/list`` + ``/ui/vault/read`` + ``/ui/vault/versions``. The
    # literal sub-route segments register before any future ``{param}``
    # route inside that router (first-match-wins); registered before the
    # stubs aggregate so its concrete paths win the first-match-wins lookup
    # against the placeholder ``/ui/{slug}``.
    router.include_router(build_vault_router())
    # Vault / secrets console confirm-gated WRITES (G10.18-T2 #1957):
    # ``GET /ui/vault/{put,delete,move}/confirm`` (the unmissable confirm
    # modals) + the CSRF-gated ``POST /ui/vault/{put,delete,move}`` dispatch
    # routes. A SEPARATE module from the T1 browser so the read / write
    # surfaces evolve without serial-merge collisions; the literal
    # ``put``/``delete``/``move`` segments register before any ``{param}``
    # route (first-match-wins, and these are POST / distinct-literal routes so
    # they cannot collide with T1's GET slug routes regardless). Registered
    # before the stubs aggregate so its concrete paths win the lookup.
    router.include_router(build_vault_writes_router())
    # Vault status view (G10.18-T3 #1958): ``/ui/vault/status`` seal/health/
    # mounts panel + ``/ui/vault/auth`` auth-methods glance, both read-only
    # GETs. The literal ``status`` / ``auth`` segments are distinct from the
    # T1 ``list`` / ``read`` / ``versions`` literals, the T2 ``put`` /
    # ``delete`` / ``move`` literals, and the bare ``/ui/vault`` index; there
    # is no ``{param}`` route on the vault surface, so the first-match-wins
    # lookup is unambiguous. Registered alongside its sibling vault routers
    # and before the stubs aggregate.
    router.include_router(build_vault_status_router())
    router.include_router(build_stubs_router())
    return router
