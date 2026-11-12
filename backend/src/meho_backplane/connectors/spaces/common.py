# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared helpers of the ``spaces.*`` handlers."""

from __future__ import annotations

import uuid
from typing import Any, Final

from meho_backplane.settings import get_settings
from meho_backplane.spaces.models import ResourceAssignment

__all__ = [
    "SPACES_CONNECTOR_ID",
    "assignment_dict",
    "parse_uuid",
    "require_enabled",
]

#: Wire connector id of the built-in spaces connector.
SPACES_CONNECTOR_ID: Final[str] = "spaces-records-1.x"


def require_enabled() -> None:
    """Refuse the call while spaces are off (the default)."""
    if not get_settings().spaces_enabled:
        raise ValueError("Spaces are off. Set SPACES_ENABLED=true to use them.")


def parse_uuid(params: dict[str, Any], field: str) -> uuid.UUID:
    """Read a UUID parameter; a bad value is a clear error."""
    raw = params.get(field)
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise ValueError(f"{field} must be a UUID, got {raw!r}") from exc


def assignment_dict(row: ResourceAssignment) -> dict[str, Any]:
    """The answer shape of one assignment row."""
    return {
        "assignment_id": str(row.id),
        "space_id": str(row.space_id),
        "kind": row.kind,
        "target_id": str(row.target_id) if row.target_id is not None else None,
        "object_id": row.object_id,
        "name": row.name_at_assignment,
        "access": row.access,
        "valid_from": row.valid_from.isoformat(),
        "valid_to": row.valid_to.isoformat() if row.valid_to is not None else None,
    }
