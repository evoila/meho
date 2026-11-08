# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shared knowledge scope: notes every tenant on one instance can read.

A knowledge entry is owned by one tenant. An entry whose metadata has
``shared: true`` and whose owner tenant is listed in
``KB_SHARED_TENANT_IDS`` is also readable from every other tenant on the
instance. Reading is the only thing that is shared:

* The entry stays in its owner tenant. It is not copied.
* Only the owner tenant writes it. A tenant that is not listed cannot
  mark an entry as shared.
* No other tenant can write or delete it, and none can save an entry
  under the same slug (:func:`refuse_shared_slug`).

Customer facts never belong here. They go to the customer's own tenant.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, ColumnElement, and_, or_, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.compiler import SQLCompiler

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.kb.schemas import (
    KB_SOURCE,
    META_SHARED,
    KbSharedNotAllowedError,
    KbSharedSlugError,
)
from meho_backplane.lesson_header import LessonHeaderError
from meho_backplane.settings import get_settings

__all__ = [
    "check_may_share",
    "refuse_shared_slug",
    "shared_owner_tenants",
    "visible_to",
]


class _IsShared(ColumnElement[bool]):
    """SQL test: the ``shared`` key of the metadata column is the JSON value ``true``.

    A cast to boolean would fail the whole query on PostgreSQL when an
    old row holds text such as ``"maybe"`` under that key. This test only
    matches the JSON ``true``.
    """

    inherit_cache = True
    type = Boolean()

    def __init__(self, column: Any) -> None:
        self.column = column


@compiles(_IsShared, "postgresql")
def _compile_is_shared_pg(element: _IsShared, compiler: SQLCompiler, **kw: Any) -> str:
    return f"({compiler.process(element.column, **kw)} -> '{META_SHARED}') = 'true'::jsonb"


@compiles(_IsShared)
def _compile_is_shared_default(element: _IsShared, compiler: SQLCompiler, **kw: Any) -> str:
    return f"(JSON_EXTRACT({compiler.process(element.column, **kw)}, '$.{META_SHARED}') IS 1)"


def _shared_rows() -> ColumnElement[bool]:
    return _IsShared(Document.doc_metadata)


def shared_owner_tenants() -> frozenset[uuid.UUID]:
    """Return the tenants whose shared entries every tenant can read."""
    return get_settings().kb_shared_tenant_set


def visible_to(tenant_id: uuid.UUID) -> ColumnElement[bool]:
    """Return the ``documents`` row filter for what *tenant_id* may read.

    Its own rows, plus the shared rows of each listed owner tenant.
    """
    own = Document.tenant_id == tenant_id
    owners = shared_owner_tenants() - {tenant_id}
    if not owners:
        return own
    return or_(own, and_(Document.tenant_id.in_(owners), _shared_rows()))


def check_may_share(tenant_id: uuid.UUID, metadata: dict[str, object] | None) -> None:
    """Raise unless *tenant_id* may write *metadata*'s ``shared`` value.

    ``shared`` must be true or false. Only a tenant listed in
    ``KB_SHARED_TENANT_IDS`` may set it to true.
    """
    if metadata is None or META_SHARED not in metadata:
        return
    value = metadata[META_SHARED]
    if not isinstance(value, bool):
        raise LessonHeaderError(f"shared must be true or false; got {value!r}")
    if value and tenant_id not in shared_owner_tenants():
        raise KbSharedNotAllowedError(tenant_id)


async def refuse_shared_slug(tenant_id: uuid.UUID, slug: str) -> None:
    """Raise :class:`KbSharedSlugError` when *slug* is a shared entry of another tenant."""
    owners = shared_owner_tenants() - {tenant_id}
    if not owners:
        return
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        found = (
            await session.execute(
                select(Document.id)
                .where(
                    Document.tenant_id.in_(owners),
                    Document.source == KB_SOURCE,
                    Document.source_id == slug,
                    _shared_rows(),
                )
                .limit(1)
            )
        ).first()
    if found is not None:
        raise KbSharedSlugError(slug)
