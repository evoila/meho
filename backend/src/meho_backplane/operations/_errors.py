# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size — pre-existing builder-collection debt (>800
# lines on main before #1782, which adds only the connector_tls_verify_failed
# builder alongside its 403/422 siblings); a split into per-error modules is its
# own refactor task, out of scope for the TLS-verify error-arm.

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
``connector_http_403`` / ``connector_http_422`` /
``connector_auth_failed`` / ``connector_tls_verify_failed`` /
``connector_vault_forbidden`` / ``connector_error``.
The ``status`` field maps
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
from meho_backplane.redaction.engine import redact
from meho_backplane.redaction.resolver import get_default_policy

__all__ = [
    "result_ambiguous_connector",
    "result_ambiguous_target",
    "result_awaiting_approval",
    "result_connector_auth_failed",
    "result_connector_error",
    "result_connector_http_403",
    "result_connector_http_422",
    "result_connector_tls_verify_failed",
    "result_connector_unsupported",
    "result_connector_vault_forbidden",
    "result_denied",
    "result_handler_unreachable",
    "result_invalid_params",
    "result_no_connector",
    "result_no_target",
    "result_target_invalid_type",
    "result_target_required",
    "result_unknown_op",
    "status_code_for_result",
    "wrap_ok_result",
]

#: Cap on the exception-message length recorded in the ``connector_error``
#: extras payload. A misbehaving connector could embed a credential into
#: a stringified exception; 256 chars is enough for an operator to
#: recognise the failure shape while capping the leak surface. The cap
#: is the *second* line of defence -- :func:`_sanitize_free_text` runs
#: the Tier-1 redactor over the text first, so a credential inside the
#: first 256 chars does not ride the envelope in cleartext.
_EXC_MESSAGE_CAP: int = 256

#: Fail-closed placeholder when Tier-1 redaction itself is unavailable
#: on the never-raises error path. Returning the raw text instead would
#: reintroduce exactly the passthrough this sanitizer exists to close.
_TEXT_WITHHELD: str = "<diagnostic text withheld: redaction unavailable>"


def _sanitize_free_text(text: str) -> str:
    """Tier-1-redact, then length-cap, one free-text diagnostic string.

    Every ``str(exc)`` / upstream-body line a result builder emits
    (``extras["exception_message"]``, ``extras["upstream_message"]``,
    ``extras["detail"]``, and the ``error`` summary tails built from
    them) passes through here before landing in the response/audit
    envelope. Redaction runs **before** the cap: capping first could
    truncate a secret mid-value into a fragment the patterns no longer
    match, leaving cleartext behind.

    The packaged default policy (credential-shaped patterns only -- no
    UUID/IP/FQDN rules, so hosts and IDs stay legible for diagnosis) is
    pinned via :func:`get_default_policy`, deliberately bypassing the
    resolver's override table: an operator-registered shadow-mode or
    narrowed policy must not silently disable redaction on the error
    path. Fail-closed: if redaction itself errors, the text is withheld
    entirely -- this is a never-raises path and an unredacted fallback
    would be the leak.
    """
    try:
        redacted = redact(text, get_default_policy()).redacted
    except Exception:  # fail-closed: never leak, never raise on the error path
        return _TEXT_WITHHELD
    if not isinstance(redacted, str):  # pragma: no cover -- str in, str out
        return _TEXT_WITHHELD
    if len(redacted) > _EXC_MESSAGE_CAP:
        return redacted[:_EXC_MESSAGE_CAP] + "...<truncated>"
    return redacted


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


def result_no_target(
    op_id: str,
    query: str,
    matches: list[dict[str, Any]],
    duration_ms: float,
) -> OperationResult:
    """A supplied ``target`` name resolved to no live target in the tenant (#136).

    Target **resolution** is a domain outcome, not a transport fault: a valid
    request naming a target that doesn't exist should ride the dispatcher
    envelope (``status="error"`` + ``extras.error_code``), the same as
    ``target_required`` / ``invalid_params`` / ``unknown_op`` — so a
    ``/operations/call`` (and ``/preview``) consumer switches on a single
    ``extras.error_code`` for every target-resolution failure instead of
    parsing a 404 body. (The meta-tool layer catches
    :exc:`~meho_backplane.targets.resolver.TargetNotFoundError` and returns
    this rather than letting the HTTP-layer 404 escape.) A genuinely malformed
    ``target`` (wrong JSON type) rides the envelope too, as
    :func:`result_target_invalid_type` (#2110 closed the last 422 boundary).

    ``extras`` carries the near-miss ``matches`` (up to 5 candidate names) and
    the ``query`` verbatim from the resolver's 404 detail, so the envelope is
    information-equivalent to the old ``404 {error, query, matches}`` body.
    """
    return OperationResult(
        status="error",
        op_id=op_id,
        error=f"no_target: no target matches {query!r} in the tenant",
        duration_ms=duration_ms,
        extras={
            "error_code": "no_target",
            "op_id": op_id,
            "query": query,
            "matches": matches,
        },
    )


