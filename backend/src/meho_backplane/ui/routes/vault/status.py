# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault status UI routes: read-only seal/health/mounts + auth-methods glance.

Initiative #1942 (G10.18 Vault / secrets console), Task #1958 (T3). Adds a
read-only **status** surface to the T1 KV browser (#1956): is Vault sealed?
healthy? which secret backends are mounted, and which auth methods are
enabled? Today the only human surface for this is the ``meho vault sys …``
CLI verb tree (``cli/internal/cmd/vault/sys.go``); this module adds the web
view the operator console was missing.

This is **BFF + template assembly over the existing dispatcher surface** --
no new connector code, no new backend route, no new meta-tool. The four
``vault.sys.*`` read ops are complete and tested.

Why a session BFF and not the Bearer ``/api/v1/operations/call`` route
--------------------------------------------------------------------

Same rationale as the T1 KV browser (see
:mod:`meho_backplane.ui.routes.vault.routes`): the REST operation routes
are Bearer-gated over a verified JWT; a browser carrying only the BFF
session cookie cannot authenticate them. So this module adds ``/ui/vault``
sub-routes that are ``require_ui_session``-gated and call the operation
meta-tool :func:`~meho_backplane.operations.meta_tools.call_operation`
IN-PROCESS -- the exact pattern ``ui/routes/operations/routes.py`` uses for
its dispatch surface, and the one the T1 browser already follows. The
operator lift, the dispatchable-id lookup, and the in-process dispatch
helper are reused verbatim from :mod:`~meho_backplane.ui.routes.vault.routes`
so the status surface and the KV browser share one identity + connector-id
+ redaction code path.

Route inventory (read-only, all GET)
------------------------------------

* ``GET /ui/vault/status`` -- the status panel. Content-negotiated on
  ``HX-Request`` (mirroring ``_is_htmx`` in the operations launcher): a
  normal navigation renders the full page (``extends base.html``,
  ``active_surface="vault"``) with a "back to the KV browser" link; an
  HTMX fetch (the index's auto-loaded panel + the Refresh button) renders
  only the ``_status.html`` partial. Dispatches three ops in parallel:
  ``vault.sys.seal_status`` (seal state + unseal progress),
  ``vault.sys.health`` (reachable / sealed / serving + version), and
  ``vault.sys.mounts.list`` (the enabled secret backends). Each op's
  envelope is rendered independently, so an unreachable Vault degrades one
  card without blanking the others.
* ``GET /ui/vault/auth`` -- the auth-methods glance (HTMX partial,
  ``_status_auth.html``). Dispatches ``vault.sys.auth.list`` and renders
  the enabled auth-method mount-paths + types -- method NAMES and config
  metadata ONLY, never a credential. Handles the set-shaped result: when
  the envelope carries ``handle is not None`` (the dispatcher reduces a
  large auth-method map to a JSONFlux sample + handle), it renders the
  handle metadata, NOT a huge blob -- mirroring the T1 list handle branch.

Both routes are GET, so they need no CSRF gate (the CSRF middleware only
guards state-changing methods). The literal ``status`` / ``auth`` segments
are distinct from the T1 ``list`` / ``read`` / ``versions`` literals and
from the bare ``/ui/vault`` index; there is no ``{param}`` route on the
vault surface, so the first-match-wins lookup is unambiguous. This router
is included immediately after :func:`build_vault_router` in
:func:`meho_backplane.ui.routes.build_router`, ahead of the stubs
aggregate.

connector_id shape (load-bearing)
---------------------------------

The dispatchable id is ``vault-1.x`` (the ``<impl_id>-<version>`` id),
NEVER the bare product slug ``vault`` -- the slug 404s through
:func:`~meho_backplane.operations._lookup.parse_connector_id`. It is
sourced from the ingested-connector list via the shared
:func:`~meho_backplane.ui.routes.vault.routes._vault_connector_id` helper
(``state == "ingested"`` only), so a re-version (``vault-2.x``) flows
through without a literal edit. A deploy where the vault connector is not
ingested renders a "no vault connector" hint rather than a dead-end 404.

Names, not values (load-bearing)
--------------------------------

``vault.sys.*`` reads return health / seal / mount / auth-method NAMES and
config metadata -- never secret values (this is status, not KV). Nothing on
this surface renders a secret value. The structlog calls here log the
op-id / connector-id / status / handle-presence only, never the envelope
``result`` -- the same redaction discipline the T1 read path holds (defence
in depth: these ops carry no secret payload to begin with).

RBAC (OPERATOR tier)
--------------------

The four ``vault.sys.*`` reads are ``safety_level="safe"`` / operator-tier,
so both routes are ``require_ui_session``-only with no ``TenantRole``
dependency -- the same gate the T1 KV browser and the operations console
read routes use. There is no tenant_admin step. (The only tenant_admin-
gated vault surface is ``llm_instructions`` in an op-detail drawer, which
this status glance does not surface.)

Tenant isolation
----------------

Every dispatch derives ``tenant_id`` from the validated
:class:`UISessionContext` only (via the lifted :class:`Operator`); the sys
reads take no tenant-spoofable input. The sys/auth reads are cluster-wide
diagnostics and are not tenant-path-scoped the way ``vault.kv.*`` is, but
they still run under the operator's identity for uniform audit attribution.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.routes.vault.routes import (
    _require_session,
    _resolve_operator,
    _vault_connector_id,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["build_vault_status_router"]

log = structlog.get_logger(__name__)

#: The vault ``sys`` read op-ids the status surface dispatches. These mirror
#: the CLI verb tree's constants verbatim (``cli/internal/cmd/vault/sys.go``);
#: the source of truth is the descriptor-spec table in
#: :mod:`meho_backplane.connectors.vault.ops_sys`. All four are
#: ``safety_level="safe"`` reads that return cluster/mount/method metadata,
#: never secret values.
_OP_SEAL_STATUS: Final[str] = "vault.sys.seal_status"
_OP_HEALTH: Final[str] = "vault.sys.health"
_OP_MOUNTS: Final[str] = "vault.sys.mounts.list"
_OP_AUTH: Final[str] = "vault.sys.auth.list"


def build_vault_status_router() -> APIRouter:
    """Construct the ``/ui/vault/status`` + ``/ui/vault/auth`` router.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without shared route state -- the convention
    every surface router follows. Included immediately after
    :func:`~meho_backplane.ui.routes.vault.routes.build_vault_router` in
    :func:`meho_backplane.ui.routes.build_router`, ahead of the stubs
    aggregate.

    The literal ``status`` / ``auth`` segments are distinct from the T1
    ``list`` / ``read`` / ``versions`` literals and the bare ``/ui/vault``
    index; there is no ``{param}`` route on the vault surface, so the
    first-match-wins lookup is unambiguous.
    """
    router = APIRouter(tags=["ui-vault"])

    @router.get("/ui/vault/status", response_class=HTMLResponse)
    async def vault_status(
        request: Request,
        session: UISessionContext = _require_session,
    ) -> HTMLResponse:
        """Render the seal / health / mounts status panel.

        Content-negotiated on ``HX-Request``: a normal navigation renders
        the full page; an HTMX fetch renders only the status partial.
        Read-only; the three ``vault.sys.*`` ops return cluster/mount
        metadata, never secret values.
        """
        return await _render_status(request, session)

    @router.get("/ui/vault/auth", response_class=HTMLResponse)
    async def vault_auth(
        request: Request,
        session: UISessionContext = _require_session,
    ) -> HTMLResponse:
        """Render the auth-methods glance partial (``vault.sys.auth.list``).

        Read-only. Renders the enabled auth-method mount-paths + types --
        method NAMES and config metadata only, never credentials. A
        set-shaped spill (``handle is not None``) renders the handle
        metadata, not a huge blob.
        """
        return await _render_auth(request, session)

    return router


def _no_connector_context() -> dict[str, Any]:
    """Context for the "no vault connector ingested" inline hint."""
    return {"error": "no_vault_connector", "seal": None, "health": None, "mounts": None}


async def _render_status(
    request: Request,
    session: UISessionContext,
) -> HTMLResponse:
    """Dispatch seal / health / mounts in-process and render the status panel.

    Picks the full page vs. the partial by ``HX-Request`` (mirroring the
    operations launcher's ``_is_htmx``). When no vault connector is
    ingested, renders the "no connector" hint instead of three dead-end
    dispatches.
    """
    operator = await _resolve_operator(session)
    connector_id = await _vault_connector_id(operator)
    htmx = request.headers.get("hx-request", "").lower() == "true"
    template = "vault/_status.html" if htmx else "vault/status.html"

    if connector_id is None:
        context = {"active_surface": "vault", "page_title": "Vault", **_no_connector_context()}
        return get_templates().TemplateResponse(request, template, context)

    # Dispatch the three independent reads concurrently -- one slow/failing
    # op (e.g. an unreachable Vault on the mounts read) must not block the
    # others. Each envelope is rendered on its own card, so a partial
    # failure degrades one card rather than blanking the panel.
    seal, health, mounts = await asyncio.gather(
        _dispatch(operator, connector_id, _OP_SEAL_STATUS),
        _dispatch(operator, connector_id, _OP_HEALTH),
        _dispatch(operator, connector_id, _OP_MOUNTS),
    )
    context = {
        "active_surface": "vault",
        "page_title": "Vault",
        "error": None,
        "seal": seal,
        "health": health,
        "mounts": mounts,
    }
    return get_templates().TemplateResponse(request, template, context)


async def _render_auth(
    request: Request,
    session: UISessionContext,
) -> HTMLResponse:
    """Dispatch ``vault.sys.auth.list`` in-process and render the glance."""
    operator = await _resolve_operator(session)
    connector_id = await _vault_connector_id(operator)
    if connector_id is None:
        return get_templates().TemplateResponse(
            request, "vault/_status_auth.html", {"error": "no_vault_connector", "envelope": None}
        )

    envelope = await _dispatch(operator, connector_id, _OP_AUTH)
    context: dict[str, Any] = {"error": None, "envelope": envelope}
    return get_templates().TemplateResponse(request, "vault/_status_auth.html", context)


async def _dispatch(
    operator: Operator,
    connector_id: str,
    op_id: str,
) -> dict[str, Any]:
    """Call ``call_operation`` in-process for one sys read; return the envelope.

    The four ``vault.sys.*`` reads take no parameters (each returns a fixed
    cluster-wide view), so ``params`` is an empty object and ``target`` is
    ``None`` (vault is connector-id-routed and its handlers ignore the
    target). The structured envelope is returned verbatim -- ``ok``
    (carrying the result / handle) / ``error`` / ``denied`` all ride inside
    it (the dispatcher contract is "always return a structured result"); an
    unreachable or sealed Vault comes back as ``status="error"`` with
    ``extras["exception_class"]`` set, never as a 4xx.

    The log line records ONLY non-secret fields -- never the envelope
    ``result``. These sys reads carry no secret payload, but the discipline
    matches the T1 read path (defence in depth).
    """
    arguments: dict[str, Any] = {
        "connector_id": connector_id,
        "op_id": op_id,
        # The sys reads are connector-id-routed and ignore the target.
        "target": None,
        # All four sys read ops forbid any parameter (schema:
        # ``additionalProperties=False``), so the params object is empty.
        "params": {},
    }
    envelope = await call_operation(operator, arguments)
    log.info(
        "ui_vault_status_dispatch",
        op_id=op_id,
        connector_id=connector_id,
        status=envelope.get("status"),
        has_handle=envelope.get("handle") is not None,
        tenant_id=str(operator.tenant_id),
    )
    return envelope
