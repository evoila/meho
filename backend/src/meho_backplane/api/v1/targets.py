# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/targets`` — CRUD surface for the targets registry.

7 routes (G0.3-T3 / Task #254; extended by G9.1-T5 / G0.14-T4):

* ``GET  /api/v1/targets``                — list, keyset-paginated. ``operator`` role.
* ``GET  /api/v1/targets/discover``       — discover candidates. ``operator`` role.
* ``GET  /api/v1/targets/{name}``         — describe (alias-aware). ``operator`` role.
* ``POST /api/v1/targets/{name}/probe``   — invoke connector probe. ``operator`` role.
* ``POST /api/v1/targets``                — create. ``tenant_admin`` role.
* ``PATCH /api/v1/targets/{name}``        — update (partial). ``tenant_admin`` role.
* ``DELETE /api/v1/targets/{name}``       — soft-delete. ``tenant_admin`` role.

All routes are tenant-scoped via ``operator.tenant_id`` extracted from the
JWT by :func:`~meho_backplane.middleware.verify_jwt_and_bind`. Cross-tenant
reads are impossible — the WHERE clause always includes ``tenant_id``.

Alias resolution
----------------

``GET /{name}`` and ``PATCH /{name}`` both pass the caller-supplied ``name``
to :func:`~meho_backplane.targets.resolver.resolve_target`, which implements
the 3-step algorithm: exact name → alias element-equality → near-miss 404.
Callers can address a target by any of its aliases and get the same result.

Audit enrichment
----------------

Routes that call ``resolve_target`` get the resolved target's id bound
into structlog contextvars at the resolver's single exit point;
:class:`~meho_backplane.audit.AuditMiddleware` reads ``target_id`` via
:func:`~meho_backplane.audit._resolve_target_id` and writes it to
``audit_log.target_id``. ``create_target`` does not call
``resolve_target`` (it creates a row rather than looking one up), so
it binds ``target_id`` directly after ``session.add(t)``.

Probe route
-----------

The probe route delegates to the product's registered
:class:`~meho_backplane.connectors.base.Connector`. Per the 2026-05-14
amendment to Initiative #224 (G0.3-T1.5 / Task #477) the probe verb
calls :meth:`Connector.fingerprint` (returning
:class:`~meho_backplane.connectors.schemas.FingerprintResult`), persists
the result to ``targets.fingerprint`` (server-managed; never writeable
via PATCH), and returns the fingerprint to the caller. The G0.6
resolver (#388) reads the persisted column to pick a connector
implementation without re-probing the live target.

The route routes connector lookup through
:func:`~meho_backplane.connectors.resolver.resolve_connector_or_label`
— the same helper the dispatcher's connector-resolution step
(:func:`~meho_backplane.operations.dispatcher._resolve_connector_instance`)
uses. This closes the pre-G0.14 asymmetry where ``/probe`` consulted
the v1 :func:`~meho_backplane.connectors.registry.get_connector` lookup
while dispatch consulted v2's
:func:`~meho_backplane.connectors.resolve_connector`, producing
disagreeing yes/no answers for the same target (consumer feedback
signal 19 in ``claude-rdc-hetzner-dc#697`` — ``rdc-vcenter`` got 501
from ``/probe`` while ``POST /operations/call`` resolved the same
target's ``vmware-rest-9.0`` connector cleanly).

Resolver outcomes map to HTTP status as follows:

* **resolved** — fingerprint the target and return the
  :class:`FingerprintResult` (200, current behavior).
* **no_connector** — no registered impl matches the target's
  ``(product, version)``. 501 with the resolver's exception message
  in ``detail`` (which already names ``target.product`` and the
  absence of a versioned candidate).
* **ambiguous_connector** — two or more impls remain after the
  full tie-break ladder. 409 with the resolver's exception message
  in ``detail`` — the message already names the candidate set and
  the remediation step (set ``target.preferred_impl_id`` to one of
  them).

Neither error branch touches the DB row; any previously-cached
fingerprint survives. A connector that raises is caught at the route
boundary and converted to a structured **500** with the
``fingerprint_failed`` envelope (G0.15-T1 #1210); the outer
``session.begin()`` in :func:`~meho_backplane.db.engine.get_session`
still rolls back on the structured raise (nothing has been flushed —
the ``t.fingerprint`` / ``t.updated_at`` writes only happen on the
success path), again leaving the row untouched. The column therefore
always reflects the *last successful* probe. The target must exist
for the probe to fire — a non-existent target returns 404 via
``resolve_target``.

DELETE + product PATCH (G0.14-T4 #1145)
----------------------------------------

The G0.14-T4 amendment closes the
*"misregistered target cannot be recovered"* hole the v0.6.0 dogfood
exercise surfaced (signal 6, `claude-rdc-hetzner-dc#697`):

* **``DELETE /api/v1/targets/{name}``** soft-deletes the row by
  stamping ``deleted_at``; the row stays queryable from the
  ``audit_log.target_id`` soft-FK (audit is append-only per
  v0.1-spec §6) but is invisible to every dispatch path that goes
  through :func:`~meho_backplane.targets.resolver.resolve_target`.
  A cascade check counts ``graph_node.target_id`` references and
  defaults to **409 with the count + a `?force=true` hint** when
  the target is wired into the topology graph; ``?force=true``
  proceeds with the soft-delete anyway (the FK is
  ``ON DELETE SET NULL`` so the graph rows are safe). Active
  audit references are not counted in the cascade check — they
  exist for every authenticated request against the target by
  design and would always block the DELETE.
* **``PATCH /api/v1/targets/{name}``** now accepts ``product`` so
  an operator who misregisters ``product='kubernetes'`` (the real
  ``rke2-infra`` typo from the dogfood) can correct it to ``'k8s'``
  in-place instead of deleting and re-creating. The new value is
  validated at request time against the registered connector
  products; an unknown product yields a structured 422 listing the
  ``valid_products`` so an agent / CLI can branch on the
  diagnostic without re-parsing prose (T3 #1144 ships the same
  validator at POST time; the two share the resolver-product
  helper in :mod:`meho_backplane.connectors.registry`).

Both pair with each other: an operator can either correct the
``product`` in-place (PATCH) or delete the broken row and re-create
a fresh one (DELETE + POST). Either recovery path works; both
available is the strongest answer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.api.v1._envelope import (
    ENVELOPE_QUERY,
    EnvelopeVersion,
    wrap_v2_envelope,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    canonical_product_token,
    registered_product_tokens,
)
from meho_backplane.connectors.resolver import resolve_connector_or_label
from meho_backplane.connectors.schemas import AuthModel, CandidateHint, FingerprintResult
from meho_backplane.db.engine import get_session
from meho_backplane.db.models import GraphNode
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.targets.resolver import resolve_target
from meho_backplane.targets.schemas import (
    Target,
    TargetCreate,
    TargetSummary,
    TargetUpdate,
    project_target_to_summary,
)

__all__ = ["router"]

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/targets", tags=["targets"])

#: Module-level Depends closures — required to satisfy ruff B008 (mutable
#: calls in default argument positions are disallowed). Pattern matches
#: :mod:`meho_backplane.api.v1.retrieve`.
_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

#: Canonical op id for the discover route's audit row + broadcast.
#: ``.discover`` is not one of the broadcast classifier's read/write
#: verb suffixes, so the explicit ``audit_op_class="read"`` override
#: this constant pairs with is load-bearing (mirrors the ``kb.show``
#: rationale in :mod:`meho_backplane.api.v1.kb`).
_DISCOVER_OP_ID = "targets.discover"

#: Canonical op id for the DELETE route's audit row. Mirrors the
#: ``meho.connector.edit_*`` audit precedent (``api/v1/connectors_ingest.py``)
#: and the ``conventions.delete`` shape so cross-tenant audit
#: queries (G8) can filter on ``audit_op_id LIKE 'targets.%'``
#: without a special case.
_DELETE_OP_ID: Final[str] = "targets.delete"

#: Cap on the exception-message length recorded in the
#: ``fingerprint_failed`` 500 detail (G0.15-T1 #1210). Mirrors
#: :data:`meho_backplane.operations._errors._EXC_MESSAGE_CAP` — a
#: misbehaving connector could embed a credential into a stringified
#: exception; 256 chars is enough for an operator to recognise the
#: failure shape while capping the leak surface.
_PROBE_EXC_MESSAGE_CAP: Final[int] = 256


def _connector_id_for(cls: type[Connector]) -> str:
    """Build the canonical ``connector_id`` string for a resolved connector.

    Matches the dispatcher's ``connector_id`` form (e.g. ``"k8s-1.x"``,
    ``"vmware-rest-9.0"``) — used in the ``fingerprint_failed`` 500
    envelope (G0.15-T1 #1210) and the structured log line so operator-
    facing diagnostics name the *specific* impl that failed, not just
    the product slug. Falls back to the bare product token when
    ``impl_id`` is empty (the v1-style unversioned shape preserved by
    the registry — see :class:`~meho_backplane.connectors.base.Connector`).
    """
    base = cls.impl_id or cls.product
    if cls.version:
        return f"{base}-{cls.version}"
    return base


def _build_fingerprint_failed_detail(
    *,
    exc: BaseException,
    cls: type[Connector],
    target_name: str,
) -> dict[str, str]:
    """Build the T11-compliant ``fingerprint_failed`` 500 detail.

    G0.15-T1 (#1210). Mirrors the dispatcher's ``_execute_and_audit``
    ``connector_error`` envelope (`operations._errors.result_connector_error`)
    at the route boundary so the probe surface and the dispatch surface
    agree on what a connector failure looks like. The detail follows
    the convention codified in ``docs/codebase/error-message-shape.md``
    (T11 #1141): a stable ``error`` code, the failing ``connector_id``
    + ``target_name``, the underlying ``exception_class`` /
    ``exception_message`` (the latter capped to keep credential-stuffed
    exception strings from leaking unbounded into the response body),
    and a ``docs`` back-reference for the operator.

    Pure builder — no I/O, no logging. The caller logs the full
    exception via :meth:`structlog.stdlib.BoundLogger.exception` so
    the stacktrace lands in the structured log; only the capped
    message lands in the response.
    """
    message = str(exc)
    if len(message) > _PROBE_EXC_MESSAGE_CAP:
        message = message[:_PROBE_EXC_MESSAGE_CAP] + "...<truncated>"
    return {
        "error": "fingerprint_failed",
        "connector_id": _connector_id_for(cls),
        "target_name": target_name,
        "exception_class": type(exc).__name__,
        "exception_message": message,
        "docs": "docs/codebase/error-message-shape.md",
    }


class SkippedConnector(BaseModel):
    """One connector that did not contribute candidates for a product.

    ``name`` is the connector implementation key (the ``impl_id`` from
    the v2 registry, or the class name for a v1-only registration).
    ``reason`` is a short human string — ``"no candidates"`` when the
    connector ran cleanly but inferred nothing, or the exception class
    + message when ``list_candidates`` raised (one bad connector must
    not fail the whole discover sweep).
    """

    model_config = ConfigDict(frozen=True)

    name: str
    reason: str


class TargetsDiscoverResult(BaseModel):
    """Aggregated discover output across every connector for a product.

    ``discovered`` is the merged candidate list from every connector
    registered for the requested product; ``skipped`` records the
    connectors that contributed nothing (clean-but-empty or errored).
    The verb never auto-creates ``targets`` rows — the operator reviews
    ``discovered`` and runs ``meho targets create`` (Initiative #363:
    auto-registration is v0.2.next).
    """

    model_config = ConfigDict(frozen=True)

    discovered: list[CandidateHint]
    skipped: list[SkippedConnector]


# G0.16-T6 review-iter-1 m1 (#1312). The ORM→TargetSummary projection
# was previously duplicated byte-for-byte on this module and on
# :mod:`meho_backplane.targets.resolver`; the duplication was the
# drift class that produced Finding D (list silently masking version
# / secret_ref / preferred_impl_id while detail returned them).
# Single canonical helper now lives at
# :func:`meho_backplane.targets.schemas.project_target_to_summary`;
# both sites import + call it directly.


def _build_unknown_product_detail(
    product: str,
    valid_products: list[str],
) -> dict[str, object]:
    """Build the T11-compliant ``unknown_product`` 422 detail.

    G0.14-T3 (#1144). Mirrors the ``/probe`` 501's diagnostic shape
    but moves it forward to the POST / PATCH validation boundary so
    the operator sees the actionable error at create time instead of
    after committing a permanent broken row. The detail follows the
    convention codified in ``docs/codebase/error-message-shape.md``
    (T11 #1141): a stable ``kind`` code, a ``message`` naming the
    offending value + remediation + a doc reference, and a machine-
    actionable ``valid_products`` list the client can branch on.

    Pure builder — no I/O, no logging. Called from both
    :func:`create_target` (POST) and (after T4 #1145 consolidation)
    :func:`update_target` (PATCH); the shared shape is what lets a
    CLI / agent handle the two surfaces identically.
    """
    return {
        "kind": "unknown_product",
        "product": product,
        "valid_products": valid_products,
        "message": (
            f"product={product!r} is not registered; "
            f"pick one of {valid_products!r} or register a "
            f"connector for it before retrying. "
            f"See docs/codebase/error-message-shape.md for "
            f"the convention."
        ),
    }


def _canonicalise_and_validate_product(supplied: str) -> str:
    """Resolve ``supplied`` to a canonical registry product or raise 422.

    G0.18-T2 (#1355). Shared canonicalisation + enum-validation step
    for the POST / PATCH write surfaces. Runs ``supplied`` through
    :func:`canonical_product_token` so the listing-spelling alias
    (``"sddc"``) maps to the registry's canonical form
    (``"sddc-manager"``) before the registered-product validator
    fires; without this an operator copying ``product`` straight out
    of ``meho connector list`` would hit a 422 (RDC #789 Finding 6;
    closes #1312 acceptance B).

    The 422 detail names the *originally supplied* token (not the
    canonicalised form) so the operator's error message shows what
    they actually typed, matching the T11 contract that
    ``unknown_product.product`` echoes the user's input. The
    canonical token is what the caller stores; the validator's
    "valid set" is also the canonical-only set (``valid_products``
    is exposed on Swagger via the same source-of-truth helper).

    The validator is skipped when the registry is empty — that state
    means "no connectors imported" (test isolation, or a deploy
    booted before :func:`_eager_import_connectors` ran). In
    production the lifespan populates the registry before the first
    request arrives.
    """
    canonical = canonical_product_token(supplied)
    valid_products = sorted(registered_product_tokens())
    if valid_products and canonical not in valid_products:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_build_unknown_product_detail(supplied, valid_products),
        )
    return canonical


def _to_full(t: TargetORM) -> Target:
    return Target(
        id=t.id,
        tenant_id=t.tenant_id,
        name=t.name,
        aliases=tuple(t.aliases),
        product=t.product,
        version=t.version,
        host=t.host,
        port=t.port,
        fqdn=t.fqdn,
        secret_ref=t.secret_ref,
        auth_model=AuthModel(t.auth_model),
        vpn_required=t.vpn_required,
        extras=t.extras,
        notes=t.notes,
        fingerprint=t.fingerprint,
        preferred_impl_id=t.preferred_impl_id,
        created_at=t.created_at,
        updated_at=t.updated_at,
        deleted_at=t.deleted_at,
    )


def _registered_products() -> set[str]:
    """Return the set of products advertised by registered connector classes.

    Read from the v2 registry snapshot (which subsumes v1 entries
    as ``(product, "", "")``) and dropped of empty-string entries so
    only meaningful product slugs surface. The set drives the
    ``product``-on-PATCH validator and matches the resolver's notion
    of "valid product" at probe / dispatch time so a PATCH that
    passes here does not surprise the operator with a 501 on the
    next probe.

    Inline at the PATCH site rather than imported from a sibling
    helper because T3 #1144 (which ships the same enum at POST time)
    has not yet merged at the time of writing; the two sites will
    consolidate against a shared helper once both land (see PR
    description on #1145).

    TODO: consolidate with sibling PR #1166's
    ``registered_product_tokens()`` helper in
    ``connectors/registry.py`` once it merges — scheduled for a
    Wave 5 follow-up so the duplication does not race the parallel
    PRs.
    """
    return {product for (product, _version, _impl_id) in all_connectors_v2() if product}


def _registered_impl_ids(product: str) -> set[str]:
    """Return the impl_ids advertised by connectors registered for *product*.

    G0.15-T6 (#1215). The v0.7.0 dogfood (RDC #753, signal 6) caught a
    UX foot-gun where PATCH accepted any string for ``preferred_impl_id``
    and the resolver silently ignored unknown values (the override is
    consulted only as step 3 of the tie-break ladder, and zero matches
    means the override never fires). The operator believed they had
    pinned an implementation but every dispatch continued resolving via
    the tie-break ladder's earlier steps. This validator runs at
    create / update time and rejects an unknown impl_id with a
    structured 422 so the operator gets the same actionable diagnostic
    at write time that the resolver would give if it surfaced the
    silent-ignore.

    G0.16-T6 review-iter-1 B1 (#1312). The set is **product-scoped**.
    A global allowlist (the pre-B1 shape) accepted any impl_id
    registered for *any* product, so a ``k8s`` target could pass
    validation with ``preferred_impl_id="vmware-rest-9.0"`` and the
    resolver would silently ignore it at dispatch time -- the exact
    foot-gun G0.15-T6 (#1215) was created to close. The filter is
    consulted at both POST (``body.product``) and PATCH (the
    effective patched product, post-update) so the validator and the
    resolver see the same impl set for the same target.

    Read from the v2 registry snapshot (which subsumes v1 entries as
    ``(product, "", "")``); the empty-string placeholder is excluded
    because a bare ``""`` impl_id has no addressable meaning at the
    PATCH surface (an operator setting ``preferred_impl_id=""`` is
    equivalent to clearing the override, and we model that via
    ``None``, not the empty string). Returned as a fresh ``set`` so
    callers can mutate / sort without affecting the underlying
    registry.

    G0.16-T6 Finding C (#1312). The set includes BOTH the base
    ``impl_id`` (``"nsx-rest"``) AND the versioned form
    ``"{impl_id}-{version}"`` (``"nsx-rest-4.2"``) for every triple
    with a non-empty ``version``. The versioned form is the canonical
    shape per ``docs/codebase/api-shape-conventions.md`` §3 because
    it's more specific (disambiguates when multiple connector versions
    ship in one release) and it matches the ``connector_id`` string
    the dispatcher's ``parse_connector_id`` round-trips through. The
    base form stays accepted so existing operators / fixtures that
    pin ``preferred_impl_id="nsx-rest"`` aren't broken. The resolver
    (:func:`meho_backplane.connectors.resolver._run_tie_break_ladder`)
    normalizes both forms to the base ``impl_id`` before matching
    candidates, so picking either form selects the same connector
    when only one version is registered.
    """
    scoped = [
        (reg_product, version, impl_id)
        for (reg_product, version, impl_id) in all_connectors_v2()
        if reg_product == product
    ]
    base_ids = {impl_id for (_p, _version, impl_id) in scoped if impl_id}
    versioned_ids = {
        f"{impl_id}-{version}" for (_p, version, impl_id) in scoped if impl_id and version
    }
    return base_ids | versioned_ids


def _build_unknown_preferred_impl_detail(
    preferred_impl_id: str,
    valid_impl_ids: list[str],
) -> dict[str, object]:
    """Build the structured 422 detail for an unknown ``preferred_impl_id``.

    Mirrors :func:`_build_unknown_product_detail` and the convention in
    ``docs/codebase/error-message-shape.md`` -- a snake_case ``kind``
    discriminator, the offending value, a machine-actionable list of
    valid alternatives, and a human-readable ``message`` carrying the
    remediation step. The 422 status (vs 404) matches the rest of the
    targets surface: a body field carried a value the server cannot
    honour, so the request was unprocessable.

    G0.16-T6 Finding C (#1312). ``valid_impl_ids`` carries BOTH the
    base ``impl_id`` and the canonical versioned
    ``"{impl_id}-{version}"`` form per
    ``docs/codebase/api-shape-conventions.md`` §3, so an operator
    typing either shape gets the same actionable diagnostic listing
    both alternatives.
    """
    return {
        "kind": "unknown_preferred_impl_id",
        "preferred_impl_id": preferred_impl_id,
        "valid_impl_ids": valid_impl_ids,
        "message": (
            f"preferred_impl_id={preferred_impl_id!r} is not registered; "
            f"pick one of {valid_impl_ids!r} or register a connector for "
            f"it before retrying. The canonical form is versioned "
            f"(e.g. 'nsx-rest-4.2'); the base form ('nsx-rest') stays "
            f"accepted for backward compatibility. The resolver silently "
            f"ignores unknown impl_id overrides; this 422 surfaces the "
            f"foot-gun at write time. See "
            f"docs/codebase/error-message-shape.md for the convention "
            f"and docs/codebase/api-shape-conventions.md §3 for the "
            f"versioned-vs-base impl-id discipline."
        ),
    }


@router.get("")
async def list_targets(
    product: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    envelope: EnvelopeVersion | None = ENVELOPE_QUERY,
    operator: Operator = _require_operator,
    session: AsyncSession = Depends(get_session),
) -> list[TargetSummary] | dict[str, object]:
    """List targets for the requesting tenant.

    Results are keyset-paginated by ``name`` (lexicographic order).
    Pass ``cursor=<last-name-seen>`` to fetch the next page. The
    ``product`` filter is exact-match; pass it to narrow by product
    slug. ``limit`` defaults to 100, max 500.

    Soft-deleted targets (``deleted_at IS NOT NULL``, G0.14-T4 #1145)
    are excluded from the list — the same filter the resolver applies
    so list and dispatch never disagree about which targets are
    visible to the tenant.

    G0.16-T6 Finding A (#1312) — non-breaking shape opt-in. The
    default response stays the v0.8.0 bare list ``[TargetSummary,
    ...]``; passing ``?envelope=v2`` returns the unified envelope
    ``{"items": [...], "next_cursor": <opaque str | null>}`` per
    ``docs/codebase/api-shape-conventions.md`` §2. The cursor on the
    v2 envelope is the last-row ``name`` of the page when the page
    filled to ``limit`` (so a re-issue carries pagination), and
    ``None`` when the page exhausted the matching set (so callers
    see "no more pages" without inspecting list length). The opt-in
    semantics let SDK / CLI / MCP sister surfaces adopt the v2
    shape at their own cadence; the v0.8.0 default flips after two
    release cycles per the §2 migration recipe.
    """
    stmt = select(TargetORM).where(
        TargetORM.tenant_id == operator.tenant_id,
        TargetORM.deleted_at.is_(None),
    )
    if product is not None:
        stmt = stmt.where(TargetORM.product == product)
    if cursor is not None:
        stmt = stmt.where(TargetORM.name > cursor)
    # G0.16-T6 review-iter-1 M1 (#1312). Over-fetch ``limit + 1`` so the
    # presence of an extra row directly proves there's another page,
    # rather than inferring from ``len(rows) >= limit`` (which produces
    # a false-positive non-null ``next_cursor`` on the terminal page
    # when the matching set size is an exact multiple of ``limit``).
    # The bare-list branch slices back to ``limit`` so the v0.8.0 shape
    # is unchanged.
    stmt = stmt.order_by(TargetORM.name).limit(limit + 1)
    result = await session.execute(stmt)
    fetched = list(result.scalars().all())
    has_more = len(fetched) > limit
    rows = fetched[:limit]
    summaries = [project_target_to_summary(t) for t in rows]
    if envelope is None:
        return summaries
    next_cursor = rows[-1].name if has_more else None
    return wrap_v2_envelope(
        [s.model_dump(mode="json") for s in summaries],
        next_cursor=next_cursor,
    )


@router.get("/discover", response_model=TargetsDiscoverResult)
async def discover_targets(
    product: str = Query(...),
    seed_target: str | None = Query(default=None),
    operator: Operator = _require_operator,
    session: AsyncSession = Depends(get_session),
) -> TargetsDiscoverResult:
    """Discover potentially-reachable targets for a *product*.

    Iterates every connector implementation registered for *product*
    and calls
    :meth:`~meho_backplane.connectors.base.Connector.list_candidates`
    on each, merging the results. ``seed_target`` (optional) scopes the
    discovery to one already-registered target's reach (e.g. peer
    Kubernetes clusters in the same kubeconfig context tree); it is
    resolved tenant-scoped via
    :func:`~meho_backplane.targets.resolver.resolve_target` so a
    principal can only seed from a target in their own tenant
    (cross-tenant seeding is impossible). 404 with near-misses when the
    seed name does not resolve.

    The verb is **read-only** — it returns candidates, never creates
    ``targets`` rows (Initiative #363: auto-registration is v0.2.next;
    the operator runs ``meho targets create`` after review). One
    connector raising does not fail the sweep: it is recorded in
    ``skipped`` with the exception summary and the rest still run.

    Registered **before** ``GET /{name}`` on the same router so the
    literal ``/discover`` segment is matched as this route rather than
    captured as a target name by the parametrised describe route
    (FastAPI resolves routes in declaration order).
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_DISCOVER_OP_ID,
        audit_op_class="read",
    )

    seed: TargetORM | None = None
    if seed_target is not None:
        # resolve_target raises HTTPException(404/409) directly and
        # binds target_id into contextvars for the audit row.
        seed = await resolve_target(session, operator.tenant_id, seed_target)

    # Enumerate every connector implementation registered for the
    # product. The v2 registry keys on (product, version, impl_id) and
    # subsumes v1 registrations (as (product, "", "")), so one pass
    # covers connectors registered by either entry point. Dedupe by
    # connector class so a product with one impl registered under both
    # registry layers is not probed twice.
    impls: dict[type[Connector], str] = {}
    for (reg_product, version, impl_id), cls in all_connectors_v2().items():
        if reg_product != product:
            continue
        # Human-meaningful label: prefer impl_id, fall back to a
        # version tag, then the class name (v1-only registration keys
        # both fields empty).
        label = impl_id or version or cls.__name__
        impls.setdefault(cls, label)

    discovered: list[CandidateHint] = []
    skipped: list[SkippedConnector] = []

    for cls, label in impls.items():
        connector = get_or_create_connector_instance(cls)
        try:
            candidates = await connector.list_candidates(seed)
        except Exception as exc:
            # One bad connector must not abort the whole sweep — record
            # the failure and continue with the remaining connectors.
            _log.warning(
                "targets_discover_connector_failed",
                product=product,
                connector=label,
                tenant_id=str(operator.tenant_id),
                error=repr(exc),
            )
            skipped.append(SkippedConnector(name=label, reason=f"{type(exc).__name__}: {exc}"))
            continue
        if candidates:
            discovered.extend(candidates)
        else:
            skipped.append(SkippedConnector(name=label, reason="no candidates"))

    _log.info(
        "targets_discover_completed",
        product=product,
        tenant_id=str(operator.tenant_id),
        connectors=len(impls),
        discovered=len(discovered),
        skipped=len(skipped),
    )
    return TargetsDiscoverResult(discovered=discovered, skipped=skipped)


@router.get("/{name}", response_model=Target)
async def describe_target(
    name: str,
    operator: Operator = _require_operator,
    session: AsyncSession = Depends(get_session),
) -> Target:
    """Describe a target by name or alias.

    Uses :func:`~meho_backplane.targets.resolver.resolve_target` so
    callers can pass any alias instead of the canonical name. Returns
    404 with near-misses when nothing matches.
    """
    t = await resolve_target(session, operator.tenant_id, name)
    return _to_full(t)


@router.post("/{name}/probe", response_model=FingerprintResult)
async def probe_target(
    name: str,
    operator: Operator = _require_operator,
    session: AsyncSession = Depends(get_session),
) -> FingerprintResult:
    """Invoke the registered connector's ``fingerprint`` method for a target.

    The probe verb returns the connector's
    :class:`~meho_backplane.connectors.schemas.FingerprintResult` and
    persists it to ``targets.fingerprint`` so the G0.6 resolver (#388)
    can pick an implementation without re-probing the live target.
    Connector selection uses
    :func:`~meho_backplane.connectors.resolver.resolve_connector_or_label`
    so this route and the dispatcher consult the same v2 registry
    through the same tie-break ladder (G0.14-T1 #1142). Returns:

    * **501** when the resolver reports ``no_connector``. ``detail``
      carries the resolver's exception message naming the target's
      product slug + the absence of a matching candidate.
    * **409** when the resolver reports ``ambiguous_connector``.
      ``detail`` carries the resolver's exception message naming the
      candidate set and the remediation step (set
      ``target.preferred_impl_id`` to one of them).
    * **500** with the structured ``fingerprint_failed`` envelope from
      :func:`_build_fingerprint_failed_detail` when the resolved
      connector's :meth:`fingerprint` raises (G0.15-T1 #1210; mirrors
      the dispatcher's ``connector_error`` envelope at the route
      boundary so probe + dispatch agree on the shape of a connector
      failure — sub-signal A of ``claude-rdc-hetzner-dc#753``).

    No error branch writes to the DB row; the previously-cached
    fingerprint survives. The outer ``async with session.begin()``
    rolls back on every structured raise (nothing has been flushed by
    that point), so the column always reflects the *last successful*
    probe. The target must exist for the probe to fire — a
    non-existent target returns 404 via ``resolve_target``.
    """
    t = await resolve_target(session, operator.tenant_id, name)
    cls, label, exception_message = resolve_connector_or_label(t)
    if label == "no_connector":
        raise HTTPException(
            status_code=501,
            detail=exception_message or f"no connector registered for product={t.product!r}",
        )
    if label == "ambiguous_connector":
        raise HTTPException(
            status_code=409,
            detail=exception_message
            or (
                f"connector resolution ambiguous for product={t.product!r}; "
                f"set target.preferred_impl_id to disambiguate"
            ),
        )
    # label is None ⇒ cls is set (contract of resolve_connector_or_label).
    assert cls is not None
    try:
        # Forward the route operator so the connector's fingerprint reads
        # per-target Vault credentials under the operator's identity, the
        # same code path the dispatch surface uses. G0.16-T4 (#1306)
        # converged probe + dispatch on this signature; pre-fix the
        # probe path passed nothing (so the connector synthesised a
        # system operator with a placeholder JWT) and Vault rejected
        # the JWT/OIDC login as ``malformed jwt: must have three
        # parts`` on the four connectors whose fingerprint authenticates
        # via Vault (k8s-1.x, vmware-rest-9.0, sddc-rest-9.0,
        # nsx-rest-4.2). The wider ABC widening (with
        # ``operator: Operator | None = None``) keeps this call
        # backwards-compatible: connectors whose fingerprint does not
        # touch Vault accept the parameter and ignore it.
        fp = await cls().fingerprint(t, operator=operator)
    except Exception as exc:
        # G0.15-T1 #1210: a connector that raises mid-fingerprint used
        # to bubble past the route and surface as FastAPI's bare 500
        # with a ``text/plain`` body. Catch + convert to a structured
        # 500 via :func:`_build_fingerprint_failed_detail` (the
        # builder mirrors the dispatcher's ``connector_error``
        # envelope). Full stacktrace lands in the structured log via
        # :meth:`_log.exception`; only the capped exception message
        # reaches the response body.
        detail = _build_fingerprint_failed_detail(exc=exc, cls=cls, target_name=t.name)
        _log.exception(
            "probe_fingerprint_failed",
            target_id=str(t.id),
            target_name=t.name,
            tenant_id=str(operator.tenant_id),
            connector_id=detail["connector_id"],
            exception_class=detail["exception_class"],
        )
        raise HTTPException(status_code=500, detail=detail) from exc
    # ``model_dump(mode='json')`` produces a JSONB-safe dict (datetime
    # → ISO string, enum → value, UUID → str). Plain ``model_dump()``
    # would leak Python-native types into the JSON column, breaking
    # round-tripping through PG's JSONB binary representation.
    t.fingerprint = fp.model_dump(mode="json")
    # Refresh ``updated_at`` on every successful probe persist so the
    # row's write-tracking matches the sibling ``update_target`` path
    # (L274) — both routes are the only writers to the row and both
    # must bump the timestamp on every successful mutation. The 501
    # no-connector branch and a connector that raises both leave the
    # row untouched (no flush), so the previously-cached fingerprint
    # and its ``updated_at`` survive.
    t.updated_at = datetime.now(UTC)
    # The outer ``async with session.begin()`` in ``get_session``
    # commits on clean exit; ``flush`` makes the write visible to any
    # follow-up SELECT in this request without forcing an explicit
    # commit (which would close the outer transaction mid-handler).
    await session.flush()
    return fp


@router.post("", response_model=Target, status_code=201)
async def create_target(
    body: TargetCreate,
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_session),
) -> Target:
    """Create a new target in the requesting tenant.

    Returns 409 when a target with the same ``name`` already exists in
    the tenant. The ``id`` and timestamps are generated server-side.
    ``tenant_id`` is always taken from the JWT — the body cannot override
    it.

    G0.14-T3 (#1144). The ``product`` field is validated at request time
    against :func:`~meho_backplane.connectors.registry.registered_product_tokens`.
    An unknown product yields a structured 422 (see
    :func:`_build_unknown_product_detail`); the OpenAPI schema also
    exposes the enum (see :func:`meho_backplane.main.build_openapi_schema`)
    so Swagger / generator tooling surfaces the valid set before the
    request leaves the editor. Both layers share the same source-of-
    truth helper so they cannot drift.

    Validation is skipped when the registry is empty — that state means
    "no connectors imported" (test isolation, or a deploy booted before
    :func:`_eager_import_connectors` ran). In production the lifespan
    populates the registry before the first request arrives, so the
    skip branch never fires; a misregistered deploy that *did* hit it
    is recoverable via T4 #1145's PATCH-product or DELETE path.
    """
    # G0.18-T2 (#1355). Canonicalise + validate the incoming product
    # token in one step so a value copied straight out of
    # ``meho connector list`` is accept-equivalent here. See
    # :func:`_canonicalise_and_validate_product` for the
    # ``sddc``/``sddc-manager`` rationale; the canonical token is
    # what gets stored so the resolver and every downstream read see
    # the registry's spelling regardless of which alias the operator
    # typed.
    product = _canonicalise_and_validate_product(body.product)
    # G0.15-T6 (#1215). Validate ``preferred_impl_id`` against the
    # registered impl set so the resolver-silently-ignores-unknown-id
    # foot-gun surfaces at write time. ``None`` is the absent / cleared
    # state and is always valid; a non-``None`` value must match an
    # impl_id registered in the v2 connector registry **for the
    # target's product** (G0.16-T6 review-iter-1 B1 #1312 -- a
    # cross-product allowlist let a ``k8s`` target pin
    # ``vmware-rest-9.0`` and the resolver would silently ignore it
    # at dispatch). The scope uses the canonical product token so an
    # ``sddc``-aliased create still resolves the SDDC impl set.
    # ``valid_impl_ids`` is empty when no connector is registered for
    # the product (test isolation / pre-lifespan state, or a
    # not-yet-registered product); we skip validation in that case for
    # parity with the ``product`` validator above -- the operator
    # already saw a structured 422 from the product check if their
    # product is unknown to the registry.
    valid_impl_ids = sorted(_registered_impl_ids(product))
    if (
        body.preferred_impl_id is not None
        and valid_impl_ids
        and body.preferred_impl_id not in valid_impl_ids
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_build_unknown_preferred_impl_detail(body.preferred_impl_id, valid_impl_ids),
        )
    now = datetime.now(UTC)
    create_fields = body.model_dump()
    # Persist the canonical product token, not the alias the operator
    # may have supplied, so the stored row matches the registry /
    # resolver spelling (G0.18-T2 #1355).
    create_fields["product"] = product
    t = TargetORM(
        id=uuid.uuid4(),
        tenant_id=operator.tenant_id,
        created_at=now,
        updated_at=now,
        **create_fields,
    )
    session.add(t)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"target {body.name!r} already exists in tenant",
        ) from exc
    # Bind target_id so AuditMiddleware writes audit_log.target_id (G0.3-T4).
    # For create, resolve_target is not called, so we bind directly here.
    structlog.contextvars.bind_contextvars(target_id=str(t.id))
    _log.info(
        "target_created",
        target_id=str(t.id),
        name=t.name,
        tenant_id=str(operator.tenant_id),
    )
    return _to_full(t)


@router.patch("/{name}", response_model=Target)
async def update_target(
    name: str,
    body: TargetUpdate,
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_session),
) -> Target:
    """Partially update a target.

    Only fields present in the request body are modified (Pydantic
    ``exclude_unset``). ``name`` is not patchable — rename a target by
    deleting and re-creating it (v0.2 decision). ``product`` is patchable
    as of G0.14-T4 (#1145); the new value is validated against the
    registered connector products and an unknown product yields a
    structured 422 listing the ``valid_products`` so an operator
    typo-correcting a misregistered target sees the same actionable
    diagnostic at PATCH time that they would see at probe time.
    ``updated_at`` is always refreshed on a successful write.
    """
    t = await resolve_target(session, operator.tenant_id, name)
    updates = body.model_dump(exclude_unset=True)
    # Reject ``{"product": null}`` explicitly. ``Field(default=None)`` on
    # ``TargetUpdate.product`` is the absent-marker for "client did not
    # send this field" -- the only legal way to keep the field optional
    # while supporting PATCH semantics in v1. Without this guard the
    # ``setattr`` loop below assigns ``None`` to ``Target.product`` (NOT
    # NULL) and SQLAlchemy / the database raises an IntegrityError that
    # FastAPI maps to a 500 -- bypassing the T11 error-message-shape
    # contract callers branch on. Mirror the ``unknown_product`` 422
    # shape so the diagnostic stays uniform: a snake_case ``kind``, a
    # human ``message`` naming the offending value + the remediation,
    # and a pointer to the convention doc.
    if "product" in updates and updates["product"] is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "kind": "invalid_null",
                "field": "product",
                "message": (
                    "product cannot be null; targets.product is NOT NULL. "
                    "Omit the field to leave it unchanged, or pass a "
                    "valid product token instead. "
                    "See docs/codebase/error-message-shape.md for the "
                    "convention."
                ),
            },
        )
    # G0.18-T2 (#1355). Canonicalise + validate the patched product
    # token the same way ``create_target`` does so a PATCH that copies
    # ``product`` out of ``meho connector list`` (e.g. ``"sddc"``)
    # lands the canonical registry token (``"sddc-manager"``) rather
    # than 422-ing or storing the alias. The shared helper raises a
    # T11-compliant 422 on unknown products; we only invoke it when
    # the operator actually changes ``product`` so a same-value PATCH
    # short-circuits (matches the pre-T2 behaviour pinned by
    # ``test_patch_product_same_value_passes_without_validator``).
    raw_new_product = updates.get("product")
    new_product = canonical_product_token(raw_new_product) if raw_new_product is not None else None
    if new_product is not None:
        updates["product"] = new_product
    if new_product is not None and new_product != t.product:
        # raw_new_product is the operator's literal input; pass it
        # through so the 422 detail echoes what they typed.
        assert raw_new_product is not None  # guarded by the outer if
        _canonicalise_and_validate_product(raw_new_product)
    # G0.15-T6 (#1215). Same impl_id rejection as on POST. ``None`` is
    # the explicit-clear state and is always valid (operator wants to
    # remove the override); a non-``None`` value must match a
    # registered impl_id **for the target's product** (G0.16-T6
    # review-iter-1 B1 #1312). When the PATCH also changes ``product``,
    # the new product is the relevant scope -- the validator must
    # agree with the post-update row state, otherwise a single PATCH
    # could land an impl_id that the resolver will silently ignore at
    # the next dispatch. Skip when no connector is registered for the
    # effective product (test isolation / pre-lifespan).
    new_preferred = updates.get("preferred_impl_id")
    if new_preferred is not None:
        effective_product = new_product if new_product is not None else t.product
        valid_impl_ids = sorted(_registered_impl_ids(effective_product))
        if valid_impl_ids and new_preferred not in valid_impl_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=_build_unknown_preferred_impl_detail(new_preferred, valid_impl_ids),
            )
    for k, v in updates.items():
        setattr(t, k, v)
    t.updated_at = datetime.now(UTC)
    _log.info(
        "target_updated",
        target_id=str(t.id),
        name=t.name,
        tenant_id=str(operator.tenant_id),
        fields=list(updates.keys()),
    )
    return _to_full(t)


@router.delete(
    "/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        # 409 ``target_has_references`` -- declared explicitly so
        # FastAPI's autogen OpenAPI surfaces the cascade-conflict shape
        # to SDK clients. The route handler below raises ``HTTPException(
        # 409, detail={"kind": "target_has_references", ...})`` when the
        # target is wired into the topology graph and ``?force=true`` is
        # not set. Without this declaration the spec only lists 204 + 422
        # and clients have no schema-driven signal for the recoverable
        # cascade-conflict (the operator's first-line "retry with
        # ?force=true" remediation). Mirrors the
        # ``GET /api/v1/topology/history/{name}`` convention from
        # ``api/v1/topology.py`` -- structured ``detail`` shape with a
        # snake_case ``kind`` discriminator and an inline content schema
        # so the regen'd Go client lands a typed JSON409 wrapper.
        409: {
            "description": (
                "Target is referenced by ``graph_node`` rows; "
                "retry with ``?force=true`` to soft-delete anyway "
                "(``graph_node.target_id`` is ``ON DELETE SET NULL`` so "
                "the topology rows survive). See "
                "``docs/codebase/error-message-shape.md`` for the "
                "convention."
            ),
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "detail": {
                                "type": "object",
                                "properties": {
                                    "kind": {
                                        "type": "string",
                                        "enum": ["target_has_references"],
                                    },
                                    "graph_node_refs": {
                                        "type": "integer",
                                        "minimum": 1,
                                    },
                                    "message": {"type": "string"},
                                },
                                "required": [
                                    "kind",
                                    "graph_node_refs",
                                    "message",
                                ],
                            },
                        },
                        "required": ["detail"],
                    },
                },
            },
        },
    },
)
async def delete_target(
    name: str,
    force: bool = Query(default=False),
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Soft-delete a target.

    G0.14-T4 (#1145) — recovery path for misregistered targets
    (signal 6 in ``claude-rdc-hetzner-dc#697``). Stamps
    ``deleted_at`` on the row instead of removing it so the
    append-only :attr:`AuditLog.target_id` soft-FK keeps pointing
    at *something* (audit is append-only per v0.1-spec §6); every
    read path filters ``deleted_at IS NULL`` so the row is
    invisible to dispatch / probe / list / discover after the
    DELETE returns.

    Cascade-check: counts ``graph_node.target_id`` references
    (G9.1-T1 #448 substrate) and defaults to **409 with the
    count + a ``?force=true`` hint** when the target is wired
    into the topology graph. ``?force=true`` proceeds with the
    soft-delete anyway — the FK is
    ``ON DELETE SET NULL`` so the graph rows survive the delete
    safely; the operator confirms by passing ``force=true``.
    Audit references are not counted because the audit table
    accumulates a row per authenticated request against the
    target and would always block the delete.

    Returns 204 on success. The audit row is written by the
    :class:`~meho_backplane.audit.AuditMiddleware` with
    ``audit_op_id='targets.delete'`` (mirrors the
    ``meho.connector.edit_*`` precedent so cross-tenant audit
    queries can filter by op id prefix).

    Re-deletes collapse to 404 — the second DELETE goes through
    :func:`~meho_backplane.targets.resolver.resolve_target`
    which filters ``deleted_at IS NULL``, so the row no longer
    resolves once the first DELETE flushes.

    ``tenant_admin`` role required (cross-tenant DELETE returns
    the standard 404 conflation that :func:`resolve_target`
    enforces).
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_DELETE_OP_ID,
        audit_op_class="write",
    )
    t = await resolve_target(session, operator.tenant_id, name)

    # Cascade count -- only graph_node references. The audit_log
    # table accumulates a row per authenticated request against
    # the target; counting them would block every DELETE forever
    # and is not the intent of the cascade check.
    count_stmt = (
        select(func.count())
        .select_from(GraphNode)
        .where(
            GraphNode.target_id == t.id,
        )
    )
    graph_node_ref_count = int((await session.execute(count_stmt)).scalar_one())

    if graph_node_ref_count > 0 and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "kind": "target_has_references",
                "graph_node_refs": graph_node_ref_count,
                "message": (
                    f"target {t.name!r} is referenced by "
                    f"{graph_node_ref_count} graph_node row(s); "
                    f"retry with ?force=true to delete anyway "
                    f"(graph_node.target_id is ON DELETE SET NULL "
                    f"so the topology rows survive). "
                    f"See docs/codebase/error-message-shape.md for "
                    f"the convention."
                ),
            },
        )

    t.deleted_at = datetime.now(UTC)
    await session.flush()
    _log.info(
        "target_deleted",
        target_id=str(t.id),
        name=t.name,
        tenant_id=str(operator.tenant_id),
        graph_node_refs=graph_node_ref_count,
        force=force,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