def result_target_invalid_type(
    op_id: str,
    received_type: str,
    duration_ms: float,
) -> OperationResult:
    """A supplied ``target`` is not a string, an object, or ``null`` (#2110).

    The last target-failure mode to join the envelope. #136 moved the
    resolution failures (``target_required`` / ``no_target`` /
    ``ambiguous_target``) into the envelope but deliberately left a
    wrong-JSON-typed ``target`` (e.g. ``target: 12345``) as a request-schema
    422 from the Pydantic body model. Issue #2110's recorded decision
    (Option A, 2026-07-06) supersedes that boundary: **every** target-failure
    mode returns HTTP 200 + this envelope, so a consumer's error handling is
    one switch on ``extras.error_code`` with no 422 ``detail[]`` array left to
    special-case. The body models keep the documented ``string | object |
    null`` schema for codegen (see ``_TargetArg`` in
    :mod:`meho_backplane.operations.meta_tools`) while accepting any JSON
    value at runtime; :func:`~meho_backplane.operations.meta_tools._normalize_target_arg`
    classifies the wrong-typed value and this builder shapes the envelope.

    ``extras`` carries the JSON-type name of the offending value
    (``received_type``: ``"integer"`` / ``"boolean"`` / ``"array"`` / ...) so
    an agent can name what it sent without re-parsing the human text.
    """
    return OperationResult(
        status="error",
        op_id=op_id,
        error=(
            f"target_invalid_type: target must be a string name, an object "
            f"with a 'name' field, or null; got {received_type}"
        ),
        duration_ms=duration_ms,
        extras={
            "error_code": "target_invalid_type",
            "op_id": op_id,
            "received_type": received_type,
        },
    )


