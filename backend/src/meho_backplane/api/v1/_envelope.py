# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared ``?envelope=v2`` opt-in helper for the topology closure reads.

The §2 list-envelope migration is complete: every reference GET-list
endpoint returns the unified ``{items, next_cursor?, ...sidecars}``
shape unconditionally (``docs/codebase/api-shape-conventions.md`` §2,
#2338 breaking pass), so the ``?envelope=v2`` opt-in they used to bridge
the migration was retired along with the ``wrap_v2_envelope`` builder.

What remains here is the opt-in glue for the topology closure reads
(``GET /api/v1/topology/dependents/{name}`` /
``dependencies/{name}``). Those converge on the §4 REST↔MCP
``{"kind": ..., "nodes": [...]}`` discriminated shape rather than the §2
``items`` shape, and still gate that shape behind ``?envelope=v2`` (their
default stays the v0.8.0 bare ``list[TopologyNode]`` pending their own
flip):

* :data:`EnvelopeVersion` — the literal type for the ``?envelope=``
  query parameter. Today the only accepted value is ``"v2"``; the
  absence of the parameter (``None``) means the endpoint returns its
  v0.8.0 default shape.
* :data:`ENVELOPE_QUERY` — a reusable FastAPI :class:`Query`
  declaration the topology reads accept as a typed parameter.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from fastapi import Query

__all__ = [
    "ENVELOPE_QUERY",
    "EnvelopeVersion",
]


#: Accepted values of the ``?envelope=`` query parameter. ``None``
#: (omitted) means "return the v0.8.0 default shape" so existing
#: clients keep working. ``"v2"`` opts into the §4 discriminated
#: envelope. A future ``"v3"`` slot stays open — the literal can widen
#: without a breaking change.
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
        "Opt into the unified REST↔MCP envelope shape per "
        "docs/codebase/api-shape-conventions.md §4. Pass `v2` to "
        "receive `{kind, nodes}`; omit to keep the v0.8.0 bare-list "
        "default. The opt-in is non-breaking across release cycles "
        "(G0.16-T6 Finding E #1312)."
    ),
)
