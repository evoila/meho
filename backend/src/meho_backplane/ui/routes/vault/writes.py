# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size -- a single-surface UI router carrying the
# three Vault write verbs (put / delete / move) of the secrets console: the
# confirm-modal GETs + dispatch POSTs + their form-field parsers + render
# helpers. Each route is a thin BFF handler delegating to a ``_render_*``
# helper, so the length is the count of write surfaces this one module owns
# (plus the load-bearing redaction/CSRF/RBAC docstring), not per-function
# complexity. Mirrors the operations router's same file-size escape valve.

"""Vault / secrets console WRITE routes: confirm-gated KV put / delete / move.

Initiative #1942 (G10.18 Vault / secrets console), Task #1957 (T2). The
mutating verbs over the T1 (#1956) read-only KV browser scaffold: the
confirm-gated ``vault.kv.put`` (CAS-aware), ``vault.kv.delete`` (soft-delete
specific versions), and ``secret.move`` (references-not-values). These are the
highest-blast-radius actions the Vault console gains -- every one is
``requires_approval=True``, so the gate must be UNMISSABLE and the result
render must surface the approval handoff rather than a silent success.

This module is intentionally SEPARATE from T1's :mod:`.routes` (which owns the
read-only GET browser) so the two surfaces -- and the parallel T3 status view
(#1958) -- can evolve without serial-merge collisions. It REUSES T1's operator
lift, connector-id resolver, and default-path helper (imported below) rather
than re-deriving them, so the load-bearing connector-id-shape invariant lives
in exactly one place.

Why a session BFF (same rationale as T1)
----------------------------------------

The Bearer-gated ``POST /api/v1/operations/call`` cannot be authenticated by a
browser carrying only the BFF session cookie, so these routes are
``require_ui_session``-gated and call
:func:`~meho_backplane.operations.meta_tools.call_operation` IN-PROCESS -- the
exact confirm-gated-dispatch pattern ``ui/routes/operations/routes.py`` uses
for its Run surface. ``call_operation`` is complete and tested; NO backend
work happens here (no new route, op, or meta-tool).

Route inventory (write -- confirm GETs + dispatch POSTs)
--------------------------------------------------------

* ``GET /ui/vault/put/confirm`` / ``…/delete/confirm`` / ``…/move/confirm`` --
  the UNMISSABLE destructive-confirm modal fragments, modelled on the
  operations Run modal (``_render_run_modal``). Each renders a prominent
  ``safety_level`` / ``requires_approval`` banner (``put`` = ``caution``,
  ``delete`` / ``move`` = ``dangerous``; all three ``requires_approval``), the
  op-specific inputs, and a confirm button carrying its OWN ``hx-headers``
  CSRF echo. The GET mints a fresh CSRF token and re-sets the ``meho_csrf``
  cookie so the double-submit pair lines up after the HTMX swap rotated it
  (the #1693 / #1754 cookie-desync class).
* ``POST /ui/vault/put`` -- dispatch ``vault.kv.put``
  (``connector_id="vault-1.x"``, target + ``{mount, path, data, cas?}``). The
  ``cas`` Check-And-Set guard rides in ``params`` ONLY when the operator
  explicitly set it (mirrors the CLI's ``Changed("cas")`` rule -- an unset
  field writes unconditionally, ``cas=0`` writes only if absent).
* ``POST /ui/vault/delete`` -- dispatch ``vault.kv.delete`` with the
  ``versions[]`` to soft-delete.
* ``POST /ui/vault/move`` -- dispatch ``secret.move`` against the DIFFERENT
  ``secret-broker-1.x`` connector with the schema's ``from`` / ``to``
  references (+ optional ``reason``). VALUE-FREE by contract: there is NO
  inline value input, the dispatched params carry no value, and the render
  surfaces only the ``{status, value_sha256, length}`` confirmation -- the
  value never enters op params, the response, a log event, or the audit row.

CSRF (load-bearing -- writes, unlike T1's reads, are gated)
-----------------------------------------------------------

T1's four GET reads are CSRF-free (the middleware only guards state-changing
methods). These POSTs ARE gated: the ``ui/csrf.py`` double-submit middleware
verifies the ``X-CSRF-Token`` header (or ``csrf_token`` form field) against
the ``meho_csrf`` cookie HMAC, BEFORE the route runs -- so a POST without the
token is a 403 the route never sees. The confirm GET mints + re-sets the
cookie so the swapped-in confirm button's ``hx-headers`` echo matches.

awaiting_approval (load-bearing -- the silent-success trap)
-----------------------------------------------------------

``call_operation`` ALWAYS returns a structured
:class:`~meho_backplane.connectors.schemas.OperationResult` envelope; errors
land INSIDE it (``status="error"``), not as HTTP 4xx. Because every write op
is ``requires_approval=True``, a human OPERATOR running one is routed to the
approval queue (G11.7-T1 #1401), so the COMMON return is
``status="awaiting_approval"`` with ``extras["approval_request_id"]`` (the
durable pending-row UUID, ``operations/_errors.py`` ``result_awaiting_approval``).
The result fragment surfaces it as a banner + a deep-link into
``/ui/approvals/{approval_request_id}`` (the surface #1778 shipped) so the
operator never thinks the write silently no-op'd.

Redaction (load-bearing -- the top-sensitivity surface)
-------------------------------------------------------

No secret value reaches a server log, audit row, telemetry frame, or
request-preview body. ``vault.kv.put``'s ``data`` object is operator-supplied
secret material: the BFF logs ONLY the op-id / connector-id / status /
approval-id (never the ``data`` / the ``params``). ``secret.move`` is
value-free by the connector's own contract; the UI keeps that invariant by
never offering a value field. The default-on per-tenant scope guard
(``connectors/vault/tenant_scope.py``) denies a write outside
``secret/tenants/{tenant_id}/`` as a ``status="error"`` envelope with
``extras.exception_class == "VaultTenantScopeError"`` -- rendered as a clear
scope message, not a raw 403.

RBAC (load-bearing -- OPERATOR tier, the gate is policy not role)
-----------------------------------------------------------------

The write verbs are OPERATOR-tier (like the operations Run surface) -- there
is NO ``tenant_admin`` hard-403 on the write POST. Safety is enforced by the
confirm modal + the dispatcher's ``policy_gate`` -> approval queue, NOT by
``TenantRole``; adding a role gate here would contradict the backend gate and
the sibling operations console. Tenant scoping derives from
``operator.tenant_id`` only (the session), never a form field a caller could
spoof.

Route ordering
--------------

The literal ``put`` / ``delete`` / ``move`` (and their ``…/confirm``)
segments register before any ``{param}`` route on the shared ``/ui/vault``
router (first-match-wins); these are POST / distinct-literal-prefix routes, so
they cannot collide with T1's GET slug routes regardless, but the ordering
discipline is pinned.
"""

