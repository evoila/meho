# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/connectors/{name}`` -- the per-target detail surface.

Initiative #340 (G10.3 Connectors + Targets UI), Task #873 (T1) work
items #2 / #6 / #7. The detail page renders four cards under the
target's identity header:

1. **Properties** -- the full :class:`~meho_backplane.db.models.Target`
   row (id, aliases, host, port, fqdn, auth_model, vpn_required,
   extras, notes, preferred_impl_id, timestamps) as a description
   list. This is the "full row" surface mentioned in the issue body.

2. **Fingerprint card** (``_fingerprint_card.html``) -- the cached
   :class:`~meho_backplane.connectors.schemas.FingerprintResult` from
   the most recent successful probe. Rendered as a structured
   summary (product/version/build/extras) plus a re-probe button.
   The button (``hx-post`` to ``/ui/connectors/{name}/probe``) is
   tenant_admin-gated -- the gate runs server-side in the
   :mod:`~meho_backplane.ui.routes.connectors.probe` handler; the
   template hides the button when the operator's session is not a
   tenant_admin so the affordance only appears to operators who can
   actually use it.

3. **Recent operations** (``_recent_ops.html``) -- the last 10
   ``audit_log`` rows scoped to this target plus the operator's
   tenant. Initially seeded by the server-rendered list, then live-
   appended via the existing ``/ui/broadcast/stream`` SSE bridge
   filtered to ``target=<name>`` (G6.1 + G10.1). The HTMX 2 ``sse``
   extension's ``sse-swap="broadcast"`` directive deserialises the
   ``event: broadcast`` frames; an Alpine.js controller prepends
   matching events to the list (cap at 50 rendered rows so a busy
   target doesn't grow the DOM unboundedly).

4. **Available operations matrix** (``_ops_matrix.html``) -- the
   ``endpoint_descriptor`` rows for the resolved connector, grouped
   by ``operation_group`` and rendered as a collapsed accordion
   (each group's ``when_to_use`` is the subtitle; clicking expands
   to the per-op rows carrying ``safety_level`` and
   ``requires_approval`` flags). The connector_id is resolved from
   the target's ``(product, fingerprint.version)`` via the same
   :func:`~meho_backplane.connectors.resolver.resolve_connector_or_label`
   helper the ``/api/v1/targets/{name}/probe`` REST route uses, so
   the matrix surfaces the dispatcher's actual choice rather than
   re-implementing connector selection. An ambiguous or missing
   connector is rendered as an explanation panel pointing the
   operator at the remediation step (set ``preferred_impl_id`` or
   register a connector); the operator can still re-probe to refresh
   the fingerprint.

Tenant scoping is non-overrideable. The target resolver
:func:`~meho_backplane.targets.resolver.resolve_target` raises 404
on a name belonging to another tenant (alias-aware match). The audit-
log read joins on ``audit_log.target_id == targets.id`` AND
``audit_log.tenant_id == session_ctx.tenant_id`` so a stale
``target_id`` from another tenant could never surface even if it
existed in this tenant's audit_log (which the soft-FK on
``audit_log.target_id`` makes structurally impossible but the join
defends against anyway). The operations-matrix query filters on
``(tenant_id IS NULL OR tenant_id == session_ctx.tenant_id)`` --
the same shape :func:`~meho_backplane.operations.meta_tools
.list_operation_groups` uses for the agent surface.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.registry import all_connectors_v2, registered_product_tokens
from meho_backplane.connectors.resolver import resolve_connector_or_label
from meho_backplane.db.engine import get_raw_session
from meho_backplane.db.models import (
    AuditLog,
    EndpointDescriptor,
    GraphNode,
    OperationGroup,
)
from meho_backplane.db.models import (
    Target as TargetORM,
)
from meho_backplane.targets.resolver import resolve_target
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.connectors.operator import (
    OperatorRoleProbe,
    resolve_role_probe,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["build_detail_router"]

_log = structlog.get_logger(__name__)

#: Number of recent ``audit_log`` rows seeded into the recent-ops card.
#: Acceptance criterion on #873 pins this at "last 10 audit rows"; the
#: SSE-live extension adds rows as they stream in, capped client-side
#: to keep the DOM bounded.
_RECENT_OPS_LIMIT: Final[int] = 10

#: Client-side DOM row cap for the recent-ops card. The seeded server
#: list is 10; SSE-live additions prepend until the cap is hit, then
#: older rows trim. Mirrors the broadcast feed's bounded-list discipline.
_RECENT_OPS_DOM_CAP: Final[int] = 50


@dataclass(frozen=True)
class _ConnectorResolution:
    """Outcome of resolving the target to a registered connector.

    Three shapes:

    * **resolved** -- ``connector_id`` carries the canonical
      ``"<impl_id>-<version>"`` id the operations matrix queries
      against. ``message`` is ``None``.
    * **no_connector** -- the resolver reported ``no_connector``;
      ``connector_id`` is ``None``, ``message`` is the diagnostic
      text. The ops matrix renders an explanation panel instead of
      group rows.
    * **ambiguous_connector** -- the resolver reported
      ``ambiguous_connector``; same shape as no_connector. The
      explanation includes the candidate set and the remediation
      step (set ``preferred_impl_id``).
    """

    connector_id: str | None
    label: str  # "resolved" | "no_connector" | "ambiguous_connector"
    message: str | None


def _build_connector_id(target: TargetORM) -> _ConnectorResolution:
    """Resolve the target to its dispatcher-chosen ``connector_id``.

    Routes through
    :func:`~meho_backplane.connectors.resolver.resolve_connector_or_label`
    -- the same helper :func:`~meho_backplane.api.v1.targets.probe_target`
    uses -- so the UI matrix and the REST probe surface agree on which
    connector the dispatcher would pick. The returned class's
    ``(product, version, impl_id)`` registry entry yields the
    canonical ``"<impl_id>-<version>"`` id the operations meta-tools
    query against.

    When the registry has the connector class under multiple keys
    (e.g. KubernetesConnector under both the v1 wildcard and a v2
    versioned entry), prefer the versioned entry -- that's the one the
    G0.6 dispatcher's lookup queries against. If the chosen class only
    appears under a wildcard entry (v1-only registration), fall back
    to the bare product as the connector_id; ``parse_connector_id``
    handles that shape symmetrically.
    """
    cls, label, message = resolve_connector_or_label(target)
    if cls is None or label is not None:
        return _ConnectorResolution(
            connector_id=None,
            label=label or "no_connector",
            message=message,
        )

    # Find the registry entry the class is registered under and build
    # the connector_id. Prefer the most-specific entry (versioned over
    # wildcard) so the operations-matrix query hits the actual
    # descriptor rows the dispatcher would dispatch to.
    versioned: tuple[str, str, str] | None = None
    wildcard: tuple[str, str, str] | None = None
    for key, registered_cls in all_connectors_v2().items():
        if registered_cls is not cls:
            continue
        product, version, impl_id = key
        if version != "" or impl_id != "":
            # Pick the lexicographically-first versioned key when the
            # class is registered under multiple. Stable so the surface
            # never flips choices between two equivalent registrations.
            if versioned is None or key < versioned:
                versioned = key
        else:
            wildcard = key

    if versioned is not None:
        _product, version, impl_id = versioned
        return _ConnectorResolution(
            connector_id=f"{impl_id}-{version}",
            label="resolved",
            message=None,
        )
    if wildcard is not None:
        product, _version, _impl_id = wildcard
        return _ConnectorResolution(connector_id=product, label="resolved", message=None)
    # No registry entry matched -- defensive; the resolver returned a
    # class that isn't in the registry. Treat as no_connector.
    return _ConnectorResolution(
        connector_id=None,
        label="no_connector",
        message="resolved connector class has no v2 registry entry",
    )


@dataclass(frozen=True)
class _OpsMatrixOp:
    """One operation row in the matrix (per-group leaf)."""

    op_id: str
    summary: str | None
    safety_level: str
    requires_approval: bool


@dataclass(frozen=True)
class _OpsMatrixGroup:
    """One operation_group's rendered shape in the matrix."""

    group_key: str
    name: str
    when_to_use: str
    operations: tuple[_OpsMatrixOp, ...]


async def _load_ops_matrix(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    product: str,
    version: str,
    impl_id: str,
) -> tuple[_OpsMatrixGroup, ...]:
    """Load the available-operations matrix for the resolved connector.

    The query mirrors the shape
    :func:`~meho_backplane.operations.meta_tools.list_operation_groups`
    uses for the agent surface:

    * Tenant scoping: tenant rows (``tenant_id == operator.tenant_id``)
      and built-in / global rows (``tenant_id IS NULL``) are both
      visible.
    * Only ``review_status='enabled'`` groups are included -- staged /
      disabled groups stay hidden from the operator the same way they
      stay hidden from the agent.
    * Within each group, only ``is_enabled=True`` descriptors are
      surfaced -- a descriptor whose ingestion review left it disabled
      should not appear in the operations matrix.

    Returns groups sorted by ``group_key``; operations within a group
    sorted by ``op_id``. Both orders are stable across page renders
    so an operator's "click the same row twice" never reshuffles
    rendering.
    """
    group_stmt = (
        select(OperationGroup)
        .where(
            OperationGroup.product == product,
            OperationGroup.version == version,
            OperationGroup.impl_id == impl_id,
            OperationGroup.review_status == "enabled",
            (OperationGroup.tenant_id.is_(None)) | (OperationGroup.tenant_id == tenant_id),
        )
        .order_by(OperationGroup.group_key)
    )
    groups = (await db_session.execute(group_stmt)).scalars().all()
    if not groups:
        return ()

    group_ids = [g.id for g in groups]
    descriptor_stmt = (
        select(EndpointDescriptor)
        .where(
            EndpointDescriptor.group_id.in_(group_ids),
            EndpointDescriptor.is_enabled.is_(True),
            (EndpointDescriptor.tenant_id.is_(None)) | (EndpointDescriptor.tenant_id == tenant_id),
        )
        .order_by(EndpointDescriptor.group_id, EndpointDescriptor.op_id)
    )
    descriptors = (await db_session.execute(descriptor_stmt)).scalars().all()

    by_group: dict[uuid.UUID, list[_OpsMatrixOp]] = {gid: [] for gid in group_ids}
    for descriptor in descriptors:
        if descriptor.group_id is None:
            continue
        by_group.setdefault(descriptor.group_id, []).append(
            _OpsMatrixOp(
                op_id=descriptor.op_id,
                summary=descriptor.summary,
                safety_level=descriptor.safety_level,
                requires_approval=descriptor.requires_approval,
            ),
        )

    return tuple(
        _OpsMatrixGroup(
            group_key=g.group_key,
            name=g.name,
            when_to_use=g.when_to_use,
            operations=tuple(by_group.get(g.id, [])),
        )
        for g in groups
    )


async def _count_graph_node_refs(
    db_session: AsyncSession,
    *,
    target_id: uuid.UUID,
) -> int:
    """Return the number of ``graph_node`` rows referencing this target.

    Mirrors the cascade count :func:`~meho_backplane.api.v1.targets.delete_target`
    runs before deciding 409-vs-204; surfaced on the detail page so the
    Delete confirm modal can show the operator the cascade impact before
    they click through (the REST handler is still the authority -- the
    modal-side count is a UX hint, the 409+``?force=true`` flow remains
    the contract). Counting ``graph_node`` only matches the REST handler:
    the ``audit_log`` table accumulates rows per request and would block
    every delete forever if counted.
    """
    stmt = select(func.count()).select_from(GraphNode).where(GraphNode.target_id == target_id)
    return int((await db_session.execute(stmt)).scalar_one())


def _classify_no_connector_cause(target: TargetORM) -> str:
    """Distinguish "no fingerprint cached" from "product slug unmatched".

    The resolver returns ``no_connector`` for two visually-identical but
    operationally-distinct cases:

    * **``missing_fingerprint``** -- ``target.fingerprint`` is ``None``
      *and* the target's ``product`` slug **is** in the registered set
      *and* no wildcard (`version == ""`) registration covers it. The
      operator's correct verb is **Re-probe** (capture the fingerprint
      so the resolver's versioned-match ladder can pick a connector).
    * **``product_mismatch``** -- ``target.product`` is not in the
      registered-product set (the slug doesn't match any connector --
      e.g. ``"kubernetes"`` when the K8s connector advertises ``"k8s"``).
      Re-probing here re-dispatches through the same resolver with the
      same ``(product, version)`` tuple and fails the same way; the
      correct verbs are **Edit** (PATCH product to a valid value) or
      **Delete**. This is the v0.7.0 dogfood signal #6 closure
      (``claude-rdc-hetzner-dc#753``).

    Returns one of the two literals above so the template can pick the
    right message + remediation hint. Returns ``"product_mismatch"`` as
    a safe default for the genuinely-pathological case (defensive --
    the resolver returned no_connector but neither branch fits).
    """
    valid_products = registered_product_tokens()
    if target.product in valid_products and target.fingerprint is None:
        return "missing_fingerprint"
    return "product_mismatch"


async def _load_recent_ops(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    target_id: uuid.UUID,
    limit: int = _RECENT_OPS_LIMIT,
) -> list[AuditLog]:
    """Return the most recent ``audit_log`` rows on this target.

    Filters on ``(tenant_id, target_id)`` so a soft-FK violation
    (audit row carrying another tenant's target_id) could never leak
    into this surface. Ordered ``occurred_at DESC, id DESC`` for a
    deterministic page-load order; ties on ``occurred_at`` are
    extremely rare (microsecond resolution on PG timestamps) but the
    explicit tie-break on ``id`` keeps two-rows-in-the-same-tick
    rendering stable across reloads.
    """
    stmt = (
        select(AuditLog)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.target_id == target_id,
        )
        .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
        .limit(limit)
    )
    result = await db_session.execute(stmt)
    return list(result.scalars().all())


