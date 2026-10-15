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

The target is resolved the way the connector tools resolve theirs
(:func:`~meho_backplane.targets.resolver.resolve_target`: by name or alias,
inside the caller's tenant), so an unknown target, or one in another tenant,
raises the same :class:`~meho_backplane.targets.resolver.TargetNotFoundError`
(or :class:`~meho_backplane.targets.resolver.AmbiguousTargetError`). Only the
target's name and fingerprint are read; its address and credentials are never
touched.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.docs_search.service import DocsScope
from meho_backplane.targets.resolver import resolve_target

__all__ = ["apply_target_default", "with_target_default"]


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


async def apply_target_default(
    session: AsyncSession,
    operator: Operator,
    scope: DocsScope,
    target: str | None,
) -> DocsScope:
    """Resolve *target* in the operator's tenant and add its default to *scope*.

    A missing or blank *target* returns *scope* unchanged. The target is
    resolved even when the caller gave ``product`` / ``version`` (so an
    unknown name is still an error and the name is still recorded); the
    caller's values then win over the default
    (:meth:`~meho_backplane.docs_search.service.DocsScope.default_scope`).

    Binds ``audit_scope_source`` and ``audit_target`` (the target's name
    only), so every face's audit row records where the scope came from.

    Raises:
        TargetNotFoundError: no live target matches *target* in the tenant.
        AmbiguousTargetError: more than one target matches *target*.
    """
    if target is not None and target.strip():
        row = await resolve_target(session, operator.tenant_id, target.strip())
        scope = with_target_default(scope, row.name, row.fingerprint)
    structlog.contextvars.bind_contextvars(
        audit_scope_source=scope.source, audit_target=scope.target
    )
    return scope
