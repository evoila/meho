# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Module-private helpers for :mod:`meho_backplane.memory.service`.

G5.1-T1 (#421). Holds the small pure functions that translate between
:class:`~meho_backplane.db.models.Document` rows and the typed memory
schemas, plus the ``source_id`` encoding scheme that makes the
``(tenant_id, source, source_id)`` natural key unique across scopes.
Lives in a separate module so :mod:`service` stays focused on the
:class:`~meho_backplane.memory.service.MemoryService` class shape and
fits under the repository's per-file size budget.

source_id encoding
------------------

The ``documents`` table's natural key is ``(tenant_id, source,
source_id)``; for ``source='memory'`` we encode scope-specific
context into ``source_id`` so different scopes cannot collide on the
same slug. The scheme is:

    user           ->  user:<user_sub>:<slug>
    user-tenant    ->  user-tenant:<user_sub>:<slug>
    user-target    ->  user-target:<user_sub>:<target_name>:<slug>
    tenant         ->  tenant:<slug>
    target         ->  target:<target_name>:<slug>

The colon separator is safe because user_sub (OIDC sub) and slug
(operator-chosen or uuid-hex prefix) reject colons by construction in
v0.2 -- a Keycloak ``sub`` is a UUID-shaped string and
:func:`auto_slug` emits hex characters only. The format is documented
here so audit consumers + future migrations can parse rows back to
their logical components without round-tripping through this module.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from meho_backplane.db.models import Document
from meho_backplane.memory.schemas import (
    USER_SCOPED,
    MemoryEntry,
    MemoryScope,
    scope_for_kind,
)

__all__ = [
    "EPOCH",
    "MEMORY_SOURCE",
    "auto_slug",
    "build_metadata",
    "document_to_entry",
    "encode_source_id",
    "has_tag",
    "is_expired",
    "metadata_datetime",
    "metadata_str",
    "slug_from_source_id",
]

#: The fixed ``source`` value memory rows carry in the ``documents``
#: table. Centralised here so the service / API / MCP / CLI layers
#: never spell the literal themselves.
MEMORY_SOURCE: str = "memory"

#: Placeholder timestamp surfaced when the retrieval substrate does
#: not expose created/updated through ``RetrievalHit``. The API layer
#: (T2 #422) renders this as ``null``; callers that need a real
#: timestamp re-fetch via :meth:`MemoryService.recall`, which carries
#: the column values from :class:`Document`.
EPOCH: datetime = datetime(1970, 1, 1, tzinfo=UTC)


def auto_slug() -> str:
    """Return a 12-char hex slug from a fresh UUID.

    12 chars is enough entropy (48 bits) that collision risk within a
    tenant lifetime is negligible while still being human-typeable for
    operators wanting to recall a memory by slug at the CLI without
    pasting a full UUID.
    """
    return uuid.uuid4().hex[:12]


def encode_source_id(
    *,
    scope: MemoryScope,
    user_sub: str,
    target_name: str | None,
    slug: str,
) -> str:
    """Compose the ``documents.source_id`` value for a memory row.

    See the module-level "source_id encoding" docstring for the
    per-scope shape. The encoding makes the natural key unique across
    operators (user-flavoured scopes embed ``user_sub``) and across
    targets (target-flavoured scopes embed ``target_name``).
    """
    if scope is MemoryScope.USER:
        return f"user:{user_sub}:{slug}"
    if scope is MemoryScope.USER_TENANT:
        return f"user-tenant:{user_sub}:{slug}"
    if scope is MemoryScope.USER_TARGET:
        assert target_name is not None  # guarded by caller
        return f"user-target:{user_sub}:{target_name}:{slug}"
    if scope is MemoryScope.TENANT:
        return f"tenant:{slug}"
    if scope is MemoryScope.TARGET:
        assert target_name is not None  # guarded by caller
        return f"target:{target_name}:{slug}"
    raise ValueError(f"unknown scope {scope!r}")  # pragma: no cover


