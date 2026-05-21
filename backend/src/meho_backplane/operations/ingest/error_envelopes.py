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