def _project_target(target: TargetORM) -> dict[str, Any]:
    """Project the ORM row into a template-friendly dict.

    Centralised so the detail page render path and the re-probe HTMX
    response (which also re-renders the fingerprint card) carry the
    same shape; a template helper accidentally widening the shape on
    one path can't break the other.
    """
    return {
        "id": str(target.id),
        "name": target.name,
        "aliases": list(target.aliases),
        "product": target.product,
        "host": target.host,
        "port": target.port,
        "fqdn": target.fqdn,
        "secret_ref": target.secret_ref,
        "auth_model": target.auth_model,
        "vpn_required": target.vpn_required,
        "extras": dict(target.extras),
        "notes": target.notes,
        "fingerprint": target.fingerprint,
        "preferred_impl_id": target.preferred_impl_id,
        "created_at": target.created_at,
        "updated_at": target.updated_at,
    }


def _project_audit_rows(rows: Iterable[AuditLog]) -> list[dict[str, Any]]:
    """Project audit rows into the template-friendly shape.

    Keeps the recent-ops template free of ORM imports + tolerant of
    the ``payload`` JSON's flexible shape; the template renders the
    method + path + status_code + occurred_at, plus ``op_id`` /
    ``op_class`` from the payload bag when present (these are the
    same fields the broadcast publisher reads in
    :func:`~meho_backplane.audit._resolve_op_id_and_class_override`).

    ``occurred_at`` is serialised to an ISO-8601 string here (not left
    as a :class:`datetime`) because the template emits the rows
    through Jinja ``| tojson`` into the Alpine ``x-data`` seed array:
    ``tojson`` rejects datetimes by default (``TypeError: Object of
    type datetime is not JSON serializable``). Pre-stringifying at
    the projection boundary keeps the template free of datetime
    handling and matches the SSE bridge's wire format (events on
    ``/ui/broadcast/stream`` already carry ISO strings).
    """
    projected: list[dict[str, Any]] = []
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        op_id = payload.get("op_id")
        op_class = payload.get("op_class")
        projected.append(
            {
                "audit_id": str(row.id),
                "occurred_at": row.occurred_at.isoformat(),
                "method": row.method,
                "path": row.path,
                "status_code": row.status_code,
                "op_id": op_id if isinstance(op_id, str) else None,
                "op_class": op_class if isinstance(op_class, str) else None,
            }
        )
    return projected


