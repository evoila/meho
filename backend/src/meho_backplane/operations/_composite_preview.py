# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Synthetic preview for governed-tier non-ingested ops (#3198 / #3312).

The counterpart to :mod:`._request_preview`, which previews the literal
would-be HTTP request of an ``source_kind='ingested'`` op (#1683). A
``typed`` / ``composite`` op runs a Python handler and has no single literal
HTTP request, so by default it previews as ``status="unavailable"``. The two
governed approval tiers are the exception and MUST be previewable:

* **``destructive``** (#3198): the governed-delete gate refuses to park the
  op unless a ``preview_operation`` of the identical tuple bound a matching
  ``preview_hash`` (decision ``governed-delete-operations.md`` requirement 2).
* **``requires_approval``** (#3312): the ``dangerous``-tier typed ops
  (canonical case ``vault.kv.delete``) park for a human. The approver already
  sees a rich park-time ``proposed_effect``; previewing removes the
  pre-dispatch asymmetry so the calling agent reads the *same* effect block
  instead of ``preview_unavailable``.

The synthetic preview binds the *logical request tuple* (the redacted params
on the ``redacted_body`` slot, a ``COMPOSITE`` sentinel ``method``, the
``op_id`` as ``resolved_path``) so :func:`compute_preview_hash` stays
param-sensitive, and layers on the reused park-time ``proposed_effect``. No
handler runs, no connector is dialled, and nothing is sent — it is a pure,
egress-free projection.
"""

from __future__ import annotations

from typing import Any, Final

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._preview import (
    PreviewContext,
    build_proposed_effect,
    describe_preview_provenance,
)
from meho_backplane.operations._request_preview import (
    _redact_request_body,
    compute_preview_hash,
)

__all__ = ["build_composite_preview"]

_log = structlog.get_logger(__name__)

#: Sentinel ``method`` for a synthetic composite/typed preview (#3198) —
#: there is no HTTP verb, but the slot is one of the hashed keys, so a
#: constant keeps the hash stable while the params on ``redacted_body`` make
#: it param-sensitive.
_COMPOSITE_PREVIEW_METHOD: Final[str] = "COMPOSITE"


async def _synthetic_proposed_effect(
    *,
    operator: Operator,
    connector_id: str,
    descriptor: EndpointDescriptor,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Reuse the park-time ``proposed_effect`` builder for a pre-dispatch preview (#3312).

    Runs :func:`~meho_backplane.operations._preview.build_proposed_effect` — the
    *same* park-path builder, so no effect logic is duplicated and redaction is
    identical — but with ``connector_instance=None`` to keep the preview
    **egress-free**: a pure builder (``vault.kv.delete``'s ``_kv_delete_preview``
    reads only its params) populates, while a live-read blast-radius builder
    declines for lack of a connector. Returns the effect only when genuinely
    populated (bespoke ``preview`` or generic ``params_echo``, per
    :func:`describe_preview_provenance`); a decline / credential suppression /
    fail-soft ``preview_unavailable`` marker yields ``None`` so the synthetic
    preview stays clean.
    """
    proposed_effect = await build_proposed_effect(
        PreviewContext(
            descriptor=descriptor,
            connector_instance=None,
            operator=operator,
            target=target,
            params=params,
            connector_id=connector_id,
        )
    )
    populated, _reason = describe_preview_provenance(proposed_effect, op_id=descriptor.op_id)
    return proposed_effect if populated else None


async def build_composite_preview(
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    descriptor: EndpointDescriptor,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the synthetic preview for a governed-tier non-ingested op (#3198/#3312).

    A composite / typed op has no single literal HTTP request, so the
    preview-hash binding (decision ``governed-delete-operations.md``
    requirement 2) binds the *logical request tuple*: the redacted params ride
    the ``redacted_body`` slot, so :func:`compute_preview_hash` — unchanged, and
    unaffected by the ``proposed_effect`` key it does not hash — is
    param-sensitive (two different deletes hash differently) while ``method`` (a
    ``COMPOSITE`` sentinel) and ``resolved_path`` (the ``op_id``) name the op. On
    top of that projection it reuses the park-time ``proposed_effect`` (#3312,
    via :func:`_synthetic_proposed_effect`) so the calling agent reads the same
    effect block the approver sees. Only reached for the ``destructive`` /
    ``requires_approval`` tiers (the gate in
    :func:`~meho_backplane.operations._request_preview._resolve_previewable_descriptor`);
    every other typed/composite op keeps the ``"unavailable"`` contract. No
    handler runs and nothing is sent.
    """
    redacted_body = _redact_request_body(
        params, connector_id=connector_id, operator=operator, op_id=op_id
    )
    _log.info(
        "preview_dispatch_composite",
        connector_id=connector_id,
        op_id=op_id,
        source_kind=descriptor.source_kind,
        safety_level=descriptor.safety_level,
        tenant_id=str(operator.tenant_id),
    )
    envelope: dict[str, Any] = {
        "status": "ok",
        "op_id": op_id,
        "connector_id": connector_id,
        "source_kind": descriptor.source_kind,
        "method": _COMPOSITE_PREVIEW_METHOD,
        "resolved_path": op_id,
        "query": None,
        "redacted_body": redacted_body,
    }
    envelope["preview_hash"] = compute_preview_hash(envelope)
    proposed_effect = await _synthetic_proposed_effect(
        operator=operator,
        connector_id=connector_id,
        descriptor=descriptor,
        target=target,
        params=params,
    )
    if proposed_effect is not None:
        envelope["proposed_effect"] = proposed_effect
    return envelope