def result_ambiguous_target(
    op_id: str,
    query: str,
    matches: list[dict[str, Any]],
    duration_ms: float,
) -> OperationResult:
    """A supplied ``target`` name matched more than one live target (#136).

    The other arm of the target-**resolution** contract alongside
    :func:`result_no_target`: an alias collision (or a repaired unique-index
    violation) makes a name ambiguous. Like every resolution outcome it rides
    the dispatcher envelope (``status="error"`` + ``extras.error_code``) rather
    than escaping as the resolver's HTTP 409 — so a ``/operations/call`` (and
    ``/preview``) consumer's single ``extras.error_code`` switch covers *every*
    resolution failure, with no 409 exception to special-case. ``extras``
    carries the colliding ``matches`` + ``query`` verbatim from the resolver's
    409 detail, information-equivalent to the old body.
    """
    return OperationResult(
        status="error",
        op_id=op_id,
        error=f"ambiguous_target: {query!r} matches more than one target in the tenant",
        duration_ms=duration_ms,
        extras={
            "error_code": "ambiguous_target",
            "op_id": op_id,
            "query": query,
            "matches": matches,
        },
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
        extras["exception_message"] = _sanitize_free_text(exception_message)
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
            "exception_message": _sanitize_free_text(exception_message),
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


def result_connector_error(
    op_id: str,
    exc: BaseException,
    duration_ms: float,
) -> OperationResult:
    """Connector / handler raised. Class + redacted, capped message land in extras.

    ``str(exc)`` on a connector exception can embed a URL with inline
    credentials, a header dump, or a driver DSN, so the message runs
    through :func:`_sanitize_free_text` (Tier-1 redaction, then the
    length cap) before it reaches the envelope.
    """
    msg = _sanitize_free_text(str(exc))
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


def _connector_unsupported_remediation(
    *,
    origin: str,
    cause: Literal["unsupported_feature", "unreplaced_auto_shim"],
    sibling_impl_id: str | None,
) -> str:
    """Build the per-cause remediation clause for ``connector_unsupported``.

    Three shapes (G0.23-T1 #1627 + G0.25-T2 #1753): an
    ``unreplaced_auto_shim`` whose ``(product, version)`` already has a
    hand-rolled sibling under a different ``impl_id`` (re-ingest under
    it), one without a sibling (register the per-product subclass), and
    an ``unsupported_feature`` (a config matter against the connector's
    supported modes). Extracted from :func:`result_connector_unsupported`
    so the builder stays under the size budget.
    """
    if cause == "unreplaced_auto_shim" and sibling_impl_id is not None:
        return (
            f"{origin} is the auto-registered ingest shim, which cannot "
            f"authenticate or execute against the upstream -- but a "
            f"hand-rolled connector class already ships for this "
            f"(product, version) under impl_id={sibling_impl_id!r}. The "
            f"shim was ingested under a near-miss impl_id and is shadowing "
            f"that working class. Re-ingest the spec under "
            f"impl_id={sibling_impl_id!r} (or delete this shim's ingested "
            f"ops); do NOT write a new subclass -- one already exists. See "
            f"docs/codebase/spec-ingestion.md for the auto-shim lifecycle."
        )
    if cause == "unreplaced_auto_shim":
        return (
            f"{origin} is the auto-registered ingest shim, which cannot "
            f"authenticate or execute against the upstream. Register the "
            f"hand-rolled per-product Connector subclass for this "
            f"(product, version, impl_id) and redeploy before enabling "
            f"dispatch on this connector's ops -- re-ingesting the spec "
            f"will NOT replace the shim. See "
            f"docs/codebase/spec-ingestion.md for the auto-shim "
            f"lifecycle."
        )
    return (
        f"{origin} deliberately does not implement what this "
        f"dispatch requires for the target. Re-check the target's "
        f"configuration (e.g. auth_model) against the modes the "
        f"connector supports, or route the op at a connector that "
        f"implements them. See docs/architecture/connector-auth.md "
        f"for the connector auth contract."
    )


def result_connector_unsupported(
    op_id: str,
    exc: BaseException,
    cause: Literal["unsupported_feature", "unreplaced_auto_shim"],
    connector_class: str | None,
    duration_ms: float,
    sibling_impl_id: str | None = None,
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
      can never authenticate or execute. Remediation depends on
      whether a hand-rolled sibling already exists (G0.25-T2 #1753):

      - *sibling_impl_id is None* -- no hand-rolled class for this
        ``(product, version)`` under any ``impl_id``. Register the
        per-product subclass before enabling dispatch; re-ingesting
        the spec will NOT replace the shim.
      - *sibling_impl_id is set* -- a hand-rolled class for the same
        ``(product, version)`` already ships under a DIFFERENT
        ``impl_id``, and the shim is shadowing it (the one-token-off
        ingest footgun behind T1 #1750). The fix is to re-ingest
        under the named sibling ``impl_id`` (or delete the stray
        shim's ops), NOT to write a new subclass -- one already
        exists. Naming the sibling stops the operator chasing
        "future work" that is already shipped.

    The dispatcher classifies the cause via ``isinstance(...,
    GenericRestConnector)`` at the catch site -- precise, not
    message-fragile -- and resolves *sibling_impl_id* via
    :func:`~meho_backplane.operations.ingest.connector_registration.sibling_handrolled_impl_id`
    (the same registry scan the ingest near-miss guard uses, so the
    proactive ingest warning and this reactive dispatch error name the
    same sibling). The shape complies with the #1141 convention: a
    stable ``connector_unsupported`` code, a diagnostic-bearing human
    message (verbatim detail + remediation imperative + doc
    reference), and a structured ``extras`` payload (``cause`` /
    ``connector_class`` / ``detail`` / ``sibling_impl_id``) so an
    agent can branch without re-parsing the text. ``detail`` reuses
    the :data:`_EXC_MESSAGE_CAP` discipline from
    :func:`result_connector_error` (both production raise sites are
    comfortably under the cap, so their texts survive verbatim).
    """
    detail = _sanitize_free_text(str(exc))
    origin = (
        f"The resolved connector ({connector_class})"
        if connector_class is not None
        else "The resolved handler"
    )
    remediation = _connector_unsupported_remediation(
        origin=origin,
        cause=cause,
        sibling_impl_id=sibling_impl_id,
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
            "sibling_impl_id": sibling_impl_id,
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


def _http_upstream_message(response: httpx.Response) -> str | None:
    """Best-effort extraction of the upstream's human error message.

    GitHub (and most REST APIs that bother) returns a JSON body with a
    top-level ``message`` (``"Resource not accessible by integration"``
    on a 403, ``"Validation Failed"`` on a 422); that is the single most
    useful line for diagnosis, so it is preferred when the body parses as
    a JSON object carrying a string ``message``. Bodies that are not
    JSON, or JSON without a usable ``message``, fall back to the capped
    raw text. ``None`` only when the body is empty. Either branch runs
    through :func:`_sanitize_free_text` -- Tier-1 redaction plus the
    :data:`_EXC_MESSAGE_CAP` discipline -- so credential-bearing
    upstream text never reaches the envelope in cleartext. Shared by
    the 403 / 422 / auth-failed builders (the body shape is identical
    across GitHub's 4xx responses).
    """
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str) and message.strip():
            return _sanitize_free_text(message)
    text = (response.text or "").strip()
    if not text:
        return None
    return _sanitize_free_text(text)


def _http_validation_errors(response: httpx.Response) -> list[Any]:
    """Extract the GitHub-style ``errors[]`` validation array from a 422 body.

    GitHub's 422 ``Validation Failed`` body carries an ``errors`` array
    naming each offending field (``[{"resource", "field", "code", ...}]``);
    that array is the actionable detail an operator/agent needs to fix the
    payload (the requestBody-mangling bug T5 #1656 is exactly the class of
    failure it pinpoints). It is echoed **verbatim** when the body parses
    as a JSON object whose ``errors`` is a list -- never required, so a
    non-GitHub 422 (or a 422 whose body carried no ``errors``) yields an
    empty list rather than a fabricated shape. A non-JSON body yields an
    empty list too; its human text is still surfaced via
    :func:`_http_upstream_message`.
    """
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        return []
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list):
            return errors
    return []


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
    upstream_message = _http_upstream_message(response)
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


def result_connector_http_422(
    op_id: str,
    exc: httpx.HTTPStatusError,
    duration_ms: float,
) -> OperationResult:
    """Connector raised an upstream **422 Unprocessable Entity** on dispatch.

    G0.24-T4 (#1649), the validation sibling of
    :func:`result_connector_http_403`. A write dispatch whose request
    *payload* the upstream rejected as invalid (a malformed body, a
    missing required field) surfaces as :exc:`httpx.HTTPStatusError` with
    a 422 status. GitHub returns a genuinely useful 422 -- a body
    ``message`` (``"Validation Failed"``) and an ``errors`` array naming
    each offending field -- but the shared :class:`HttpConnector` adapter
    does no error mapping, so routing it through
    :func:`result_connector_error` flattened all of it into an opaque
    ``connector_error: HTTPStatusError`` with only the httpx status line,
    the actionable detail buried in ``extras["exception_message"]``. That
    bare shape is exactly what slowed the diagnosis of the
    requestBody-mangling bug T5 #1656 (consumer
    ``claude-rdc-hetzner-dc#1138``).

    The cause is kept **connector-agnostic**: any upstream 422 means the
    upstream parsed the request and rejected its *content*, not transport
    or authorization -- so the operator-facing ``error`` names the
    payload-rejected cause regardless of which connector raised.
    ``extras`` carries the machine-usable fields an agent can branch on
    without re-parsing a transport error: ``http_status`` (always
    ``422`` -- the dispatcher scopes this builder to that code), the
    upstream ``upstream_message`` when the body carried one, and the
    GitHub-style ``validation_errors`` array (the body's ``errors[]``)
    echoed verbatim when present -- never required, so a non-GitHub 422
    (or one with no ``errors``) still yields the structured cause with an
    empty ``validation_errors``.
    """
    response = exc.response
    upstream_message = _http_upstream_message(response)
    validation_errors = _http_validation_errors(response)
    summary = (
        "connector_http_422: the upstream rejected the request payload as "
        "invalid (HTTP 422 Unprocessable Entity). The credential reached the "
        "upstream and the request was understood, but its content failed the "
        "upstream's validation -- a request-shape matter, not a meho transport "
        "or permission fault. Inspect extras.validation_errors (the upstream's "
        "field-level errors[] when it sent them) to see which fields were "
        "rejected, correct the payload, and retry. See "
        "docs/codebase/error-message-shape.md for the dispatch error "
        "convention."
    )
    if upstream_message is not None:
        summary = f"{summary} Upstream said: {upstream_message}"
    extras: dict[str, Any] = {
        "error_code": "connector_http_422",
        "http_status": 422,
        "upstream_message": upstream_message,
        "validation_errors": validation_errors,
    }
    return OperationResult(
        status="error",
        op_id=op_id,
        error=summary,
        duration_ms=duration_ms,
        extras=extras,
    )


#: The non-2xx statuses the dispatcher classifies as an auth/session
#: failure rather than the generic ``connector_error`` **for a typed
#: (hand-coded) connector**. ``401`` is the load-bearing, connector-
#: agnostic case. G0.29-T2 (#2067) wires invalidate-and-retry-once on an
#: auth-class status into the generic-ingested dispatch path itself
#: (:func:`~meho_backplane.operations.dispatcher._handle_http_status_error`):
#: a session-stateful connector advertising ``invalidate_session(target)``
#: has its cached token evicted and the op re-dispatched once, so an expired
#: vCenter (401) / vRLI (440) session re-logs-in there. Only when that
#: dispatch-path re-login *also* fails does an auth-class status reach
#: :func:`result_connector_auth_failed` -- meaning the credential is missing
#: / invalid / expired in Vault, or the target's ``auth_model`` is wrong.
#: (Earlier comments here claimed the connectors retried internally on a 401
#: via ``_get_json_with_session_retry``; that helper had no caller on the
#: ingested dispatch path, which is the gap #2067 closes.) ``440`` is
#: vRLI's own session-expiry status (the literal code the operator saw
#: flattened to ``connector_error (440)`` on the #1798 dispatch); the team
#: opted to recognise it here so the appliance status that surfaced the gap
#: maps to the same actionable class. Every other non-2xx (404, 5xx, 429,
#: ...) is deliberately excluded and falls through to ``connector_error``
#: unchanged -- 429 (rate-limit) is a separate deliberate follow-up, not an
#: auth failure.
#:
#: This global is the fallback used for typed connectors, which carry no
#: :class:`~meho_backplane.connectors.profile.ExecutionProfile`. A
#: *profiled* connector instead declares its expiry-status set once on its
#: profile (``ExecutionProfile.expiry_statuses``, default ``{401}``; vRLI
#: ``{401, 440}``) -- the single source #1973 unifies across the session
#: harness and this arm -- and the dispatcher threads that set into
#: :func:`is_auth_failed_status` via ``expiry_statuses``. Typed-connector
#: classification is therefore unchanged.
_AUTH_FAILED_STATUSES: frozenset[int] = frozenset({401, 440})


def is_auth_failed_status(
    status_code: int,
    expiry_statuses: frozenset[int] | None = None,
) -> bool:
    """Whether *status_code* is one the dispatcher treats as an auth failure.

    The single source of truth for the recognised set so the dispatcher's
    narrowing arm and this module agree on which statuses siphon into
    :func:`result_connector_auth_failed`.

    *expiry_statuses* is the profile-declared set for a profiled connector
    (``ExecutionProfile.expiry_statuses``); when supplied it is the
    authoritative set for that dispatch, so the profile's declaration feeds
    this classification arm and the session-retry harness from one source
    (#1973). When ``None`` -- every typed (hand-coded) connector, which has
    no profile -- the module global :data:`_AUTH_FAILED_STATUSES` is used,
    leaving typed-connector classification unchanged.
    """
    recognised = _AUTH_FAILED_STATUSES if expiry_statuses is None else expiry_statuses
    return status_code in recognised


def result_connector_auth_failed(
    op_id: str,
    exc: httpx.HTTPStatusError,
    target: Any,
    duration_ms: float,
) -> OperationResult:
    """Connector raised an upstream **auth/session failure** on dispatch.

    T5 (#1804) of the G0.26 v0.16.0 dogfood-hardening Initiative (#1800),
    the auth-class sibling of :func:`result_connector_http_403` (#1649)
    and :func:`result_connector_tls_verify_failed` (#1782). A dispatch
    whose backing credential is missing / invalid / expired -- or whose
    target ``auth_model`` is wrong -- surfaces as an
    :exc:`httpx.HTTPStatusError` with an auth-class status
    (:data:`_AUTH_FAILED_STATUSES`: ``401``, plus vRLI's ``440``).

    ``401`` is the load-bearing case and is connector-agnostic. The
    dispatch path itself now re-logs-in and retries once on an auth-class
    status when the connector advertises ``invalidate_session`` (G0.29-T2
    #2067), so a status that reaches *this* builder means **re-login also
    failed** -- not a transient blip but a credential / ``auth_model``
    problem the operator must fix. Routing
    it through :func:`result_connector_error` flattened that into an
    opaque ``connector_error: HTTPStatusError`` with the cause buried in
    ``extras["exception_message"]``, which is exactly the diagnosability
    gap that made the #1798 vRLI dispatch (seen as ``connector_error
    (440)``) look like a stub-auth problem.

    This builder names the **host** (read from ``target.host`` -- the
    operator's own configured value, so not an info-leak per
    ``docs/codebase/error-message-shape.md``, the same reasoning as the
    TLS builder and the ``/ui/auth/login`` 503 naming the operator's env
    vars), the **status**, the likely **cause** (session/credential
    expiry or a misconfigured ``auth_model``), and the **remediation**
    (verify the target's Vault credential and its ``auth_model``). The
    upstream body message (when present) tails the operator-facing
    string. ``target`` is typed :class:`typing.Any` because the
    dispatcher threads the live ORM/duck-typed target through; only
    ``.host`` is read, with a ``getattr`` guard so a target shape without
    it degrades to a bare host label rather than raising inside the
    never-raises error path.

    ``extras`` carries the machine-usable fields an agent can branch on
    without re-parsing the transport error: ``http_status`` (the actual
    auth-class status the upstream returned), ``host``, and the upstream
    ``upstream_message`` (the body's ``message`` when JSON, else capped
    raw text, ``None`` when the body was empty -- shared with the 403/422
    builders via :func:`_http_upstream_message`).
    """
    response = exc.response
    status_code = response.status_code
    upstream_message = _http_upstream_message(response)
    host = getattr(target, "host", None) or "the target host"
    summary = (
        f"connector_auth_failed: the upstream returned an auth/session "
        f"failure (HTTP {status_code}) for {host}. The connector reached the "
        f"host but its credential or session was rejected. The dispatch path "
        f"already re-logged-in and retried once on a session-expiry status "
        f"(401 or vRLI's 440), so when this reaches you the re-login also "
        f"failed: the credential is most likely missing, invalid, or expired "
        f"in Vault, or the target's auth_model is wrong. Verify the target's "
        f"Vault credential and its auth_model against what the connector "
        f"expects, then retry. See docs/architecture/connector-auth.md "
        f"for the connector auth contract and "
        f"docs/codebase/error-message-shape.md for the dispatch error "
        f"convention."
    )
    if upstream_message is not None:
        summary = f"{summary} Upstream said: {upstream_message}"
    extras: dict[str, Any] = {
        "error_code": "connector_auth_failed",
        "http_status": status_code,
        "host": host,
        "upstream_message": upstream_message,
    }
    return OperationResult(
        status="error",
        op_id=op_id,
        error=summary,
        duration_ms=duration_ms,
        extras=extras,
    )


def result_connector_vault_forbidden(
    op_id: str,
    exc: BaseException,
    target: Any,
    duration_ms: float,
    expected_secret_ref: str | None = None,
) -> OperationResult:
    """Vault denied a read/write with **permission denied** during dispatch.

    #2091 (Initiative #2150), the Vault-authorization sibling of
    :func:`result_connector_http_403` (#1649) and
    :func:`result_connector_auth_failed` (#1804). A dispatch whose
    credential resolution reads the target's ``secret_ref`` from Vault
    under the operator's identity
    (:func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`)
    surfaces an out-of-subtree ref as :exc:`hvac.exceptions.Forbidden` —
    Vault's opaque ``permission denied``. Routing it through
    :func:`result_connector_error` flattened that into
    ``connector_error: Forbidden`` with the cause buried in
    ``extras["exception_message"]``, reading exactly like a missing
    Vault grant — and the first instinct (widen the deploy-owned
    ``meho-mcp`` Vault policy) is the wrong fix: the policy is re-applied
    on every deploy, and the design stores a target's credential at the
    per-tenant path ``tenants/<tenant_id>/<name>`` (#1723).

    Two message shapes, keyed on whether the dispatch carried a target
    with a configured ``secret_ref``:

    * **credential resolution** (``target.secret_ref`` set) — names the
      target's ``secret_ref``, states the likely cause (the ref is
      outside the operator's readable per-tenant subtree), names the
      canonical convention and the exact *expected_secret_ref* the
      caller derived (:func:`~meho_backplane.connectors.vault.tenant_paths.tenant_secret_ref`),
      and carries the stage-the-credential remediation plus the explicit
      "do NOT widen the Vault policy" warning.
    * **no target / no ref** (a typed ``vault.*`` op denied by the Vault
      ACL itself) — a generic Vault-authorization cause naming the
      operator-identity read and the tenant-scope convention, without
      fabricating a ``secret_ref`` diagnosis that does not apply.

    ``target`` is typed :class:`typing.Any` because the dispatcher
    threads the live ORM/duck-typed target (or ``None``) through; only
    ``.name`` / ``.secret_ref`` are read, with ``getattr`` guards so any
    target shape degrades gracefully inside the never-raises error path.
    The hvac exception text (``permission denied, on GET <url>``) names
    only the operator-supplied path — no secret material — and tails the
    operator-facing string under the :data:`_EXC_MESSAGE_CAP` discipline.
    ``extras`` carries the machine-usable fields (``error_code`` /
    ``secret_ref`` / ``expected_secret_ref`` / ``exception_class`` /
    ``exception_message``) so an agent can branch without re-parsing the
    text, per the #1141 convention.
    """
    msg = str(exc)
    if len(msg) > _EXC_MESSAGE_CAP:
        msg = msg[:_EXC_MESSAGE_CAP] + "...<truncated>"
    secret_ref = getattr(target, "secret_ref", None)
    target_name = getattr(target, "name", None) or "the target"
    if secret_ref:
        expected_clause = (
            f"stage the credential at {expected_secret_ref!r} on the 'secret' mount "
            if expected_secret_ref is not None
            else "stage the credential under 'tenants/<tenant_id>/<name>' on the 'secret' mount "
        )
        summary = (
            f"connector_vault_forbidden: Vault denied the credential read for "
            f"{target_name} (permission denied). The target's secret_ref "
            f"{secret_ref!r} is most likely outside the operator's readable "
            f"per-tenant subtree — the canonical layout stores a target's "
            f"credential at 'tenants/<tenant_id>/<name>' (#1723, enforced by "
            f"the rendered VAULT_KV_TENANT_SCOPE_PREFIX). To fix, "
            f"{expected_clause}and update the target's secret_ref, then "
            f"retry. Do NOT widen the backplane's Vault policy — it is "
            f"deploy-owned and re-applied on every upgrade. See "
            f"docs/codebase/connectors-vault-tenant-scope.md for the "
            f"namespace convention and "
            f"docs/codebase/error-message-shape.md for the dispatch error "
            f"convention."
        )
    else:
        summary = (
            "connector_vault_forbidden: Vault denied this operation "
            "(permission denied) under the operator's identity. The "
            "requested path is outside what the operator's Vault ACL policy "
            "/ tenant scope grants — per-tenant secrets live under "
            "'tenants/<tenant_id>/...' on the 'secret' mount (#1723). Check "
            "the requested path against that convention and the deploy's "
            "Vault policy for the meho role. See "
            "docs/codebase/connectors-vault-tenant-scope.md for the "
            "namespace convention and "
            "docs/codebase/error-message-shape.md for the dispatch error "
            "convention."
        )
    if msg:
        # hvac renders Forbidden as "permission denied, on GET <url>" —
        # the URL is the operator-supplied path, never secret material.
        summary = f"{summary} Vault said: {msg}"
    return OperationResult(
        status="error",
        op_id=op_id,
        error=summary,
        duration_ms=duration_ms,
        extras={
            "error_code": "connector_vault_forbidden",
            "secret_ref": secret_ref,
            "expected_secret_ref": expected_secret_ref,
            "exception_class": type(exc).__name__,
            "exception_message": msg,
        },
    )


#: The three remediation clauses the ``connector_tls_verify_failed``
#: message names, in preference order (most secure first). Kept as module
#: constants so the builder body stays under the size budget and the
#: operator-facing text is auditable in one place.
_TLS_REMEDIATION_SECURE: str = (
    "Preferred (global): make meho trust the endpoint's certificate chain -- "
    "point SSL_CERT_FILE at a CA bundle that includes the issuing CA, or "
    "inject the internal-CA / self-signed cert into the chart trust-bundle, "
    "so verification succeeds without weakening it."
)
#: T5 (#1784). The secure per-target supersession of ``verify_tls=false``:
#: pin the appliance's CA on the target itself. Keeps CERT_REQUIRED +
#: hostname verification on (the govc-thumbprint pattern), so -- unlike the
#: last resort below -- the channel stays authenticated against a MITM.
#: Named ahead of ``verify_tls=false`` because it is the preferred fix when
#: the global bundle can't be changed.
_TLS_REMEDIATION_CA_PIN: str = (
    "Preferred (per-target): pin this appliance's CA on the target itself -- "
    "set tls_ca_pin to its CA/cert PEM. meho then trusts that specific CA "
    "while keeping certificate-chain AND hostname verification on, so the "
    "channel stays protected against a man-in-the-middle. Use this when you "
    "can't add the CA to the global bundle above."
)
_TLS_REMEDIATION_LAST_RESORT: str = (
    "Last resort: set verify_tls=false on this target to skip TLS "
    "verification for it alone (audited, per-target, never global). This "
    "still forwards the target's resolved credential over the unverified "
    "channel, exposing it to a man-in-the-middle -- use only against a "
    "trusted-network appliance you cannot yet pin a CA for, and prefer "
    "tls_ca_pin above."
)


def result_connector_tls_verify_failed(
    op_id: str,
    exc: BaseException,
    target: Any,
    duration_ms: float,
) -> OperationResult:
    """Connector dispatch failed TLS certificate verification.

    T3 (#1782) of the per-target-TLS Initiative (#1774). A dispatch whose
    transport opened the socket but failed cert-chain verification raises
    an :exc:`httpx.ConnectError` whose ``__cause__`` is an
    :exc:`ssl.SSLCertVerificationError` (the self-signed / internal-CA
    appliance case). Such an error has no ``.response``, so it skips the
    :exc:`httpx.HTTPStatusError` arm and used to fall through to the
    generic :func:`result_connector_error` -- flattening it to an opaque
    ``connector_error: ConnectError`` that discarded the SSL cause, so the
    operator saw ``[SSL: CERTIFICATE_VERIFY_FAILED]`` with no guidance.

    This builder names the **host** and **three** remediations in
    preference order (most secure first): the global ``SSL_CERT_FILE`` /
    chart trust-bundle path (verification stays on); the per-target
    ``tls_ca_pin`` CA-pin (T5 #1784 -- the secure supersession, keeps
    ``CERT_REQUIRED`` + hostname on against the pinned CA); and
    ``verify_tls=false`` as the audited per-target last resort (the opt-in
    T1 #1780 adds), with the MITM / credential-exposure caveat. The raw
    ``[SSL: CERTIFICATE_VERIFY_FAILED]...`` string is preserved in
    ``extras.exception_message`` (capped at :data:`_EXC_MESSAGE_CAP`) so
    the operator can confirm the failure shape without it being lost into
    the generic envelope.

    The host is read from ``target.host`` -- a hostname the operator
    configured themselves, so it is not an info-leak per
    ``docs/codebase/error-message-shape.md`` (the same reasoning as the
    gold-standard ``/ui/auth/login`` naming the operator's own env vars).
    ``target`` is typed :class:`typing.Any` because the dispatcher threads
    the live ORM/duck-typed target through; only ``.host`` is read, with a
    ``getattr`` guard so a target shape without it degrades to a bare host
    label rather than raising inside the never-raises error path.
    """
    msg = _sanitize_free_text(str(exc))
    host = getattr(target, "host", None) or "the target host"
    summary = (
        f"connector_tls_verify_failed: TLS certificate verification failed "
        f"for {host}. The socket opened and the host answered, but its "
        f"certificate chain is not trusted (typically a self-signed or "
        f"internal-CA appliance). {_TLS_REMEDIATION_SECURE} "
        f"{_TLS_REMEDIATION_CA_PIN} {_TLS_REMEDIATION_LAST_RESORT} See "
        f"docs/codebase/error-message-shape.md for the dispatch error "
        f"convention."
    )
    return OperationResult(
        status="error",
        op_id=op_id,
        error=summary,
        duration_ms=duration_ms,
        extras={
            "error_code": "connector_tls_verify_failed",
            "host": host,
            "exception_class": type(exc).__name__,
            "exception_message": msg,
            "remediation_secure": _TLS_REMEDIATION_SECURE,
            "remediation_ca_pin": _TLS_REMEDIATION_CA_PIN,
            "remediation_last_resort": _TLS_REMEDIATION_LAST_RESORT,
        },
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