#: Module-level :class:`fastapi.Depends` closures -- ruff B008 idiom.
_require_ui_session_dep = Depends(require_ui_session)
_get_raw_session_dep = Depends(get_raw_session)
_role_probe_dep = Depends(resolve_role_probe)


def _resolve_target_or_404(
    *,
    target_name: str,
) -> None:
    """Validate the path parameter before the substrate hits the DB.

    Names longer than 200 characters cannot exist in the ``targets``
    table (the schema caps ``name`` at 200), so a longer path segment
    is a guaranteed 404. Reject explicitly with the same 404 the
    resolver would raise after the round-trip; this saves the DB
    query in the spam-vector case (a fuzzer hammering long names).
    """
    if not target_name or len(target_name) > 200:
        raise HTTPException(status_code=404, detail=f"target {target_name!r} not found")


async def _render_detail(
    request: Request,
    *,
    target_name: str,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
    role_probe: OperatorRoleProbe,
) -> HTMLResponse:
    """Render the detail page for *target_name* in the operator's tenant."""
    _resolve_target_or_404(target_name=target_name)
    target = await resolve_target(db_session, session_ctx.tenant_id, target_name)
    resolution = _build_connector_id(target)
    ops_matrix: tuple[_OpsMatrixGroup, ...] = ()
    if resolution.connector_id is not None:
        from meho_backplane.operations._lookup import parse_connector_id

        product, version, impl_id = parse_connector_id(resolution.connector_id)
        ops_matrix = await _load_ops_matrix(
            db_session,
            tenant_id=session_ctx.tenant_id,
            product=product,
            version=version,
            impl_id=impl_id,
        )
    recent_ops = await _load_recent_ops(
        db_session,
        tenant_id=session_ctx.tenant_id,
        target_id=target.id,
    )

    # The ``no_connector`` resolver verdict conflates two operationally
    # distinct cases (G0.15-T10 #1218): "fingerprint not cached yet"
    # (Re-probe is the right verb) vs "product slug doesn't match any
    # registered connector" (Edit / Delete is the right verb). Classify
    # here so the template can render the right remediation hint.
    no_connector_cause: str | None = None
    valid_products: tuple[str, ...] = ()
    if resolution.label == "no_connector":
        no_connector_cause = _classify_no_connector_cause(target)
        valid_products = tuple(sorted(registered_product_tokens()))

    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context = {
        "page_title": f"Targets · {target.name}",
        "active_surface": "connectors",
        "target": _project_target(target),
        "connector_id": resolution.connector_id,
        "connector_label": resolution.label,
        "connector_message": resolution.message,
        "no_connector_cause": no_connector_cause,
        "valid_products": valid_products,
        "ops_matrix": ops_matrix,
        "recent_ops": _project_audit_rows(recent_ops),
        "recent_ops_dom_cap": _RECENT_OPS_DOM_CAP,
        # SSE bridge URL -- the existing G10.1 stream supports a
        # ``target=<name>`` filter so we ride it straight rather than
        # duplicating the bridge. URL-encoded so a name carrying
        # ``&`` or ``?`` does not split the query string.
        "stream_endpoint": _build_stream_endpoint(target.name),
        # tenant_admin gate for the re-probe button. The handler in
        # :mod:`probe` re-checks the role server-side; the template
        # hides the button when the operator can't use it so the
        # affordance only surfaces to operators with the privilege.
        "is_tenant_admin": role_probe.is_tenant_admin,
        "csrf_token": csrf_token,
        # The footer in ``base.html`` reads ``ready`` to colour the
        # readiness pill; the connectors surface doesn't poll readiness
        # itself, so ship ``False`` here so Jinja's ``StrictUndefined``
        # env does not raise on the read.
        "ready": False,
    }
    response = get_templates().TemplateResponse(request, "connectors/detail.html", context)
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )
    return response


