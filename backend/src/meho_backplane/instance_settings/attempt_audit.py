# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit keys that name an attempted settings import.

An import that stops (a rollback, a cancelled request, a refusal) leaves one
audit row that names the attempt and no change. The keys of the attempt are
listed here, once. The service, the REST route and the web console page all
use this list, so a key added later reaches every path.

``audit_op_id`` and ``audit_op_class`` are bound by the route. The other
three are bound by :func:`bind_attempt` (the file, before the first check)
and by the service (the tenants, before the first write).
"""

from __future__ import annotations

import hashlib
from typing import Any, Final

import structlog

__all__ = [
    "ATTEMPT_AUDIT_KEYS",
    "attempt_values",
    "bind_attempt",
    "drop_saved_change_keys",
]

ATTEMPT_AUDIT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "audit_op_id",
        "audit_op_class",
        "audit_settings_import",
        "audit_settings_file_sha256",
        "audit_settings_tenant_ids",
    }
)


def attempt_values(file_sha256: str, tenant_ids: list[str]) -> dict[str, Any]:
    """The attempt keys the import binds: the file mark, its hash and its tenants."""
    return {
        "audit_settings_import": True,
        "audit_settings_file_sha256": file_sha256,
        "audit_settings_tenant_ids": tenant_ids,
    }


def bind_attempt(content: bytes) -> None:
    """Name the import and its file in the audit row, before MEHO checks the file.

    The tenants follow later: the service binds them before its first write.
    """
    structlog.contextvars.bind_contextvars(
        audit_settings_import=True,
        audit_settings_file_sha256=hashlib.sha256(content).hexdigest(),
    )


def drop_saved_change_keys(before: set[str]) -> None:
    """Unbind every key the import bound since *before*, except the attempt keys.

    Call it when the import stops. What the row keeps names the attempt only:
    no ``*_before``, ``*_after`` or ``*_changed`` key and no tenant of a change.
    """
    added = set(structlog.contextvars.get_contextvars()) - before - ATTEMPT_AUDIT_KEYS
    structlog.contextvars.unbind_contextvars(*added)
