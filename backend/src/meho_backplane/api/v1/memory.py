# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/memory*`` -- REST surface for the memory layer.

G5.1-T2 (#422) of Initiative #332. Four routes mounted under
``/api/v1/memory`` that expose :class:`MemoryService` (T1 #421) to
operators and agents. The MCP meta-tools (T3 #423) and CLI verbs
(T4 #424) call into the same service from their own transports; this
module is the HTTP front of the memory backplane.

Route inventory
---------------

* ``POST /api/v1/memory`` -- create one memory (``remember``). Body:
  :class:`RememberBody`. Returns :class:`MemoryEntry` with HTTP 201.
  Role: ``operator``; service-layer
  :class:`~meho_backplane.memory.rbac.MemoryRbacResolver` enforces the
  per-scope matrix (e.g. only ``tenant_admin`` may write ``tenant``).
* ``GET /api/v1/memory`` -- list memories visible to the operator
  (``list``). Query params: ``scope`` / ``q`` (the canonical free-text
  slug filter; ``slug_pattern`` is the deprecated alias, #1854) /
  ``tag`` / ``include_expired`` / ``limit``. Returns
  :class:`MemoryListResponse`. Role: ``operator``.
* ``GET /api/v1/memory/{scope}/{slug}`` -- fetch one memory by natural
  key (``recall``). Optional ``target_name`` query param required for
  ``user-target`` / ``target`` scopes. Returns :class:`MemoryEntry`.
  Role: ``operator``.
* ``DELETE /api/v1/memory/{scope}/{slug}`` -- delete one memory
  (``forget``). Optional ``target_name`` query param required for
  ``user-target`` / ``target`` scopes. Returns 204 on success **and**
  when the row was already absent (idempotent, matching
  ``/api/v1/kb``'s contract). Role: ``operator``.

Info-leak avoidance on recall
-----------------------------

The acceptance criterion "GET ``/api/v1/memory/user/their-pref``
returns 404" is load-bearing: an operator must not be able to
distinguish "no such memory" from "you don't have access" by the
response status. The service's
:meth:`~meho_backplane.memory.service.MemoryService.recall` already
collapses both into a ``None`` return; the route translates that to
404 across the board. The route does **not** raise 403 on read
denial -- the info-leak avoidance is the whole point of this surface
shape.

Write paths still surface :class:`PermissionDeniedError` as 403
because the operator *has* identified themselves; the matrix
mismatch is honest feedback (the alternative would be silently
dropping operator-initiated writes, which produces worse audit
behaviour than a loud 403).

Tenant scoping
--------------

Every route derives ``tenant_id`` from the JWT-validated
:class:`Operator`. There is no surface that accepts a tenant id from
the body or query string -- cross-tenant probes are impossible by
construction. A cross-tenant ``recall`` surfaces as 404 (same shape
the kb surface uses).

Audit + broadcast contract
--------------------------

Every route binds two contextvars **before** the service call so the
chassis :class:`AuditMiddleware` (G2.3-T1) and the publish-on-write
broadcast hook (G6.1-T3) classify the row correctly:

* ``audit_op_id`` -- one of ``memory.remember`` / ``memory.list`` /
  ``memory.recall`` / ``memory.forget`` (canonical operation
  identifiers tracked under :data:`_MEMORY_OP_IDS`).
* ``audit_op_class`` -- ``"read"`` for ``memory.list`` /
  ``memory.recall``, ``"write"`` for ``memory.remember`` /
  ``memory.forget``. Bound explicitly because
  :func:`~meho_backplane.broadcast.events.classify_op` would only
  match ``memory.list`` against its read-suffix table --
  ``memory.recall`` (no ``.list`` / ``.get`` / ``.info`` suffix),
  ``memory.remember`` (no ``.create`` suffix), and ``memory.forget``
  (no ``.delete`` suffix) would fall through to the ``other`` bucket
  and broadcast under the wrong sensitivity class.

The ``remember`` and ``forget`` routes additionally bind
``audit_scope`` / ``audit_slug`` so the audit payload (and the
broadcast ``params``) carries the natural-key coordinates. The
memory body itself is **never** bound -- the audit row is for the
operation, not the document content (which lives in the
``documents`` table and is recoverable via ``memory.recall``).

Schema constraints
------------------

:class:`RememberBody` is a pydantic v2 frozen model with
``extra="forbid"`` so unexpected request fields trip 422 at the
framework layer rather than silently being dropped. ``slug`` is
constrained to :data:`SLUG_PATTERN` via the schema and the
service-layer
:func:`~meho_backplane.memory.schemas.validate_slug`; the substrate
is the single source of truth for the safe-URL alphabet so a typo
('BAD!') surfaces with the same error across REST / MCP / CLI.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from meho_backplane.api.v1._freetext_filter import (
    free_text_q_query,
    resolve_free_text_filter,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.memory import (
    InvalidPromotionStepError,
    MemoryEntry,
    MemoryScope,
    MemoryService,
    PermissionDeniedError,
)
from meho_backplane.memory.schemas import SLUG_PATTERN
from meho_backplane.memory.ttl import resolve_default_expires_at

__all__ = ["MemoryListResponse", "PromoteBody", "RememberBody", "router"]

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])

#: Module-level Depends closures -- required to satisfy ruff B008
#: (calls in default argument positions are disallowed).
#:
#: Reads are open to ``read_only`` operators (and above) because the
#: per-scope :class:`~meho_backplane.memory.rbac.MemoryRbacResolver`
#: matrix explicitly allows ``read_only`` to read ``tenant`` and
#: ``target`` scopes (the "team becomes the unit of memory" property
#: from consumer-needs.md §G5 L131). User-flavoured scopes still
#: filter to ``operator.sub == stored.user_sub`` at the service layer,
#: so a read_only operator never sees another operator's user-scoped
#: memory.
#:
#: Writes require ``operator`` minimum at the FastAPI layer; the
#: matrix further restricts ``tenant`` scope to ``tenant_admin``,
#: surfaced as :class:`PermissionDeniedError` → 403 from the service.
_require_read = Depends(require_role(TenantRole.READ_ONLY))
_require_operator = Depends(require_role(TenantRole.OPERATOR))

#: Canonical operation identifiers bound into ``audit_op_id`` per
#: route. Pinned as module constants so the contract is greppable
#: from tests + G8 dashboards and a typo in a route handler surfaces
#: at first call rather than as a silent broadcast under the wrong
#: op_id. Identifiers match the verbs consumer-needs.md §G5 names
#: (``remember`` / ``recall`` / ``list`` / ``forget``).
_MEMORY_OP_IDS: Final[dict[str, str]] = {
    "remember": "memory.remember",
    "list": "memory.list",
    "recall": "memory.recall",
    "forget": "memory.forget",
    "promote": "memory.promote",
}

#: Maximum length of the ``slug`` path / query parameter accepted by
#: ``/{scope}/{slug}`` routes. Defence-in-depth on top of
#: :data:`SLUG_PATTERN` -- a pathological slug substring in the URL
#: (10 KB of dots, hyphens) would never match the substrate but
#: would still cost a regex pass; capping at the path-parameter
#: parse stage bounds the cost. 256 mirrors :mod:`meho_backplane.api.v1.kb`.
_SLUG_MAX_LENGTH: Final[int] = 256

#: Cross-surface field hints for :class:`RememberBody`. An operator who
#: copies an ``add_to_memory`` (MCP) body into a REST request sends the
#: MCP-surface field names (``ttl`` -- an ISO-8601 *duration*; a
#: top-level ``tags`` array) that REST does not accept. ``extra="forbid"``
#: would reject them with a generic ``extra_forbidden`` that names no
#: replacement. The :meth:`RememberBody._reject_cross_surface_fields`
#: before-validator special-cases exactly these keys so the 422 names the
#: correct REST field. The MCP<->REST split is intentional and documented
#: (``mcp/tools/memory.py`` TTL-semantics docstring); this is
#: error-legibility only, not a behaviour change to accepted shapes.
_CROSS_SURFACE_FIELD_HINTS: Final[dict[str, str]] = {
    "ttl": (
        "`ttl` is the MCP-surface field; on REST use `expires_at` "
        "(ISO-8601 datetime, e.g. `2026-06-30T12:00:00+00:00`), or omit "
        "for the scope default / `null` to persist."
    ),
    "tags": ("top-level `tags` is the MCP-surface shape; on REST use `metadata.tags`."),
}

#: Maximum number of memories returned by ``GET /api/v1/memory``.
#: The service's ``list_memories`` already over-pulls to compensate
#: for in-process filtering (``candidate_pull = max(limit * 4,
#: 200)``); capping at 500 at the API surface keeps a single page
#: from materialising more than ~2000 rows server-side.
_LIST_LIMIT_MAX: Final[int] = 500


def _resolve_default_ttl(body: RememberBody) -> datetime | None:
    """Return the effective ``expires_at`` for a ``remember`` write.

    Thin REST-side adapter over
    :func:`meho_backplane.memory.ttl.resolve_default_expires_at`. The
    shared resolver carries the G5.2-T2 (#624) policy; this wrapper's
    only job is to translate Pydantic's ``model_fields_set`` view of
    "field absent from JSON" vs "field present with value null" into
    the primitives shape the resolver expects.

    G0.9.1-T3 (#775) introduced the shared resolver so the MCP
    ``add_to_memory`` tool applies the same default-TTL discrimination
    -- previously the MCP path always passed ``expires_at`` explicitly
    to :meth:`MemoryService.remember` and the default never fired on
    that surface, producing a silent divergence between REST and MCP.

    The "field absent" vs "explicit null" discrimination uses
    :attr:`BaseModel.model_fields_set` (pydantic v2): the set carries
    every field name the constructor saw in the input, regardless of
    its value. Without this discrimination, an explicit
    ``"expires_at": null`` would round-trip to ``body.expires_at is
    None`` and be indistinguishable from the field being missing --
    which would collapse the ``--persist`` opt-out into the default
    path. Verified against the installed pydantic 2.13.4 (`uv run
    python -c "from pydantic import BaseModel; ..."` -- explicit
    ``b=None`` is in ``model_fields_set``, absent ``b`` is not).
    """
    return resolve_default_expires_at(
        body.scope,
        expires_at_was_set="expires_at" in body.model_fields_set,
        explicit_expires_at=body.expires_at,
    )


class RememberBody(BaseModel):
    """POST body for ``/api/v1/memory`` -- create one memory.

    ``slug`` is constrained to :data:`SLUG_PATTERN` at the model
    boundary; the service-layer
    :func:`~meho_backplane.memory.schemas.validate_slug` is the
    parallel gate for non-Pydantic callers. ``frozen=True`` matches
    the rest of the memory + kb schemas (a handler accidentally
    mutating a parsed request body surfaces as a pydantic error
    instead of a confused-deputy bug). ``extra="forbid"`` rejects
    unknown fields at 422 so a typo (``"bodytext"``) doesn't
    silently become a no-op. A ``mode="before"`` validator runs *ahead*
    of that rejection to turn the two known cross-surface field names
    (``ttl`` / top-level ``tags``, the MCP ``add_to_memory`` shape) into
    a targeted 422 that names the correct REST field rather than the
    opaque ``extra_forbidden`` (see :data:`_CROSS_SURFACE_FIELD_HINTS`).

    ``target_name`` is required when ``scope`` is ``user-target`` or
    ``target``; the service-level
    :meth:`MemoryService._require_target_name` raises ``ValueError``
    when missing, which this route maps to 422 (parity with
    pydantic's own missing-field shape).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _reject_cross_surface_fields(cls, data: Any) -> Any:
        """Name the correct REST field for known MCP-surface keys.

        Runs before field/extra validation (``mode="before"``) so a
        request carrying ``ttl`` or a top-level ``tags`` array raises a
        targeted ``ValueError`` -- which FastAPI renders as a 422 whose
        detail names ``expires_at`` / ``metadata.tags`` -- instead of the
        generic ``extra_forbidden`` that ``extra="forbid"`` would emit.
        Only the two known cross-surface keys are special-cased; any
        other unknown field falls through to ``extra_forbidden``. No
        accepted shape, default, or semantic change -- error-legibility
        only.
        """
        if isinstance(data, dict):
            for field_name, hint in _CROSS_SURFACE_FIELD_HINTS.items():
                if field_name in data:
                    raise ValueError(hint)
        return data

    scope: MemoryScope
    body: str = Field(min_length=1)
    slug: str | None = Field(
        default=None,
        max_length=_SLUG_MAX_LENGTH,
        pattern=SLUG_PATTERN,
    )
    metadata: dict[str, Any] | None = None
    # Pydantic v2 parses ISO-8601 strings into ``datetime`` directly
    # (and emits them back as ISO strings in the response body
    # encoder), so the wire shape stays "string" while the handler
    # sees the typed value the service expects. Malformed ISO
    # strings surface as 422 from pydantic's own coercion -- no
    # custom parse layer needed.
    expires_at: datetime | None = None
    target_name: str | None = Field(default=None, max_length=_SLUG_MAX_LENGTH)


class PromoteBody(BaseModel):
    """POST body for ``/api/v1/memory/{scope}/{slug}/promote``.

    G5.2-T4 (#626) of Initiative #374. Two required-ish fields plus a
    target-scope name for the ``user -> user-target`` / ``user-target ->
    target`` ladder steps; ``frozen=True`` + ``extra="forbid"`` mirror
    :class:`RememberBody`.

    * ``to`` -- the target scope. The route delegates ladder validation
      to :func:`~meho_backplane.memory.rbac.assert_can_promote`, which
      raises :class:`InvalidPromotionStepError` on non-ladder pairs (the
      route maps that to 400).
    * ``move`` -- when ``True``, delete the source row in the same
      transaction as the target insert (broadens-and-leaves vs.
      broadens-and-rewires). Default ``False`` per the issue body.
    * ``target_name`` -- required when ``to`` is target-flavoured AND
      the source scope is not (``user -> user-target``). For
      ``user-target -> target`` the service inherits ``target_name``
      from the source row, so the caller may omit it; we still accept
      it for forward-compat with the CLI verb (T5 #627). Omitted
      values land as ``None``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    to: MemoryScope
    move: bool = False
    target_name: str | None = Field(default=None, max_length=_SLUG_MAX_LENGTH)


class MemoryListResponse(BaseModel):
    """Response envelope for ``GET /api/v1/memory``.

    Wrapped in ``{"entries": [...]}`` so a future cursor field can
    land non-breakingly -- same shape :mod:`meho_backplane.api.v1.kb`
    adopted for its list response. Per-entry shape is the full
    :class:`MemoryEntry` (no preview truncation); memory bodies are
    expected to be short hand-written notes rather than the multi-KB
    blobs the kb surface carries, so streaming the full body is the
    right default. A future preview / pagination contract lives in
    G5.2 #374's promote / expire verbs.
    """

    model_config = ConfigDict(frozen=True)

    entries: list[MemoryEntry]


@router.post(
    "",
    response_model=MemoryEntry,
    status_code=http_status.HTTP_201_CREATED,
)
async def remember(
    body: RememberBody,
    operator: Operator = _require_operator,
) -> MemoryEntry:
    """Create one memory under the operator's tenant.

    Service-layer
    :class:`~meho_backplane.memory.rbac.MemoryRbacResolver` enforces
    the per-scope role matrix:

    * ``user`` / ``user-tenant`` / ``user-target`` -- any
      authenticated operator (``read_only`` excluded).
    * ``tenant`` -- ``tenant_admin`` only.
    * ``target`` -- any operator in the tenant.

    A matrix mismatch raises
    :class:`~meho_backplane.memory.rbac.PermissionDeniedError` which
    this route maps to 403. ``target_name`` missing for a target-
    scoped write raises :class:`ValueError` from the service, mapped
    to 422 (same status pydantic returns for shape failures so
    callers can branch on 4xx uniformly).

    Binds ``audit_op_id="memory.remember"`` + ``audit_op_class=
    "write"`` + ``audit_scope=<scope>`` before the service call so a
    handler exception still produces an audit row classified under
    the canonical op id. ``audit_slug`` is rebound *after* the
    service call because the slug is auto-generated when the body
    omits it -- binding before would leave ``audit_slug=None`` in
    that path; the post-call rebind carries the actually-persisted
    slug.

    Default-TTL injection (G5.2-T2 #624)
    ------------------------------------

    When ``body.scope`` is :attr:`MemoryScope.USER` *and* the request
    omitted ``expires_at`` entirely, the handler injects
    ``expires_at = now(UTC) + Settings.memory_user_default_ttl_days``
    so session-scoped hints expire by default (consumer-needs.md §G5:
    "session-scoped hints expire after 7 days unless re-pinned").
    The G5.2-T1 expiry sweeper (#623) reaps these rows once
    ``expires_at`` passes.

    Two opt-outs are deliberately distinguished from "field omitted":

    * Explicit ``"expires_at": null`` in the JSON body → no default;
      the row persists forever. The CLI's ``--persist`` flag emits
      this shape.
    * Explicit ``"expires_at": "<ISO-8601>"`` → that value is honoured
      verbatim (no override).

    The discrimination uses :attr:`BaseModel.model_fields_set`
    (pydantic v2) rather than ``body.expires_at is None``: the former
    distinguishes "field absent from JSON" from "field present with
    value null", which is the load-bearing semantics for the CLI's
    ``--persist`` opt-out. The default is applied *only* to the
    user-only :attr:`MemoryScope.USER` kind (the one consumer-needs
    targets); ``user-tenant`` / ``user-target`` are not in scope per
    the issue ("``kind == 'memory-user'``").
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_MEMORY_OP_IDS["remember"],
        audit_op_class="write",
        audit_scope=body.scope.value,
    )
    # Resolve the effective ``expires_at`` *before* calling the service
    # so the audit row, the broadcast payload, and the persisted
    # ``doc_metadata.expires_at`` all see the same value. ``service``
    # itself is the storage substrate; the default-TTL contract is a
    # surface-layer policy (G5.2-T2 #624) bolted onto the
    # ``/api/v1/memory`` route, not :class:`MemoryService` proper --
    # MCP / direct callers stay unaffected.
    expires_at = _resolve_default_ttl(body)
    service = MemoryService()
    try:
        entry = await service.remember(
            operator=operator,
            scope=body.scope,
            body=body.body,
            slug=body.slug,
            metadata=body.metadata,
            expires_at=expires_at,
            target_name=body.target_name,
        )
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail=f"permission_denied: {exc.reason}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    structlog.contextvars.bind_contextvars(audit_slug=entry.slug)
    return entry


@router.get("", response_model=MemoryListResponse)
async def list_memories(
    scope: MemoryScope | None = Query(default=None),
    q: str | None = free_text_q_query(max_length=_SLUG_MAX_LENGTH),
    slug_pattern: str | None = Query(
        default=None,
        max_length=_SLUG_MAX_LENGTH,
        deprecated=True,
        description="Deprecated alias for `q`; still honoured.",
    ),
    tag: str | None = Query(default=None, max_length=_SLUG_MAX_LENGTH),
    include_expired: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=_LIST_LIMIT_MAX),
    operator: Operator = _require_read,
) -> MemoryListResponse:
    """List memories visible to the operator in this tenant.

    Tenant-scoped via ``operator.tenant_id`` -- no surface accepts a
    tenant id from the query string. ``read_only`` operators get 403
    via :func:`require_role` before reaching this handler.

    ``q`` is the canonical free-text filter param across the kb /
    memory / operations-search list surfaces (#1854); ``slug_pattern``
    is its deprecated alias, kept working for back-compat. The resolved
    value plus ``scope`` / ``tag`` are forwarded verbatim to
    :meth:`MemoryService.list_memories`; the service is the contract
    owner for filter semantics (slug ``in`` substring match, tag
    membership in metadata, etc.). Supplying both ``q`` and
    ``slug_pattern`` with different values is a 422 rather than a
    silent pick. ``include_expired`` defaults to ``False`` so expired
    entries are filtered out on the read side until G5.2 #374's daily
    cleanup task physically removes them.

    Binds ``audit_op_id="memory.list"`` + ``audit_op_class="read"``
    before the service call so a handler exception still produces
    an audit row classified under the canonical op id.
    """
    resolved_slug_pattern = resolve_free_text_filter(
        q=q,
        legacy_value=slug_pattern,
        legacy_name="slug_pattern",
    )
    structlog.contextvars.bind_contextvars(
        audit_op_id=_MEMORY_OP_IDS["list"],
        audit_op_class="read",
    )
    service = MemoryService()
    entries = await service.list_memories(
        operator=operator,
        scope=scope,
        slug_pattern=resolved_slug_pattern,
        tag=tag,
        include_expired=include_expired,
        limit=limit,
    )
    return MemoryListResponse(entries=entries)


@router.get("/{scope}/{slug}", response_model=MemoryEntry)
async def recall(
    scope: MemoryScope,
    slug: str,
    target_name: str | None = Query(default=None, max_length=_SLUG_MAX_LENGTH),
    operator: Operator = _require_read,
) -> MemoryEntry:
    """Fetch one memory by natural key. 404 on not-found OR RBAC deny.

    The 404-vs-403 collapse on read is the load-bearing info-leak
    avoidance from the issue AC: a caller must not be able to
    distinguish "no such memory" from "you don't have access" by the
    response shape. The service's
    :meth:`~meho_backplane.memory.service.MemoryService.recall` returns
    ``None`` for both cases; the route translates that to 404 across
    the board.

    ``target_name`` is a query param (not a path segment) because
    only ``user-target`` and ``target`` scopes need it; pushing it
    into the path would force every recall URL to carry an empty
    placeholder. A target-scoped recall without ``target_name``
    short-circuits to ``None`` at the service layer (same return
    value as not-found), maintaining the info-leak avoidance.

    Slug length is bounded at the path-parameter parse stage so a
    pathological slug substring in the URL can't pessimize the
    substrate's regex pass.

    Binds ``audit_op_id="memory.recall"`` + ``audit_op_class="read"``
    + ``audit_scope`` + ``audit_slug`` before the service call.
    """
    if len(slug) > _SLUG_MAX_LENGTH:
        # Path parameter overrun -- defence-in-depth before the
        # service's regex pass. Surface as 404 to preserve the
        # info-leak avoidance (a 422 here would let a probing
        # caller distinguish "slug shape rejected at the edge" from
        # "slug not found").
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="memory_not_found",
        )
    structlog.contextvars.bind_contextvars(
        audit_op_id=_MEMORY_OP_IDS["recall"],
        audit_op_class="read",
        audit_scope=scope.value,
        audit_slug=slug,
    )
    service = MemoryService()
    entry = await service.recall(
        operator=operator,
        scope=scope,
        slug=slug,
        target_name=target_name,
    )
    if entry is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="memory_not_found",
        )
    return entry


@router.delete(
    "/{scope}/{slug}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def forget(
    scope: MemoryScope,
    slug: str,
    target_name: str | None = Query(default=None, max_length=_SLUG_MAX_LENGTH),
    operator: Operator = _require_operator,
) -> Response:
    """Delete one memory by natural key. Idempotent (204 even when absent).

    Mirrors :mod:`meho_backplane.api.v1.kb`'s idempotent-delete
    contract: returns 204 whether the row existed or not. A non-
    idempotent surface that returned 404 on missing-on-delete would
    let an operator probe for the presence of a slug they cannot
    read; the idempotent shape preserves the tenant boundary's
    confidentiality properties (and the info-leak avoidance the
    recall route holds).

    Service-layer RBAC raises
    :class:`~meho_backplane.memory.rbac.PermissionDeniedError` on a
    matrix mismatch (e.g. ``operator`` role trying to forget a
    ``tenant``-scoped memory). The route maps that to 403; the
    operator *has* identified themselves and the matrix mismatch is
    honest feedback (the alternative would be silent no-op, which
    audits worse than a loud 403). ``target_name`` missing for a
    target-scoped forget raises :class:`ValueError`, mapped to 422.

    Binds ``audit_op_id="memory.forget"`` + ``audit_op_class=
    "write"`` + ``audit_scope`` + ``audit_slug`` **before** the
    service call so an exception still produces a correctly-
    classified audit row. ``audit_existed`` is bound *after* the
    service returns -- on the exception path it stays unbound,
    which is the truthful signal (we don't know whether the row
    existed because the call failed).

    Slug length is bounded at the path-parameter parse stage
    (mirrors the recall route) so a pathological slug substring in
    the URL can't be bound onto audit/broadcast contextvars before
    the service's regex pass would have rejected it. 404 (not 422)
    preserves the idempotent-delete contract: a probing caller
    can't distinguish "slug shape rejected at the edge" from
    "slug not found".
    """
    if len(slug) > _SLUG_MAX_LENGTH:
        # Path parameter overrun -- defence-in-depth before the
        # service's regex pass, and before bind_contextvars writes
        # the oversized slug into the audit/broadcast payload.
        # Surface as 404 to match the idempotent-missing contract
        # the rest of this route documents (a 422 here would let a
        # probing caller distinguish edge-reject from not-found).
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="memory_not_found",
        )
    structlog.contextvars.bind_contextvars(
        audit_op_id=_MEMORY_OP_IDS["forget"],
        audit_op_class="write",
        audit_scope=scope.value,
        audit_slug=slug,
    )
    service = MemoryService()
    try:
        existed = await service.forget(
            operator=operator,
            scope=scope,
            slug=slug,
            target_name=target_name,
        )
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail=f"permission_denied: {exc.reason}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    structlog.contextvars.bind_contextvars(audit_existed=existed)
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


def _promote_value_error_to_http(exc: ValueError) -> HTTPException:
    """Map a service-raised :class:`ValueError` to the right HTTPException.

    Two distinct failure modes share the :class:`ValueError` type at
    the service boundary:

    * ``promote_target_conflict`` -- target slug occupied by a row
      with different provenance (409 conflict; not a request-shape
      error).
    * Other ValueErrors (e.g. ``target_name required for
      user-target``) -- request-shape errors mapped to 422 in line
      with the rest of the memory router.

    The dispatch on prefix keeps the failure surface readable at one
    function rather than splattered across the route handler.
    """
    message = str(exc)
    if message.startswith("promote_target_conflict"):
        return HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=message,
        )
    return HTTPException(
        status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=message,
    )


@router.post(
    "/{scope}/{slug}/promote",
    response_model=MemoryEntry,
    status_code=http_status.HTTP_200_OK,
)
async def promote(
    scope: MemoryScope,
    slug: str,
    body: PromoteBody,
    operator: Operator = _require_operator,
) -> MemoryEntry:
    """Promote one memory to a strictly broader scope. Idempotent.

    G5.2-T4 (#626). Delegates ladder + authority checks to T3's
    :func:`~meho_backplane.memory.rbac.assert_can_promote`. The route
    is the HTTP error-mapping layer plus the audit contextvar
    (``audit_promotion_target_scope``) that distinguishes promote
    rows from regular ``memory.remember`` writes for G8 forensic
    queries.

    Status mapping: **200** new target row OR idempotent re-run
    (existing row); **400** :class:`InvalidPromotionStepError`;
    **403** :class:`PermissionDeniedError` with detail
    ``insufficient_promotion_authority``; **404** source absent or
    not visible (tenant-boundary info-leak avoidance); **409**
    target slug owned by a different ``promoted_from`` marker;
    **501** :class:`NotImplementedError` for the unshipped per-
    target ACL branch.

    Binds five audit contextvars before the service call so a mid-
    service exception still produces a correctly-classified audit
    row. The contextvar pattern mirrors G0.4-T5's ``audit_query_hash``
    on ``/api/v1/retrieve``.
    """
    if len(slug) > _SLUG_MAX_LENGTH:
        # Path-parameter overrun defence-in-depth; 404 (not 422)
        # preserves the recall route's info-leak avoidance.
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="memory_not_found",
        )
    structlog.contextvars.bind_contextvars(
        audit_op_id=_MEMORY_OP_IDS["promote"],
        audit_op_class="write",
        audit_scope=scope.value,
        audit_slug=slug,
        audit_promotion_target_scope=body.to.value,
    )
    service = MemoryService()
    try:
        entry = await service.promote(
            operator=operator,
            source_scope=scope,
            source_slug=slug,
            target_scope=body.to,
            move=body.move,
            target_name=body.target_name,
        )
    except InvalidPromotionStepError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except PermissionDeniedError:
        # Canonical detail per Initiative #374. The helper's
        # ``reason`` field carries the same literal, but pinning it
        # here lets audit-query consumers grep one string.
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="insufficient_promotion_authority",
        ) from None
    except NotImplementedError as exc:
        # G0.3 #224 per-target ACL gap surfaces loudly per the
        # helper's contract; 501 is the spec-correct status.
        raise HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"not_implemented: {exc}",
        ) from exc
    except ValueError as exc:
        raise _promote_value_error_to_http(exc) from exc

    if entry is None:
        # 404 across "absent" and "not visible" preserves the
        # tenant-boundary info-leak avoidance contract.
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="memory_not_found",
        )
    return entry
