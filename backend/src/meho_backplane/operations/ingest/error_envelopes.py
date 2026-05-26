# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared structured-detail builders for ingest validation errors.

The REST route at :mod:`meho_backplane.api.v1.connectors_ingest` and
the MCP admin tool at :mod:`meho_backplane.mcp.tools.connector_admin`
both need to surface :class:`VersionMismatchError` and
:class:`UncoveredVersionLabel` as caller-input validation errors
carrying structured diagnostic detail (expected-vs-received versions,
the list of advertised ``supported_version_range`` strings) so the
operator — or the agent acting on the operator's behalf — can self-
correct without re-prompting.

Both routes used to build their own envelopes; the REST path even
shipped a structured 422 detail dict (G0.9-T8, #740) while the MCP
path let the exception propagate to the dispatcher's generic
:data:`~meho_backplane.mcp.schemas.INTERNAL_ERROR` handler, which
stripped the (already-detailed) exception message and emitted
``"internal error: VersionMismatchError"`` — opaque to the agent and
misclassified as ``-32603``. G0.9.1-T5 (#777) lifts both detail
builders into this module so the envelopes can't drift again.

The shape returned here is a JSON-safe ``dict`` (no UUIDs / datetimes
/ Pydantic models) — REST embeds it in the ``HTTPException.detail``
field; MCP embeds it in the JSON-RPC ``error.data`` member (spec
§5.1: "A Primitive or Structured value that contains additional
information about the error").
"""

from __future__ import annotations

from typing import Any

from meho_backplane.operations.ingest.exceptions import (
    UncoveredVersionLabel,
    VersionMismatchError,
)

__all__ = [
    "build_catalog_entry_malformed_detail",
    "build_catalog_entry_not_found_detail",
    "build_catalog_entry_typed_connector_detail",
    "build_uncovered_version_label_detail",
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
