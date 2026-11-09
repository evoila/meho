# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Plain-sentence outcomes for ``search_operations``, behind a switch (#5490).

An agent often describes the operation it needs in a plain sentence. Two
rules made such a search answer ``ambiguous`` even when the top hit was
right:

* The keyword (BM25) half uses ``plainto_tsquery``, which needs **every**
  word of the query in the operation's text. A sentence of several words
  almost never matches, so the top hit has no keyword score, and
  :func:`~meho_backplane.operations.meta_tools._classify_hits` answers
  ``ambiguous`` for a top hit without one.
* A ``group`` filter that leaves one operation still answered
  ``ambiguous``, although there is nothing left to narrow.

The switch ``SEARCH_PLAIN_SENTENCES``
(:attr:`~meho_backplane.settings.Settings.search_plain_sentences`, off by
default) changes only the outcome, never the hit order:

* A top hit without a keyword score counts as a word match when its text
  holds **any** word of the query (:func:`top_hit_holds_a_query_word`).
  The near-tie check still applies.
* A search with a ``group`` filter that leaves exactly one enabled
  operation answers ``matched`` (:func:`group_leaves_one_operation`).

The ranking keeps the every-word keyword match. A local measurement with
an any-word keyword match in the ranking put the right operation first
less often, so the ranking does not change here.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, text

from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._lookup import (
    enabled_for_tenant_clause,
    installed_owner_clause,
    visible_scope_clause,
)
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.operations.meta_tools import OperationSearchHit

__all__ = [
    "group_leaves_one_operation",
    "search_plain_sentences",
    "top_hit_holds_a_query_word",
]

# Does the text hold any word of the query? The text is the one the keyword
# index is built on (``BM25_ORIGINAL_DOCUMENT_SQL`` in ``_search.py``): the
# summary, a space, the description the agent reads. The query is the words
# ``plainto_tsquery`` keeps (stemmed, without stop words), joined by OR
# (``|``) in place of AND (``&``). ``plainto_tsquery`` puts `` & `` only
# between words, and a word never holds a space, so the replace changes no
# word. A query of stop words only stays empty and matches nothing.
_HOLDS_A_QUERY_WORD_SQL = text(
    """
        SELECT to_tsvector(
                'english',
                coalesce(CAST(:summary AS text), '') || ' '
                    || coalesce(CAST(:description AS text), ''))
            @@ CAST(replace(CAST(plainto_tsquery('english', CAST(:query AS text)) AS text),
                            ' & ', ' | ') AS tsquery)
        """
)


def search_plain_sentences() -> bool:
    """Return the state of the ``SEARCH_PLAIN_SENTENCES`` switch."""
    return get_settings().search_plain_sentences


async def top_hit_holds_a_query_word(
    session: Any,
    *,
    hits: Sequence[OperationSearchHit],
    query: str,
) -> bool:
    """Return ``True`` when the top hit has no keyword score but holds a query word.

    Only with the switch on. On PostgreSQL the check uses the same full text
    rules as the keyword index, on the text
    the hit shows (``summary`` and ``description``). On SQLite (unit tests)
    it is the substring rule of the fallback search.
    """
    if not hits or hits[0].bm25_score is not None or not search_plain_sentences():
        return False
    top = hits[0]
    conn = await session.connection()
    if conn.dialect.name == "postgresql":
        result = await session.execute(
            _HOLDS_A_QUERY_WORD_SQL,
            {"summary": top.summary, "description": top.description, "query": query},
        )
        return bool(result.scalar())
    blob = f"{top.summary or ''} {top.description or ''}".lower()
    return any(term in blob for term in query.lower().split())


async def group_leaves_one_operation(
    session: Any,
    *,
    hits: Sequence[OperationSearchHit],
    group_id: uuid.UUID | None,
    tenant_id: uuid.UUID,
    product: str,
    version: str,
    impl_id: str,
) -> bool:
    """Return ``True`` when the ``group`` filter leaves exactly one operation.

    Only with the switch on, a ``group`` filter and one hit. The count uses
    the same tenant scope, enabled rule and installed rule as the search,
    so a disabled or foreign-tenant operation never counts. It reads at
    most two rows.
    """
    if group_id is None or len(hits) != 1 or not search_plain_sentences():
        return False
    ids = (
        select(EndpointDescriptor.id)
        .where(
            visible_scope_clause(
                EndpointDescriptor,
                tenant_id=tenant_id,
                product=product,
                version=version,
                impl_id=impl_id,
            ),
            EndpointDescriptor.product == product,
            EndpointDescriptor.version == version,
            EndpointDescriptor.impl_id == impl_id,
            EndpointDescriptor.group_id == group_id,
            enabled_for_tenant_clause(tenant_id=tenant_id),
            installed_owner_clause(),
        )
        .limit(2)
    )
    count = (await session.execute(select(func.count()).select_from(ids.subquery()))).scalar_one()
    return int(count) == 1
