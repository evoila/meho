# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic schemas for the ``event_source`` registry (#2880).

Moulded on :mod:`meho_backplane.targets.schemas`: a frozen full-read
:class:`EventSource`, a narrow frozen :class:`EventSourceSummary` for
lists, an ``extra='forbid'`` :class:`EventSourceCreate`, and an
all-optional :class:`EventSourceUpdate`.

The one shape that departs from targets is the **secret**. A target's
``secret_ref`` is a Vault *path* an operator types and RDC provisions
out of band, so it is a plain ``str``. An event source's admin surface
*writes* the auth secret to Vault itself (#2880 acceptance criterion 2),
so the write schemas carry a **write-only** :class:`~pydantic.SecretStr`
``secret`` field that is never echoed back: it is absent from
:class:`EventSource` / :class:`EventSourceSummary`, and ``SecretStr``
masks it in every repr / log / traceback. The persisted row stores only
the derived ``secret_ref`` path, never the value (secret-broker custody
discipline, ``docs/codebase/connectors-secret-broker.md``).

``kind`` / ``auth_strategy`` / ``status`` are closed :class:`~enum.StrEnum`
sets whose values are mirrored by the ``ck_event_source_*`` CHECK
constraints on the ORM model.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

if TYPE_CHECKING:
    from meho_backplane.db.models import EventSource as EventSourceORM

__all__ = [
    "EventSource",
    "EventSourceAuthStrategy",
    "EventSourceCreate",
    "EventSourceKind",
    "EventSourceListResponse",
    "EventSourceStatus",
    "EventSourceSummary",
    "EventSourceUpdate",
    "project_event_source_to_summary",
]

#: URL-token grammar for ``slug``: lowercase alphanumerics and internal
#: hyphens, first/last char alphanumeric. The slug is the sole routing
#: key in the JWT-less ingest URL (``/api/v1/events/ingest/{source_slug}``),
#: so it must be path-safe with no percent-encoding surprises.
_SLUG_PATTERN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"

_NAME_MAX_LENGTH = 200
_SLUG_MAX_LENGTH = 100


class EventSourceKind(StrEnum):
    """Producer family. Selects the payload normaliser (#2882) at ingest.

    A closed set (unlike :attr:`~meho_backplane.db.models.EventOutbox.event_kind`'s
    free text) because each kind needs hand-written normaliser code, so
    adding one is a code change regardless. Values mirror the
    ``ck_event_source_kind`` CHECK constraint.
    """

    ALERTMANAGER = "alertmanager"
    GRAFANA = "grafana"
    VCF_OPERATIONS = "vcf-operations"
    HARBOR = "harbor"
    GENERIC_JSON = "generic-json"


class EventSourceAuthStrategy(StrEnum):
    """How the ingest endpoint (#2881) authenticates the sender.

    All three verify against the source's Vault secret. Values mirror the
    ``ck_event_source_auth_strategy`` CHECK constraint.

    * ``hmac-sha256`` -- the sender signs the body with a shared key.
    * ``static-header`` -- a fixed bearer / API-key header value.
    * ``basic`` -- HTTP Basic (the non-secret username lives in ``extras``;
      the password is the Vault secret).
    """

    HMAC_SHA256 = "hmac-sha256"
    STATIC_HEADER = "static-header"
    BASIC = "basic"


class EventSourceStatus(StrEnum):
    """Registry status. Values mirror the ``ck_event_source_status`` CHECK.

    ``paused`` makes the ingest path reject the sender; because nothing
    caches the row, a PATCH to ``paused`` takes effect on the ingest
    path's very next lookup (#2880 acceptance criterion 1).
    """

    ACTIVE = "active"
    PAUSED = "paused"


class EventSourceSummary(BaseModel):
    """Narrow list-row shape. Frozen; omits ``extras`` to keep lists small."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    name: str
    slug: str
    kind: EventSourceKind
    auth_strategy: EventSourceAuthStrategy
    status: EventSourceStatus
    secret_ref: str | None
    created_by_sub: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class EventSourceListResponse(BaseModel):
    """``{items, next_cursor}`` list envelope (api-shape-conventions §2).

    ``next_cursor`` is the last row's ``name`` when a further page exists,
    ``None`` when this page exhausted the tenant's live sources.
    """

    model_config = ConfigDict(frozen=True)

    items: list[EventSourceSummary]
    next_cursor: str | None = None


class EventSource(BaseModel):
    """Full read shape. Maps 1:1 to the live ``event_source`` columns.

    Frozen. Carries ``secret_ref`` (the Vault *path*) but never the secret
    value -- there is no ``secret`` field on any read model.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    name: str
    slug: str
    kind: EventSourceKind
    auth_strategy: EventSourceAuthStrategy
    secret_ref: str | None
    status: EventSourceStatus
    extras: dict[str, Any]
    created_by_sub: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class EventSourceCreate(BaseModel):
    """POST body. ``name`` and ``slug`` are immutable after creation.

    ``secret`` is the optional auth secret to custody in Vault. When
    present the create handler writes it to the derived per-tenant Vault
    path and persists that path as ``secret_ref``; when absent the source
    is registered with no secret (``secret_ref`` NULL) and the ingest path
    (#2881) fails closed until a later PATCH sets one. The value is a
    write-only :class:`~pydantic.SecretStr` -- it never appears on a read
    model, in a log line, or in an audit row.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=_NAME_MAX_LENGTH)
    slug: str = Field(min_length=1, max_length=_SLUG_MAX_LENGTH, pattern=_SLUG_PATTERN)
    kind: EventSourceKind
    auth_strategy: EventSourceAuthStrategy
    status: EventSourceStatus = EventSourceStatus.ACTIVE
    extras: dict[str, Any] = Field(default_factory=dict)
    secret: SecretStr | None = None


class EventSourceUpdate(BaseModel):
    """PATCH body. Every field optional; ``name`` / ``slug`` are absent.

    ``name`` and ``slug`` are immutable (rename or re-slug = delete +
    re-create) because ``slug`` is a live routing key external senders are
    already pointed at. Sending ``secret`` rotates the Vault secret in
    place at the source's existing ``secret_ref`` -- a new KV-v2 version at
    the same path, so the ingest path reads the new value on its next
    lookup with no downtime (#2880 acceptance criterion 2). Omitting
    ``secret`` leaves the stored secret untouched.
    """

    model_config = ConfigDict(extra="forbid")

    kind: EventSourceKind | None = None
    auth_strategy: EventSourceAuthStrategy | None = None
    status: EventSourceStatus | None = None
    extras: dict[str, Any] | None = None
    secret: SecretStr | None = None


def project_event_source_to_summary(row: EventSourceORM) -> EventSourceSummary:
    """Project an ``EventSource`` ORM row to the wire summary shape."""
    return EventSourceSummary(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        slug=row.slug,
        kind=EventSourceKind(row.kind),
        auth_strategy=EventSourceAuthStrategy(row.auth_strategy),
        status=EventSourceStatus(row.status),
        secret_ref=row.secret_ref,
        created_by_sub=row.created_by_sub,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )
