# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Structured :class:`OperationResult` builders for the G0.6 dispatcher.

The dispatcher (T5 #396) never raises -- every operator-visible failure
mode returns one of these :class:`OperationResult` shapes. Keeping the
builders here (rather than inline in :mod:`dispatcher`) lets the
dispatcher's :func:`dispatch` body stay focused on control flow.

Each builder owns one ``error_code`` from the contract documented in
:mod:`meho_backplane.operations.dispatcher`'s module docstring:
``unknown_op`` / ``invalid_params`` / ``no_connector`` /
``ambiguous_connector`` / ``handler_unreachable`` / ``denied`` /
``awaiting_approval`` / ``connector_unsupported`` /
``connector_http_403`` / ``connector_error``. The ``status`` field maps
to ``OperationResult.status``; the ``error_code`` lives in ``extras``
so callers can both string-match the ``error`` field
(``error.startswith("unknown_op:")``) and parse the code for structured
handling.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import httpx

from meho_backplane.connectors import OperationResult, ResultHandle

__all__ = [
    "result_ambiguous_connector",
    "result_awaiting_approval",
    "result_composite_l2_disabled",
    "result_composite_l2_missing",
    "result_connector_error",
    "result_connector_http_403",
    "result_connector_unsupported",
    "result_denied",
    "result_handler_unreachable",
    "result_invalid_params",
    "result_no_connector",
    "result_target_required",
    "result_unknown_op",
    "status_code_for_result",
    "wrap_ok_result",
]

#: Cap on the exception-message length recorded in the ``connector_error``
#: extras payload. A misbehaving connector could embed a credential into
#: a stringified exception; 256 chars is enough for an operator to
#: recognise the failure shape while capping the leak surface.
_EXC_MESSAGE_CAP: int = 256


def result_unknown_op(op_id: str, known_op_count: int, duration_ms: float) -> OperationResult:
    """Descriptor lookup miss for *(product, version, impl_id, op_id)*."""
    return OperationResult(
        status="error",
        op_id=op_id,
        error=f"unknown_op: {op_id}",
        duration_ms=duration_ms,
        extras={"error_code": "unknown_op", "known_op_count": known_op_count},
    )


def result_invalid_params(
    op_id: str,
    validation_errors: list[dict[str, Any]],
    duration_ms: float,
) -> OperationResult:
    """JSON Schema validation against ``parameter_schema`` failed."""
    return OperationResult(
        status="error",
        op_id=op_id,
        error=f"invalid_params: {len(validation_errors)} validation error(s)",
        duration_ms=duration_ms,
        extras={
            "error_code": "invalid_params",
            "validation_errors": validation_errors,
        },
    )


def result_target_required(op_id: str, duration_ms: float) -> OperationResult:
    """Op needs a ``target`` but the caller supplied none.

    G0.20-T6 (#1506). A typed/composite op whose handler is a
    connector-bound method (self-first) can only run against a resolved
    connector instance, which the dispatcher reaches *through* the
    ``target``. Invoking it with ``target=None`` is an omitted-argument
    usage error: the dispatcher catches it at connector-resolution time
    (:func:`~meho_backplane.operations.dispatcher._resolve_connector_instance`)
    and returns this structured ``target_required`` rather than letting
    the handler proceed unbound and trip the deliberate self-guard
    :exc:`RuntimeError` in
    :func:`~meho_backplane.operations._branches.dispatch_typed` (which
    stayed a loud internal signal for genuine instance-cache faults).

    Invalid-params-style shape — ``status="error"``,
    ``error="target_required: <op> requires a target"``, ``error_code``
    in ``extras`` — so callers that already branch on
    ``result.extras["error_code"]`` for ``invalid_params`` extend the
    same pattern. The op id rides in ``extras`` so an agent can name the
    op it must re-call with a target.
    """
    return OperationResult(
        status="error",
        op_id=op_id,
        error=f"target_required: {op_id!r} requires a target; none was supplied",
        duration_ms=duration_ms,
        extras={"error_code": "target_required", "op_id": op_id},
    )


def result_no_connector(
    op_id: str,
    product: str,
    version: str,
    duration_ms: float,
    exception_message: str | None = None,
) -> OperationResult:
    """Resolver miss -- no registered impl for *(product, version)*.

    ``exception_message`` (added by G0.14-T1 #1142) carries the
    :exc:`~meho_backplane.connectors.NoMatchingConnector` exception text
    so the operator-facing surface can show the diagnostic detail the
    resolver computed (``target.product`` value, the absence of a
    matching v1/v2 entry, etc.) rather than a bare summary. The field
    lands under ``extras["exception_message"]`` matching the
    ``connector_error`` shape so the structured-error consumer can read
    a uniform key across the two diagnostic codes.

    The argument is optional for backward compatibility with call sites
    that pre-date the resolver-helper unification — they pass through
    the bare ``(product, version)`` form and ``extras`` omits the field.
    """
    extras: dict[str, Any] = {
        "error_code": "no_connector",
        "product": product,
        "version": version,
    }
    if exception_message is not None:
        extras["exception_message"] = exception_message
    return OperationResult(
        status="error",
        op_id=op_id,
        error=f"no_connector: no implementation for product={product!r} version={version!r}",
        duration_ms=duration_ms,
        extras=extras,
    )


def result_ambiguous_connector(
    op_id: str,
    product: str,
    version: str,
    exception_message: str,
    duration_ms: float,
) -> OperationResult:
    """Resolver tie-break ladder couldn't pick a single connector.

    G0.14-T1 (#1142). The resolver raises
    :exc:`~meho_backplane.connectors.AmbiguousConnectorResolution` when
    two or more connectors remain after every step of the tie-break
    ladder (specificity → operator preference → priority). The exception
    message *already* carries the diagnostic shape an operator needs:
    the target's ``(product, version)``, the candidate list, and the
    remediation step ("set ``target.preferred_impl_id`` to one of
    them"). This builder preserves that message verbatim under
    ``extras["exception_message"]`` so the structured-error envelope
    on ``/operations/call`` (and any other dispatcher consumer) surfaces
    it without a paraphrase.

    Mirrors :func:`result_no_connector`'s shape — ``status="error"``,
    ``error="<code>: <human-readable>"``, full diagnostic detail in
    ``extras`` — so callers that already string-match
    ``error.startswith("no_connector:")`` can extend the same pattern
    to ``"ambiguous_connector:"`` without re-shaping their consumer.
    """
    return OperationResult(
        status="error",
        op_id=op_id,
        error=(
            f"ambiguous_connector: resolution ambiguous for "
            f"product={product!r} version={version!r}; "
            f"set target.preferred_impl_id to one of the candidates"
        ),
        duration_ms=duration_ms,
        extras={
            "error_code": "ambiguous_connector",
            "product": product,
            "version": version,
            "exception_message": exception_message,
        },
    )


def result_handler_unreachable(
    op_id: str, handler_ref: str, exc: BaseException, duration_ms: float
) -> OperationResult:
    """``importlib`` couldn't resolve ``handler_ref`` to a callable."""
    return OperationResult(
        status="error",
        op_id=op_id,
        error=f"handler_unreachable: {handler_ref}",
        duration_ms=duration_ms,
        extras={
            "error_code": "handler_unreachable",
            "handler_ref": handler_ref,
            "exception_class": type(exc).__name__,
        },
    )


def result_denied(op_id: str, reason: str, duration_ms: float) -> OperationResult:
    """Policy gate denied the call.

    Returned when the effective verdict is
    :attr:`~meho_backplane.db.models.PermissionVerdict.DENY` — either
    because the op is ``dangerous`` and no explicit grant overrides it,
    or because an explicit ``deny`` row was found, or because the
    principal's role ceiling forced the verdict to ``deny`` (for an
    agent principal), or because a human/service principal hit a
    ``requires_approval`` op (which is hard-denied for non-agents).

    The ``reason`` string is agent-readable: it names the verdict
    source and any ceilings that were applied so an agent can diagnose
    the refusal without human intervention.
    """
    return OperationResult(
        status="denied",
        op_id=op_id,
        error=f"denied: {reason}",
        duration_ms=duration_ms,
        extras={"error_code": "denied", "reason": reason},
    )


def result_awaiting_approval(
    op_id: str,
    approval_request_id: uuid.UUID,
    duration_ms: float,
) -> OperationResult:
    """Policy gate issued a ``needs-approval`` verdict; pending row created.

    G11.2-T4 (#817). The dispatcher calls this (for an agent principal,
    via the G11.2-T3 :attr:`~meho_backplane.db.models.PermissionVerdict.NEEDS_APPROVAL`
    verdict) after creating a durable
    :class:`~meho_backplane.db.models.ApprovalRequest` row for the call.
    The ``approval_request_id`` in ``extras`` is the UUID of the pending
    row; callers (the agent runtime, REST consumers) can poll or surface
    it so a human reviewer can approve or reject via
    ``POST /api/v1/approvals/{approval_request_id}/approve`` or ``…/reject``.

    The result's ``status`` is ``"awaiting_approval"`` -- distinct from
    ``"ok"`` (executed), ``"denied"`` (outright blocked), and ``"error"``
    (internal failure). Callers that string-match ``status`` must handle
    this value; callers that only handled ``"ok"`` / ``"error"`` /
    ``"denied"`` will treat it as an unrecognised status and surface it
    as a pending call, which is the correct semantics.
    """
    return OperationResult(
        status="awaiting_approval",
        op_id=op_id,
        error=f"awaiting_approval: {op_id!r} requires approval before execution",
        duration_ms=duration_ms,
        extras={
            "error_code": "awaiting_approval",
            "approval_request_id": str(approval_request_id),
        },
    )


def result_composite_l2_missing(
    op_id: str,
    missing_op_ids: tuple[str, ...],
    catalog_command: str,
    duration_ms: float,
) -> OperationResult:
    """Composite handler pre-flight detected missing L2 sub-op descriptors.

    G0.14-T10 (#1151). A composite (``vmware.composite.*``) declares the
    raw-REST sub-ops it dispatches into via
    :func:`~meho_backplane.connectors.vmware_rest.composites._preflight.preflight_l2_dependencies`.
    When one or more are not registered in ``endpoint_descriptor`` --
    no operator has run ``meho connector ingest --catalog
    <product>/<version>`` yet for this connector -- the helper raises
    :class:`~meho_backplane.operations.composite.CompositeL2DependencyMissing`
    and the dispatcher converts it to this structured result.

    Wording is the v0.9 reframe from G0.16-T1 (#1303), refined by
    G0.18-T7 (#1360) and #1386 to state that the escape-hatch ingest
    needs ``ANTHROPIC_API_KEY`` set for its grouping pass. The v0.8.0
    envelope cast
    the catalog command as "the remediation path" and operators read
    it as the recommended next step; reality is the opposite (per
    ``docs/codebase/api-shape-conventions.md`` §1) -- the curated
    daily-driver is the recommended path and the OpenAPI ingest is
    the escape hatch operators reach for when they're willing to
    handle vendor-shape responses without operator-shape envelopes
    or ``requires_approval`` annotations.

    The escape-hatch ingest needs an ``ANTHROPIC_API_KEY`` to run its
    grouping pass: #1386 wires a production ``LlmClient`` at FastAPI
    lifespan startup
    (``build_anthropic_ingest_llm_client``, reusing
    ``settings.anthropic_api_key``), so non-dry-run ``meho connector
    ingest --catalog ...`` groups successfully on a deploy with the key
    set. A deploy that configured no key still fails closed with HTTP
    503 / ``LlmClientUnavailable`` (RDC #789 N9 surfaced operators
    following the escape-hatch hint into a silent 503), so the human
    message names the key requirement rather than claiming the path is
    build-time-only.

    The error shape still complies with the
    ``docs/codebase/error-message-shape.md`` convention (G0.14-T11
    #1141): a stable ``composite_l2_missing`` code, a
    diagnostic-bearing human message (curation gap + the missing
    op-ids + the escape-hatch recipe + the key requirement +
    two doc references), and a structured ``data`` payload
    (``missing_op_ids`` + ``catalog_command``) so an agent can branch
    on the diagnostic without re-parsing the human text.
    """
    missing_repr = ", ".join(missing_op_ids) if missing_op_ids else "(none)"
    return OperationResult(
        status="error",
        op_id=op_id,
        error=(
            f"composite_l2_missing: composite {op_id!r} depends on sub-ops "
            f"not curated for this connector: [{missing_repr}]. The curated "
            f"daily-driver is the recommended path -- file an issue for an "
            f"L1 wrapper that exposes these ops in operator shape. As an "
            f"escape hatch, run {catalog_command!r} to ingest the raw "
            f"vendor ops (vendor-shape responses, no approval annotations) "
            f"and retry -- note that ingest grouping needs ANTHROPIC_API_KEY "
            f"set (the chassis wires the grouping LlmClient at lifespan "
            f"startup, reusing that key); a deploy with no key configured "
            f"fails closed with 503 / LlmClientUnavailable (#1386). See "
            f"docs/codebase/api-shape-conventions.md "
            f"section 1 for the strategic framing, "
            f"docs/codebase/spec-ingestion.md section 'LLM-client wiring' "
            f"for the key requirement, and "
            f"docs/codebase/connectors-vmware-rest.md for the L1+L2 "
            f"dispatch contract."
        ),
        duration_ms=duration_ms,
        extras={
            "error_code": "composite_l2_missing",
            "missing_op_ids": list(missing_op_ids),
            "catalog_command": catalog_command,
        },
    )


def result_composite_l2_disabled(
    op_id: str,
    disabled_op_ids: tuple[str, ...],
    connector_id: str,
    duration_ms: float,
) -> OperationResult:
    """Composite pre-flight found L2 sub-ops present in the catalog but **disabled**.

    #1601. The sibling of :func:`result_composite_l2_missing` for the
    *ingested-but-disabled* deploy state. A composite's L2 sub-op has a
    descriptor row in ``endpoint_descriptor`` whose ``is_enabled = false``,
    so :func:`~meho_backplane.operations._lookup.lookup_descriptor`
    (which hard-filters ``is_enabled = TRUE``) cannot resolve it and the
    composite is non-dispatchable -- but the catalog has already been
    ingested, so the ``composite_l2_missing`` remediation
    (``meho connector ingest --catalog ...``) would send the operator in
    the wrong direction.

    The pre-flight classifies the non-dispatchable sub-op via the
    ``is_enabled``-agnostic
    :func:`~meho_backplane.operations._lookup.descriptor_exists_any_state`
    probe and raises
    :class:`~meho_backplane.operations.composite.CompositeL2DependencyDisabled`
    when the row is present; the dispatcher converts it to this result.

    Remediation contract: name only verbs that **exist**. The reliable
    path is per-op ``meho connector edit-op <connector_id> <op_id>
    --enable``. Connector-level ``meho connector enable <connector_id>``
    is named only as the broad-strokes alternative **with the caveat that
    it does not re-enable spec-ingested ops** -- those land
    ``group_id = NULL`` and the enable cascade filters on ``group_id``
    (see ``ingest/_internals.py`` / ``ingest/_upsert.py``), so for an L2
    surface ingested from a spec, only the per-op ``edit-op --enable`` is
    deterministic. The original report proposed a group-level enable verb;
    no such verb exists, so this message must never reference one (the
    ``connector edit-group`` CLI command patches ``when_to_use`` / ``name``
    only -- it has no enable flag).

    The shape complies with the ``docs/codebase/error-message-shape.md``
    convention (#1141): a stable ``composite_l2_disabled`` code, a
    diagnostic-bearing human message (disabled op-ids + the real per-op
    enable verb + the connector-level caveat + a doc reference), and a
    structured ``extras`` payload (``disabled_op_ids`` + ``connector_id``)
    so an agent can branch on the diagnostic without re-parsing the text.
    """
    disabled_repr = ", ".join(disabled_op_ids) if disabled_op_ids else "(none)"
    return OperationResult(
        status="error",
        op_id=op_id,
        error=(
            f"composite_l2_disabled: composite {op_id!r} depends on sub-ops "
            f"that are present in this connector's catalog but disabled: "
            f"[{disabled_repr}]. The catalog is already ingested, so re-ingest "
            f"is not the fix -- re-enable each op per-op with "
            f"'meho connector edit-op {connector_id} <op_id> --enable' (the "
            f"reliable path), then retry. Note: connector-level "
            f"'meho connector enable {connector_id}' does NOT re-enable "
            f"spec-ingested ops -- they land with group_id=NULL and the enable "
            f"cascade filters on group_id, so per-op edit-op --enable is the "
            f"deterministic remediation. See "
            f"docs/codebase/connectors-vmware-rest.md for the L1+L2 dispatch "
            f"contract and docs/codebase/error-message-shape.md for the error "
            f"convention."
        ),
        duration_ms=duration_ms,
        extras={
            "error_code": "composite_l2_disabled",
            "disabled_op_ids": list(disabled_op_ids),
            "connector_id": connector_id,
        },
    )


def result_connector_error(
    op_id: str,
    exc: BaseException,
    duration_ms: float,
) -> OperationResult:
    """Connector / handler raised. Exception class + capped message land in extras."""
    msg = str(exc)
    if len(msg) > _EXC_MESSAGE_CAP:
        msg = msg[:_EXC_MESSAGE_CAP] + "...<truncated>"
    return OperationResult(
        status="error",
        op_id=op_id,
        error=f"connector_error: {type(exc).__name__}",
        duration_ms=duration_ms,
        extras={
            "error_code": "connector_error",
            "exception_class": type(exc).__name__,
            "exception_message": msg,
        },
    )


def result_connector_unsupported(
    op_id: str,
    exc: BaseException,
    cause: Literal["unsupported_feature", "unreplaced_auto_shim"],
    connector_class: str | None,
    duration_ms: float,
) -> OperationResult:
    """Connector / handler raised :exc:`NotImplementedError` on dispatch.

    G0.23-T1 (#1627). :exc:`NotImplementedError` from a connector is a
    *deliberate* "I don't do this" signal, not an unforeseen crash --
    the raise sites already carry actionable, operator-readable
    messages (``VmwareRestConnector.auth_headers`` naming the
    unsupported ``target.auth_model``; the ingest auto-shim's
    "must be replaced with a per-product Connector subclass"). Routing
    it through :func:`result_connector_error` flattened that diagnostic
    to an opaque ``connector_error: NotImplementedError`` with the
    message buried in ``extras["exception_message"]`` where the
    operator never looked -- exactly the opaque-error class the
    ``docs/codebase/error-message-shape.md`` convention exists to
    prevent (the RDC cycle-8 ``vmware-l2-dispatch-notimplemented``
    dead end).

    This builder promotes the exception message verbatim into the
    operator-facing ``error`` string and appends a per-*cause*
    remediation:

    * ``unsupported_feature`` -- a hand-rolled connector explicitly
      does not implement what the dispatch requires for this target
      (an unsupported ``target.auth_model``, an unwired session mode).
      Remediation: fix the target configuration against the modes the
      connector supports -- a config matter, not a code gap.
    * ``unreplaced_auto_shim`` -- the resolved connector is the
      auto-registered :class:`GenericRestConnector` ingest shim, which
      can never authenticate or execute. Remediation: register the
      hand-rolled per-product subclass before enabling dispatch.

    The dispatcher classifies the cause via ``isinstance(...,
    GenericRestConnector)`` at the catch site -- precise, not
    message-fragile. The shape complies with the #1141 convention: a
    stable ``connector_unsupported`` code, a diagnostic-bearing human
    message (verbatim detail + remediation imperative + doc
    reference), and a structured ``extras`` payload (``cause`` /
    ``connector_class`` / ``detail``) so an agent can branch without
    re-parsing the text. ``detail`` reuses the
    :data:`_EXC_MESSAGE_CAP` discipline from
    :func:`result_connector_error` (both production raise sites are
    comfortably under the cap, so their texts survive verbatim).
    """
    detail = str(exc)
    if len(detail) > _EXC_MESSAGE_CAP:
        detail = detail[:_EXC_MESSAGE_CAP] + "...<truncated>"
    origin = (
        f"The resolved connector ({connector_class})"
        if connector_class is not None
        else "The resolved handler"
    )
    if cause == "unreplaced_auto_shim":
        remediation = (
            f"{origin} is the auto-registered ingest shim, which cannot "
            f"authenticate or execute against the upstream. Register the "
            f"hand-rolled per-product Connector subclass for this "
            f"(product, version, impl_id) and redeploy before enabling "
            f"dispatch on this connector's ops -- re-ingesting the spec "
            f"will NOT replace the shim. See "
            f"docs/codebase/spec-ingestion.md for the auto-shim "
            f"lifecycle."
        )
    else:
        remediation = (
            f"{origin} deliberately does not implement what this "
            f"dispatch requires for the target. Re-check the target's "
            f"configuration (e.g. auth_model) against the modes the "
            f"connector supports, or route the op at a connector that "
            f"implements them. See docs/architecture/connector-auth.md "
            f"for the connector auth contract."
        )
    return OperationResult(
        status="error",
        op_id=op_id,
        error=f"connector_unsupported: {detail}. {remediation}",
        duration_ms=duration_ms,
        extras={
            "error_code": "connector_unsupported",
            "cause": cause,
            "connector_class": connector_class,
            "detail": detail,
        },
    )


#: GitHub returns the accepted/required fine-grained permissions on an App
#: or fine-grained-PAT 403 via this header, and the granted classic-OAuth
#: scopes via ``x-oauth-scopes``. They are echoed verbatim (when present)
#: so an operator/agent can read the missing grant off the structured
#: error instead of re-issuing the call to inspect raw headers. Matched
#: case-insensitively through :class:`httpx.Headers`.
_HTTP_403_ECHOED_HEADERS: tuple[str, ...] = (
    "X-Accepted-GitHub-Permissions",
    "x-oauth-scopes",
)


def _http_403_upstream_message(response: httpx.Response) -> str | None:
    """Best-effort extraction of the upstream's human 403 message.

    GitHub (and most REST APIs that bother) returns a JSON body with a
    top-level ``message`` (``"Resource not accessible by integration"``);
    that is the single most useful line for diagnosis, so it is preferred
    when the body parses as a JSON object carrying a string ``message``.
    Bodies that are not JSON, or JSON without a usable ``message``, fall
    back to the capped raw text. ``None`` only when the body is empty.
    The same :data:`_EXC_MESSAGE_CAP` discipline as the other builders
    bounds any credential-bearing upstream text.
    """
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str) and message.strip():
            return message[:_EXC_MESSAGE_CAP] + (
                "...<truncated>" if len(message) > _EXC_MESSAGE_CAP else ""
            )
    text = (response.text or "").strip()
    if not text:
        return None
    return text[:_EXC_MESSAGE_CAP] + ("...<truncated>" if len(text) > _EXC_MESSAGE_CAP else "")


