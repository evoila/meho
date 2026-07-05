# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Review-queue state machine for ingested connectors (G0.7-T4).

A newly-ingested connector lands in ``review_status='staged'``: every
:class:`~meho_backplane.db.models.OperationGroup` row is staged and
every child :class:`~meho_backplane.db.models.EndpointDescriptor` row
is ``is_enabled=False``. The agent never sees staged operations. An
operator must review the LLM-generated groups + per-op metadata and
then flip the connector to ``review_status='enabled'`` before any of
its operations become dispatchable. Regressions roll back via
``review_status='disabled'``.

State machine
-------------

::

    staged    → enabled   via enable_connector / enable_group
    staged    → disabled  via disable_connector  (bad-ingest reset; rare)
    enabled   → disabled  via disable_connector  (regression rollback)
    disabled  → enabled   via enable_connector / enable_group  (re-enable after fix)
    enabled   → staged    NOT ALLOWED — operators cannot downgrade to
                          "needs review again". If a re-review is required,
                          disable, then re-ingest from scratch.

The ``is_enabled`` flag on each child
:class:`~meho_backplane.db.models.EndpointDescriptor` row is tied to
its group's ``review_status`` by default: a group moving to
``enabled`` cascades ``is_enabled=True`` onto every child row, and
a group moving to ``disabled`` (or being created in ``staged``)
cascades ``is_enabled=False``. There is **one** explicit exception:
when an operator has set ``is_enabled=False`` on a single op via
:meth:`ReviewService.edit_op` (a "per-op override"), a subsequent
``enable_connector`` does NOT clobber that operator override. The
override is recovered from the audit log
(:func:`~meho_backplane.operations.ingest._internals.operator_disabled_op_ids`)
rather than tracked in a dedicated column — the latter is the
v0.2.next refinement once audit growth makes the per-call scan
expensive.

Audit emission
--------------

Every state-mutating method writes exactly **one**
:class:`~meho_backplane.db.models.AuditLog` row when at least one DB
row actually changes. An idempotent re-invocation is a true no-op:
no rows change, no audit row written.

API authorisation
-----------------

The service is constructed with an
:class:`~meho_backplane.auth.operator.Operator`. Every method takes
an explicit ``tenant_id: UUID | None`` parameter naming the scope to
operate on:

* ``tenant_id == operator.tenant_id`` — always allowed.
* ``tenant_id is None`` (built-in scope) — allowed iff the operator
  carries :class:`~meho_backplane.auth.operator.TenantRole.TENANT_ADMIN`.
* Any other ``tenant_id`` — raises :class:`ConnectorNotFoundError`,
  regardless of role. Cross-tenant probing surfaces no information.

