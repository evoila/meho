# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operation search for the operations surface of the retrieval eval (#4752).

Operations live in the ``endpoint_descriptor`` table, not in the
``documents`` table that :func:`~meho_backplane.retrieval.retriever.retrieve`
ranks. So the operations surface of the eval calls the ranked search of
``search_operations`` instead: the same tenant scope, the same enabled
and installed rules, and the same BM25 + cosine ranking the agent gets.

A question can only be scored when its connector can be searched on
this install. When the connector has no rows, its code is not installed,
its package is not installed, or none of its operations is enabled, the
question is "not run": the result carries the reason and the eval does
not count it as a miss.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations._code_presence import connector_code_missing
from meho_backplane.operations._lookup import connector_exists, parse_connector_id
from meho_backplane.operations.meta_tools import _run_search

__all__ = [
    "OperationSearchResult",
    "SearchOperationsCallable",
    "search_corpus_operations",
]


@dataclass(frozen=True)
class OperationSearchResult:
    """The ranked ``op_id`` list for one question, or why it was not run."""

    op_ids: list[str] = field(default_factory=list)
    not_run_reason: str | None = None


#: Type of the ``search_operations_fn`` the runner calls per operations
#: question. Keyword arguments: ``tenant_id``, ``connector_id``, ``query``,
#: ``limit``. Tests pass a stub; the default is
#: :func:`search_corpus_operations`.
SearchOperationsCallable = Callable[..., Awaitable[OperationSearchResult]]


async def search_corpus_operations(
    *,
    tenant_id: uuid.UUID,
    connector_id: str,
    query: str,
    limit: int,
) -> OperationSearchResult:
    """Run the ranked operation search for one eval question.

    Returns the ``op_id`` of each hit in rank order, cut to *limit*. A
    connector that this tenant cannot search gives an empty list and a
    ``not_run_reason`` instead.
    """
    product, version, impl_id = parse_connector_id(connector_id)
    if not await connector_exists(
        tenant_id=tenant_id, product=product, version=version, impl_id=impl_id
    ):
        return OperationSearchResult(
            not_run_reason=f"connector {connector_id!r} has no operations on this install"
        )
    async with get_sessionmaker()() as session:
        if await connector_code_missing(
            session, tenant_id=tenant_id, product=product, version=version, impl_id=impl_id
        ):
            return OperationSearchResult(
                not_run_reason=f"the code of connector {connector_id!r} is not installed"
            )
        hits, outcome, detail = await _run_search(
            session,
            tenant_id=tenant_id,
            product=product,
            version=version,
            impl_id=impl_id,
            group_key=None,
            query=query,
            limit=limit,
        )
    if outcome == "package_not_installed":
        package = detail[0] if detail else "?"
        return OperationSearchResult(
            not_run_reason=(
                f"the connector package {package!r} of connector {connector_id!r} is not installed"
            )
        )
    if outcome == "zero_enabled_operations":
        return OperationSearchResult(
            not_run_reason=f"connector {connector_id!r} has no enabled operations"
        )
    return OperationSearchResult(op_ids=[hit.op_id for hit in hits])
