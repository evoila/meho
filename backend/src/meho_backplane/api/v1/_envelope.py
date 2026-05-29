# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared ``?envelope=v2`` opt-in helper for list endpoints (G0.16-T6 Finding A).

The convention doc (``docs/codebase/api-shape-conventions.md`` §2)
names the unified list-endpoint shape::

    {
      "items": [ ... ],
      "next_cursor": "<opaque string | null>",
      "budget_status": { ... }   # optional sidecar
    }

This module carries the small bits of glue every list endpoint
adopting the opt-in needs:

* :data:`EnvelopeVersion` — the literal type for the
  ``?envelope=`` query parameter. Today the only accepted value is
  ``"v2"``; the absence of the parameter (``None``) means the
  endpoint should return its v0.8.0 default shape so no client
  breaks.
* :data:`ENVELOPE_QUERY` — a reusable FastAPI :class:`Query`
  declaration. Endpoints accept it as a typed parameter; the type
  system covers the validation, the description text covers the
  operator-facing help in the OpenAPI doc.
* :func:`wrap_v2_envelope` — given a list of items (already
  serialised to JSON-safe dicts) plus optional sidecars, returns
  the unified envelope dict. Mirrors the §2 contract: items always
  first, ``next_cursor`` always second (``None`` when this page is
  the last; the field is *present* either way), sidecars at the
  top level (not under a ``meta`` envelope) so a client that only
  reads ``items`` doesn't walk extra structure.

The migration path codified in §2 of the convention doc — add a
``?envelope=v2`` opt-in, flip the default after two release
cycles, remove the bare/keyed shape three releases later — is
why this helper exists as a separate module rather than as a
private helper inside :mod:`meho_backplane.api.v1.targets`: the
identical helper lands on every list endpoint that opts in, and
the consolidation point is the convention doc, not any one
endpoint.

Code reference: see :func:`meho_backplane.api.v1.targets.list_targets`
for the reference adoption (the first endpoint widened to honour the
opt-in). G0.16-T6 Finding A (#1312) partially landed this round
— the targets endpoint as the reference. The four sister
endpoints
(``conventions`` / ``audit/my-recent`` / ``broadcast/overrides``
/ ``connectors``) plus the CLI / MCP sister-surface forwarding
ship in a follow-up Task; see the result file
``.claude/auto/state/issue-1312-attempt-1.json``
under ``findings_deferred``.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from fastapi import Query

__all__ = [
    "ENVELOPE_QUERY",
    "EnvelopeVersion",
    "wrap_v2_envelope",
]


#: Accepted values of the ``?envelope=`` query parameter. ``None``
#: (omitted) means "return the v0.8.0 default shape" so existing
#: clients keep working. ``"v2"`` opts into the §2 unified envelope.
#: A future ``"v3"`` slot stays open — the literal can widen without
#: a breaking change.
EnvelopeVersion = Literal["v2"]


_ENVELOPE_VALUES: tuple[str, ...] = tuple(get_args(EnvelopeVersion))


#: Reusable :class:`fastapi.Query` declaration. The closure is
#: assigned at module load (rather than inlined into each endpoint
#: signature) so the OpenAPI doc text stays uniform across the
#: adopters; widening the description (or adding a future value)
#: lands in one place.
ENVELOPE_QUERY: Any = Query(
    default=None,
    description=(
        "Opt into the unified list-envelope shape per "
        "docs/codebase/api-shape-conventions.md §2. Pass `v2` to "
        "receive `{items, next_cursor?, ...sidecars}`; omit to keep "
        "the v0.8.0 bare/keyed default. The opt-in is non-breaking "
        "across release cycles — the default flips after two cycles "
        "and the legacy shape is removed three cycles after that "
        "(G0.16-T6 Finding A #1312)."
    ),
)


def wrap_v2_envelope(
    items: list[Any],
    *,
    next_cursor: str | None = None,
    **sidecars: Any,
) -> dict[str, Any]:
    """Return the §2 unified envelope around *items*.

    Args:
        items: The serialised list contents. Caller is responsible
            for the per-item shape (typically the endpoint's existing
            response_model entry projected via ``model_dump(mode='json')``
            so UUIDs / datetimes render as strings; same shape the
            bare list would have carried in the v0.8.0 response).
        next_cursor: Forward-only continuation cursor; ``None`` when
            the current page exhausted the matching set. The field
            is always present in the envelope so clients can read
            ``response["next_cursor"]`` without a ``KeyError`` guard.
        **sidecars: Endpoint-specific top-level fields
            (e.g. ``budget_status`` for ``conventions``). Per §2 the
            sidecars are NOT nested under a ``meta`` envelope.

    Returns:
        ``{"items": items, "next_cursor": next_cursor, **sidecars}``.
        ``items`` first, ``next_cursor`` second, sidecars trailing
        so the JSON renders with a predictable order
        (Python ``dict`` preserves insertion order since 3.7;
        FastAPI's JSON encoder preserves the order through
        :func:`json.dumps`).
    """
    return {
        "items": items,
        "next_cursor": next_cursor,
        **sidecars,
    }