The originating Task (#402) shows
:meth:`get_review_payload` taking an explicit ``tenant_id`` parameter
but the other methods omitting it. This implementation adds
``tenant_id`` to **every** method as a keyword-only argument so
built-in and tenant-curated mutations have a uniform call shape —
without it, no caller could disambiguate between built-in and
operator's-tenant rows when both exist.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations.ingest._internals import (
    OP_DELETE_CONNECTOR,
    OP_DISABLE_CONNECTOR,
    OP_EDIT_GROUP,
    OP_EDIT_OP,
    OP_ENABLE_CONNECTOR,
    OP_ENABLE_GROUP,
    OP_ENABLE_READS,
    ConnectorScope,
    apply_op_overrides,
    audit_profile_stamp,
    bulk_enable_read_ops,
    cascade_is_enabled,
    count_ops_in_scope,
    enable_time_auto_shim_warnings,
    load_group,
    load_groups,
    load_op,
    load_ops_in_groups,
    operator_disabled_op_ids,
    scope_has_groups,
    validate_edit_op_args,
    write_audit_row,
)
from meho_backplane.operations.ingest.api_schemas import EditOpWarning
from meho_backplane.operations.ingest.connector_registration import resolve_authoring_kind
from meho_backplane.operations.ingest.delete_connector import (
    DeleteConnectorResult,
    deregister_staged_auto_shims,
    stage_connector_delete,
)
from meho_backplane.operations.ingest.exceptions import (
    AmbiguousConnectorScopeError,
    ConnectorNotFoundError,
    ConnectorScopeCandidate,
    InvalidStateTransitionError,
)
from meho_backplane.operations.ingest.parser import parse_connector_id
from meho_backplane.operations.ingest.payload import (
    ConnectorReviewGroup,
    ConnectorReviewOp,
    ConnectorReviewPayload,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.base import Connector
    from meho_backplane.operations.ingest.api_schemas import ConnectorAuthoringKind

__all__ = ["ReviewService"]

_log = structlog.get_logger(__name__)


def _authoring_kind_for_payload(
    scope: ConnectorScope,
    rendered_groups: list[ConnectorReviewGroup],
) -> tuple[ConnectorAuthoringKind, bool]:
    """Project the review payload's ``(kind, dispatchable)`` for *scope* (#1979).

    The review gate (#1971) is "cleared" once any op is enabled; the
    enabled-op count is derived from the already-rendered groups so no
    second DB round-trip is needed. Delegates the resolver replay + tier
    mapping to :func:`resolve_authoring_kind`.
    """
    enabled_op_count = sum(1 for group in rendered_groups for op in group.ops if op.is_enabled)
    return resolve_authoring_kind(
        product=scope.product,
        version=scope.version,
        enabled_operation_count=enabled_op_count,
    )


class ReviewService:
    """Manage the review-queue state machine for ingested connectors.

    Constructed with an :class:`Operator` and, optionally, a
    sessionmaker. The default sessionmaker comes from
    :func:`~meho_backplane.db.engine.get_sessionmaker`; tests pass a
    per-test maker that points at the SQLite tmp DB.

    Every state-mutating method opens its own transaction. State
    changes and the audit-log row commit together: if either
    fails, neither lands. Read-only :meth:`get_review_payload`
    opens its own read transaction and writes no service-level
    audit row — the surrounding HTTP / MCP request is audited by
    the chassis middleware.
    """

    def __init__(
        self,
        operator: Operator,
        *,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._operator = operator
        # Resolve lazily on first use rather than at construction:
        # the chassis engine cache is reset between tests, and a
        # service constructed in fixture setup would otherwise
        # hold a sessionmaker pointing at a disposed engine.
        self._explicit_sessionmaker = sessionmaker

    def _sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        if self._explicit_sessionmaker is not None:
            return self._explicit_sessionmaker
        return get_sessionmaker()

    # -- scope resolution --------------------------------------------------

    def _authorize_scope(
        self,
        connector_id: str,
        tenant_id: UUID | None,
    ) -> None:
        """Raise :class:`ConnectorNotFoundError` if the operator can't act on the scope.

        Three failure modes collapse into one exception: built-in
        scope without tenant_admin, cross-tenant access, and the
        "connector genuinely doesn't exist" case (detected later
        at the DB-query site). The unified exception class prevents
        an unprivileged operator from enumerating other tenants
        via status-code differential.
        """
        if tenant_id is None:
            if self._operator.tenant_role is not TenantRole.TENANT_ADMIN:
                raise ConnectorNotFoundError(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                )
            return
        if tenant_id != self._operator.tenant_id:
            raise ConnectorNotFoundError(
                connector_id=connector_id,
                tenant_id=tenant_id,
            )

    def _resolve_scope(
        self,
        connector_id: str,
        tenant_id: UUID | None,
    ) -> ConnectorScope:
        """Authorise + parse a ``(connector_id, tenant_id)`` pair.

        A parse failure surfaces as :class:`ConnectorNotFoundError`
        rather than the raw :class:`ValueError`: from the operator's
        perspective, an unparseable identifier is indistinguishable
        from a non-existent connector.
        """
        self._authorize_scope(connector_id, tenant_id)
        try:
            product, version, impl_id = parse_connector_id(connector_id)
        except ValueError:
            _log.info(
                "review_service_parse_failed",
                connector_id=connector_id,
            )
            raise ConnectorNotFoundError(
                connector_id=connector_id,
                tenant_id=tenant_id,
            ) from None
        return ConnectorScope(
            product=product,
            version=version,
            impl_id=impl_id,
            tenant_id=tenant_id,
        )

    async def _resolve_preferred_scope(
        self,
        connector_id: str,
        tenant_id: UUID | None,
        scope: ConnectorScope,
        session: AsyncSession,
        *,
        prefer: Literal["tenant", "builtin"],
    ) -> ConnectorScope:
        """Resolve a connector to the explicitly-``prefer``-named scope (#2029).

        The operator passed ``prefer`` to disambiguate a label that maps
        to both a tenant-curated row and a built-in row, so this skips
        the ambiguity probe entirely and resolves directly to the named
        scope. *scope* is the already-parsed tenant scope from
        :meth:`_resolve_scope` (carrying the operator's *tenant_id*).

        * ``prefer == "tenant"`` — the tenant scope. ``tenant_id is None``
          has no tenant row to prefer, so it is a miss; otherwise the
          tenant row must hold rows or :class:`ConnectorNotFoundError` is
          raised. There is **no** built-in fall-back: an explicit
          ``prefer=tenant`` against a built-in-only label is a genuine
          miss, not a silent slide to the built-in row.
        * ``prefer == "builtin"`` — the built-in (``tenant_id IS NULL``)
          scope, which must hold rows or :class:`ConnectorNotFoundError`.
          No extra gate here: this mirrors the un-preferred built-in
          fall-back in :meth:`_resolve_existing_scope`, which returns the
          built-in scope to a tenant operator without re-gating — built-in
          *reads* are operator-level by design (matching the list endpoint
          + the #1135 read fall-back), and built-in *writes*
          (:meth:`enable_reads`) are already ``tenant_admin``-gated at the
          surface (the REST route's ``_require_admin`` dependency and the
          MCP tool's ``required_role=TENANT_ADMIN``). So ``prefer=builtin``
          grants no access the operator did not already have on the
          built-in scope through the un-preferred path.
        """
        if prefer == "builtin":
            builtin_scope = ConnectorScope(
                product=scope.product,
                version=scope.version,
                impl_id=scope.impl_id,
                tenant_id=None,
            )
            if await scope_has_groups(session, builtin_scope):
                return builtin_scope
            raise ConnectorNotFoundError(
                connector_id=connector_id,
                tenant_id=None,
            )
        # prefer == "tenant"
        if tenant_id is not None and await scope_has_groups(session, scope):
            return scope
        raise ConnectorNotFoundError(
            connector_id=connector_id,
            tenant_id=tenant_id,
        )

    async def _resolve_existing_scope(
        self,
        connector_id: str,
        tenant_id: UUID | None,
        session: AsyncSession,
        *,
        prefer: Literal["tenant", "builtin"] | None = None,
    ) -> ConnectorScope:
        """Resolve ``(connector_id, tenant_id)`` to the one row-scope to act on.

        The single resolution path the read (:meth:`get_review_payload`)
        and write (:meth:`enable_reads`) actions share so they can never
        diverge on which row they touch (G0.26-T1 #1801). Authorises +
        parses via :meth:`_resolve_scope`, then probes the database for
        which scope actually holds rows:

        * ``tenant_id is None`` (the MCP admin path's explicit built-in
          probe) — single-pass: the built-in scope when rows exist, else
          :class:`ConnectorNotFoundError`. No tenant fall-back, no
          ambiguity.
        * ``tenant_id == operator.tenant_id`` (the daily-driver path) —
          two-scope probe: both a tenant-curated **and** a built-in row
          exist → :class:`AmbiguousConnectorScopeError` (no silent pick —
          the footgun #1801 closes); only one exists → that scope (the
          built-in-only case is the G0.13-T5 #1135 global fall-back,
          now shared with writes and intentionally operator-readable —
          writes are ``tenant_admin``-gated at the route); neither →
          :class:`ConnectorNotFoundError`.

        ``prefer`` (G0.26-T? #2029) makes the ambiguity *actionable*
        without weakening the #1801 fail-loud default: when set it routes
        through :meth:`_resolve_preferred_scope` (which skips the probe
        and resolves directly to the named scope); when ``None`` the
        probe + fail-loud raise below is byte-identical to the pre-#2029
        resolver.

        A cross-tenant ``tenant_id`` (≠ the operator's, not ``None``)
        never reaches this method: :meth:`_resolve_scope` ->
        :meth:`_authorize_scope` already collapsed it into
        :class:`ConnectorNotFoundError`, preserving the cross-tenant
        404 conflation.
        """
        scope = self._resolve_scope(connector_id, tenant_id)
        if prefer is not None:
            return await self._resolve_preferred_scope(
                connector_id,
                tenant_id,
                scope,
                session,
                prefer=prefer,
            )
        if tenant_id is None:
            # Explicit built-in probe (admin path) — single-pass, no
            # fallback, no ambiguity.
            if await scope_has_groups(session, scope):
                return scope
            raise ConnectorNotFoundError(
                connector_id=connector_id,
                tenant_id=tenant_id,
            )
        builtin_scope = ConnectorScope(
            product=scope.product,
            version=scope.version,
            impl_id=scope.impl_id,
            tenant_id=None,
        )
        tenant_exists = await scope_has_groups(session, scope)
        builtin_exists = await scope_has_groups(session, builtin_scope)
        if tenant_exists and builtin_exists:
            raise AmbiguousConnectorScopeError(
                connector_id=connector_id,
                candidates=[
                    ConnectorScopeCandidate(
                        product=scope.product,
                        version=scope.version,
                        impl_id=scope.impl_id,
                        tenant_id=scope.tenant_id,
                    ),
                    ConnectorScopeCandidate(
                        product=builtin_scope.product,
                        version=builtin_scope.version,
                        impl_id=builtin_scope.impl_id,
                        tenant_id=None,
                    ),
                ],
            )
        if tenant_exists:
            return scope
        if builtin_exists:
            return builtin_scope
        raise ConnectorNotFoundError(
            connector_id=connector_id,
            tenant_id=tenant_id,
        )

    # -- public read API ---------------------------------------------------

    async def get_review_payload(
        self,
        connector_id: str,
        tenant_id: UUID | None,
        *,
        prefer: Literal["tenant", "builtin"] | None = None,
    ) -> ConnectorReviewPayload:
        """Return the review payload for *connector_id* in *tenant_id*.

        Loads every group + every child op and packs them into a
        :class:`ConnectorReviewPayload`. Groups are sorted by
        ``group_key`` and ops by ``op_id`` so consecutive calls
        produce identical payloads.

        Empty group set → :class:`ConnectorNotFoundError`. A group
        with zero ops is legal and renders as ``op_count=0,
        ops=[]``.

        No audit row is written; the surrounding HTTP / MCP request
        already audits the read at the chassis layer.

        Visibility scope mirrors :func:`list_ingested_connectors`:
        an operator sees rows under their own tenant **and** built-in
        (``tenant_id IS NULL``) rows. Resolution runs through the
        shared :meth:`_resolve_existing_scope` so this read path and the
        :meth:`enable_reads` write path resolve the **same** row for the
        same ``(connector_id, tenant_id)`` — including the global
        fallback. When *tenant_id* equals the operator's tenant and only
        the built-in row exists, the lookup resolves to ``tenant_id IS
        NULL`` so a ``GET /api/v1/connectors/{id}/review`` against a
        global connector returns 200 (G0.13-T5 #1135). When **both** a
        tenant row and a built-in row exist for the label,
        :class:`AmbiguousConnectorScopeError` is raised rather than silently
        picking one (G0.26-T1 #1801). When *tenant_id* is passed
        explicitly as ``None`` (MCP admin path), the existing admin-only
        gate stays and the probe is single-pass; a cross-tenant probe
        keeps the 404 conflation.

        *prefer* (G0.26-T? #2029) disambiguates that 409 without
        weakening the default: ``prefer="tenant"`` returns the tenant
        row directly, ``prefer="builtin"`` the built-in row, and
        ``prefer=None`` (the default) keeps the fail-loud probe. See
        :meth:`_resolve_existing_scope`.
        """
        sessionmaker = self._sessionmaker()
        async with sessionmaker() as session:
            scope = await self._resolve_existing_scope(
                connector_id,
                tenant_id,
                session,
                prefer=prefer,
            )
            return await self._render_payload(connector_id, scope, session)

    async def _render_payload(
        self,
        connector_id: str,
        scope: ConnectorScope,
        session: AsyncSession,
    ) -> ConnectorReviewPayload:
        """Load groups + ops for *scope* and pack them into the payload.

        Raises :class:`ConnectorNotFoundError` when no group rows
        exist under *scope* (a defensive guard — the caller resolves
        the scope via :meth:`_resolve_existing_scope` first, so on the
        normal path rows are present). The caller owns the open
        *session* / transaction so scope resolution and payload
        rendering observe one consistent snapshot.
        """
        groups = await load_groups(session, scope, connector_id)
        ops = await load_ops_in_groups(
            session,
            scope,
            [group.id for group in groups],
        )
        ops_by_group: dict[UUID, list[Any]] = {group.id: [] for group in groups}
        for op in ops:
            if op.group_id is not None and op.group_id in ops_by_group:
                ops_by_group[op.group_id].append(op)
        rendered_groups = [
            ConnectorReviewGroup(
                group_key=group.group_key,
                name=group.name,
                when_to_use=group.when_to_use,
                review_status=group.review_status,
                op_count=len(ops_by_group[group.id]),
                ops=[
                    ConnectorReviewOp(
                        op_id=op.op_id,
                        summary=op.summary,
                        description=op.description,
                        custom_description=op.custom_description,
                        safety_level=op.safety_level,
                        requires_approval=op.requires_approval,
                        is_enabled=op.is_enabled,
                        tags=list(op.tags),
                    )
                    for op in ops_by_group[group.id]
                ],
            )
            for group in groups
        ]
        kind, dispatchable = _authoring_kind_for_payload(scope, rendered_groups)
        grouped_op_count = sum(group.op_count for group in rendered_groups)
        # #125: count the full descriptor universe (the same one the
        # ``GET /api/v1/connectors`` listing counts) so ops not in a rendered
        # group aren't silently dropped from the connector's reported total.
        # ``ungrouped_op_count`` is the reconciling remainder:
        # ``total_op_count + ungrouped_op_count == listing.operation_count``.
        all_op_count = await count_ops_in_scope(session, scope)
        return ConnectorReviewPayload(
            connector_id=connector_id,
            product=scope.product,
            version=scope.version,
            impl_id=scope.impl_id,
            tenant_id=scope.tenant_id,
            groups=rendered_groups,
            total_op_count=grouped_op_count,
            ungrouped_op_count=max(all_op_count - grouped_op_count, 0),
            kind=kind,
            dispatchable=dispatchable,
        )

    # -- public write API: edits ------------------------------------------

    async def edit_group(
        self,
        connector_id: str,
        group_key: str,
        *,
        tenant_id: UUID | None,
        when_to_use: str | None = None,
        name: str | None = None,
    ) -> None:
        """Update the operator-editable fields on one :class:`OperationGroup`.

        Passing neither ``when_to_use`` nor ``name`` raises
        :class:`ValueError`. ``None`` for a single field leaves it
        unchanged. Writes one audit row with payload
        ``{connector_id, group_key, fields_updated}``. The new
        values are deliberately NOT echoed into the payload
        (operator-authored prose can be long enough to bloat the
        audit table without value).
        """
        if when_to_use is None and name is None:
            raise ValueError(
                "edit_group requires at least one of when_to_use or name",
            )
        scope = self._resolve_scope(connector_id, tenant_id)
        sessionmaker = self._sessionmaker()
        async with sessionmaker() as session:
            group = await load_group(session, scope, connector_id, group_key)
            fields_updated: list[str] = []
            if when_to_use is not None:
                group.when_to_use = when_to_use
                fields_updated.append("when_to_use")
            if name is not None:
                group.name = name
                fields_updated.append("name")
            await write_audit_row(
                session,
                operator_sub=self._operator.sub,
                operator_tenant_id=self._operator.tenant_id,
                op_id=OP_EDIT_GROUP,
                payload={
                    "connector_id": connector_id,
                    "group_key": group_key,
                    "fields_updated": fields_updated,
                },
            )
            await session.commit()

    async def edit_op(
        self,
        connector_id: str,
        op_id: str,
        *,
        tenant_id: UUID | None,
        custom_description: str | None = None,
        safety_level: Literal["safe", "caution", "dangerous"] | None = None,
        requires_approval: bool | None = None,
        is_enabled: bool | None = None,
        llm_instructions: dict[str, object] | None = None,
    ) -> list[EditOpWarning]:
        """Update operator-controlled overrides on one :class:`EndpointDescriptor`.

        Passing none of the five fields raises :class:`ValueError`.
        Out-of-enum ``safety_level`` raises :class:`ValueError`.

        Returns :class:`EditOpWarning` advisories — empty on the
        common path. ``is_enabled=True`` runs the enable-time
        auto-shim probe (G0.23-T4 #1630): a shim-resolved op is a
        guaranteed ``connector_unsupported`` /
        ``cause='unreplaced_auto_shim'`` dispatch dead end (G0.23-T1
        #1627), so the advisory names the missing per-product
        subclass up-front. Never blocks the write — flag set, audit
        row written, warnings or not.

        ``is_enabled`` override is load-bearing for post-enable
        behaviour: once an operator sets ``is_enabled=False`` on a
        single op, a subsequent :meth:`enable_connector` MUST NOT
        clobber it back to True. The cascade in
        :meth:`enable_connector` consults the audit log to find
        operator-set False rows and skips them.

        ``llm_instructions`` is the agent-facing per-op guidance blob
        ingested rows land with as ``NULL``. The typed connectors
        populate it at ``register_typed_operation`` time
        (see e.g. :mod:`meho_backplane.connectors.vault.ops` and
        :mod:`meho_backplane.connectors.bind9.ops_zone`); for
        ingested connectors the operator-review pass writes the same
        shape via this method so :func:`search_operations` and the
        agent's ``call_operation`` see populated guidance once the
        op is enabled. The argument is a plain ``dict`` (the model's
        ``llm_instructions`` column is ``JSON``); the helper persists
        it as-is. Passing ``{}`` clears the column to an empty
        mapping rather than re-deleting it — operators wanting to
        un-author the field should disable the op instead.

        Writes one audit row with payload
        ``{connector_id, op_id, fields_updated, is_enabled_set_to?}``.
        The ``is_enabled_set_to`` key is included verbatim only
        when ``is_enabled`` was edited — that's the key the cascade
        query reads. The ``llm_instructions`` value itself is NOT
        echoed into the payload (operator-authored blobs are big
        enough to bloat the audit table without value, same
        rationale ``edit_group`` uses for ``when_to_use``).
        """
        validate_edit_op_args(
            custom_description=custom_description,
            safety_level=safety_level,
            requires_approval=requires_approval,
            is_enabled=is_enabled,
            llm_instructions=llm_instructions,
        )
        scope = self._resolve_scope(connector_id, tenant_id)
        sessionmaker = self._sessionmaker()
        async with sessionmaker() as session:
            op_row = await load_op(session, scope, connector_id, op_id)
            fields_updated = apply_op_overrides(
                op_row,
                custom_description=custom_description,
                safety_level=safety_level,
                requires_approval=requires_approval,
                is_enabled=is_enabled,
                llm_instructions=llm_instructions,
            )
            payload: dict[str, Any] = {
                "connector_id": connector_id,
                "op_id": op_id,
                "fields_updated": fields_updated,
            }
            if is_enabled is not None:
                payload["is_enabled_set_to"] = is_enabled
            await write_audit_row(
                session,
                operator_sub=self._operator.sub,
                operator_tenant_id=self._operator.tenant_id,
                op_id=OP_EDIT_OP,
                payload=payload,
            )
            await session.commit()
        # Probe only after the write landed: a 404/400 path above must
        # never emit an advisory for an edit that didn't happen.
        if is_enabled is True:
            return enable_time_auto_shim_warnings(connector_id, op_id, scope)
        return []

    # -- public write API: profile stamping -------------------------------

    async def record_profile_stamp(
        self,
        connector_id: str,
        *,
        tenant_id: UUID | None,
        connector_class: type[Connector],
    ) -> bool:
        """Stamp an :class:`ExecutionProfile` onto an ingested connector.

        G0.28-T5 (#1971). The review-gate interlock seam: stamping makes
        the connector **dispatchable** (registers *connector_class* — a
        :class:`~meho_backplane.connectors.profiled.ProfiledRestConnector`
        carrying the vetted profile — under the connector's ``(product,
        version, impl_id)`` v2 key) but deliberately does **not** touch
        any op's ``is_enabled`` / ``review_status``. Every op stays
        ``is_enabled=False`` / ``review_status='staged'`` exactly as it
        was ingested; dispatch against an unreviewed op is blocked by the
        ``is_enabled`` filter in
        :func:`~meho_backplane.operations._lookup.lookup_descriptor` just
        as a staged bare-shim op is, until an operator clears the gate
        per-op via :meth:`edit_op` (or connector-wide via
        :meth:`enable_connector`). A stamp can therefore never auto-enable
        dispatch — the security-load-bearing property of #1971.

        Idempotent: a profiled connector already registered for the
        triple is a no-op (returns ``False``, writes no audit row), so a
        re-stamp does not double-audit. Returns ``True`` on the first
        stamp, when the registration landed and an :data:`OP_PROFILE_STAMP`
        audit row was written.

        Raises :class:`TypeError` when *connector_class* is not a
        ``ProfiledRestConnector`` — only a profiled class carries an
        ``ExecutionProfile`` to stamp; a bare shim or hand-coded class is
        a programming error here, not a review action.

        The registration's audit row commits in the same transaction as
        the v2-registry write would conceptually pair with; the v2
        registry itself is a process-global, not a DB row, so the audit
        row is the durable, attributable record of the dispatchability
        change.
        """
        from meho_backplane.connectors.base import shim_kind
        from meho_backplane.connectors.registry import all_connectors_v2, register_connector_v2

        if shim_kind(connector_class) != "profiled":
            raise TypeError(
                f"record_profile_stamp requires a ProfiledRestConnector "
                f"(shim_kind == 'profiled'); got {connector_class.__name__!r} "
                f"(shim_kind == {shim_kind(connector_class)!r})"
            )

        scope = self._resolve_scope(connector_id, tenant_id)
        triple = (scope.product, scope.version, scope.impl_id)
        if triple in all_connectors_v2():
            # Already stamped (or a hand-coded/bare class occupies the
            # key) — registering again would raise; treat as idempotent.
            return False

        register_connector_v2(
            product=scope.product,
            version=scope.version,
            impl_id=scope.impl_id,
            cls=connector_class,
        )
        sessionmaker = self._sessionmaker()
        async with sessionmaker() as session:
            await audit_profile_stamp(
                session,
                operator_sub=self._operator.sub,
                operator_tenant_id=self._operator.tenant_id,
                connector_id=connector_id,
                scope=scope,
                connector_class=connector_class.__name__,
            )
            await session.commit()
        _log.info(
            "connector_profile_stamped",
            connector_id=connector_id,
            product=scope.product,
            version=scope.version,
            impl_id=scope.impl_id,
            connector_class=connector_class.__name__,
        )
        return True

    # -- public write API: state transitions ------------------------------

    async def enable_connector(
        self,
        connector_id: str,
        *,
        tenant_id: UUID | None,
    ) -> None:
        """Transition every group in *connector_id* to ``'enabled'``.

        Allowed source states: ``staged`` or ``disabled``. Already-
        enabled groups are no-op'd; if every group is already
        enabled, no audit row is written (idempotent). Any group
        in any other state raises :class:`InvalidStateTransitionError`
        (defensive — the DB CHECK constraint prevents other values).

        Cascade rule: every child op gets ``is_enabled=True``,
        except rows whose most-recent ``edit_op`` audit row set
        ``is_enabled=False`` (operator override).
        """
        await self._transition_connector(
            connector_id,
            tenant_id=tenant_id,
            target_status="enabled",
            allowed_source=("staged", "disabled"),
            op_id=OP_ENABLE_CONNECTOR,
        )

    async def enable_reads(
        self,
        connector_id: str,
        *,
        tenant_id: UUID | None,
        prefer: Literal["tenant", "builtin"] | None = None,
    ) -> int:
        """Enable every read-class (GET/HEAD) ingested op in one pass (G0.25-T7 #1749).

        Flips ``is_enabled=True`` on every ingested operation whose
        HTTP method is ``GET`` or ``HEAD`` (:data:`READ_HTTP_METHODS`),
        leaving every write-shaped verb (POST / PUT / PATCH / DELETE)
        and every typed / composite op **default-deny**. The point is
        broad governed *read* coverage on big ingested surfaces
        (``vmware-rest-9.0`` is 3000+ ops, mostly staged GETs) without
        a per-op death-march; writes keep their per-op / composite
        curation by design — that's the governance boundary.

        Returns the number of ops enabled. Unlike
        :meth:`enable_connector`, this does **not** move any group's
        ``review_status`` — it is purely a per-op ``is_enabled`` flip
        (the dumb substrate the agent's ``search_operations`` /
        dispatch path reads), so a connector can stay ``staged`` at the
        group level while its reads are dispatchable, the same way
        :meth:`edit_op` flips one op without a group transition.

        Scope-aware and idempotent. Resolution runs through the shared
        :meth:`_resolve_existing_scope`, so this write path resolves the
        **same** row the :meth:`get_review_payload` read path does for a
        given ``(connector_id, tenant_id)`` — including the G0.13-T5
        #1135 global fallback (a label that exists only as a built-in
        row enables its reads instead of 404'ing, closing the read/write
        asymmetry #1801 was filed for). The connector must exist in the
        resolved scope or :class:`ConnectorNotFoundError` is raised (the
        same 404 conflation every other method uses); when a label maps
        to **both** a tenant row and a built-in row,
        :class:`AmbiguousConnectorScopeError` is raised rather than silently
        flipping one. Exactly **one** ``meho.connector.enable_reads``
        audit row is written, carrying ``ops_enabled_count``, and only
        when at least one op actually flipped: a re-run once the reads
        are enabled matches no rows, writes no audit row, and returns
        ``0``.

        *prefer* (G0.26-T? #2029) resolves the ambiguous-scope 409
        directly: ``prefer="tenant"`` applies to the tenant row,
        ``prefer="builtin"`` to the built-in row (still behind the
        ``tenant_admin`` gate :meth:`_resolve_existing_scope` re-checks),
        and ``prefer=None`` (the default) keeps the fail-loud raise.
        """
        sessionmaker = self._sessionmaker()
        async with sessionmaker() as session:
            # Resolve the exact row-scope (else 404 / ambiguous) before
            # the blind bulk UPDATE — the UPDATE's rowcount alone can't
            # tell "no read ops to flip" from "no such connector", and
            # resolving here is what keeps this write path's target row
            # identical to the /review read path's.
            scope = await self._resolve_existing_scope(
                connector_id,
                tenant_id,
                session,
                prefer=prefer,
            )
            ops_enabled = await bulk_enable_read_ops(session, scope)
            if ops_enabled == 0:
                # Idempotent no-op: nothing changed, so write no audit
                # row (mirrors the enable/disable transition contract).
                return 0
            await write_audit_row(
                session,
                operator_sub=self._operator.sub,
                operator_tenant_id=self._operator.tenant_id,
                op_id=OP_ENABLE_READS,
                payload={
                    "connector_id": connector_id,
                    "ops_enabled_count": ops_enabled,
                },
            )
            await session.commit()
        return ops_enabled

    async def disable_connector(
        self,
        connector_id: str,
        *,
        tenant_id: UUID | None,
    ) -> None:
        """Transition every group in *connector_id* to ``'disabled'``.

        Allowed source states: ``staged`` or ``enabled``. Re-running
        on a fully-disabled connector is a no-op.

        Cascade: every child op gets ``is_enabled=False``. Unlike
        the enable cascade, the disable cascade does NOT consult
        operator overrides — a connector-level disable is a
        regression rollback / bad-ingest reset and the operator's
        intent overrides their own earlier per-op overrides for
        the duration of the disabled state.
        """
        await self._transition_connector(
            connector_id,
            tenant_id=tenant_id,
            target_status="disabled",
            allowed_source=("staged", "enabled"),
            op_id=OP_DISABLE_CONNECTOR,
        )

    async def enable_group(
        self,
        connector_id: str,
        group_key: str,
        *,
        tenant_id: UUID | None,
    ) -> None:
        """Transition one group to ``'enabled'``.

        Allowed source states: ``staged`` or ``disabled``. Cascade
        matches :meth:`enable_connector` but applies only to the
        single group's children. Idempotent on a group already in
        ``enabled``.
        """
        scope = self._resolve_scope(connector_id, tenant_id)
        sessionmaker = self._sessionmaker()
        async with sessionmaker() as session:
            group = await load_group(session, scope, connector_id, group_key)
            if group.review_status == "enabled":
                return
            if group.review_status not in ("staged", "disabled"):
                raise InvalidStateTransitionError(
                    current_status=group.review_status,
                    requested_status="enabled",
                    group_key=group_key,
                )
            previous_status = group.review_status
            group.review_status = "enabled"
            overridden_op_ids = await operator_disabled_op_ids(
                session,
                scope,
                [group.id],
            )
            ops_changed = await cascade_is_enabled(
                session,
                scope,
                [group.id],
                target=True,
                excluded_op_ids=overridden_op_ids,
            )
            await write_audit_row(
                session,
                operator_sub=self._operator.sub,
                operator_tenant_id=self._operator.tenant_id,
                op_id=OP_ENABLE_GROUP,
                payload={
                    "connector_id": connector_id,
                    "group_key": group_key,
                    "from_status": previous_status,
                    "to_status": "enabled",
                    "ops_cascade_count": ops_changed,
                    "ops_excluded_for_operator_override": sorted(overridden_op_ids),
                },
            )
            await session.commit()

    async def delete_connector(
        self,
        connector_id: str,
        *,
        tenant_id: UUID | None,
    ) -> DeleteConnectorResult:
        """Delete *connector_id* in *tenant_id*'s scope (G0.25-T2 #1700).

        Removes the scope's ``endpoint_descriptor`` + ``operation_group``
        rows and, when no rows remain for the triple under any scope,
        pops the triple's auto-registered
        :class:`~meho_backplane.operations.ingest.connector_registration.GenericRestConnector`
        shim from the v2 registry — the zero-op stubs aborted ingests
        leave behind are the primary target, and for those (no rows
        anywhere) the delete is registry-only. Hand-coded connector
        classes are never deregistered.

        Unknown connector, cross-tenant probe, and a triple whose rows
        live only under a scope the caller did not name all raise
        :class:`ConnectorNotFoundError` — the same 404 conflation every
        other connector route uses. Exactly one
        ``meho.connector.delete`` audit row commits atomically with the
        row deletes; the registry pop runs strictly after the commit so
        a failed transaction never leaves the process-local registry
        ahead of the database. A connector that still had enabled
        operations is deleted anyway and the returned
        :attr:`DeleteConnectorResult.warnings` carries the advisory
        (warnings never block the write — the
        :meth:`edit_op` discipline). Re-ingesting the same triple
        afterwards re-registers the connector from scratch.
        """
        scope = self._resolve_scope(connector_id, tenant_id)
        sessionmaker = self._sessionmaker()
        async with sessionmaker() as session:
            staged = await stage_connector_delete(session, scope, connector_id)
            await write_audit_row(
                session,
                operator_sub=self._operator.sub,
                operator_tenant_id=self._operator.tenant_id,
                op_id=OP_DELETE_CONNECTOR,
                payload=staged.audit_payload,
            )
            await session.commit()
        deregister_staged_auto_shims(staged)
        for warning in staged.result.warnings:
            _log.warning(
                "connector_delete_enabled_ops",
                connector_id=connector_id,
                enabled_op_count=warning.enabled_op_count,
                code=warning.code,
            )
        return staged.result

    # -- internal connector-wide transition --------------------------------

    async def _transition_connector(
        self,
        connector_id: str,
        *,
        tenant_id: UUID | None,
        target_status: str,
        allowed_source: tuple[str, ...],
        op_id: str,
    ) -> None:
        """Shared body of :meth:`enable_connector` + :meth:`disable_connector`.

        Loads every group; partitions into already-at-target
        (no-op), transitionable (in *allowed_source*), and
        rejected (any other state). A non-empty rejected set
        raises :class:`InvalidStateTransitionError`. Idempotent
        path: empty transitionable set → no audit row.
        """
        scope = self._resolve_scope(connector_id, tenant_id)
        sessionmaker = self._sessionmaker()
        async with sessionmaker() as session:
            groups = await load_groups(session, scope, connector_id)
            transitionable = []
            rejected = []
            already_at_target = []
            for group in groups:
                if group.review_status == target_status:
                    already_at_target.append(group)
                elif group.review_status in allowed_source:
                    transitionable.append(group)
                else:
                    rejected.append(group)
            if rejected:
                first = rejected[0]
                raise InvalidStateTransitionError(
                    current_status=first.review_status,
                    requested_status=target_status,
                    group_key=first.group_key,
                )
            if not transitionable:
                return
            transitioned_keys: list[str] = []
            for group in transitionable:
                group.review_status = target_status
                transitioned_keys.append(group.group_key)
            transitioned_ids = [group.id for group in transitionable]
            excluded_op_ids: list[str] = []
            if target_status == "enabled":
                excluded_op_ids = await operator_disabled_op_ids(
                    session,
                    scope,
                    transitioned_ids,
                )
                ops_changed = await cascade_is_enabled(
                    session,
                    scope,
                    transitioned_ids,
                    target=True,
                    excluded_op_ids=excluded_op_ids,
                )
            else:
                ops_changed = await cascade_is_enabled(
                    session,
                    scope,
                    transitioned_ids,
                    target=False,
                    excluded_op_ids=[],
                )
            await write_audit_row(
                session,
                operator_sub=self._operator.sub,
                operator_tenant_id=self._operator.tenant_id,
                op_id=op_id,
                payload={
                    "connector_id": connector_id,
                    "to_status": target_status,
                    "transitioned_group_keys": sorted(transitioned_keys),
                    "already_at_target_count": len(already_at_target),
                    "ops_cascade_count": ops_changed,
                    "ops_excluded_for_operator_override": sorted(excluded_op_ids),
                },
            )
            await session.commit()
