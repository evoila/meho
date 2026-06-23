# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault KV browser UI routes: target/mount picker + list + read + versions.

Initiative #1942 (G10.18 Vault / secrets console), Task #1956 (T1). The
read-only KV browser over the vault connector. Today the only human
surface over ``vault.kv.*`` is the ``meho vault kv …`` CLI verb tree
(``cli/internal/cmd/vault/kv.go``); this package adds the purpose-built
KV tree browser the operator console was missing. The KV write (T2) and
seal/health/mounts status (T3) surfaces build on this scaffold.

Why a session BFF and not the Bearer ``/api/v1/operations/call`` route
--------------------------------------------------------------------

The REST operation routes (``api/v1/operations.py``) are Bearer-gated
over a verified JWT; a browser carrying only the BFF session cookie
cannot authenticate them. So this module adds ``/ui/vault`` sub-routes
that are ``require_ui_session``-gated and call the operation meta-tool
:func:`~meho_backplane.operations.meta_tools.call_operation` IN-PROCESS
-- the exact pattern ``ui/routes/operations/routes.py`` uses for its
dispatch (Run) surface. ``call_operation`` is complete and tested; no
backend work happens here. There is deliberately NO ``/ui/vault`` REST
route and NO second dispatcher.

Route inventory (read-only, all GET)
------------------------------------

* ``GET /ui/vault`` -- the full-page browser (``extends base.html``,
  ``active_surface="vault"``): a connector/target picker, a mount/path
  picker defaulted to the operator's tenant prefix, and an empty key
  list. Content-negotiated on ``HX-Request`` is unnecessary here (the
  list/read/versions partials have their own routes), so the index is
  always the full page.
* ``GET /ui/vault/list`` -- the HTMX list partial. Dispatches
  ``vault.kv.list`` (``connector_id="vault-1.x"``, ``target`` +
  ``{mount, path}`` params) and renders the child key NAMES. Handles the
  set-shaped result: when the envelope carries ``handle is not None``
  (the dispatcher reduces a >threshold key set to a JSONFlux sample +
  handle), it renders the handle metadata + a "full list available
  out-of-band" note, NOT a huge blob -- mirroring the operations
  launcher's handle branch.
* ``GET /ui/vault/read`` -- the HTMX read partial. Dispatches
  ``vault.kv.read`` and renders the secret. The values reach the
  operator's browser (reveal-on-click + copy) but the BFF NEVER logs the
  envelope ``result`` -- the deliberate, scoped un-redaction happens only
  at the response template (see the redaction note below).
* ``GET /ui/vault/versions`` -- the HTMX version-history partial.
  Dispatches ``vault.kv.versions`` and renders the per-version metadata
  table (created / deleted / destroyed). Never returns secret values.

The literal ``list`` / ``read`` / ``versions`` segments register BEFORE
any future ``{param}`` route on the same router (first-match-wins); the
router is registered ahead of the stubs aggregate in
:func:`meho_backplane.ui.routes.build_router`. All four routes are GET,
so they need no CSRF gate (the CSRF middleware only guards state-changing
methods); the write routes T2 adds are the ``POST``s that will.

connector_id shape (load-bearing)
---------------------------------

The dispatchable id is ``vault-1.x`` (the ``<impl_id>-<version>`` id),
NEVER the bare product slug ``vault`` -- the slug parses to a different /
non-ingested id and 404s through
:func:`~meho_backplane.operations._lookup.parse_connector_id`. It is
sourced from the ingested-connector list
(:func:`~meho_backplane.operations.ingest.list_connectors.list_ingested_connectors`,
``state == "ingested"`` only) -- the same listing the operations picker
uses -- so a re-version (``vault-2.x``) flows through without a literal
edit. A deploy where the vault connector is not ingested renders an
empty picker + a "no vault connector" hint rather than a dead-end 404.

Redaction (load-bearing -- the top-sensitivity surface)
-------------------------------------------------------