def result_connector_http_403(
    op_id: str,
    exc: httpx.HTTPStatusError,
    duration_ms: float,
) -> OperationResult:
    """Connector raised an upstream **403 Forbidden** on dispatch.

    G0.24-T4 (#1649), extending the G0.23-T1 (#1627) dispatch
    structured-cause pattern to the transport-error sibling. A write
    dispatch whose backing credential is authenticated but lacks the
    *permission* the operation needs (e.g. a GitHub App with
    ``issues: read`` but not ``issues: write`` hitting
    ``POST /repos/{owner}/{repo}/issues``) surfaces as
    :exc:`httpx.HTTPStatusError`. The shared :class:`HttpConnector`
    adapter does no error mapping, so routing it through
    :func:`result_connector_error` flattened a genuinely useful 403
    -- GitHub returns a body message *and* headers enumerating the
    accepted/required permissions -- into an opaque
    ``connector_error: HTTPStatusError`` with only the httpx status
    line, the actionable detail buried in
    ``extras["exception_message"]`` (consumer
    ``claude-rdc-hetzner-dc#1138``).

    The cause is kept **connector-agnostic**: any upstream 403 means the
    credential reached the upstream and was rejected on authorization,
    not transport -- so the operator-facing ``error`` names the likely
    insufficient-permission cause regardless of which connector raised.
    ``extras`` carries the machine-usable fields an agent can branch on
    without re-parsing a transport error: ``http_status`` (always
    ``403`` -- the dispatcher scopes this builder to that code; 401/429
    are deliberate follow-ups, not this surface), the upstream
    ``upstream_message`` when the body carried one, and any of the
    standard GitHub permission headers
    (:data:`_HTTP_403_ECHOED_HEADERS`) that were present -- echoed,
    never required, so a non-GitHub 403 still yields the structured
    cause with an empty ``permission_headers``.
    """
    response = exc.response
    upstream_message = _http_403_upstream_message(response)
    permission_headers = {
        header: value
        for header in _HTTP_403_ECHOED_HEADERS
        if (value := response.headers.get(header)) is not None
    }
    summary = (
        "connector_http_403: the upstream returned HTTP 403 Forbidden. The "
        "target credential reached the upstream and was authenticated, but "
        "may lack the permission this operation requires -- a credential "
        "scope matter on the target, not a meho transport fault. Grant the "
        "missing permission on the backing credential (for a GitHub App / "
        "fine-grained PAT, the accepted permission is echoed in "
        "extras.permission_headers when the upstream sent it) and retry. See "
        "docs/codebase/error-message-shape.md for the dispatch error "
        "convention."
    )
    if upstream_message is not None:
        summary = f"{summary} Upstream said: {upstream_message}"
    extras: dict[str, Any] = {
        "error_code": "connector_http_403",
        "http_status": 403,
        "upstream_message": upstream_message,
        "permission_headers": permission_headers,
    }
    return OperationResult(
        status="error",
        op_id=op_id,
        error=summary,
        duration_ms=duration_ms,
        extras=extras,
    )


