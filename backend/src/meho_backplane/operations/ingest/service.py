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

from typing import Any, Literal
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations.ingest._internals import (
    OP_DISABLE_CONNECTOR,
    OP_EDIT_GROUP,
    OP_EDIT_OP,
    OP_ENABLE_CONNECTOR,
    OP_ENABLE_GROUP,
    ConnectorScope,
    apply_op_overrides,
    cascade_is_enabled,
    enable_time_auto_shim_warnings,
    load_group,
    load_groups,
    load_op,
    load_ops_in_groups,
    operator_disabled_op_ids,
    validate_edit_op_args,
    write_audit_row,
)
from meho_backplane.operations.ingest.api_schemas import EditOpWarning
from meho_backplane.operations.ingest.exceptions import (
    ConnectorNotFoundError,
    InvalidStateTransitionError,
)
from meho_backplane.operations.ingest.parser import parse_connector_id
from meho_backplane.operations.ingest.payload import (
    ConnectorReviewGroup,
    ConnectorReviewOp,
    ConnectorReviewPayload,
)

__all__ = ["ReviewService"]

_log = structlog.get_logger(__name__)


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

    # -- public read API ---------------------------------------------------

    async def get_review_payload(
        self,
        connector_id: str,
        tenant_id: UUID | None,
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
        (``tenant_id IS NULL``) rows. When *tenant_id* equals the
        operator's tenant and that probe misses, the lookup falls
        back to ``tenant_id IS NULL`` so a ``GET
        /api/v1/connectors/{id}/review`` against a global connector
        returns 200 instead of 404 (G0.13-T5 #1135). When *tenant_id*
        is passed explicitly as ``None`` (MCP admin path), the
        existing admin-only gate stays; when it's any other UUID
        (cross-tenant probe), the lookup stays single-pass and the
        cross-tenant 404 conflation is preserved.
        """
        scope = self._resolve_scope(connector_id, tenant_id)
        try:
            return await self._render_payload(connector_id, scope)
        except ConnectorNotFoundError:
            if tenant_id is None or tenant_id != self._operator.tenant_id:
                # Explicit built-in probe (MCP admin path) or genuine
                # cross-tenant probe — no fall-through. The original
                # 404 stays.
                raise
            # Operator's own-tenant probe missed; try the built-in
            # scope. Build a fresh scope tuple with tenant_id=None;
            # bypass _authorize_scope's admin-only gate intentionally
            # because read access to built-ins is operator-level
            # (matches the list endpoint).
            fallback_scope = ConnectorScope(
                product=scope.product,
                version=scope.version,
                impl_id=scope.impl_id,
                tenant_id=None,
            )
            return await self._render_payload(connector_id, fallback_scope)

    async def _render_payload(
        self,
        connector_id: str,
        scope: ConnectorScope,
    ) -> ConnectorReviewPayload:
        """Load groups + ops for *scope* and pack them into the payload.

        Raises :class:`ConnectorNotFoundError` when no group rows
        exist under *scope*. Extracted from :meth:`get_review_payload`
        so the two-pass tenant lookup there can reuse the same
        rendering pipeline against a fallback scope without
        duplicating the DB roundtrip + payload assembly.
        """
        sessionmaker = self._sessionmaker()
        async with sessionmaker() as session:
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
        return ConnectorReviewPayload(
            connector_id=connector_id,
            product=scope.product,
            version=scope.version,
            impl_id=scope.impl_id,
            tenant_id=scope.tenant_id,
            groups=rendered_groups,
            total_op_count=sum(group.op_count for group in rendered_groups),
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
