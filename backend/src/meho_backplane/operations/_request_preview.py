# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read-only dispatch preview -- the literal would-be HTTP request (#1683).

G0.24 follow-up (#1683), the observability counterpart to T5 #1656
(requestBody unwrap) and T4 #1649 (structured 4xx error shape). When an
ingested-L2 **write** dispatch fails upstream, an operator could not read
back *what meho actually put on the wire*: the operation audit persists
only a **hashed** ``params_hash`` (an intentional privacy + row-size
choice -- full args may carry secrets), not the resolved method / path /
body. During the #1656 dogfood the consumer had to "bisect payload
shapes from the outside" to discover the body was being sent wrapped,
because nothing inside meho exposed the constructed request
(``claude-rdc-hetzner-dc#1138``).

This module is the lowest-friction fix consistent with the dumb-substrate
posture: a **read-only preview** that resolves an op + params to the
literal request and *returns* it -- ``{method, resolved_path, query,
redacted_body}`` -- instead of sending it. It is request-time
observability, **not** a new persisted-secret surface: nothing is written
to the audit row, the ``params_hash`` design is untouched, and the body
is run through the **same** connector-boundary redaction pipeline the
response path uses (:func:`apply_connector_boundary_redaction`), so a
field the redactor masks in a real response is masked in the preview too.

Scope (honouring #1683's out-of-scope dispositions):

* **No dispatch.** The connector's HTTP transport is never called; no
  network egress, no audit row, no broadcast event, no policy-gate park.
* **No replay.** Inspecting a *would-be* request only; re-dispatching a
  past audited request is a separate governance concern (Goal #1651).
* **Ingested ops only.** A "would-be HTTP request" exists only for
  ``source_kind='ingested'`` ops (they construct a literal method/path/
  body). ``typed`` / ``composite`` ops invoke Python handlers and have no
  single HTTP request to preview -- the preview says so explicitly rather
  than fabricating one.

The resolution itself is shared verbatim with the execute path via
:func:`~meho_backplane.operations._branches.resolve_ingested_request`, so
the previewed request can never drift from what
:func:`~meho_backplane.operations._branches.dispatch_ingested` actually
sends (the path substitution, the ``mount_op_path`` prefix, the
requestBody unwrap all run identically).
"""

from __future__ import annotations

from typing import Any

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import resolve_connector_or_label
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._branches import resolve_ingested_request
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations._lookup import (
    count_known_ops,
    lookup_descriptor,
    parse_connector_id,
)
from meho_backplane.operations._validate import validate_params
from meho_backplane.redaction import apply_connector_boundary_redaction

__all__ = ["preview_dispatch"]

_log = structlog.get_logger(__name__)


async def _resolve_previewable_descriptor(
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    params: dict[str, Any],
) -> EndpointDescriptor | dict[str, Any]:
    """Run dispatch Steps 2-3 + the previewability gate; return descriptor or error.

    Returns the validated :class:`EndpointDescriptor` when the op resolves,
    is an ``source_kind='ingested'`` op, and its params pass the schema.
    Otherwise returns the structured envelope the caller propagates verbatim:

    * ``unknown_op`` -- the natural key resolved no descriptor.
    * ``preview_unavailable`` (status ``"unavailable"``) -- a ``typed`` /
      ``composite`` op has no single literal HTTP request to preview.
    * ``invalid_params`` -- params failed the descriptor's
      ``parameter_schema`` (same validation ``dispatch`` runs).
    """
    product, version, impl_id = parse_connector_id(connector_id)

    # --- Step 2: descriptor lookup (mirrors dispatch) ---------------------
    descriptor = await lookup_descriptor(
        tenant_id=operator.tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=op_id,
    )
    if descriptor is None:
        known_op_count = await count_known_ops(
            tenant_id=operator.tenant_id,
            product=product,
            version=version,
            impl_id=impl_id,
        )
        return {
            "status": "error",
            "op_id": op_id,
            "connector_id": connector_id,
            "error": (
                f"unknown_op: no operation {op_id!r} for connector "
                f"{connector_id!r} ({known_op_count} known op(s) for the "
                "connector). Discover op ids via search_operations."
            ),
            "extras": {"error_code": "unknown_op", "known_op_count": known_op_count},
        }

    # --- Non-ingested ops have no single literal HTTP request -------------
    # A ``typed`` / ``composite`` op invokes a Python handler (which may
    # itself make zero or many HTTP calls, or none); there is no one
    # method/path/body to preview. Say so explicitly rather than fabricate.
    if descriptor.source_kind != "ingested":
        return {
            "status": "unavailable",
            "op_id": op_id,
            "connector_id": connector_id,
            "source_kind": descriptor.source_kind,
            "error": (
                f"preview_unavailable: op {op_id!r} is source_kind="
                f"{descriptor.source_kind!r}, not an HTTP-ingested op -- it "
                "runs a typed/composite handler with no single literal HTTP "
                "request to preview. The dispatch-request preview covers "
                "source_kind='ingested' ops only."
            ),
            "extras": {
                "error_code": "preview_unavailable",
                "reason": "not_ingested",
                "source_kind": descriptor.source_kind,
            },
        }

    # --- Step 3: parameter_schema validation (mirrors dispatch) -----------
    validation_errors = validate_params(descriptor.parameter_schema, params)
    if validation_errors:
        return {
            "status": "error",
            "op_id": op_id,
            "connector_id": connector_id,
            "source_kind": descriptor.source_kind,
            "error": (
                "invalid_params: params failed the operation's parameter_schema; "
                "fix the params shape (see extras.validation_errors) before "
                "previewing."
            ),
            "extras": {
                "error_code": "invalid_params",
                "validation_errors": validation_errors,
            },
        }

    return descriptor


def _redact_request_body(
    body: Any,
    *,
    connector_id: str,
    operator: Operator,
    op_id: str,
) -> Any:
    """Redact a would-be request body through the connector-boundary pipeline.

    The exact pipeline the response path uses
    (``dispatcher._apply_redaction_middleware`` →
    :func:`apply_connector_boundary_redaction`): resolve the
    per-(connector_id, tenant, op) ``RedactionPolicy`` and run the engine.
    A body field the redactor masks in a real response is masked here too --
    no new raw-secret surface. A ``None`` body (no requestBody) round-trips
    to ``None`` without touching the engine.
    """
    if body is None:
        return None
    tenant = str(operator.tenant_id) if operator.tenant_id is not None else None
    redaction = apply_connector_boundary_redaction(
        body,
        connector_id=connector_id,
        tenant=tenant,
        op=op_id,
    )
    return redaction.redacted


async def _build_ingested_preview(
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    descriptor: EndpointDescriptor,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the connector + literal request for a validated ingested op.

    Dispatch Step 5 (connector resolution) followed by the literal-request
    resolve (shared with :func:`dispatch_ingested`) and body redaction.
    Returns the ``status="ok"`` envelope, or a structured ``no_connector`` /
    ``ambiguous_connector`` error envelope when the target resolves no
    connector. Never sends the request.
    """
    cls, label, exc_message = resolve_connector_or_label(target)
    if label is not None:
        return {
            "status": "error",
            "op_id": op_id,
            "connector_id": connector_id,
            "source_kind": descriptor.source_kind,
            "error": f"{label}: {exc_message or 'connector could not be resolved for the target'}",
            "extras": {"error_code": label, "exception_message": exc_message},
        }
    # ``label is None`` ⇔ ``cls`` is set (resolver contract).
    assert cls is not None
    connector_instance = get_or_create_connector_instance(cls)

    # The literal request, resolved through the SAME code path
    # ``dispatch_ingested`` sends through -- no drift.
    request = await resolve_ingested_request(
        connector=connector_instance,
        descriptor=descriptor,
        operator=operator,
        target=target,
        params=params,
    )
    redacted_body = _redact_request_body(
        request.body, connector_id=connector_id, operator=operator, op_id=op_id
    )

    _log.info(
        "preview_dispatch",
        connector_id=connector_id,
        op_id=op_id,
        method=request.method,
        source_kind=descriptor.source_kind,
        has_body=request.body is not None,
        tenant_id=str(operator.tenant_id),
    )

    return {
        "status": "ok",
        "op_id": op_id,
        "connector_id": connector_id,
        "source_kind": descriptor.source_kind,
        "method": request.method,
        "resolved_path": request.path,
        "query": request.query,
        "redacted_body": redacted_body,
    }


async def preview_dispatch(
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Resolve an op + params to the literal would-be HTTP request, redacted.

    The read-only sibling of :func:`~meho_backplane.operations.dispatcher.dispatch`.
    Runs the same Steps 1-3 (parse connector id, look up the descriptor,
    validate params against ``parameter_schema`` -- via
    :func:`_resolve_previewable_descriptor`) and Step 5 (resolve the
    connector instance), then -- instead of executing -- resolves the
    literal request via
    :func:`~meho_backplane.operations._branches.resolve_ingested_request`
    and returns it with the body redacted through the connector-boundary
    pipeline (:func:`_redact_request_body`).

    Never sends the request, never writes an audit row, never parks. The
    policy gate (Step 4) is deliberately skipped: a preview reveals only
    what *would* be sent (and the body is redacted), so it carries no
    side effect to authorize -- the same posture as ``search_operations``
    over the same descriptors. (Both surfaces are still ``OPERATOR``-gated
    at the route / tool layer.)

    Returns a JSON-shaped envelope:

    * ``status`` -- ``"ok"`` when the request resolved; ``"error"`` on a
      structured failure (``unknown_op`` / ``invalid_params`` /
      ``no_connector`` / ``ambiguous_connector``); ``"unavailable"`` when
      the op is not previewable (a ``typed`` / ``composite`` op has no
      literal HTTP request).
    * ``op_id`` / ``connector_id`` -- echoed for correlation.
    * ``source_kind`` -- the descriptor's source kind (present whenever a
      descriptor resolved).
    * On ``status == "ok"``: ``method``, ``resolved_path``, ``query``
      (object or ``null``), and ``redacted_body`` (the raw body after the
      redaction pipeline; ``null`` when the op declares no body).
    * On a non-ok status: ``error`` (``"<code>: <human-readable>"``) and,
      for structured failures, an ``extras`` object carrying the
      machine-readable ``error_code`` and any per-code detail.

    The function does not raise for operator-input faults (unknown op,
    invalid params, unresolvable connector) -- they come back as the
    structured ``error`` envelope, mirroring the dispatcher's never-raises
    contract so the REST route and MCP tool keep one uniform shape.
    """
    resolved = await _resolve_previewable_descriptor(
        operator=operator,
        connector_id=connector_id,
        op_id=op_id,
        params=params,
    )
    if isinstance(resolved, dict):
        return resolved  # structured unknown_op / unavailable / invalid_params
    return await _build_ingested_preview(
        operator=operator,
        connector_id=connector_id,
        op_id=op_id,
        descriptor=resolved,
        target=target,
        params=params,
    )