def wrap_ok_result(
    op_id: str, payload: Any, duration_ms: float, handle: ResultHandle | None
) -> OperationResult:
    """Build a successful :class:`OperationResult` from a reducer's output.

    :class:`OperationResult.result` is typed ``dict[str, Any] |
    list[Any] | None``; scalars are wrapped in a single-key
    ``{"value": ...}`` dict so the contract stays honest. The
    :class:`ResultHandle` (when non-None) lands on the dedicated
    :attr:`OperationResult.handle` field — T6 (#397) promoted it from
    the ``extras`` stash T5 used to surface it.
    """
    if payload is None or isinstance(payload, (dict, list)):
        result_value: dict[str, Any] | list[Any] | None = payload
    else:
        result_value = {"value": payload}
    return OperationResult(
        status="ok",
        op_id=op_id,
        result=result_value,
        duration_ms=duration_ms,
        handle=handle,
    )


def status_code_for_result(result_status: str) -> int:
    """Map a dispatcher ``result_status`` to a synthetic HTTP-shaped status code.

    The ``audit_log.status_code`` column is NOT NULL :class:`int` --
    optimised for the HTTP middleware path. The dispatcher contract is
    not HTTP, so the dispatcher synthesises one: ``200`` for ok,
    ``202`` for awaiting approval / pending (accepted but not yet
    executed — the agent needs-approval path), ``403`` for denied,
    ``500`` for error. The synthetic values are not surfaced to
    operators; the canonical signal lives in
    ``payload["result_status"]`` on the audit row.
    """
    if result_status == "ok":
        return 200
    if result_status == "awaiting_approval":
        return 202
    if result_status == "denied":
        return 403
    if result_status == "pending":
        return 202
    return 500
