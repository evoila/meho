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
fingerprint survives. A connector that raises propagates the exception
(the outer ``session.begin()`` in
:func:`~meho_backplane.db.engine.get_session` rolls back), again
leaving the row untouched. The column therefore always reflects the
*last successful* probe. The target must exist for the probe to fire —
a non-existent target returns 404 via ``resolve_target``.

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

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.resolver import resolve_connector_or_label
from meho_backplane.connectors.schemas import AuthModel, CandidateHint, FingerprintResult
from meho_backplane.db.engine import get_session
from meho_backplane.db.models import GraphNode
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.targets.resolver import resolve_target
from meho_backplane.targets.schemas import Target, TargetCreate, TargetSummary, TargetUpdate

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


def _to_summary(t: TargetORM) -> TargetSummary:
    return TargetSummary(
        id=t.id,
        name=t.name,
        # ORM stores aliases as ``list[str]`` (mutable JSON column);
        # the response schema declares ``tuple[str, ...]`` for
        # frozen-model immutability. Coerce at the boundary.
        aliases=tuple(t.aliases),
        product=t.product,
        host=t.host,
    )


def _to_full(t: TargetORM) -> Target:
    return Target(
        id=t.id,
        tenant_id=t.tenant_id,
        name=t.name,
        aliases=tuple(t.aliases),
        product=t.product,
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


@router.get("", response_model=list[TargetSummary])
async def list_targets(
    product: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    operator: Operator = _require_operator,
    session: AsyncSession = Depends(get_session),
) -> list[TargetSummary]:
    """List targets for the requesting tenant.

    Results are keyset-paginated by ``name`` (lexicographic order).
    Pass ``cursor=<last-name-seen>`` to fetch the next page. The
    ``product`` filter is exact-match; pass it to narrow by product
    slug. ``limit`` defaults to 100, max 500.

    Soft-deleted targets (``deleted_at IS NOT NULL``, G0.14-T4 #1145)
    are excluded from the list — the same filter the resolver applies
    so list and dispatch never disagree about which targets are
    visible to the tenant.
    """
    stmt = select(TargetORM).where(
        TargetORM.tenant_id == operator.tenant_id,
        TargetORM.deleted_at.is_(None),
    )
    if product is not None:
        stmt = stmt.where(TargetORM.product == product)
    if cursor is not None:
        stmt = stmt.where(TargetORM.name > cursor)
    stmt = stmt.order_by(TargetORM.name).limit(limit)
    result = await session.execute(stmt)
    return [_to_summary(t) for t in result.scalars().all()]


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
    can pick an implementation without re-probing the live target. Per
    the 2026-05-14 amendment to Initiative #224 the probe verb is the
    *only* writer to the column — :class:`TargetCreate` and
    :class:`TargetUpdate` reject the field with 422.

    Connector selection uses
    :func:`~meho_backplane.connectors.resolver.resolve_connector_or_label`
    so this route and the dispatcher's connector-resolution step
    consult the same v2 registry through the same tie-break ladder
    (G0.14-T1 #1142). Returns:

    * **501** when the resolver reports ``no_connector`` (no registered
      impl matches the target's ``(product, version)``). ``detail``
      carries the resolver's exception message naming the target's
      product slug + the absence of a matching candidate.
    * **409** when the resolver reports ``ambiguous_connector`` (two
      or more impls tied through the full ladder). ``detail`` carries
      the resolver's exception message naming the candidate set and
      the remediation step — set ``target.preferred_impl_id`` to one
      of them. 409 mirrors REST's "the request can't be processed due
      to current resource state" semantics: the operator has to
      disambiguate the target before this verb can resolve a single
      connector.

    Neither error branch writes to the DB row; the previously-cached
    fingerprint (if any) survives. A connector that raises propagates
    the exception (rolling back the outer transaction); the row again
    stays untouched. The column therefore always reflects the *last
    successful* probe.
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
    fp = await cls().fingerprint(t)
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
    """
    now = datetime.now(UTC)
    t = TargetORM(
        id=uuid.uuid4(),
        tenant_id=operator.tenant_id,
        created_at=now,
        updated_at=now,
        **body.model_dump(),
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
    new_product = updates.get("product")
    if new_product is not None and new_product != t.product:
        # Mirror the probe / dispatch resolver's notion of "valid
        # product" at PATCH time so a typo-correcting operator does
        # not trade an immediately-broken target for a target that
        # will surface as 501 at the next probe. The structured
        # detail follows ``docs/codebase/error-message-shape.md`` --
        # a snake_case code, a human-readable message naming the
        # offending value and the remediation step, and a machine-
        # actionable ``valid_products`` list the client can branch
        # on.
        valid_products = sorted(_registered_products())
        if new_product not in valid_products:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "kind": "unknown_product",
                    "product": new_product,
                    "valid_products": valid_products,
                    "message": (
                        f"product={new_product!r} is not registered; "
                        f"pick one of {valid_products!r} or register a "
                        f"connector for it before retrying. "
                        f"See docs/codebase/error-message-shape.md for "
                        f"the convention."
                    ),
                },
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
