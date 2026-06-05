# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared structured-detail builders for ingest validation errors.

The REST route at :mod:`meho_backplane.api.v1.connectors_ingest` and
the MCP ingest tool at :mod:`meho_backplane.mcp.tools.connector_ingest`
both need to surface the typed ingest ``SpecError`` siblings —
:class:`VersionMismatchError`, :class:`UncoveredVersionLabel`,
:class:`UpstreamNotSpecError`, :class:`UnsupportedSpecError`,
:class:`InvalidSpecError`, :class:`InvalidSchemaError`,
:class:`OpIdCollision`, :class:`LlmOutputInvalid` — as caller-input
validation errors carrying structured diagnostic detail (expected-vs-
received versions, the offending spec flavour, the colliding op-ids,
the failing grouping pass) so the operator — or the agent acting on
the operator's behalf — can self-correct without re-prompting.

Both routes used to build their own envelopes; the REST path even
shipped a structured 422 detail dict (G0.9-T8, #740) while the MCP
path let the exception propagate to the dispatcher's generic
:data:`~meho_backplane.mcp.schemas.INTERNAL_ERROR` handler, which
stripped the (already-detailed) exception message and emitted
``"internal error: VersionMismatchError"`` — opaque to the agent and
misclassified as ``-32603``. G0.9.1-T5 (#777) lifted the first two
detail builders into this module so the envelopes can't drift again;
#1534 added the remaining sibling builders and wired the MCP inline
path to all of them, closing the same ``-32603``-stripping hole for
the rest of the set.

The shape returned here is a JSON-safe ``dict`` (no UUIDs / datetimes
/ Pydantic models) — REST embeds it in the ``HTTPException.detail``
field; MCP embeds it in the JSON-RPC ``error.data`` member (spec
§5.1: "A Primitive or Structured value that contains additional
information about the error").
"""

from __future__ import annotations

from typing import Any

from meho_backplane.operations.ingest.exceptions import (
    InvalidSchemaError,
    InvalidSpecError,
    LlmOutputInvalid,
    OpIdCollision,
    UncoveredVersionLabel,
    UnsupportedSpecError,
    VersionMismatchError,
)

__all__ = [
    "build_catalog_entry_malformed_detail",
    "build_catalog_entry_not_found_detail",
    "build_catalog_entry_typed_connector_detail",
    "build_catalog_entry_upstream_not_spec_detail",
    "build_invalid_schema_detail",
    "build_invalid_spec_detail",
    "build_llm_output_invalid_detail",
    "build_op_id_collision_detail",
    "build_uncovered_version_label_detail",
    "build_unsupported_spec_detail",
    "build_upstream_not_spec_detail",
    "build_version_mismatch_detail",
]


def build_version_mismatch_detail(exc: VersionMismatchError) -> dict[str, Any]:
    """Structured detail for :class:`VersionMismatchError`.

    Body shape (load-bearing — the CLI and the MCP-driving agent both
    parse it):

    * ``kind`` — ``"spec_label_mismatch"`` or ``"multi_spec_inconsistent"``;
      :class:`VersionMismatchError` already carries this on the
      attribute and the route layer surfaces it so callers can branch
      without re-parsing the message.
    * ``requested_version`` — the operator-supplied
      :class:`~meho_backplane.operations.ingest.IngestRequest.version`
      label.
    * ``spec_info_versions`` — list of ``{spec_uri, info_version}``
      objects, one per spec participating in the failure. For the
      ``spec_label_mismatch`` kind only the mismatching specs are
      listed; for ``multi_spec_inconsistent`` every spec in the
      bundle is listed so the operator sees the conflict at a glance.
    * ``message`` — the rendered exception string. Carried so a
      client that ignores the structured fields still gets the
      human-readable detail.
    """
    return {
        "kind": exc.kind,
        "requested_version": exc.requested_version,
        "spec_info_versions": [
            {"spec_uri": uri, "info_version": version} for uri, version in exc.spec_info_versions
        ],
        "message": str(exc),
    }


def build_catalog_entry_not_found_detail(
    *,
    catalog_entry: str,
    available_entries: list[str],
) -> dict[str, Any]:
    """Structured 422 detail for ``POST /api/v1/connectors/ingest``
    when the operator-supplied ``catalog_entry`` is well-formed but
    not present in the packaged catalog (G0.14-T9 / #1150).

    Body shape (T11 convention, see
    :doc:`docs/codebase/error-message-shape.md`):

    * ``detail`` — stable ``snake_case`` classifier
      ``"catalog_entry_not_found"`` so callers can branch without
      re-parsing the message.
    * ``catalog_entry`` — the operator-supplied
      ``"<product>/<version>"`` reference, echoed back so an agent
      doesn't need to re-thread it from its own prompt.
    * ``available_entries`` — sorted list of ``"<product>/<version>"``
      strings the catalog actually contains. An agent picks one and
      retries; an interactive operator gets the same enumeration the
      ``meho connector catalog list`` verb prints.
    * ``message`` — the rendered human-readable detail for clients
      that ignore the structured fields.
    """
    sorted_available = sorted(set(available_entries))
    return {
        "detail": "catalog_entry_not_found",
        "catalog_entry": catalog_entry,
        "available_entries": sorted_available,
        "message": (
            f"catalog_entry_not_found: no catalog entry for {catalog_entry!r}; "
            f"available: {', '.join(sorted_available) if sorted_available else '<empty catalog>'}. "
            "Pick one of the available entries or use the explicit-quadruple shape. "
            "See docs/codebase/error-message-shape.md."
        ),
    }


def build_catalog_entry_malformed_detail(
    *,
    catalog_entry: str,
) -> dict[str, Any]:
    """Structured 422 detail when the operator's ``catalog_entry`` does
    not match the ``"<product>/<version>"`` shape (G0.14-T9 / #1150).

    Distinct from ``catalog_entry_not_found`` so an agent can branch on
    "the reference itself is malformed" (fix the slash) vs "the
    reference is well-formed but no entry exists" (fix the value). Per
    T11 convention.
    """
    return {
        "detail": "catalog_entry_malformed",
        "catalog_entry": catalog_entry,
        "message": (
            f"catalog_entry_malformed: {catalog_entry!r} is not "
            f"'<product>/<version>' (e.g. 'vmware/9.0'). "
            "See docs/codebase/error-message-shape.md."
        ),
    }


def build_catalog_entry_typed_connector_detail(
    *,
    catalog_entry: str,
    product: str,
    version: str,
    impl_id: str,
) -> dict[str, Any]:
    """Structured 422 detail when the operator's ``catalog_entry``
    resolves to a typed connector with no ingestable spec
    (``upstream is None``) — there is nothing to ingest
    (G0.14-T9 / #1150).

    The detail names the resolved triple so an interactive operator
    sees that the entry exists in the catalog but is intentionally
    typed (e.g. ``vault/1.x``, ``k8s/1.x``, ``bind9/9.x``). Per T11
    convention; the doc reference belongs at the operator's checked-
    out clone.
    """
    return {
        "detail": "catalog_entry_typed_connector",
        "catalog_entry": catalog_entry,
        "product": product,
        "version": version,
        "impl_id": impl_id,
        "message": (
            f"catalog_entry_typed_connector: {catalog_entry!r} is a typed "
            f"connector ({product}/{version}/{impl_id}) with no ingestable "
            "spec; nothing to ingest. See docs/cross-repo/connector-catalog.md. "
            "See docs/codebase/error-message-shape.md."
        ),
    }


def build_catalog_entry_upstream_not_spec_detail(
    *,
    catalog_entry: str,
    upstream_url: str,
    content_type: str | None,
) -> dict[str, Any]:
    """Structured 422 detail for catalog-driven ingest when the upstream URL
    served non-spec content (HTML developer portal, etc.) -- G0.15-T2 / #1211.

    Concrete trigger: ``vmware/9.0`` and ``sddc-manager/9.0`` upstream URLs
    point at the Broadcom Developer Portal landing pages, which return HTML,
    not OpenAPI YAML/JSON. Before this envelope, the route fell through to
    the parser's generic ``could not decode spec`` 400 ("while scanning for
    the next token found character that cannot start any token in '<file>',
    line 33, column 1") -- a true statement about the bytes but a useless
    one for the operator. T11 convention says: name the values, name the
    remediation, point at the doc.

    Body shape (T11 convention, see
    :doc:`docs/codebase/error-message-shape.md`):

    * ``detail`` -- stable ``snake_case`` classifier
      ``"catalog_entry_upstream_not_spec"`` so callers branch without
      re-parsing the message.
    * ``catalog_entry`` -- the operator-supplied
      ``"<product>/<version>"`` reference, echoed back so an agent does
      not need to re-thread it from its own prompt.
    * ``upstream_url`` -- the URL the route fetched and rejected.
    * ``content_type`` -- the verbatim ``Content-Type`` header the
      server returned (e.g. ``"text/html; charset=utf-8"``). ``null``
      when the server omitted the header entirely.
    * ``message`` -- the rendered human-readable detail with the
      remediation step (fetch the spec manually, pass via the explicit-
      quadruple shape) for clients that ignore the structured fields.
    """
    rendered_ct = repr(content_type) if content_type is not None else "<missing>"
    return {
        "detail": "catalog_entry_upstream_not_spec",
        "catalog_entry": catalog_entry,
        "upstream_url": upstream_url,
        "content_type": content_type,
        "message": (
            f"catalog_entry_upstream_not_spec: {catalog_entry!r} upstream "
            f"{upstream_url!r} returned non-spec content "
            f"(Content-Type={rendered_ct}); the URL serves a developer-portal "
            "landing page or other non-OpenAPI content, not raw YAML/JSON. "
            "Fetch the spec manually and pass it via the explicit-quadruple "
            "shape (product/version/impl_id/specs[]) -- the operator's "
            "checked-out clone, an appliance URL, or a mirrored file path. "
            "See docs/cross-repo/connector-catalog.md. "
            "See docs/codebase/error-message-shape.md."
        ),
    }


def build_upstream_not_spec_detail(
    *,
    upstream_url: str,
    content_type: str | None,
) -> dict[str, Any]:
    """Structured 422 detail for explicit-quadruple ingest when an
    operator-supplied spec URL served non-spec content -- G0.15-T2 / #1211.

    Same diagnostic as :func:`build_catalog_entry_upstream_not_spec_detail`
    but without the ``catalog_entry`` field -- explicit-quadruple requests
    pass spec URIs directly via ``specs[]``, so there is no catalog reference
    to echo back. The route layer picks whichever builder the request shape
    produced.
    """
    rendered_ct = repr(content_type) if content_type is not None else "<missing>"
    return {
        "detail": "upstream_not_spec",
        "upstream_url": upstream_url,
        "content_type": content_type,
        "message": (
            f"upstream_not_spec: upstream {upstream_url!r} returned "
            f"non-spec content (Content-Type={rendered_ct}); the URL serves "
            "a developer-portal landing page or other non-OpenAPI content, "
            "not raw YAML/JSON. Supply a URL that serves OpenAPI YAML/JSON "
            "directly, or pass a local file path. "
            "See docs/codebase/error-message-shape.md."
        ),
    }


def build_uncovered_version_label_detail(
    exc: UncoveredVersionLabel,
) -> dict[str, Any]:
    """Structured detail for :class:`UncoveredVersionLabel`.

    Body shape:

    * ``product`` / ``version`` / ``impl_id`` — the connector triple
      the operator submitted, surfaced so the agent doesn't need to
      re-thread them from its own prompt.
    * ``registered_classes`` — list of ``{class_name, version,
      impl_id, supported_version_range}`` objects, one per existing
      registered v2 connector class for the ``(product, impl_id)``
      pair. The agent picks a label inside one of these ranges and
      retries.
    * ``message`` — the rendered exception string, for clients that
      ignore the structured fields.
    """
    return {
        "product": exc.product,
        "version": exc.version,
        "impl_id": exc.impl_id,
        "registered_classes": [
            {
                "class_name": class_name,
                "version": cand_version,
                "impl_id": cand_impl_id,
                "supported_version_range": supported_range,
            }
            for cand_version, cand_impl_id, class_name, supported_range in exc.candidates
        ],
        "message": str(exc),
    }


def build_unsupported_spec_detail(exc: UnsupportedSpecError) -> dict[str, Any]:
    """Structured detail for :class:`UnsupportedSpecError`.

    The exception already carries a remedy-bearing message — for Swagger
    2.0 it names the ``swagger2openapi`` / ``converter.swagger.io``
    conversion path, for cross-document ``$ref`` and OpenAPI 4.x it names
    the offending shape. The builder surfaces the stable ``detail``
    classifier so a client branches without re-parsing prose, and carries
    the message verbatim (T11 #1141 shape — detected-vs-expected +
    remedy already live on the message string).

    There is no machine-resolvable field beyond the message: the remedy
    is a manual conversion or a v0.2.next request, not a retry with
    corrected params, so the envelope stays code-plus-message.
    """
    return {
        "detail": "unsupported_spec",
        "message": str(exc),
    }


def build_invalid_spec_detail(exc: InvalidSpecError) -> dict[str, Any]:
    """Structured detail for :class:`InvalidSpecError`.

    The document is not a structurally valid OpenAPI spec (missing
    ``paths``, not a mapping, unreadable local file). The exception
    message names the structural fault; the builder adds the stable
    ``invalid_spec`` classifier so the agent can branch on "fix the
    document" without re-parsing the message (T11 #1141 shape).
    """
    return {
        "detail": "invalid_spec",
        "message": str(exc),
    }


def build_invalid_schema_detail(exc: InvalidSchemaError) -> dict[str, Any]:
    """Structured detail for :class:`InvalidSchemaError`.

    A referenced JSON Schema is structurally broken (dangling ``$ref``,
    component-path drill-down, non-list parameters). The message names
    the offending reference; the builder adds the stable
    ``invalid_schema`` classifier (T11 #1141 shape).
    """
    return {
        "detail": "invalid_schema",
        "message": str(exc),
    }


def build_op_id_collision_detail(exc: OpIdCollision) -> dict[str, Any]:
    """Structured detail for :class:`OpIdCollision`.

    Unlike the other parser-family siblings, :class:`OpIdCollision`
    carries machine-resolvable attributes (the colliding ``op_ids`` and
    the connector triple), so the envelope ships them as structured
    fields per the T11 #1141 ``data``-payload rule — an agent can name
    the offending op-ids to the operator, or decide which spec to drop,
    without re-parsing the message. ``existing_spec_source`` /
    ``incoming_spec_source`` are present only for the cross-call branch
    (``None`` for within-batch collisions); they are surfaced so the
    agent can see which two specs fight over the ``op_id``.
    """
    return {
        "detail": "op_id_collision",
        "op_ids": list(exc.op_ids),
        "product": exc.product,
        "version": exc.version,
        "impl_id": exc.impl_id,
        "existing_spec_source": exc.existing_spec_source,
        "incoming_spec_source": exc.incoming_spec_source,
        "message": str(exc),
    }


def build_llm_output_invalid_detail(exc: LlmOutputInvalid) -> dict[str, Any]:
    """Structured detail for :class:`LlmOutputInvalid`.

    The grouping-pass LLM returned output that failed schema validation.
    The builder names the failing ``pass_name`` (``"propose_groups"`` /
    ``"assign_ops"``) so an agent can branch on which pass to retry, and
    carries the message verbatim. The raw LLM output is **not** surfaced
    on the wire — it can be large and is debug-log material, not response
    material — only the (already truncated) message preview travels in
    ``message`` (T11 #1141 shape).
    """
    return {
        "detail": "llm_output_invalid",
        "pass_name": exc.pass_name,
        "message": str(exc),
    }