def _build_stream_endpoint(target_name: str) -> str:
    """Build the URL-encoded SSE bridge endpoint for *target_name*.

    The G10.1 broadcast SSE bridge (``/ui/broadcast/stream``) takes a
    ``target=<name>`` query parameter and filters the stream to events
    whose ``BroadcastEvent.target_name`` matches. We piggy-back on it
    (instead of standing up a parallel connectors-only stream) so the
    SSE plumbing -- session gate, cursor resolver, heartbeat, replay
    -- stays single-sourced. The target name is percent-encoded so a
    name containing ``&`` / ``?`` / ``#`` does not split the query
    string.
    """
    from urllib.parse import quote

    return f"/ui/broadcast/stream?target={quote(target_name, safe='')}"


def build_detail_router() -> APIRouter:
    """Construct the targets-detail :class:`APIRouter`.

    Registers the single ``GET /ui/connectors/{name}`` route. The
    route name (``ui_connectors_detail``) is referenced by the list
    template's row "View" links and by the probe handler's
    HX-Redirect on the fingerprint-card refresh -- a rename here must
    update both in lockstep.
    """
    router = APIRouter(tags=["ui-connectors"])

    async def _handler(
        request: Request,
        name: str,
        session_ctx: UISessionContext = _require_ui_session_dep,
        db_session: AsyncSession = _get_raw_session_dep,
        role_probe: OperatorRoleProbe = _role_probe_dep,
    ) -> HTMLResponse:
        """``GET /ui/connectors/{name}``."""
        return await _render_detail(
            request,
            target_name=name,
            session_ctx=session_ctx,
            db_session=db_session,
            role_probe=role_probe,
        )

    router.add_api_route(
        "/ui/connectors/{name}",
        _handler,
        methods=["GET"],
        name="ui_connectors_detail",
        response_class=HTMLResponse,
        responses={
            404: {
                "description": (
                    "Target name does not resolve in the caller's tenant. "
                    "Returns the 404 from the shared "
                    ":func:`~meho_backplane.targets.resolver.resolve_target` "
                    "helper -- alias-aware, with near-miss suggestions when "
                    "the substring search lands hits."
                ),
                "content": {"text/html": {}},
            },
            409: {
                "description": (
                    "Target name resolves to multiple targets in the caller's "
                    "tenant (ambiguous alias match). Returns the 409 from "
                    ":func:`resolve_target`."
                ),
                "content": {"text/html": {}},
            },
        },
    )
    return router
