# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault secret custody for the ``event_source`` registry (#2880).

An event source's auth secret (HMAC signing key, static bearer token,
Basic password) lives in Vault, never in the DB. The registry row stores
only a logical ``secret_ref`` path; this module derives that path and
writes the value to it, reusing the secret-broker's vault-kv **sink**
(:class:`~meho_backplane.connectors.secret.vault_endpoint.VaultKvSecretEndpoint`)
so the custody discipline is shared, not re-implemented:

* the value is held as :class:`~pydantic.SecretStr` up to the write and
  handed to a :class:`~meho_backplane.connectors.secret.endpoints.SecretMaterial`
  at the boundary, both of which redact themselves in every repr / log;
* the write runs under the operator's own Vault identity
  (``vault_client_for_operator``), so it stays inside their authz envelope
  and the Vault audit log attributes it to them;
* only the *path* and the *field name* ever reach a log line -- never the
  value.

The derived path sits under the per-tenant Vault subtree the #1643 guard
enforces (``tenants/<tenant_id>/...``), namespaced under ``event-sources/``
so it never collides with a target's ``tenants/<tenant_id>/<name>`` secret.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from pydantic import SecretStr

from meho_backplane.connectors._shared.vault_creds import DEFAULT_KV_MOUNT
from meho_backplane.connectors.secret.endpoints import SecretMaterial
from meho_backplane.connectors.secret.vault_endpoint import VaultKvSecretEndpoint
from meho_backplane.connectors.vault.tenant_paths import TENANT_SECRET_PREFIX

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "EVENT_SOURCE_SECRET_FIELD",
    "event_source_secret_ref",
    "read_event_source_secret",
    "store_event_source_secret",
]

_log = structlog.get_logger(__name__)

#: Logical-path segment separating an event source's secret from a
#: target's secret under the same per-tenant Vault subtree.
_EVENT_SOURCE_SECRET_LEAF = "event-sources"

#: The single KV-v2 field an event source's secret is stored under. The
#: ingest endpoint (#2881) reads this field to authenticate the sender,
#: whatever the ``auth_strategy`` (the signing key / token / password all
#: land here; a Basic username -- not a secret -- lives in ``extras``).
EVENT_SOURCE_SECRET_FIELD = "secret"


def event_source_secret_ref(tenant_id: UUID, slug: str) -> str:
    """Derive the per-tenant logical ``secret_ref`` for a source's secret.

    Returns ``"tenants/<tenant_id>/event-sources/<slug>"`` -- the logical
    KV-v2 path relative to the mount (no mount segment, no ``/data/`` API
    segment; hvac inserts both at read/write time). ``slug`` is already
    validated URL-safe by :class:`~meho_backplane.event_source.schemas.EventSourceCreate`,
    so it needs no further sanitising to key a path leaf.

    The dashed lowercase ``tenant_id`` places the ref inside the per-tenant
    prefix the #1643 Vault-scope guard enforces, and the ``event-sources/``
    segment keeps it clear of a target's ``tenants/<tenant_id>/<name>``.
    """
    return f"{TENANT_SECRET_PREFIX}/{tenant_id}/{_EVENT_SOURCE_SECRET_LEAF}/{slug}"


async def store_event_source_secret(
    operator: Operator,
    secret_ref: str,
    secret: SecretStr,
) -> None:
    """Write *secret* to Vault at *secret_ref* under the operator's identity.

    Reuses the secret-broker vault-kv sink so the value is carried only by
    a :class:`SecretMaterial` (redacted repr) between :class:`SecretStr`
    and the KV-v2 write. ``create_or_update_secret`` writes a new version
    at the same path, so calling this again with a fresh value is a
    zero-downtime rotation: the ingest path reads the latest version on its
    next lookup.

    Raises whatever the vault-kv sink raises on an I/O / auth failure
    (an ``hvac`` error); the caller maps it to a fail-closed HTTP status.
    The value never reaches the raised message -- the sink names only the
    path and field.
    """
    endpoint = VaultKvSecretEndpoint(
        f"{secret_ref}#{EVENT_SOURCE_SECRET_FIELD}", mount=DEFAULT_KV_MOUNT
    )
    _log.info("event_source_secret_write", secret_ref=secret_ref)
    await endpoint.write_secret(operator, SecretMaterial(secret.get_secret_value()))


async def read_event_source_secret(operator: Operator, secret_ref: str) -> SecretStr:
    """Read the source's auth secret from Vault at *secret_ref* under *operator*.

    The read counterpart to :func:`store_event_source_secret`, reusing the
    same secret-broker vault-kv endpoint so custody is shared, not
    re-implemented. The ingest path (#2881) calls this with its synthetic
    ``__ingest__`` operator (a background-dispatch service principal, since
    the JWT-less webhook carries no operator) to fetch the shared HMAC key /
    static token / Basic password it verifies the sender against.

    The value is carried by a :class:`~pydantic.SecretStr` from the KV-v2
    read boundary onward -- it masks itself in every repr / log / traceback,
    so a fail-closed auth-verification path that logs the rejection never
    leaks the material. The value is reachable only through
    :meth:`~pydantic.SecretStr.get_secret_value` at the constant-time
    comparison site.

    Raises whatever the vault-kv source raises on an I/O / auth failure (an
    ``hvac`` error, or :class:`~meho_backplane.connectors.secret.vault_endpoint.VaultSecretRefError`
    when the field is absent); the caller maps it to a fail-closed HTTP
    status. The value never reaches the raised message -- the source names
    only the path and field.
    """
    endpoint = VaultKvSecretEndpoint(
        f"{secret_ref}#{EVENT_SOURCE_SECRET_FIELD}", mount=DEFAULT_KV_MOUNT
    )
    _log.info("event_source_secret_read", secret_ref=secret_ref)
    material = await endpoint.read_secret(operator)
    return SecretStr(material.value.decode("utf-8"))
