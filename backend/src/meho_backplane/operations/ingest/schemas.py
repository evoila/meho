# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""In-memory shapes produced by :func:`parse_openapi`.

The parser is pure (no DB writes, no LLM calls). It returns a list of
:class:`EndpointDescriptorProto` values that T2 (#403) consumes via
``register_ingested_operations()`` to upsert
:class:`meho_backplane.db.models.EndpointDescriptor` rows.

Decouples parsing from persistence so the parser stays trivially
testable without an event loop, a DB session, or a tenant context.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EndpointDescriptorProto",
    "SafetyLevel",
]

SafetyLevel = Literal["safe", "caution", "dangerous"]


class EndpointDescriptorProto(BaseModel):
    """Intermediate representation between an OpenAPI operation and the ORM row.

    Each field maps 1:1 to a column on
    :class:`meho_backplane.db.models.EndpointDescriptor`. Fields the
    parser cannot populate (``tenant_id``, ``embedding``, ``group_id``,
    ``source_kind``, ``handler_ref``, ``llm_instructions``, ``custom_*``)
    are owned by T2 (registration) / T3 (LLM grouping) / T4 (operator
    review).

    ``parameter_schema`` is a JSON Schema 2020-12 object with one
    property per OpenAPI parameter. Each property carries an
    ``x-meho-param-loc`` extension keyword (``"path"`` / ``"query"`` /
    ``"header"`` / ``"body"``) so the dispatcher (G0.6-T5 #396) can
    split params back into the correct HTTP slot at call time. The
    flat shape gives the dispatcher one JSON Schema to validate
    against; the extension preserves the per-param routing information
    OpenAPI's nested ``parameters[]`` array would otherwise lose.

    The model is ``frozen=True`` so parser output cannot be mutated
    between parse and persist (defence against in-place edits that
    would silently divert from the upstream spec). Field reassignment
    raises ``ValidationError``; container fields (``tags``,
    ``parameter_schema``, ``response_schema``) remain mutable at the
    container level by Pydantic v2 default — callers treat them as
    read-only.
    """

    model_config = ConfigDict(frozen=True)

    op_id: str
    """Connector-side natural key — ``f"{method.upper()}:{path}"`` for
    ingested OpenAPI rows (e.g. ``"GET:/api/vcenter/cluster"``)."""

    method: str
    """Upper-cased HTTP verb (``"GET"`` / ``"POST"`` / ``"PUT"`` /
    ``"PATCH"`` / ``"DELETE"`` / ``"HEAD"`` / ``"OPTIONS"``)."""

    path: str
    """URL path template with ``{var}`` placeholders. Verbatim from the
    spec's ``paths`` key, except that a relative OpenAPI server base
    (``servers:[{url:"/api/v2"}]``) is folded onto the front at ingest
    (``/version`` -> ``/api/v2/version``, #1796) so the dispatcher's
    ``host:port + path`` join honours the Server Object."""

    summary: str | None = None
    description: str | None = None

    tags: list[str] = Field(default_factory=list)
    """Raw OpenAPI ``tags[]`` array plus the synthetic
    ``"spec:<source>"`` tag injected by the parser when
    ``spec_source`` is passed. T3's LLM grouping pass uses these as a
    starting hint but is free to override."""

    parameter_schema: dict[str, object] = Field(default_factory=dict)
    """Flattened JSON Schema 2020-12 object with
    ``x-meho-param-loc`` per property. Empty dict when the operation
    takes no params."""

    response_schema: dict[str, object] | None = None
    """JSON Schema for the operation's success response
    (``2xx``-status ``application/json`` content). ``None`` when the
    spec declares no success-response schema."""

    safety_level: SafetyLevel = "safe"
    """Heuristic from the HTTP verb. ``GET`` / ``HEAD`` / ``OPTIONS``
    → ``safe``; ``POST`` / ``PUT`` / ``PATCH`` → ``caution``;
    ``DELETE`` → ``dangerous``. Operator can override at review
    (T4 state machine)."""

    requires_approval: bool = False
    """Always ``False`` at parse time. Operators flip per-op during
    review (T4) for ops that should pause the dispatcher for
    out-of-band approval."""
