# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Write and probe helpers for a target's ``secret_field_map`` (#3711).

A ``secret_field_map`` is ``{connector_field: source_field}``. It lets a
secret whose keys differ from the ones a connector reads back the target
without a re-keyed copy. The credential reader applies it (see
:func:`meho_backplane.connectors._shared.vault_creds.secret_field_map_of`);
this module holds the two route-side pieces:

* :func:`merge_secret_field_map` -- how a PATCH changes the stored map.
* :func:`check_secret_field_map_sources` -- the probe-time check that
  every mapped source field exists in the secret.

Both deal in field names only. No secret value is returned, logged, or put
in an error.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import (
    BasicCredentialsTargetLike,
    missing_secret_field_map_sources,
)
from meho_backplane.targets.schemas import SECRET_FIELD_MAP_MAX_ENTRIES

__all__ = [
    "check_secret_field_map_sources",
    "merge_secret_field_map",
]


def merge_secret_field_map(
    current: Mapping[str, str] | None,
    patch: Mapping[str, str | None] | None,
) -> dict[str, str] | None:
    """Return the stored map after a PATCH, or ``None`` for no map.

    ``patch=None`` (the body sent ``"secret_field_map": null``) clears the
    map. Otherwise each entry is merged into *current*: a string adds or
    replaces that entry, ``None`` removes it. An empty result is stored as
    ``None``. A result above the entry limit is a 422.
    """
    if patch is None:
        return None
    merged = dict(current or {})
    for connector_field, source_field in patch.items():
        if source_field is None:
            merged.pop(connector_field, None)
        else:
            merged[connector_field] = source_field
    if len(merged) > SECRET_FIELD_MAP_MAX_ENTRIES:
        raise HTTPException(
            status_code=422,
            detail={
                "kind": "secret_field_map_too_large",
                "message": (
                    f"secret_field_map would have {len(merged)} entries; "
                    f"the limit is {SECRET_FIELD_MAP_MAX_ENTRIES}"
                ),
            },
        )
    return merged or None


async def check_secret_field_map_sources(
    target: BasicCredentialsTargetLike, operator: Operator
) -> None:
    """Fail the probe when a mapped source field is not in the secret.

    Reads the target's secret under *operator* and raises a 422 with
    ``kind="secret_field_map_source_missing"`` naming the missing source
    fields. Does nothing when the target has no map. Store read errors
    propagate to the caller unchanged.
    """
    missing = await missing_secret_field_map_sources(target, operator)
    if not missing:
        return
    raise HTTPException(
        status_code=422,
        detail={
            "kind": "secret_field_map_source_missing",
            "missing_source_fields": missing,
            "message": (
                f"target {target.name!r}: the secret has no field named "
                f"{', '.join(repr(f) for f in missing)}, but the target's "
                "secret_field_map points to it. Fix the map with "
                "`meho targets update --secret-field` or add the field to the secret."
            ),
        },
    )
