# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Canonical free-text filter query param across list/search surfaces (#1854).

The convention doc (``docs/codebase/api-shape-conventions.md`` §15)
names ``q`` the single free-text filter parameter on every list/search
surface. Before #1854 the same concept was spelled three ways:

* ``GET /api/v1/kb`` -> ``filter``
* ``GET /api/v1/memory`` -> ``slug_pattern``
* ``GET /api/v1/operations/search`` -> ``query``

so an operator who reached for the obvious ``?q=`` (or reused one
surface's name on another) had their filter **silently dropped**.

This module carries the glue every surface needs to converge on ``q``
while keeping its v0.x name working as a deprecated alias (the §14.2
MCP-cursor pattern, applied on the REST side):

* :data:`FREE_TEXT_Q_QUERY` — the canonical ``q`` :class:`Query`
  declaration. Every adopting surface accepts it verbatim so the
  OpenAPI doc (and the generated CLI client) carry one shape.
* :func:`resolve_free_text_filter` — collapses the canonical ``q`` and
  the surface's deprecated legacy param into one value, enforcing XOR:
  passing both with different values raises a 422 with a clear message
  rather than silently honouring one. Passing both with the *same*
  value is tolerated (a caller that sets ``q`` and the legacy name to
  the same string is unambiguous).

Migration shape mirrors §2's ``?envelope=v2`` discipline and §14.2's
MCP-cursor alias: ``q`` is canonical now, the legacy names survive as
``deprecated: true`` OpenAPI params through the next two release
cycles, and a later sweep drops them.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Query
from fastapi import status as http_status

#: One operator-facing description for the canonical ``q`` param, shared
#: so every surface's OpenAPI doc carries identical help text.
FREE_TEXT_Q_DESCRIPTION = (
    "Free-text filter. Canonical across the kb / memory / "
    "operations-search list surfaces. On `kb` this is a SQL `LIKE` "
    "pattern; on `memory` a slug substring; on `operations/search` "
    "the hybrid-retrieval query. Supersedes the per-surface legacy "
    "param (`filter` / `slug_pattern` / `query`), which stays accepted "
    "but deprecated."
)


def free_text_q_query(*, max_length: int, **extra: Any) -> Any:
    """Build the canonical ``q`` :class:`Query` for a list/search surface.

    Each surface passes its own ``max_length`` (the per-surface slug /
    pattern ceiling) so ``q`` inherits the exact constraint its legacy
    param carried; everything else (the shared description, the
    ``None`` default that makes it optional) is fixed here so the param
    reads identically across surfaces in the OpenAPI doc. ``extra``
    forwards surface-specific knobs (e.g. ``min_length`` on
    operations-search).
    """
    return Query(
        default=None,
        max_length=max_length,
        description=FREE_TEXT_Q_DESCRIPTION,
        **extra,
    )


def resolve_free_text_filter(
    *,
    q: str | None,
    legacy_value: str | None,
    legacy_name: str,
) -> str | None:
    """Collapse the canonical ``q`` and a surface's legacy param to one value.

    ``q`` is canonical; ``legacy_value`` is the surface's pre-#1854 name
    (``filter`` / ``slug_pattern`` / ``query``). Resolution:

    * Only one set -> that value wins.
    * Neither set -> ``None`` (the surface decides whether that is an
      error; the kb/memory list surfaces treat it as "no filter", the
      operations-search surface requires a value and 422s separately).
    * Both set to the **same** string -> that value (unambiguous; a
      caller mirroring ``q`` onto the legacy name is fine).
    * Both set to **different** strings -> 422 ``ambiguous_free_text_filter``.
      The conflict is surfaced rather than silently honouring one, which
      is the exact silent-ignore foot-gun #1854 retires.
    """
    if q is not None and legacy_value is not None and q != legacy_value:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"ambiguous_free_text_filter: 'q' and the deprecated "
                f"'{legacy_name}' were both supplied with different "
                f"values. Use 'q' alone ('{legacy_name}' is deprecated)."
            ),
        )
    return q if q is not None else legacy_value