def slug_from_source_id(source_id: str) -> str:
    """Extract the trailing slug component from an encoded ``source_id``.

    The slug is always the final ``:``-separated segment under the
    encoding scheme in :func:`encode_source_id`. Pure string helper;
    the schema doc lives at module level so a forensic reader of an
    audit row can run the same parse without importing the module.
    """
    return source_id.rsplit(":", 1)[-1]


def build_metadata(
    *,
    caller_metadata: dict[str, Any] | None,
    scope: MemoryScope,
    user_sub: str,
    target_name: str | None,
    expires_at: datetime | None,
) -> dict[str, Any]:
    """Merge caller metadata with service-owned bookkeeping fields.

    Service-owned keys (``user_sub``, ``target_name``, ``expires_at``,
    ``scope``) always win over caller-supplied values for the same key
    -- the encoding is canonical and an operator cannot smuggle an
    alternative value in via the request body. The ``scope`` field is
    redundant with ``documents.kind`` but stored in metadata too so
    forensic readers parsing rows in isolation (audit replays, future
    migration tools) see the scope without translating ``kind``.
    """
    merged: dict[str, Any] = dict(caller_metadata) if caller_metadata else {}
    merged["scope"] = scope.value
    merged["user_sub"] = user_sub if scope in USER_SCOPED else None
    merged["target_name"] = target_name
    # Store as ISO 8601 with timezone so round-tripping through JSONB
    # (which has no datetime type) preserves the wall-clock and the
    # offset cleanly. ``None`` is stored as JSON null.
    merged["expires_at"] = expires_at.isoformat() if expires_at is not None else None
    return merged


def document_to_entry(doc: Document) -> MemoryEntry:
    """Build a :class:`MemoryEntry` from a :class:`Document` row.

    Lifts ``expires_at`` out of ``doc_metadata`` into the typed
    pydantic field for filter convenience; the full metadata dict
    stays accessible on :attr:`MemoryEntry.metadata` for callers that
    need it (audit middleware, MCP resource handlers).
    """
    metadata = dict(doc.doc_metadata) if doc.doc_metadata else {}
    return MemoryEntry(
        id=doc.id,
        tenant_id=doc.tenant_id,
        scope=scope_for_kind(doc.kind),
        slug=slug_from_source_id(doc.source_id),
        body=doc.body,
        metadata=metadata,
        expires_at=metadata_datetime(metadata, "expires_at"),
        user_sub=metadata_str(metadata, "user_sub"),
        target_name=metadata_str(metadata, "target_name"),
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


def metadata_str(metadata: dict[str, Any], key: str) -> str | None:
    """Extract a string field from ``doc_metadata`` defensively.

    Returns ``None`` when the key is absent or the value is not a
    string (a corrupt row from a future migration would surface here
    as None rather than a confused TypeError later in the call stack).
    """
    value = metadata.get(key)
    if isinstance(value, str):
        return value
    return None


def metadata_datetime(metadata: dict[str, Any], key: str) -> datetime | None:
    """Extract an ISO 8601 datetime from ``doc_metadata`` defensively.

    Returns ``None`` when the key is absent, the value is ``None``,
    or the value is not a parseable ISO 8601 string. Same fail-soft
    contract as :func:`metadata_str`.
    """
    value = metadata.get(key)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # ``datetime.fromisoformat`` produces a tz-naive value when the
    # ISO string carries no offset; normalise to UTC so the
    # :func:`is_expired` comparison stays sound.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def has_tag(metadata: dict[str, Any], tag: str) -> bool:
    """Return True when ``tag`` is present in ``metadata['tags']``.

    Tags are an optional JSON array under the ``tags`` key; missing /
    malformed entries fail to ``False`` rather than raising. Equal-only
    membership (no substring / regex) keeps the surface predictable
    for operators.
    """
    tags = metadata.get("tags")
    if not isinstance(tags, list):
        return False
    return tag in tags


def is_expired(expires_at: datetime) -> bool:
    """Return True when ``expires_at`` is in the past.

    ``datetime.now(UTC)`` is the comparison anchor; an entry with
    ``expires_at`` exactly equal to "now" is treated as expired (the
    asymmetric ``<=`` matches the standard TTL contract where the
    boundary moment is past the live window).
    """
    return expires_at <= datetime.now(UTC)