``vault.kv.read`` legitimately returns ``{"data": <secret key/values>,
"version": …}`` -- the values must reach the operator's browser to be
useful, but must NOT bleed into server logs, request-preview bodies,
audit rows, or telemetry. The dispatcher's audit/broadcast path already
redacts secret material at the connector boundary (``vault.kv.read`` is
``credential_read``-classified, aggregate-only on the broadcast feed),
so the read render is a *deliberate, scoped* un-redaction at the
response template ONLY: this module's structlog calls log the op-id /
target / status / handle-presence, NEVER ``envelope["result"]``. The
read template gates the value (reveal-on-click + copy-without-logging).
``vault.kv.list`` / ``vault.kv.versions`` return only key names / version
metadata, never values.

Tenant-scope guard (default-on since v0.15.0, #1725)
----------------------------------------------------

The per-tenant guard
(:func:`~meho_backplane.connectors.vault.tenant_scope.enforce_tenant_scope`)
is enforcing by default; it matches the mount-pinned prefix
``secret/tenants/{tenant_id}/`` (the canonical layout is
``secret/data/tenants/<tenant_id>/<target>``). The mount/path picker
DEFAULTS to the operator's rendered tenant prefix so the common case
never trips the guard. A list/read/versions request outside that prefix
raises :class:`~meho_backplane.connectors.vault.tenant_scope.VaultTenantScopeError`,
which the dispatcher wraps into a ``status="error"`` envelope with
``extras["exception_class"] == "VaultTenantScopeError"``; the render
surfaces it as a clear "outside your tenant scope" message, not a raw
403 envelope.

Tenant isolation
----------------

Every dispatch derives ``tenant_id`` from the validated
:class:`UISessionContext` only (via the lifted :class:`Operator`); the
form carries no tenant field for a caller to spoof.
"""

from __future__ import annotations

from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vault.tenant_scope import rendered_tenant_prefix
from meho_backplane.operations.ingest.list_connectors import list_ingested_connectors
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.auth.refresh import (
    load_fresh_session,
    verify_access_token_with_refresh,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["build_vault_router"]

log = structlog.get_logger(__name__)

#: The vault KV op-ids the browser dispatches. These mirror the CLI verb
#: tree's constants verbatim (``cli/internal/cmd/vault/kv.go:22-26``); the
#: source of truth is the descriptor-spec table in
#: :mod:`meho_backplane.connectors.vault.ops`.
_OP_LIST: Final[str] = "vault.kv.list"
_OP_READ: Final[str] = "vault.kv.read"
_OP_VERSIONS: Final[str] = "vault.kv.versions"

#: The product slug whose ingested connector backs the KV ops. Used to
#: pick the dispatchable ``<impl_id>-<version>`` id out of the ingested
#: list rather than hard-coding ``vault-1.x`` (which drifts on a
#: re-version). The bare slug is NEVER emitted as a connector_id.
_VAULT_PRODUCT: Final[str] = "vault"

#: Default KV-v2 mount the picker pre-fills. Matches the connector's
#: ``_DEFAULT_KV_MOUNT`` so the rendered ``<mount>/<path>`` lands inside
#: the mount-pinned tenant-scope prefix ``secret/tenants/{tenant_id}/``.
_DEFAULT_MOUNT: Final[str] = "secret"

#: Maximum mount-name length accepted on the query string. A mount handle
#: is a short slug (``secret``); bounding the wire shape rejects an
#: oversized paste at the form boundary (422) rather than forwarding it.
_MAX_MOUNT_LENGTH: Final[int] = 256

#: Maximum path length accepted on the query string. A KV path is a
#: short ``tenants/<id>/<target>`` slug; the bound protects the form
#: parse against a paste-from-clipboard accident.
_MAX_PATH_LENGTH: Final[int] = 1024

#: Maximum target-name length accepted on the query string. A target name
#: is a short slug (``rdc-vault``); the bound protects the form parse.
_MAX_TARGET_LENGTH: Final[int] = 256

#: Module-level ``Depends`` closure -- built once (rather than inline) to
#: satisfy ruff B008, matching the operations / approvals routers.
_require_session = Depends(require_ui_session)


async def _resolve_operator(session: UISessionContext) -> Operator:
    """Reconstruct the full :class:`Operator` from the BFF session.

    ``call_operation`` needs a real :class:`Operator` (it reads
    ``operator.tenant_id`` for the tenant-scoped dispatch + the
    tenant-scope guard's binding identity, and ``operator.raw_jwt`` for
    the vault handlers' JWT/OIDC login). Mirrors the operations router's
    same lift: load the decrypted session row and re-verify its access
    token through the chassis JWT chain via
    :func:`~meho_backplane.ui.auth.refresh.verify_access_token_with_refresh`,
    which silently refreshes once on the ``token_expired`` 401 before
    re-verifying -- so an expired-but-refreshable token serves the surface
    (and ``operator.raw_jwt`` carries the refreshed token into the vault
    handlers' JWT/OIDC login) instead of 401-ing mid-session, while a
    same-session role demotion is still caught the next time the operator
    browses.

    Raises :class:`fastapi.HTTPException` 401 when the session was revoked
    / expired between the middleware check and here, or when the refresh
    attempt itself fails (``session_expired`` -- the BFF error handler maps
    it to a login redirect for HTML requests).
    """
    decrypted = await load_fresh_session(session.session_id)
    if decrypted is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ui_session_required",
        )
    settings = get_settings()
    _refreshed, operator = await verify_access_token_with_refresh(
        decrypted,
        expected_audience=settings.keycloak_audience,
    )
    return operator


async def _vault_connector_id(operator: Operator) -> str | None:
    """Resolve the dispatchable vault ``<impl_id>-<version>`` id, or None.

    Picks the vault product's id out of the ingested-connector list (the
    same listing the operations picker is populated from), so the emitted
    id round-trips :func:`parse_connector_id` by construction (#773) and
    a re-version (``vault-2.x``) flows through without a literal edit. The
    bare product slug ``vault`` is never emitted. ``None`` when no vault
    connector is ingested for this operator's visibility -- the index
    renders a "no vault connector" hint rather than a dead-end 404.
    """
    items = await list_ingested_connectors(operator=operator)
    for item in items:
        if item.state == "ingested" and item.product == _VAULT_PRODUCT:
            return item.connector_id
    return None


def _default_path(operator: Operator) -> str:
    """Render the path the picker pre-fills: the operator's tenant prefix.

    The tenant-scope guard matches ``<mount>/<path>`` against the rendered
    ``secret/tenants/{tenant_id}/`` prefix. The picker defaults the mount
    to ``secret`` and the path to ``tenants/<tenant_id>`` (the prefix with
    its mount segment stripped, since the mount is a separate field), so a
    first browse lands inside the guard's namespace and never trips it.

    When the guard is disabled (``VAULT_KV_TENANT_SCOPE_PREFIX=""``) there
    is no tenant partition to default into, so the path is left blank.
    """
    template = get_settings().vault_kv_tenant_scope_prefix.strip()
    if not template:
        return ""
    rendered = rendered_tenant_prefix(operator, template=template).strip().strip("/")
    # Strip the leading mount segment (the mount is its own picker field):
    # ``secret/tenants/<id>`` -> ``tenants/<id>``. If the prefix is a
    # single segment (mount only), the path defaults to empty.
    head, _, tail = rendered.partition("/")
    if head == _DEFAULT_MOUNT and tail:
        return tail
    return rendered


# code-quality-allow: function-size -- a FastAPI route-registration factory.
# Every ``@router.<verb>`` handler must be declared INSIDE the factory so a
# test app can build parallel routers without shared route state (the
# convention every UI surface router follows, mirroring the operations
# router's same escape valve). The four handler stubs are thin -- each
# delegates straight to a module-level ``_render_*`` helper -- so the length
# is decorator boilerplate, not logic.
def build_vault_router() -> APIRouter:
    """Construct the ``/ui/vault*`` :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without shared route state -- the
    convention every surface router (operations / approvals / kb) follows.
    Registered ahead of the stubs aggregate in
    :func:`meho_backplane.ui.routes.build_router`.

    The literal ``list`` / ``read`` / ``versions`` segments register
    BEFORE the bare ``/ui/vault`` index so the first-match-wins lookup is
    unambiguous; there is no ``{param}`` route on this router yet (the T2
    write routes are ``POST``s under literal segments), but the ordering
    discipline is pinned for when one lands.
    """
    router = APIRouter(tags=["ui-vault"])

    @router.get("/ui/vault/list", response_class=HTMLResponse)
    async def vault_list(
        request: Request,
        session: UISessionContext = _require_session,
        target: str = Query(default="", max_length=_MAX_TARGET_LENGTH),
        mount: str = Query(default=_DEFAULT_MOUNT, max_length=_MAX_MOUNT_LENGTH),
        path: str = Query(default="", max_length=_MAX_PATH_LENGTH),
    ) -> HTMLResponse:
        """Render the child-key-names list partial (``vault.kv.list``).

        Read-only. The result carries key NAMES only -- Vault never
        exposes secret values through the list endpoint. A set-shaped
        spill (``handle is not None``) renders the handle metadata, not a
        huge blob.
        """
        return await _render_list(request, session, target, mount, path)

    @router.get("/ui/vault/read", response_class=HTMLResponse)
    async def vault_read(
        request: Request,
        session: UISessionContext = _require_session,
        target: str = Query(default="", max_length=_MAX_TARGET_LENGTH),
        mount: str = Query(default=_DEFAULT_MOUNT, max_length=_MAX_MOUNT_LENGTH),
        path: str = Query(default="", max_length=_MAX_PATH_LENGTH),
    ) -> HTMLResponse:
        """Render the secret read partial (``vault.kv.read``), reveal-gated.

        The secret values reach the browser but are reveal-on-click +
        copy-without-logging; the BFF never logs the envelope ``result``.
        """
        return await _render_read(request, session, target, mount, path)

    @router.get("/ui/vault/versions", response_class=HTMLResponse)
    async def vault_versions(
        request: Request,
        session: UISessionContext = _require_session,
        target: str = Query(default="", max_length=_MAX_TARGET_LENGTH),
        mount: str = Query(default=_DEFAULT_MOUNT, max_length=_MAX_MOUNT_LENGTH),
        path: str = Query(default="", max_length=_MAX_PATH_LENGTH),
    ) -> HTMLResponse:
        """Render the version-history metadata table (``vault.kv.versions``).

        Read-only metadata; never returns secret values.
        """
        return await _render_versions(request, session, target, mount, path)

    @router.get("/ui/vault", response_class=HTMLResponse)
    async def vault_index(
        request: Request,
        session: UISessionContext = _require_session,
    ) -> HTMLResponse:
        """Render the full-page KV browser: pickers + an empty key list."""
        return await _render_index(request, session)

    return router


async def _render_index(
    request: Request,
    session: UISessionContext,
) -> HTMLResponse:
    """Render the full-page browser: connector/target picker + mount/path picker."""
    operator = await _resolve_operator(session)
    connector_id = await _vault_connector_id(operator)
    context: dict[str, Any] = {
        "active_surface": "vault",
        "page_title": "Vault",
        "connector_id": connector_id,
        "default_mount": _DEFAULT_MOUNT,
        "default_path": _default_path(operator),
    }
    return get_templates().TemplateResponse(request, "vault/index.html", context)


def _no_connector_error_context() -> dict[str, Any]:
    """Context for the "no vault connector ingested" inline error panel."""
    return {"error": "no_vault_connector", "envelope": None}


async def _render_list(
    request: Request,
    session: UISessionContext,
    target: str,
    mount: str,
    path: str,
) -> HTMLResponse:
    """Dispatch ``vault.kv.list`` in-process and render the child key names."""
    operator = await _resolve_operator(session)
    connector_id = await _vault_connector_id(operator)
    if connector_id is None:
        return get_templates().TemplateResponse(
            request, "vault/_keys.html", _no_connector_error_context()
        )

    envelope = await _dispatch(operator, connector_id, _OP_LIST, target, mount, path)
    context: dict[str, Any] = {
        "error": None,
        "envelope": envelope,
        "mount": mount.strip(),
        "path": path.strip(),
        "target": target.strip(),
    }
    return get_templates().TemplateResponse(request, "vault/_keys.html", context)


async def _render_read(
    request: Request,
    session: UISessionContext,
    target: str,
    mount: str,
    path: str,
) -> HTMLResponse:
    """Dispatch ``vault.kv.read`` in-process and render the secret reveal-gated."""
    operator = await _resolve_operator(session)
    connector_id = await _vault_connector_id(operator)
    if connector_id is None:
        return get_templates().TemplateResponse(
            request, "vault/_secret.html", _no_connector_error_context()
        )

    envelope = await _dispatch(operator, connector_id, _OP_READ, target, mount, path)
    context: dict[str, Any] = {
        "error": None,
        "envelope": envelope,
        "mount": mount.strip(),
        "path": path.strip(),
        "target": target.strip(),
    }
    return get_templates().TemplateResponse(request, "vault/_secret.html", context)


async def _render_versions(
    request: Request,
    session: UISessionContext,
    target: str,
    mount: str,
    path: str,
) -> HTMLResponse:
    """Dispatch ``vault.kv.versions`` in-process and render the version table."""
    operator = await _resolve_operator(session)
    connector_id = await _vault_connector_id(operator)
    if connector_id is None:
        return get_templates().TemplateResponse(
            request, "vault/_versions.html", _no_connector_error_context()
        )

    envelope = await _dispatch(operator, connector_id, _OP_VERSIONS, target, mount, path)
    context: dict[str, Any] = {
        "error": None,
        "envelope": envelope,
        "mount": mount.strip(),
        "path": path.strip(),
        "target": target.strip(),
    }
    return get_templates().TemplateResponse(request, "vault/_versions.html", context)


async def _dispatch(
    operator: Operator,
    connector_id: str,
    op_id: str,
    target: str,
    mount: str,
    path: str,
) -> dict[str, Any]:
    """Call ``call_operation`` in-process for one KV op; return the envelope.

    Builds the ``{connector_id, op_id, target, params}`` arguments the
    meta-tool expects. ``target`` is forwarded as a bare string (empty ->
    ``None``: the vault connector is connector-id-routed and its handlers
    do not consume a target, so an unset target field is valid). ``mount``
    is omitted from ``params`` when blank so the connector applies its
    ``_DEFAULT_KV_MOUNT``.

    The structured envelope is returned verbatim -- ``ok`` (carrying the
    result / handle) / ``error`` / ``denied`` all ride inside it (the
    dispatcher contract is "always return a structured result"); the
    ``VaultTenantScopeError`` an out-of-scope path raises comes back as
    ``status="error"`` with ``extras["exception_class"]`` set, never as a
    4xx. The log line records ONLY non-secret fields -- the read op's
    secret ``result`` is never logged (the redaction invariant).
    """
    params: dict[str, Any] = {"path": path.strip()}
    clean_mount = mount.strip()
    if clean_mount:
        params["mount"] = clean_mount
    arguments: dict[str, Any] = {
        "connector_id": connector_id,
        "op_id": op_id,
        # Bare-string target; empty -> None (vault is connector-id-routed
        # and its handlers ignore the target).
        "target": target.strip() or None,
        "params": params,
    }
    envelope = await call_operation(operator, arguments)
    # Log ONLY non-secret fields. NEVER log ``envelope["result"]`` -- for
    # ``vault.kv.read`` that is the secret material the redaction invariant
    # keeps off logs/audit/telemetry.
    log.info(
        "ui_vault_dispatch",
        op_id=op_id,
        connector_id=connector_id,
        status=envelope.get("status"),
        has_handle=envelope.get("handle") is not None,
        tenant_id=str(operator.tenant_id),
    )
    return envelope
