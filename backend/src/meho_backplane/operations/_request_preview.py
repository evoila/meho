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
* **Ingested ops, plus a synthetic preview for the approval tiers.** A
  literal "would-be HTTP request" exists only for ``source_kind='ingested'``
  ops (they construct a literal method/path/body). A ``typed`` /
  ``composite`` op invokes a Python handler and has no single HTTP request
  to preview -- so by default the preview says so explicitly
  (``status="unavailable"``) rather than fabricating one. The two governed
  approval tiers are the exception: a ``safety_level='destructive'`` op
  (#3198) and a non-credential-class op that ``requires_approval`` (#3312,
  canonical case the ``dangerous``-tier typed ``vault.kv.delete``; a
  credential-class op like ``vault.kv.put`` stays ``unavailable`` because its
  secret rides in the params) instead get a **synthetic preview** -- a
  params-bound projection (the hashed binding) plus the
  reused park-time ``proposed_effect`` content -- so the agent reads the
  same effect block the approver sees, pre-dispatch. That proposed-effect
  reuse is **egress-free**: it runs the registered builder with **no
  connector instance**, so a pure builder (e.g. ``vault.kv.delete``, which
  reads only its params) populates while a live-read blast-radius builder
  declines. No handler is ever invoked and nothing is sent.

The resolution itself is shared verbatim with the execute path via
:func:`~meho_backplane.operations._branches.resolve_ingested_request`, so
the previewed request can never drift from what
:func:`~meho_backplane.operations._branches.dispatch_ingested` actually
sends (the path substitution, the ``mount_op_path`` prefix, the
requestBody unwrap all run identically).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Final

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors import resolve_connector_or_label
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._branches import resolve_ingested_request
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations._lookup import (
    count_known_ops,
    lookup_descriptor,
    parse_connector_id,
)
from meho_backplane.operations._preview import _SENSITIVE_CLASSES
from meho_backplane.operations._validate import InvalidOpSchemaError, validate_params
from meho_backplane.redaction import apply_connector_boundary_redaction

__all__ = ["compute_preview_hash", "preview_dispatch"]

_log = structlog.get_logger(__name__)

#: The envelope keys that define the *resolved request* — the deterministic
#: projection of ``(connector_id, op_id, target, params)`` a preview binds.
#: Volatile / advisory fields (``status``, ``source_kind``) are excluded so
#: the hash is stable across preview and the subsequent governed dispatch.
_PREVIEW_HASH_KEYS: Final[tuple[str, ...]] = (
    "connector_id",
    "op_id",
    "method",
    "resolved_path",
    "query",
    "redacted_body",
)