from __future__ import annotations

import json
from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.operations.ingest.list_connectors import list_ingested_connectors
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.routes.approvals.render import set_csrf_cookie
from meho_backplane.ui.routes.vault.routes import (
    _DEFAULT_MOUNT,
    _MAX_MOUNT_LENGTH,
    _MAX_PATH_LENGTH,
    _MAX_TARGET_LENGTH,
    _default_path,
    _resolve_operator,
    _vault_connector_id,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["build_vault_writes_router"]

log = structlog.get_logger(__name__)

#: The mutating Vault KV op-ids this surface dispatches. ``vault.kv.put`` and
#: ``vault.kv.delete`` go to the ``vault-1.x`` connector; ``secret.move`` goes
#: to the SEPARATE ``secret-broker-1.x`` connector (sourced independently
#: below). Source of truth: the descriptor-spec tables in
#: :mod:`meho_backplane.connectors.vault.ops` /
#: :mod:`meho_backplane.connectors.secret.ops`.
_OP_PUT: Final[str] = "vault.kv.put"
_OP_DELETE: Final[str] = "vault.kv.delete"
_OP_MOVE: Final[str] = "secret.move"

#: The product slug whose ingested connector backs ``secret.move``. The
#: dispatchable id (``secret-broker-1.x``) is picked out of the ingested list
#: rather than hard-coded, so a re-version flows through without a literal
#: edit; the bare slug ``secret`` is never emitted (it 404s through
#: ``parse_connector_id``). DISTINCT from the vault product slug -- ``move``
#: is a different connector than ``put`` / ``delete``.
_SECRET_BROKER_PRODUCT: Final[str] = "secret"

#: ``safety_level`` per write verb, for the confirm modal's banner. ``put`` is
#: caution (state-changing, replaces the latest version wholesale); ``delete``
#: and ``move`` are dangerous (destructive / change-class). These mirror the
#: backend descriptor specs exactly; rendered, never dispatched.
_SAFETY_PUT: Final[str] = "caution"
_SAFETY_DELETE: Final[str] = "dangerous"
_SAFETY_MOVE: Final[str] = "dangerous"

#: Maximum raw ``data`` JSON length accepted on the put form. The secret
#: key/value object is a free-form JSON textarea; 64 KiB is a comfortable
#: ceiling for a real secret while bounding the body parse against an oversized
#: paste. The CSRF middleware already caps the buffered body at 256 KiB; this
#: is the surface-specific, friendlier-error bound. Matches the operations
#: console's ``_MAX_PARAMS_LENGTH``.
_MAX_DATA_LENGTH: Final[int] = 64 * 1024

#: Maximum ``versions`` field length on the delete form. The field is a short
#: comma-separated list of integers (``3,4,5``); a tight bound rejects an
#: oversized paste at the form boundary.
_MAX_VERSIONS_LENGTH: Final[int] = 1024

#: Maximum length of a ``secret.move`` ``<kind>:<ref>`` reference / the reason
#: string. A reference is a short store-qualified pointer
#: (``vault:secret/db/prod#password``); 1024 leaves room for a real URI-shaped
#: ref while bounding the parse.
_MAX_REF_LENGTH: Final[int] = 1024

#: Module-level ``Depends`` closure -- built once (rather than inline) to
#: satisfy ruff B008, matching the operations / approvals / T1 routers.
_require_session = Depends(require_ui_session)


async def _secret_broker_connector_id(operator: Operator) -> str | None:
    """Resolve the dispatchable ``secret-broker-1.x`` id, or ``None``.

    ``secret.move`` lives on a DIFFERENT connector than ``vault.kv.*``. Picks
    the secret-broker product's id out of the ingested-connector list (the
    same listing the operations picker + T1's vault resolver use), so the
    emitted id round-trips :func:`parse_connector_id` by construction (#773)
    and a re-version (``secret-broker-2.x``) flows through without a literal
    edit. The bare product slug ``secret`` is never emitted. ``None`` when no
    secret-broker connector is ingested for this operator's visibility -- the
    move confirm renders a "no secret-broker connector" hint rather than a
    dead-end 404.
    """
    items = await list_ingested_connectors(operator=operator)
    for item in items:
        if item.state == "ingested" and item.product == _SECRET_BROKER_PRODUCT:
            return item.connector_id
    return None


# A FastAPI route-registration factory. Every ``@router.<verb>`` handler must
# be declared INSIDE the factory so a test app can build parallel routers
# without shared route state (the convention every UI surface router follows,
# mirroring the operations / T1-vault routers' same escape valve). The six
# handler stubs are thin -- each delegates straight to a module-level
# ``_render_*`` helper -- so the length is decorator boilerplate, not logic.
# code-quality-allow: function-size -- route-registration factory (see above).
def build_vault_writes_router() -> APIRouter:
    """Construct the ``/ui/vault`` WRITE :class:`APIRouter` (T2 #1957).

    Factory function (not a module-level constant) so a test app can construct
    parallel routers without shared route state -- the convention every
    surface router follows. Included ahead of the stubs aggregate in
    :func:`meho_backplane.ui.routes.build_router`, alongside T1's
    :func:`~meho_backplane.ui.routes.vault.routes.build_vault_router`.

    The literal ``put`` / ``delete`` / ``move`` (+ ``…/confirm``) segments
    register BEFORE any ``{param}`` route on the shared ``/ui/vault`` router so
    the first-match-wins lookup is unambiguous; these are POST / distinct-
    literal routes so they cannot collide with T1's GET slug routes
    regardless, but the ordering discipline is pinned.
    """
    router = APIRouter(tags=["ui-vault-writes"])

    # ----- Confirm-modal GETs (mint + re-set the CSRF cookie) -----

    @router.get("/ui/vault/put/confirm", response_class=HTMLResponse)
    async def vault_put_confirm(
        request: Request,
        session: UISessionContext = _require_session,
        target: str = "",
        mount: str = _DEFAULT_MOUNT,
        path: str = "",
    ) -> HTMLResponse:
        """Render the ``vault.kv.put`` confirm modal (caution / requires approval).

        Mints + re-sets the ``meho_csrf`` cookie so the confirm button's OWN
        ``hx-headers`` echo lines up after the HTMX swap rotated it. Put is
        OPERATOR-tier -- no tenant_admin step; the policy gate escalates the
        ``requires_approval`` op to ``awaiting_approval`` on dispatch.
        """
        return await _render_confirm(
            request,
            session,
            verb="put",
            op_id=_OP_PUT,
            safety_level=_SAFETY_PUT,
            target=target,
            mount=mount,
            path=path,
        )

    @router.get("/ui/vault/delete/confirm", response_class=HTMLResponse)
    async def vault_delete_confirm(
        request: Request,
        session: UISessionContext = _require_session,
        target: str = "",
        mount: str = _DEFAULT_MOUNT,
        path: str = "",
    ) -> HTMLResponse:
        """Render the ``vault.kv.delete`` confirm modal (dangerous / requires approval).

        Soft-delete is reversible, but it stops reads from returning the named
        versions -- a destructive-class op, so the banner gets the loud
        treatment. Mints + re-sets the CSRF cookie like the put confirm.
        """
        return await _render_confirm(
            request,
            session,
            verb="delete",
            op_id=_OP_DELETE,
            safety_level=_SAFETY_DELETE,
            target=target,
            mount=mount,
            path=path,
        )

    @router.get("/ui/vault/move/confirm", response_class=HTMLResponse)
    async def vault_move_confirm(
        request: Request,
        session: UISessionContext = _require_session,
    ) -> HTMLResponse:
        """Render the ``secret.move`` confirm modal (dangerous / requires approval).

        VALUE-FREE: the modal carries ``from`` / ``to`` reference inputs and an
        optional reason, but NO value input -- the no-``--value`` invariant the
        CLI keeps. Resolves the SEPARATE ``secret-broker-1.x`` connector;
        renders a hint when it is not ingested. Mints + re-sets the CSRF cookie.
        """
        return await _render_move_confirm(request, session)

    # ----- Dispatch POSTs (CSRF-gated by the middleware) -----

    @router.post("/ui/vault/put", response_class=HTMLResponse)
    async def vault_put(
        request: Request,
        session: UISessionContext = _require_session,
        target: str = Form(default="", max_length=_MAX_TARGET_LENGTH),
        mount: str = Form(default=_DEFAULT_MOUNT, max_length=_MAX_MOUNT_LENGTH),
        path: str = Form(default="", max_length=_MAX_PATH_LENGTH),
        data: str = Form(default="", max_length=_MAX_DATA_LENGTH),
        cas: str = Form(default="", max_length=32),
    ) -> HTMLResponse:
        """Dispatch ``vault.kv.put`` in-process; render the result inline.

        CSRF-gated (a ``POST`` under ``/ui/``) + ``require_ui_session``-gated.
        ``data`` is the operator-supplied secret key/value JSON object;
        ``cas`` rides in ``params`` ONLY when the operator set it (the CLI
        ``Changed("cas")`` rule). The BFF logs no secret material.
        """
        return await _render_put_result(request, session, target, mount, path, data, cas)

    @router.post("/ui/vault/delete", response_class=HTMLResponse)
    async def vault_delete(
        request: Request,
        session: UISessionContext = _require_session,
        target: str = Form(default="", max_length=_MAX_TARGET_LENGTH),
        mount: str = Form(default=_DEFAULT_MOUNT, max_length=_MAX_MOUNT_LENGTH),
        path: str = Form(default="", max_length=_MAX_PATH_LENGTH),
        versions: str = Form(default="", max_length=_MAX_VERSIONS_LENGTH),
    ) -> HTMLResponse:
        """Dispatch ``vault.kv.delete`` in-process; render the result inline.

        CSRF-gated + ``require_ui_session``-gated. ``versions`` is the
        comma-separated list of version numbers to soft-delete.
        """
        return await _render_delete_result(request, session, target, mount, path, versions)

    @router.post("/ui/vault/move", response_class=HTMLResponse)
    async def vault_move(
        request: Request,
        session: UISessionContext = _require_session,
        from_ref: str = Form(default="", alias="from", max_length=_MAX_REF_LENGTH),
        to_ref: str = Form(default="", alias="to", max_length=_MAX_REF_LENGTH),
        reason: str = Form(default="", max_length=_MAX_REF_LENGTH),
    ) -> HTMLResponse:
        """Dispatch ``secret.move`` in-process; render the value-free result.

        CSRF-gated + ``require_ui_session``-gated. Dispatches the SEPARATE
        ``secret-broker-1.x`` connector with ``from`` / ``to`` references (+
        optional reason). VALUE-FREE: no value field is accepted, dispatched,
        logged, or rendered -- only the ``{status, value_sha256, length}``
        confirmation surfaces.
        """
        return await _render_move_result(request, session, from_ref, to_ref, reason)

    return router


# ---------------------------------------------------------------------------
# Confirm-modal renders (mint + re-set the CSRF cookie)
# ---------------------------------------------------------------------------


async def _render_confirm(
    request: Request,
    session: UISessionContext,
    *,
    verb: str,
    op_id: str,
    safety_level: str,
    target: str,
    mount: str,
    path: str,
) -> HTMLResponse:
    """Render a ``vault.kv.put`` / ``vault.kv.delete`` confirm modal fragment.

    Both share the ``vault-1.x`` connector + the mount/path inputs, so one
    renderer drives both via *verb* / *op_id* / *safety_level*. Mints a fresh
    CSRF token and re-sets the ``meho_csrf`` cookie so the confirm button's own
    ``hx-headers`` echo lines up after the HTMX swap rotated it (#1693 /
    #1754). A deploy with no ingested vault connector renders the
    no-connector hint rather than a confirm form that would 404 on dispatch.
    """
    operator = await _resolve_operator(session)
    connector_id = await _vault_connector_id(operator)
    csrf_token = mint_csrf_token(str(session.session_id))
    context: dict[str, Any] = {
        "verb": verb,
        "op_id": op_id,
        "safety_level": safety_level,
        "connector_id": connector_id,
        "default_mount": (mount or _DEFAULT_MOUNT).strip(),
        # Prefill the path with the supplied value, falling back to the
        # operator's tenant prefix so the common case never trips the guard.
        "default_path": path.strip() or _default_path(operator),
        "target": target.strip(),
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(request, f"vault/_confirm_{verb}.html", context)
    set_csrf_cookie(response, csrf_token)
    return response


async def _render_move_confirm(
    request: Request,
    session: UISessionContext,
) -> HTMLResponse:
    """Render the ``secret.move`` confirm modal fragment (VALUE-FREE).

    Resolves the SEPARATE ``secret-broker-1.x`` connector (not the vault
    connector); renders a hint when it is not ingested. The modal carries
    ``from`` / ``to`` reference inputs + an optional reason but NO value input
    -- the no-``--value`` invariant. Mints + re-sets the CSRF cookie like the
    put / delete confirms.
    """
    operator = await _resolve_operator(session)
    connector_id = await _secret_broker_connector_id(operator)
    csrf_token = mint_csrf_token(str(session.session_id))
    context: dict[str, Any] = {
        "verb": "move",
        "op_id": _OP_MOVE,
        "safety_level": _SAFETY_MOVE,
        "connector_id": connector_id,
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(request, "vault/_confirm_move.html", context)
    set_csrf_cookie(response, csrf_token)
    return response


# ---------------------------------------------------------------------------
# Dispatch + result renders
# ---------------------------------------------------------------------------


async def _render_put_result(
    request: Request,
    session: UISessionContext,
    target: str,
    mount: str,
    path: str,
    data: str,
    cas: str,
) -> HTMLResponse:
    """Dispatch ``vault.kv.put`` in-process and render the result inline.

    Parses the free-form ``data`` JSON object server-side (a malformed body is
    a typed inline 400 form error, not a 422); attaches ``cas`` ONLY when the
    operator set it (the CLI ``Changed("cas")`` rule). The structured envelope
    (``awaiting_approval`` is the common case; ``error`` / ``denied`` /
    ``ok``) is surfaced verbatim by the shared write-result fragment.
    """
    operator = await _resolve_operator(session)
    connector_id = await _vault_connector_id(operator)
    if connector_id is None:
        return _render_write_form_error(request, "put", "no_vault_connector")

    try:
        data_obj = _parse_data_object(data)
    except ValueError as exc:
        return _render_write_form_error(request, "put", str(exc))
    try:
        cas_value = _parse_cas(cas)
    except ValueError as exc:
        return _render_write_form_error(request, "put", str(exc))

    params: dict[str, Any] = {"path": path.strip(), "data": data_obj}
    clean_mount = mount.strip()
    if clean_mount:
        params["mount"] = clean_mount
    # CAS is the Check-And-Set guard. Carry it ONLY when the operator set the
    # field, so an unset field writes unconditionally and an explicit ``0``
    # writes only if the key is absent (the CLI ``Changed("cas")`` rule). A
    # blank field is "flag absent", NOT ``cas=0``.
    if cas_value is not None:
        params["cas"] = cas_value

    envelope = await _dispatch_write(operator, connector_id, _OP_PUT, target, params)
    return _render_write_result(request, "put", envelope)


async def _render_delete_result(
    request: Request,
    session: UISessionContext,
    target: str,
    mount: str,
    path: str,
    versions: str,
) -> HTMLResponse:
    """Dispatch ``vault.kv.delete`` in-process and render the result inline.

    Parses the comma-separated ``versions`` field into a list of positive
    integers (an empty / malformed list is a typed inline 400 form error). The
    structured envelope is surfaced verbatim by the shared write-result
    fragment.
    """
    operator = await _resolve_operator(session)
    connector_id = await _vault_connector_id(operator)
    if connector_id is None:
        return _render_write_form_error(request, "delete", "no_vault_connector")

    try:
        version_list = _parse_versions(versions)
    except ValueError as exc:
        return _render_write_form_error(request, "delete", str(exc))

    params: dict[str, Any] = {"path": path.strip(), "versions": version_list}
    clean_mount = mount.strip()
    if clean_mount:
        params["mount"] = clean_mount

    envelope = await _dispatch_write(operator, connector_id, _OP_DELETE, target, params)
    return _render_write_result(request, "delete", envelope)


async def _render_move_result(
    request: Request,
    session: UISessionContext,
    from_ref: str,
    to_ref: str,
    reason: str,
) -> HTMLResponse:
    """Dispatch ``secret.move`` in-process and render the VALUE-FREE result.

    Dispatches the SEPARATE ``secret-broker-1.x`` connector with the schema's
    ``from`` / ``to`` reference fields (+ optional ``reason``). NO value field
    is accepted or forwarded -- the no-``--value`` invariant -- and the result
    fragment renders only ``{status, value_sha256, length}``. A blank ``from``
    or ``to`` is a typed inline 400 form error (the schema requires both).
    """
    operator = await _resolve_operator(session)
    connector_id = await _secret_broker_connector_id(operator)
    if connector_id is None:
        return _render_write_form_error(request, "move", "no_secret_broker_connector")

    clean_from = from_ref.strip()
    clean_to = to_ref.strip()
    if not clean_from or not clean_to:
        return _render_write_form_error(
            request, "move", "Both a source (from) and a destination (to) reference are required."
        )

    # References ONLY -- never a value field. ``additionalProperties: false``
    # on the backend schema rejects a smuggled value, but the UI never offers
    # one in the first place (the no-``--value`` invariant).
    params: dict[str, Any] = {"from": clean_from, "to": clean_to}
    clean_reason = reason.strip()
    if clean_reason:
        params["reason"] = clean_reason

    envelope = await _dispatch_write(operator, connector_id, _OP_MOVE, target="", params=params)
    return _render_write_result(request, "move", envelope)


async def _dispatch_write(
    operator: Operator,
    connector_id: str,
    op_id: str,
    target: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Call ``call_operation`` in-process for one write op; return the envelope.

    Builds the ``{connector_id, op_id, target, params}`` arguments the
    meta-tool expects (``target`` empty -> ``None``: vault + secret-broker are
    connector-id-routed and their handlers do not consume a target). The
    structured envelope rides back verbatim -- ``awaiting_approval`` (the
    common case for these ``requires_approval`` ops) / ``ok`` / ``error`` /
    ``denied`` all inside it; a ``VaultTenantScopeError`` comes back as
    ``status="error"`` with ``extras.exception_class`` set, never a 4xx.

    The log line records ONLY non-secret fields -- the op-id / connector-id /
    status / approval-id. It NEVER logs ``params`` (``vault.kv.put``'s ``data``
    is secret material) or the envelope ``result`` (the redaction invariant on
    the top-sensitivity surface).
    """
    arguments: dict[str, Any] = {
        "connector_id": connector_id,
        "op_id": op_id,
        "target": target.strip() or None,
        "params": params,
    }
    envelope = await call_operation(operator, arguments)
    extras = envelope.get("extras") or {}
    log.info(
        "ui_vault_write_dispatch",
        op_id=op_id,
        connector_id=connector_id,
        status=envelope.get("status"),
        approval_request_id=extras.get("approval_request_id"),
        tenant_id=str(operator.tenant_id),
    )
    return envelope


def _render_write_result(
    request: Request,
    verb: str,
    envelope: dict[str, Any],
) -> HTMLResponse:
    """Render the shared write-result fragment for a dispatched write op.

    One fragment (``vault/_write_result.html``) drives all three verbs; *verb*
    selects the value-free ``secret.move`` ``ok`` branch vs the put / delete
    success branch. The ``awaiting_approval`` (deep-link), ``error`` /
    ``denied`` (with the tenant-scope special case), and form-error branches
    are verb-agnostic.
    """
    context: dict[str, Any] = {
        "verb": verb,
        "envelope": envelope,
        "error_message": None,
        "error_kind": None,
    }
    return get_templates().TemplateResponse(request, "vault/_write_result.html", context)


def _render_write_form_error(
    request: Request,
    verb: str,
    message_or_kind: str,
) -> HTMLResponse:
    """Render the write-result fragment with an inline form error (HTTP 400).

    Covers the malformed-input cases (bad ``data`` JSON, empty / non-integer
    ``versions``, missing ``from`` / ``to``) and the no-connector-ingested
    cases (``no_vault_connector`` / ``no_secret_broker_connector``, passed as a
    typed *kind* the template maps to a friendly hint). The op was never
    dispatched; the ``400`` status lets a caller distinguish a form fault from
    a structured in-envelope status. No ``envelope`` is set so the template
    renders the alert instead of a dispatch result.
    """
    known_kinds = {"no_vault_connector", "no_secret_broker_connector"}
    error_kind = message_or_kind if message_or_kind in known_kinds else None
    context: dict[str, Any] = {
        "verb": verb,
        "envelope": None,
        "error_message": None if error_kind else message_or_kind,
        "error_kind": error_kind,
    }
    return get_templates().TemplateResponse(
        request,
        "vault/_write_result.html",
        context,
        status_code=status.HTTP_400_BAD_REQUEST,
    )


# ---------------------------------------------------------------------------
# Form-field parsers (typed inline errors, never a 422)
# ---------------------------------------------------------------------------


def _parse_data_object(data: str) -> dict[str, Any]:
    """Parse the put form's free-form ``data`` JSON into a non-empty object.

    The backend schema requires a JSON object with ``minProperties: 1``. A
    blank field, a malformed body, a non-object value (list / scalar), or an
    empty object raises :class:`ValueError` with an operator-legible message
    the caller renders inline -- so the operator stays in the modal rather than
    getting a raw 422. The error message names only the structural fault, never
    echoes the (secret) value.
    """
    text = data.strip()
    if not text:
        raise ValueError('Provide the secret data as a JSON object (e.g. {"password": "..."}).')
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"data is not valid JSON: {exc.msg} (line {exc.lineno})") from exc
    if not isinstance(value, dict):
        raise ValueError('data must be a JSON object (e.g. {"password": "..."}).')
    if not value:
        raise ValueError("data must contain at least one key/value pair.")
    return value


def _parse_cas(cas: str) -> int | None:
    """Parse the optional Check-And-Set field into an int, or ``None`` if unset.

    A blank field means "flag absent" -> ``None`` (write unconditionally), NOT
    ``cas=0``. A non-empty field must parse to a non-negative integer (``0`` ⇒
    write only if absent; ``N`` ⇒ write only if the current version is exactly
    ``N``). Anything else raises :class:`ValueError` rendered inline.
    """
    text = cas.strip()
    if not text:
        return None
    _cas_error = "CAS must be a non-negative integer (or blank to write unconditionally)."
    try:
        value = int(text)
    except ValueError as exc:
        raise ValueError(_cas_error) from exc
    if value < 0:
        raise ValueError(_cas_error)
    return value


def _parse_versions(versions: str) -> list[int]:
    """Parse the delete form's comma-separated ``versions`` into positive ints.

    The backend schema requires a non-empty array of integers ``>= 1``. A
    blank field, a non-integer token, or a non-positive value raises
    :class:`ValueError` rendered inline -- so the operator stays in the modal.
    Duplicate / unordered tokens are accepted as typed (Vault de-dupes); the
    parse only enforces the ``>= 1`` integer shape.
    """
    text = versions.strip()
    if not text:
        raise ValueError("Provide at least one version number to delete (e.g. 3,4,5).")
    result: list[int] = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError as exc:
            raise ValueError(
                f"versions must be a comma-separated list of integers; '{token}' is not an integer."
            ) from exc
        if value < 1:
            raise ValueError("version numbers must be 1 or greater.")
        result.append(value)
    if not result:
        raise ValueError("Provide at least one version number to delete (e.g. 3,4,5).")
    return result
