# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The default docs scope from a named target's fingerprint (#3920).

An agent working on a target often asks the docs tools about that target's
product without naming the version. The backplane already knows it: every
probed target carries a fingerprint (product, version, build). When the
caller names a ``target`` and gives neither ``product`` nor ``version``, the
target's fingerprint product and version become the **default** scope.

The default is a ranking hint, never a filter. It reaches the backend only
as a soft ``scope`` with ``source: "default"``, under the collection's soft
gate (:func:`~meho_backplane.docs_search.service.forwarded_scope`), and the
backend applies it only when the question names no version itself.

The target is resolved and authorised the way the connector tools resolve
and authorise theirs. The lookup is
:func:`~meho_backplane.targets.resolver.resolve_target` (by name or alias,
inside the caller's tenant), so an unknown target, or one in another tenant,
raises the same :class:`~meho_backplane.targets.resolver.TargetNotFoundError`
(or :class:`~meho_backplane.targets.resolver.AmbiguousTargetError`). The
permission check is :func:`~meho_backplane.auth.permissions.resolve_verdict`,
the resolver behind ``call_operation``'s policy gate, run for the docs op on
the resolved target: an agent principal whose permission rows deny it that
target gets :class:`TargetDeniedError`, which carries the same ``denied`` code
and reason ``call_operation`` returns. Only the target's name, id and
fingerprint are read; its address and credentials are never touched.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.auth.permissions import resolve_verdict
from meho_backplane.db.models import PermissionVerdict, Target
from meho_backplane.docs_search.service import DocsScope
from meho_backplane.targets.resolver import resolve_target

__all__ = ["TargetDeniedError", "apply_target_default", "with_target_default"]

log = structlog.get_logger(__name__)


class TargetDeniedError(HTTPException):
    """The caller may not use the named target: its permission rows deny it.

    Raised for an agent principal whose rows (the same ones ``call_operation``'s
    policy gate reads) resolve to ``deny`` for the docs op on this target.
    ``detail`` carries the ``denied`` code, the name the caller gave and the
    agent-readable ``reason``: the fields ``call_operation``'s ``denied``
    result carries, so an agent can diagnose the refusal the same way.
    """

    def __init__(self, query: str, reason: str) -> None:
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "denied", "query": query, "reason": reason},
        )


def _fingerprint_value(fingerprint: Mapping[str, Any], key: str) -> str | None:
    """Read one fingerprint field as a stripped string, or ``None``."""
    value = fingerprint.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def with_target_default(
    scope: DocsScope,
    target: str,
    fingerprint: Mapping[str, Any] | None,
) -> DocsScope:
    """Add a named target and its fingerprint default to *scope*.

    *target* is the resolved target's name. *fingerprint* is its stored
    fingerprint (``Target.fingerprint``); its ``product`` and ``version``
    become the default scope, as reported (the backend normalises them). A
    target without a fingerprint adds only its name.
    """
    fp = fingerprint or {}
    return scope.model_copy(
        update={
            "target": target,
            "target_product": _fingerprint_value(fp, "product"),
            "target_release": _fingerprint_value(fp, "version"),
        }
    )


async def _check_target_access(
    session: AsyncSession,
    operator: Operator,
    row: Target,
    *,
    op_id: str,
    query: str,
) -> None:
    """Raise :class:`TargetDeniedError` when *operator* may not use *row*.

    Mirrors the principal branch of ``call_operation``'s policy gate
    (:func:`~meho_backplane.operations._validate.policy_gate`): an **agent**
    principal is checked against its permission rows through
    :func:`~meho_backplane.auth.permissions.resolve_verdict`, keyed on the
    docs *op_id* and the target's id; a human or service principal is not
    gated by those rows. A docs lookup is a read with no vendor traffic, so
    it is checked as a ``safe`` op: with no matching row it is allowed, and
    only an explicit ``deny`` refuses it. A ``needs-approval`` verdict does
    not refuse it, because there is nothing here to approve.
    """
    if operator.principal_kind is not PrincipalKind.AGENT:
        return
    verdict, reason = await resolve_verdict(
        session=session,
        operator=operator,
        op_id=op_id,
        safety_level="safe",
        target_id=row.id,
    )
    if verdict is PermissionVerdict.DENY:
        log.info(
            "docs_target_denied",
            operator_sub=operator.sub,
            tenant_id=str(operator.tenant_id),
            op_id=op_id,
            target=row.name,
            reason=reason,
        )
        raise TargetDeniedError(query, reason)


async def apply_target_default(
    session: AsyncSession,
    operator: Operator,
    scope: DocsScope,
    target: str | None,
    *,
    op_id: str,
) -> DocsScope:
    """Resolve *target* in the operator's tenant and add its default to *scope*.

    A missing or blank *target* returns *scope* unchanged. The target is
    resolved and checked even when the caller gave ``product`` / ``version``
    (so an unknown or denied name is still an error and the name is still
    recorded); the caller's values then win over the default
    (:meth:`~meho_backplane.docs_search.service.DocsScope.default_scope`).
    *op_id* is the docs op the caller is running (``meho.docs.search`` or
    ``meho.docs.ask``), the op the permission rows are matched against.

    Binds ``audit_scope_source`` and ``audit_target`` (the target's name
    only), so every face's audit row records where the scope came from. The
    name is bound before the permission check, so a denied call's audit row
    names the target too.

    Raises:
        TargetNotFoundError: no live target matches *target* in the tenant.
        AmbiguousTargetError: more than one target matches *target*.
        TargetDeniedError: the caller's permission rows deny it this target.
    """
    if target is not None and target.strip():
        query = target.strip()
        row = await resolve_target(session, operator.tenant_id, query)
        structlog.contextvars.bind_contextvars(audit_target=row.name)
        await _check_target_access(session, operator, row, op_id=op_id, query=query)
        scope = with_target_default(scope, row.name, row.fingerprint)
    structlog.contextvars.bind_contextvars(
        audit_scope_source=scope.source, audit_target=scope.target
    )
    return scope
