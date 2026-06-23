# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/kb*`` -- REST surface for the knowledge-base layer.

G4.1-T2 (#416) of Initiative #331. Five routes mounted under
``/api/v1/kb*`` that expose :class:`KbService` (T1 #415) to operators
and agents. The MCP meta-tools (T3 #417) and the CLI verbs (T4 #418)
call into the same service from their own transports; this module is
the HTTP front of the kb backplane.

Route inventory
---------------

* ``GET /api/v1/kb`` -- paginated list of kb entries for the operator's
  tenant. Query params: ``q`` (the canonical free-text SQL ``LIKE``
  pattern; ``filter`` is the deprecated alias, #1854), ``limit``,
  ``offset``. Returns :class:`KbListResponse`. Role: ``operator``.
* ``GET /api/v1/kb/{slug}`` -- fetch one entry by slug. Returns
  :class:`KbEntry`. 404 when absent. Role: ``operator``.
* ``POST /api/v1/kb`` -- create / re-index one entry. Body:
  :class:`KbEntryCreate`. Returns :class:`KbEntry` with HTTP **201**
  for a genuinely-new slug and **200** for an in-place overwrite of an
  existing slug (#1845 -- ``201`` on overwrite is REST-incorrect).
  Role: ``tenant_admin``.
* ``DELETE /api/v1/kb/{slug}`` -- delete an entry. Returns 204 on
  success **and** when the row was already absent (idempotent
  semantics per the acceptance criteria; T6 documents the
  "delete-already-missing is 204" contract). Role: ``tenant_admin``.

Cross-principal write & delete model (#1845)
--------------------------------------------

kb rows are tenant-shared with **no per-row ownership check**: any
``tenant_admin`` may overwrite or delete **any** other principal's slug
in the same tenant. This is **wiki-like and intentional** -- there is
no ``403``/``409`` on a same-slug cross-principal write, and ``DELETE``
is not principal-scoped. Operators must not assume per-author
ownership. (This differs from ``memory`` DELETE, which *is*
principal-scoped.) Writes stamp ``created_by_sub`` /
``last_updated_by_sub`` into the entry's ``metadata`` so a row is
self-describing about authorship without an audit-log join; see
:func:`meho_backplane.kb.attribution.merge_attribution`.
* ``POST /api/v1/kb/ingest`` -- server-side bulk ingest. Body:
  :class:`IngestKbRequest`. Returns :class:`KbIngestionResult`. Role:
  ``tenant_admin``.

Tenant scoping
--------------

Every route derives ``tenant_id`` from the JWT-validated
:class:`Operator`. There is no surface that accepts a tenant id from
the body or query string -- cross-tenant probes are impossible by
construction. A cross-tenant slug probe surfaces as 404
``slug_not_found`` (not 403); the conflation matches the connectors
surface and prevents enumerating other tenants via status-code
differential.

Audit + broadcast contract
--------------------------

Every route binds two contextvars **before** the service call so the
chassis :class:`AuditMiddleware` (G2.3-T1) and the publish-on-write
broadcast hook (G6.1-T3) classify the row correctly:

* ``audit_op_id`` -- one of ``kb.list`` / ``kb.show`` / ``kb.create``
  / ``kb.delete`` / ``kb.ingest`` (the canonical operation
  identifiers tracked under :data:`_KB_OP_IDS`).
* ``audit_op_class`` -- ``"read"`` for ``kb.list`` / ``kb.show``,
  ``"write"`` for ``kb.create`` / ``kb.delete`` / ``kb.ingest``.
  Bound explicitly because
  :func:`~meho_backplane.broadcast.events.classify_op` would only
  match ``kb.list`` / ``kb.create`` / ``kb.delete`` against its
  suffix tables -- ``kb.show`` (no ``.list`` / ``.get`` / ``.info``
  suffix) and ``kb.ingest`` (no ``.create`` / ``.update`` suffix)
  would fall through to the ``other`` bucket and broadcast under
  the wrong sensitivity class.

The ingest route additionally binds ``audit_inserted_count`` /
``audit_updated_count`` / ``audit_skipped_count`` /
``audit_error_count`` so the audit_log payload (and the broadcast
``params``) carries the per-call summary required by the
acceptance criteria -- the file contents are **never** in the
payload, only the counters.

Schema constraints
------------------

* :class:`KbEntryCreate` and :class:`IngestKbRequest` are pydantic v2
  frozen models with ``extra="forbid"`` so unexpected request fields
  trip 422 at the framework layer rather than silently being dropped.
* :class:`IngestKbRequest` validates ``"exactly one of `directory`
  / `tarball_url`"`` via a model-level ``@model_validator``; both set
  OR neither set returns 422.
* ``tarball_url`` is accepted by the request shape for forward-compat
  with the task body's schema, but :class:`KbService` (T1 #415) only
  exposes ``ingest_directory``. A request with ``tarball_url`` set
  returns **501 Not Implemented**, matching the posture
  :mod:`meho_backplane.api.v1.retrieve_eval` uses for its
  ``baseline`` field (honest about the unimplemented branch instead
  of silently dropping). Tarball-URL support is filed as v0.2.next
  follow-up against Initiative #331.

Out of scope
------------

* Per-entry version history -- v0.2.next.
* Multipart tarball upload via ``multipart/form-data`` -- v0.2.next
  (the request schema accepts a ``tarball_url`` only).
* KB search endpoint -- already covered by ``/api/v1/retrieve``
  (G0.4-T5 #262) with ``source="kb"``; no new search route here.
"""

from __future__ import annotations

from pathlib import Path
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
from meho_backplane.kb import (
    KbEntry,
    KbIngestionResult,
    KbIngestRootError,
    KbService,
)
from meho_backplane.kb.schemas import InvalidKbSlugError

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/kb", tags=["knowledge"])

#: Module-level Depends closures -- required to satisfy ruff B008
#: (calls in default argument positions are disallowed). Same shape as
#: :mod:`meho_backplane.api.v1.retrieve` and
#: :mod:`meho_backplane.api.v1.connectors_ingest`.
_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

#: Canonical operation identifiers bound into ``audit_op_id`` per route.
#: Pinned as module constants so the contract is greppable from tests +
#: G8 dashboards and a typo in a route handler surfaces at first call
#: rather than as a silent broadcast under the wrong op_id.
_KB_OP_IDS: Final[dict[str, str]] = {
    "list": "kb.list",
    "show": "kb.show",
    "create": "kb.create",
    "delete": "kb.delete",
    "ingest": "kb.ingest",
}

#: Maximum length of the ``slug`` path parameter accepted by /{slug}
#: routes. Defence-in-depth on top of :data:`SLUG_PATTERN` -- a
#: pathological slug substring in the URL (10 KB of dots, hyphens) would
#: never match the substrate but would still cost a regex pass; capping
#: at the path-parameter parse stage bounds the cost. 256 is generous
#: relative to the consumer kb's typical slug length (~30 chars) but
#: short enough that adversarial input can't pessimize the regex.
_SLUG_MAX_LENGTH: Final[int] = 256


class KbEntryCreate(BaseModel):
    """POST body for ``/api/v1/kb`` -- create / re-index one entry.

    ``slug`` is validated by :func:`validate_slug` inside the route
    handler (the substrate's contract) rather than as a pydantic
    constraint here -- the regex lives on the substrate side so the
    error message is identical across REST / MCP / CLI consumers.
    ``frozen=True`` mirrors the rest of the kb / retrieve schemas
    (a handler accidentally mutating a parsed request body surfaces
    as a pydantic error instead of a confused-deputy bug).
    ``extra="forbid"`` rejects unknown fields at 422 so a typo
    ("body_text") doesn't silently become a no-op.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: str = Field(
        min_length=1,
        max_length=_SLUG_MAX_LENGTH,
        description=(
            "Operator-facing identifier in kebab-case. Must start with "
            "a lowercase letter, end with a lowercase letter or digit, "
            "and contain only lowercase letters, digits, hyphens, or "
            "dots. The leading-letter rule (G0.9.1-T8, Signal #15) is "
            "the most common surprise: '657-recovery' is rejected; "
            "rewrite as 'ticket-657-recovery'."
        ),
        examples=["vcenter-9.0-snapshot-revert"],
    )
    body: str = Field(min_length=1)
    metadata: dict[str, Any] | None = None


class KbEntryPreview(BaseModel):
    """Lightweight projection of :class:`KbEntry` for list responses.

    Drops the full body and emits a 200-char preview instead --
    consumers that need the full body issue a follow-up ``GET
    /{slug}``. Mirrors the kb-search-hit snippet shape (T1's
    :class:`KbEntrySearchHit`) so an operator can do
    ``list → identify slug → fetch full body`` without learning a
    different field name on each surface.
    """

    model_config = ConfigDict(frozen=True)

    slug: str
    preview: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class KbListResponse(BaseModel):
    """Response envelope for ``GET /api/v1/kb``.

    Wrapped in ``{"entries": [...]}`` so a future paging / cursor
    field can land non-breakingly -- same shape
    :mod:`meho_backplane.api.v1.connectors_ingest` adopted for its
    list response. Per-entry shape matches :class:`KbEntryPreview`;
    the body is truncated to a 200-char preview so a casual
    ``meho kb list`` does not stream every entry's full body.
    """

    model_config = ConfigDict(frozen=True)

    entries: list[KbEntryPreview]


#: Maximum number of preview characters returned per entry in the list
#: response. Same cap that :data:`KbService._SNIPPET_CHARS` uses on
#: the search-hit snippet so an operator hitting two surfaces sees
#: matching slice sizes.
_PREVIEW_CHARS: Final[int] = 200


def _build_preview(entry: KbEntry) -> KbEntryPreview:
    """Project a substrate :class:`KbEntry` to a list-response item.

    Truncates ``body`` at :data:`_PREVIEW_CHARS` characters with a
    trailing ellipsis (matching the snippet contract on
    :class:`~meho_backplane.kb.schemas.KbEntrySearchHit`). Renders
    ``created_at`` / ``updated_at`` as ISO-8601 strings via
    :meth:`datetime.isoformat` because :class:`pydantic.BaseModel`
    serialises a :class:`~datetime.datetime` to an ISO string when
    fed through FastAPI's JSON encoder anyway; lifting that into the
    Pydantic type system upfront keeps the response shape stable
    across pydantic version bumps that have tweaked the default
    datetime encoder before.
    """
    preview = entry.body if len(entry.body) <= _PREVIEW_CHARS else entry.body[:_PREVIEW_CHARS] + "…"
    return KbEntryPreview(
        slug=entry.slug,
        preview=preview,
        metadata=dict(entry.metadata),
        created_at=entry.created_at.isoformat(),
        updated_at=entry.updated_at.isoformat(),
    )


class IngestKbRequest(BaseModel):
    """POST body for ``/api/v1/kb/ingest`` -- server-side bulk ingest.

    Exactly one of ``directory`` / ``tarball_url`` must be set; the
    constraint is validated by a model-level
    :func:`~pydantic.model_validator`. ``dry_run`` short-circuits the
    write path (matches :meth:`KbService.ingest_directory`'s contract).

    ``tarball_url`` is accepted by the schema for forward-compat with
    the task body's stated contract but :class:`KbService` (T1)
    only exposes ``ingest_directory``; a request with ``tarball_url``
    set returns **501 Not Implemented** from the route handler.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    directory: str | None = None
    tarball_url: str | None = None
    dry_run: bool = False

    @model_validator(mode="after")
    def _exactly_one_source(self) -> IngestKbRequest:
        """Reject requests that set both or neither of the two source fields.

        The "exactly one of" constraint is part of the acceptance
        criteria for this Task: an operator who sets neither (or
        both) gets a 422 from this validator rather than a confused
        downstream error from the substrate. Empty-string is treated
        as unset to keep the JSON shape forgiving -- an operator
        running ``curl -d '{"directory": ""}'`` shouldn't escape the
        validator on a technicality.
        """
        has_dir = bool(self.directory)
        has_tar = bool(self.tarball_url)
        if has_dir == has_tar:
            raise ValueError(
                "exactly one of 'directory' or 'tarball_url' must be set",
            )
        return self


@router.get("", response_model=KbListResponse)
async def list_kb(
    q: str | None = free_text_q_query(max_length=_SLUG_MAX_LENGTH),
    filter: str | None = Query(  # noqa: A002 - public API field name
        default=None,
        max_length=_SLUG_MAX_LENGTH,
        deprecated=True,
        description="Deprecated alias for `q`; still honoured.",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    operator: Operator = _require_operator,
) -> KbListResponse:
    """List kb entries for the operator's tenant, slug-sorted.

    Tenant-scoped to ``operator.tenant_id`` -- no surface accepts a
    tenant id from the query string. ``read_only`` operators get 403
    via :func:`require_role` before reaching this handler;
    ``operator`` and ``tenant_admin`` both pass.

    ``q`` is the canonical free-text filter param across the kb /
    memory / operations-search list surfaces (#1854); ``filter`` is its
    deprecated alias, kept working for back-compat. The resolved value
    is forwarded to :meth:`KbService.list_entries` as a SQL ``LIKE``
    pattern; the operator is the trust boundary for pattern shape.
    Supplying both ``q`` and ``filter`` with different values is a 422
    rather than a silent pick. ``limit`` is capped at 500 (the
    substrate's :data:`DEFAULT_LIST_LIMIT` is 100; the API surface
    allows higher explicit values so a one-shot dump of a 200-entry
    corpus completes in one round trip).

    Binds ``audit_op_id="kb.list"`` + ``audit_op_class="read"`` before
    the substrate call so a handler exception still produces an
    audit row classified under the canonical op id.
    """
    filter_pattern = resolve_free_text_filter(
        q=q,
        legacy_value=filter,
        legacy_name="filter",
    )
    structlog.contextvars.bind_contextvars(
        audit_op_id=_KB_OP_IDS["list"],
        audit_op_class="read",
    )
    service = KbService()
    entries = await service.list_entries(
        tenant_id=operator.tenant_id,
        filter_pattern=filter_pattern,
        limit=limit,
        offset=offset,
    )
    return KbListResponse(entries=[_build_preview(entry) for entry in entries])


@router.get("/{slug}", response_model=KbEntry)
async def show_kb(
    slug: str,
    operator: Operator = _require_operator,
) -> KbEntry:
    """Return the full body of one kb entry by slug.

    Cross-tenant slug probes surface as 404 ``slug_not_found`` (not
    403) -- the conflation prevents an operator from enumerating
    other tenants via status-code differential. Same shape the
    connectors review route uses.

    Binds ``audit_op_id="kb.show"`` + ``audit_op_class="read"`` before
    the substrate call; ``kb.show`` does not match any suffix in
    :data:`_READ_SUFFIXES`, so the explicit override is load-bearing
    to keep the broadcast classifier from defaulting to
    ``op_class="other"`` (which would emit the full request payload
    instead of read-shaped detail).
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_KB_OP_IDS["show"],
        audit_op_class="read",
    )
    service = KbService()
    entry = await service.get_entry(tenant_id=operator.tenant_id, slug=slug)
    if entry is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="slug_not_found",
        )
    return entry


@router.post(
    "",
    response_model=KbEntry,
    status_code=http_status.HTTP_201_CREATED,
    responses={
        http_status.HTTP_200_OK: {
            "model": KbEntry,
            "description": (
                "An existing entry with this slug was overwritten in "
                "place (id + created_at preserved, updated_at bumped). "
                "201 is returned only for a genuinely-new slug."
            ),
        },
    },
)
async def create_kb(
    body: KbEntryCreate,
    response: Response,
    operator: Operator = _require_admin,
) -> KbEntry:
    """Create / re-index one kb entry under the operator's tenant.

    ``tenant_admin`` only -- ``operator`` role gets 403 via
    :func:`require_role`. Invalid slug shape (regex miss) surfaces as
    422 ``invalid_slug``; same status code Pydantic uses for shape
    failures so callers can branch on 4xx uniformly. The substrate's
    body-hash short-circuit means re-creating an entry with an
    unchanged body pays only an ``updated_at`` bump.

    Status code (#1845 ask 1): the service reports whether the slug
    was genuinely new. This handler returns **201 Created** only for a
    fresh slug and **200 OK** when an existing row was overwritten in
    place -- ``201`` for an in-place mutation is REST-incorrect, and
    the response body already differentiates the two cases (preserved
    ``id`` + ``created_at`` on overwrite). The decorator declares
    ``201`` as the default so OpenAPI advertises the create path;
    ``response.status_code`` is reset to ``200`` on the overwrite path
    (FastAPI lets an injected :class:`Response` override the declared
    code while still applying ``response_model``).

    Cross-principal overwrite is wiki-like and intentional (any
    ``tenant_admin`` may replace any other's row in-tenant); this
    handler adds no ownership gate. It does pass ``operator.sub`` as
    ``actor_sub`` so the service stamps ``created_by_sub`` /
    ``last_updated_by_sub`` into the entry's ``metadata`` -- the entry
    is then self-describing about authorship on every read surface.

    Binds ``audit_op_id="kb.create"`` + ``audit_op_class="write"``
    before the substrate call. ``audit_slug`` is bound so the audit
    log payload (and the broadcast ``params``) carries the slug
    that was created. The body content is NOT bound -- the audit
    row is for the operation, not the document content (which lives
    in the ``documents`` table and is recoverable via ``kb.show``).
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_KB_OP_IDS["create"],
        audit_op_class="write",
        audit_slug=body.slug,
    )
    service = KbService()
    try:
        entry, created = await service.create_entry(
            tenant_id=operator.tenant_id,
            slug=body.slug,
            body=body.body,
            metadata=body.metadata,
            actor_sub=operator.sub,
        )
    except InvalidKbSlugError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    if not created:
        response.status_code = http_status.HTTP_200_OK
    structlog.contextvars.bind_contextvars(audit_created=created)
    return entry


@router.delete(
    "/{slug}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_kb(
    slug: str,
    operator: Operator = _require_admin,
) -> Response:
    """Delete one kb entry by slug. Idempotent.

    ``tenant_admin`` only. Returns 204 whether the row existed or
    not -- the acceptance criteria pin idempotent semantics ("delete
    already-missing returns 204"). A non-idempotent surface that
    returns 404 on missing-on-delete would let an operator probe
    for the presence of a slug they cannot read; the idempotent
    shape preserves the tenant boundary's confidentiality
    properties.

    Binds ``audit_op_id="kb.delete"`` + ``audit_op_class="write"``
    + ``audit_slug=<slug>`` **before** the substrate call so an
    exception on the substrate's ``delete_entry`` still produces an
    audit row classified under ``kb.delete``. The ``audit_existed``
    boolean (real deletion vs no-op) is bound *after* the substrate
    returns -- on the exception path it stays unbound, which is the
    truthful signal (we don't know whether the row existed because
    the query failed). The audit row is written either way; every
    authenticated request gets one row, per the audit middleware
    contract.

    Bind ordering matters: if the bind landed *after* the substrate
    call, an exception during ``delete_entry`` would skip the bind
    entirely. The audit middleware would then fall back to its
    pre-handler default ``op_id`` (``http.delete:/api/v1/kb/<slug>``)
    and :func:`~meho_backplane.broadcast.events.classify_op` would
    bucket the row as ``op_class="other"`` -- defeating the
    broadcast-redaction contract from decision #3.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_KB_OP_IDS["delete"],
        audit_op_class="write",
        audit_slug=slug,
    )
    service = KbService()
    existed = await service.delete_entry(tenant_id=operator.tenant_id, slug=slug)
    structlog.contextvars.bind_contextvars(audit_existed=existed)
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@router.post("/ingest", response_model=KbIngestionResult)
async def ingest_kb(
    body: IngestKbRequest,
    operator: Operator = _require_admin,
) -> KbIngestionResult:
    """Server-side bulk ingest from a directory on the backplane host.

    ``tenant_admin`` only -- bulk ingest is a privileged operation.
    The request shape accepts both ``directory`` and ``tarball_url``
    fields (the task body's contract), but only the ``directory``
    branch is implemented in v0.2; a ``tarball_url`` request returns
    **501 Not Implemented**, mirroring
    :mod:`meho_backplane.api.v1.retrieve_eval`'s posture toward its
    unimplemented baseline field.

    Walks the supplied directory via
    :meth:`KbService.ingest_directory` and returns the four-bucket
    :class:`KbIngestionResult`. The directory is **confined to**
    ``KB_INGEST_ROOT`` (default ``/opt/meho/kb-ingest``): a path
    resolving outside it (``..`` traversal or escaping symlink) raises
    :class:`~meho_backplane.kb.KbIngestRootError`, mapped to a **400**
    ``kb_ingest_path_outside_root`` here before any file is read --
    closing the path-traversal / LFI hole (#101 L8 + L14) even for a
    ``tenant_admin``. A path inside the root that does not exist or is
    not a directory fails closed with ``directory_not_found`` /
    ``not_a_directory`` (also 400).

    Binds ``audit_op_id="kb.ingest"`` + ``audit_op_class="write"``
    plus the four ``KbIngestionResult`` counters in the audit
    payload (NOT the file contents -- the task body's audit
    contract). ``audit_dry_run`` flags dry-run calls so an operator
    can grep their audit log for trial vs real ingests.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_KB_OP_IDS["ingest"],
        audit_op_class="write",
        audit_dry_run=body.dry_run,
    )
    if body.tarball_url:
        # Forward-compat: the request schema accepts the field, but
        # the substrate does not yet implement tarball ingest. Return
        # 501 so the API surface is honest about its limits; same
        # pattern :mod:`retrieve_eval` uses for its server-side
        # baseline field. v0.2.next can wire up
        # ``KbService.ingest_tarball`` and flip this branch.
        #
        # Truthiness check (not ``is not None``) matches
        # :meth:`IngestKbRequest._exactly_one_source`, which treats an
        # empty string as unset so a curl payload like
        # ``{"directory": "/tmp/kb", "tarball_url": ""}`` reaches this
        # handler with ``tarball_url=""`` instead of being rejected at
        # the validator. ``is not None`` here would surface as a 501
        # for that payload while the validator already accepted it --
        # divergent semantics between the two layers.
        raise HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "tarball_url ingest is not implemented; use 'directory' "
                "or run 'meho kb ingest' against a locally-extracted tree"
            ),
        )

    # ``directory`` is guaranteed non-empty by the model validator
    # (mode="after" checks "exactly one of"); the assert narrows the
    # type for mypy.
    assert body.directory is not None
    service = KbService()
    try:
        result = await service.ingest_directory(
            directory=Path(body.directory),
            tenant_id=operator.tenant_id,
            dry_run=body.dry_run,
        )
    except KbIngestRootError as exc:
        # Path-traversal / LFI guard (#101): directory resolved outside
        # KB_INGEST_ROOT. 400 with the ``kb_ingest_path_outside_root``
        # marker (same ``<marker>: <detail>`` shape as the branches
        # below) -- the file is never read.
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"directory_not_found: {exc}",
        ) from exc
    except NotADirectoryError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"not_a_directory: {exc}",
        ) from exc

    structlog.contextvars.bind_contextvars(
        audit_inserted_count=result.inserted_count,
        audit_updated_count=result.updated_count,
        audit_skipped_count=result.skipped_count,
        audit_error_count=result.error_count,
    )
    return result
