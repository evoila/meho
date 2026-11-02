# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Checks an approved request passes before it runs.

A person approves one call: one target with its connection settings and,
when the request has a preview hash, one exact preview of the request.
Every way an approved request runs (the approve surfaces, the agent's own
resume and the re-run after a failure) compares that with the state now
and refuses to run when it differs:

* :func:`compute_target_hash` hashes the target's connection settings.
  :func:`park_time_target_hash` stores it when the request is made, and
  :func:`target_changed_since_request` compares it with the target as it
  is now. A rename, a new alias, notes or a new detected version do not
  count; only the fields that decide where and how MEHO connects do.
* :func:`preview_changed_since_request` builds the preview again and
  compares its hash with the one the request stored.

The refusal results are
:func:`~meho_backplane.operations._errors.result_target_changed_since_approval`
and
:func:`~meho_backplane.operations._errors.result_preview_changed_since_approval`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select

from meho_backplane.db.models import ApprovalRequest, Target

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from meho_backplane.auth.operator import Operator

__all__ = [
    "TARGET_HASH_FIELDS",
    "compute_target_hash",
    "park_time_target_hash",
    "preview_changed_since_request",
    "target_changed_since_request",
]

#: The target fields that decide where and how MEHO connects. A change to
#: any of them after a request was made means the approval no longer
#: covers the run.
TARGET_HASH_FIELDS: Final[tuple[str, ...]] = (
    "product",
    "host",
    "port",
    "fqdn",
    "secret_ref",
    "secret_field_map",
    "auth_model",
    "vpn_required",
    "verify_tls",
    "tls_ca_pin",
    "tls_server_name",
    "extras",
)


def compute_target_hash(values: Mapping[str, Any]) -> str:
    """Return a SHA-256 hex over the :data:`TARGET_HASH_FIELDS` of *values*.

    Same canonical JSON as the params and preview hashes
    (``sort_keys=True``, ``default=str``, compact separators).
    """
    source = {field: values.get(field) for field in TARGET_HASH_FIELDS}
    canonical = json.dumps(source, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _target_values(target: Any) -> dict[str, Any]:
    return {field: getattr(target, field, None) for field in TARGET_HASH_FIELDS}


async def park_time_target_hash(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    target_id: uuid.UUID | None,
) -> str | None:
    """Hash the stored connection settings of *target_id*, or ``None``.

    Reads the columns from the database, not the in-memory target of the
    call: a call may change a field in memory for itself only (the per-call
    ``fqdn``), and the approved run loads the target again by id. Selects
    plain columns, so a target object already in *session* is not touched.
    ``None`` when there is no target or no live row for it.
    """
    if target_id is None:
        return None
    columns = [getattr(Target, field) for field in TARGET_HASH_FIELDS]
    result = await session.execute(
        select(*columns).where(
            Target.id == target_id,
            Target.tenant_id == tenant_id,
            Target.deleted_at.is_(None),
        )
    )
    row = result.first()
    if row is None:
        return None
    return compute_target_hash(dict(row._mapping))


def target_changed_since_request(request: ApprovalRequest, target: Any) -> bool:
    """True when *target* no longer has the connection settings of the request.

    ``False`` when the request stored no hash (no target, or a row saved
    before the hash existed).
    """
    if request.target_hash is None or target is None:
        return False
    return compute_target_hash(_target_values(target)) != request.target_hash


async def preview_changed_since_request(
    operator: Operator,
    request: ApprovalRequest,
    target: Any,
    params: dict[str, Any],
) -> bool:
    """True when the request's preview hash differs from a preview built now.

    Only a request that stored a preview hash is checked (a ``destructive``
    request always has one). A preview that does not build counts as
    changed.
    """
    if request.preview_hash is None:
        return False
    from meho_backplane.operations._request_preview import (
        compute_preview_hash,
        preview_dispatch,
    )

    envelope = await preview_dispatch(
        operator=operator,
        connector_id=request.connector_id,
        op_id=request.op_id,
        target=target,
        params=params,
    )
    if envelope.get("status") != "ok":
        return True
    return compute_preview_hash(envelope) != request.preview_hash