def compute_preview_hash(envelope: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 hex over a preview's canonicalised resolved envelope.

    The preview-result-hash binding for the ``destructive`` tier (#3197,
    decision requirement 2). Hashes only the :data:`_PREVIEW_HASH_KEYS`
    projection of an ``status="ok"`` :func:`preview_dispatch` envelope — the
    literal would-be request (``method`` / ``resolved_path`` / ``query`` /
    ``redacted_body``) plus its ``connector_id`` / ``op_id`` identity — so two
    previews of the identical ``(connector_id, op_id, target, params)`` yield
    the identical hash, and the dispatcher can recompute it to detect a
    param-swap between preview and the governed call.

    Canonicalisation matches
    :func:`~meho_backplane.operations._validate.compute_params_hash`
    (``json.dumps(..., sort_keys=True, default=str, separators=(",", ":"))``)
    so the hashing discipline is uniform across the two binding hashes.
    """
    source = {key: envelope.get(key) for key in _PREVIEW_HASH_KEYS}
    canonical = json.dumps(source, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _preview_unavailable_envelope(
    *,
    op_id: str,
    connector_id: str,
    source_kind: str,
) -> dict[str, Any]:
    """Structured ``unavailable`` envelope for a non-previewable typed/composite op.

    A ``typed`` / ``composite`` op runs a Python handler with no single
    literal HTTP request to preview. (The two governed approval tiers are
    the exception — a ``destructive`` op, #3198, and a ``requires_approval``
    op, #3312 — are routed to
    :func:`~meho_backplane.operations._composite_preview.build_composite_preview`
    instead of here.)
    """
    return {
        "status": "unavailable",
        "op_id": op_id,
        "connector_id": connector_id,
        "source_kind": source_kind,
        "error": (
            f"preview_unavailable: op {op_id!r} is source_kind="
            f"{source_kind!r}, not an HTTP-ingested op -- it "
            "runs a typed/composite handler with no single literal HTTP "
            "request to preview. The dispatch-request preview covers "
            "source_kind='ingested' ops only."
        ),
        "extras": {
            "error_code": "preview_unavailable",
            "reason": "not_ingested",
            "source_kind": source_kind,
        },
    }


def _invalid_op_schema_envelope(
    *,
    op_id: str,
    connector_id: str,
    source_kind: str,
    missing_ref: str,
) -> dict[str, Any]:
    """Structured envelope for a self-broken stored ``parameter_schema`` (#3095).

    Mirrors the dispatcher's ``result_invalid_op_schema`` shape: the
    descriptor — not the caller's params — is at fault, and the missing
    pointer rides in ``extras`` so the operator can repair the spec /
    re-ingest.
    """
    return {
        "status": "error",
        "op_id": op_id,
        "connector_id": connector_id,
        "source_kind": source_kind,
        "error": (
            f"invalid_op_schema: stored parameter_schema for {op_id!r} "
            f"contains an unresolvable $ref ({missing_ref}); the op "
            "cannot validate any params until its descriptor is repaired "
            "(re-ingest the connector's spec)."
        ),
        "extras": {
            "error_code": "invalid_op_schema",
            "missing_ref": missing_ref,
        },
    }


def _is_previewable(descriptor: EndpointDescriptor) -> bool:
    """True when the op has a preview surface; else it previews as ``unavailable``.

    An ``source_kind='ingested'`` op has a literal would-be HTTP request. A
    non-ingested (``typed`` / ``composite``) op has none, so it is previewable
    only in a governed approval tier, via the synthetic preview
    (:func:`._composite_preview.build_composite_preview`):

    * ``safety_level='destructive'`` (#3198) — unconditional: the
      governed-delete gate refuses to park without a bound preview hash, so the
      op MUST be previewable. Destructive ops are non-credential deletes.
    * ``requires_approval`` (#3312), **except a credential-class op** — so the
      calling agent reads the same park-time ``proposed_effect`` the approver
      sees. A credential-class op (``classify_op`` ∈ the aggregate-only
      :data:`~meho_backplane.operations._preview._SENSITIVE_CLASSES`) is
      **excluded**: its secret rides in the request params, and the synthetic
      preview's ``redacted_body`` slot uses only connector-boundary value-shape
      redaction — which does not scrub a structured secret like
      ``{"data": {"password": …}}``. The park path suppresses credential-class
      request detail for exactly this reason (``build_proposed_effect`` step 3);
      mirror it here so a ``vault.kv.put`` / ``k8s.secret.create`` preview never
      surfaces the written secret. Single-sourced on ``classify_op``; the class
      set is imported, never re-declared.
    """
    if descriptor.source_kind == "ingested":
        return True
    if descriptor.safety_level == "destructive":
        return True
    return descriptor.requires_approval and classify_op(descriptor.op_id) not in _SENSITIVE_CLASSES


async def _resolve_previewable_descriptor(
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    params: dict[str, Any],
) -> EndpointDescriptor | dict[str, Any]:
    """Run dispatch Steps 2-3 + the previewability gate; return descriptor or error.

    Returns the validated :class:`EndpointDescriptor` when the op resolves,
    is previewable (:func:`_is_previewable` — ``source_kind='ingested'``, or a
    governed-tier non-ingested op), and its params pass the schema. Otherwise
    returns the structured envelope the caller propagates verbatim:

    * ``unknown_op`` -- the natural key resolved no descriptor.
    * ``preview_unavailable`` (status ``"unavailable"``) -- a ``typed`` /
      ``composite`` op outside the governed approval tiers.
    * ``invalid_params`` -- params failed the descriptor's
      ``parameter_schema`` (same validation ``dispatch`` runs).
    * ``invalid_op_schema`` -- the stored ``parameter_schema`` itself
      carries an unresolvable ``$ref`` (#3095); same structured envelope
      ``dispatch`` returns for the same broken descriptor.
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

    # --- Previewability gate (see :func:`_is_previewable`) -----------------
    # A non-ingested op previews as "unavailable" unless it is in a governed
    # approval tier (destructive #3198 / non-credential requires_approval
    # #3312), which routes to the synthetic preview.
    if not _is_previewable(descriptor):
        return _preview_unavailable_envelope(
            op_id=op_id, connector_id=connector_id, source_kind=descriptor.source_kind
        )

    # --- Step 3: parameter_schema validation (mirrors dispatch) -----------
    try:
        validation_errors = validate_params(descriptor.parameter_schema, params)
    except InvalidOpSchemaError as exc:
        return _invalid_op_schema_envelope(
            op_id=op_id,
            connector_id=connector_id,
            source_kind=descriptor.source_kind,
            missing_ref=exc.missing_ref,
        )
    if validation_errors:
        return _invalid_params_envelope(
            op_id=op_id,
            connector_id=connector_id,
            source_kind=descriptor.source_kind,
            validation_errors=validation_errors,
        )

    return descriptor


def _invalid_params_envelope(
    *,
    op_id: str,
    connector_id: str,
    source_kind: str,
    validation_errors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Structured envelope for params that failed the stored schema."""
    return {
        "status": "error",
        "op_id": op_id,
        "connector_id": connector_id,
        "source_kind": source_kind,
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
    Returns the ``status="ok"`` envelope, a structured ``no_connector`` /
    ``ambiguous_connector`` error envelope when the target resolves no
    connector, or a ``dispatch_error`` envelope when the request can't be
    resolved (an unsubstituted path var or a descriptor missing its
    method/path -- the two faults :func:`resolve_ingested_request` raises).
    Never sends the request; never raises for those faults.
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
    #
    # ``resolve_ingested_request`` deliberately *raises* on a path-template
    # fault (``KeyError`` for an unsubstituted path var, ``RuntimeError`` for
    # a descriptor missing its method/path) because the execute path relies on
    # the dispatcher's structured-error mapping (``dispatcher`` generic
    # ``except``) to convert them. The preview path has no such wrapper, so we
    # catch those two -- and only those two -- here and map them to the same
    # structured ``error`` envelope every other preview fault returns, honouring
    # this module's documented never-raises contract (#2066). ``resolve_*``
    # itself is left untouched so the execute-path contract is unchanged.
    try:
        request = await resolve_ingested_request(
            connector=connector_instance,
            descriptor=descriptor,
            operator=operator,
            target=target,
            params=params,
        )
    except (KeyError, RuntimeError) as exc:
        return {
            "status": "error",
            "op_id": op_id,
            "connector_id": connector_id,
            "source_kind": descriptor.source_kind,
            "error": (
                f"dispatch_error: the operation's request could not be resolved "
                f"({exc}). This usually means a required path parameter was not "
                "supplied or the descriptor is missing its method/path."
            ),
            "extras": {
                "error_code": "dispatch_error",
                "exception_message": str(exc),
            },
        }
    return _ok_ingested_envelope(
        operator=operator,
        connector_id=connector_id,
        op_id=op_id,
        descriptor=descriptor,
        request=request,
    )


def _ok_ingested_envelope(
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    descriptor: EndpointDescriptor,
    request: Any,
) -> dict[str, Any]:
    """Assemble the ``status="ok"`` preview envelope for a resolved request.

    Redacts the body through the connector-boundary pipeline, logs the
    resolution, and stamps the preview-result-hash binding (#3197) —
    :func:`compute_preview_hash` over the resolved-request projection, so a
    caller can present it on the subsequent governed ``call_operation`` of a
    ``destructive``-tier op. Only ``status="ok"`` previews carry the hash: a
    non-resolvable request has no literal effect to bind.
    """
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
    envelope: dict[str, Any] = {
        "status": "ok",
        "op_id": op_id,
        "connector_id": connector_id,
        "source_kind": descriptor.source_kind,
        "method": request.method,
        "resolved_path": request.path,
        "query": request.query,
        "redacted_body": redacted_body,
    }
    envelope["preview_hash"] = compute_preview_hash(envelope)
    return envelope


async def preview_dispatch(
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Resolve an op + params to the would-be request (or a synthetic preview), redacted.

    The read-only sibling of :func:`~meho_backplane.operations.dispatcher.dispatch`.
    Runs the same Steps 1-3 (parse connector id, look up descriptor, validate
    params — via :func:`_resolve_previewable_descriptor`) and Step 5 (resolve
    connector instance), then — instead of executing — resolves the literal
    request via :func:`~meho_backplane.operations._branches.resolve_ingested_request`
    and returns it with the body redacted (:func:`_redact_request_body`). A
    non-ingested op in a governed approval tier (``destructive`` #3198 /
    non-credential ``requires_approval`` #3312 — see :func:`_is_previewable`)
    instead gets the synthetic preview
    (:func:`~meho_backplane.operations._composite_preview.build_composite_preview`).
    Never sends, never audits, never parks;
    the policy gate (Step 4) is skipped — a preview carries no side effect to
    authorize (both surfaces stay ``OPERATOR``-gated at the route/tool layer).

    Returns a JSON-shaped envelope: ``status`` ``"ok"`` (request/synthetic
    preview resolved), ``"error"`` (structured ``unknown_op`` /
    ``invalid_params`` / ``no_connector`` / ``ambiguous_connector`` /
    ``dispatch_error``), or ``"unavailable"`` (a non-governed typed/composite
    op). ``op_id`` / ``connector_id`` / ``source_kind`` echo for correlation.
    On ``ok``: ``method``, ``resolved_path``, ``query``, ``redacted_body``,
    ``preview_hash`` (#3197 — the binding a caller presents on the governed
    ``call_operation``), and, for a synthetic preview whose builder populated,
    ``proposed_effect`` (#3312). On non-ok: ``error`` + an ``extras`` object.
    Never raises for operator-input faults — they ride the ``error`` envelope,
    mirroring the dispatcher's never-raises contract.
    """
    resolved = await _resolve_previewable_descriptor(
        operator=operator,
        connector_id=connector_id,
        op_id=op_id,
        params=params,
    )
    if isinstance(resolved, dict):
        return resolved  # structured unknown_op / unavailable / invalid_params
    if resolved.source_kind != "ingested":
        # A destructive/requires_approval typed/composite op cleared the
        # previewability gate (#3198/#3312) — bind the logical request tuple
        # + reuse the park-time proposed_effect, no handler runs. Imported
        # lazily: the synthetic-preview module imports back from here (shared
        # ``compute_preview_hash`` / ``_redact_request_body``), so a top-level
        # import would cycle.
        from meho_backplane.operations._composite_preview import build_composite_preview

        return await build_composite_preview(
            operator=operator,
            connector_id=connector_id,
            op_id=op_id,
            descriptor=resolved,
            target=target,
            params=params,
        )
    return await _build_ingested_preview(
        operator=operator,
        connector_id=connector_id,
        op_id=op_id,
        descriptor=resolved,
        target=target,
        params=params,
    )
