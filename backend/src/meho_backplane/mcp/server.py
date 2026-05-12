# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /mcp`` — MCP Streamable HTTP transport entrypoint.

This module is the MCP server's front door: a FastAPI ``APIRouter``
mounted at ``/mcp`` by :mod:`meho_backplane.main` and a module-level
method-dispatch table that mirrors the
:func:`~meho_backplane.health.register_probe` registry pattern. The
route accepts JSON-RPC 2.0 envelopes per the
[2025-06-18 Streamable HTTP spec](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
and routes each ``method`` to a registered handler.

Auth in T1
==========

There is **no Bearer-token validation on this route in T1**. Every
well-formed JSON-RPC request currently succeeds, regardless of who
calls it. This is by design — T2 (#247) layers the OAuth 2.1
resource-server chain on top: ``/.well-known/oauth-protected-resource``,
``WWW-Authenticate`` headers on 401s, audience validation per RFC 8707,
and reuse of :func:`~meho_backplane.auth.jwt.verify_jwt`. Origin-header
validation per the MCP transport spec's DNS-rebinding security warning
is also a T2 concern (it depends on the ``MCP_ALLOWED_ORIGINS`` setting
T2 introduces).

Response shapes
===============

Per the Streamable HTTP §"Sending Messages to the Server":

* If the input is a JSON-RPC **request** (has an ``id``): the server
  returns HTTP 200 with ``Content-Type: application/json`` and a single
  JSON-RPC response envelope in the body. SSE (``text/event-stream``)
  for long-running tools is **out of scope** for v0.2.
* If the input is a JSON-RPC **notification** (no ``id``): the server
  returns HTTP 202 Accepted with an empty body. The MCP spec is strict
  on this — 204 is not a substitute even though both communicate
  "nothing to send back".

A JSON-RPC-level error (parse error, invalid request, method not found,
invalid params, internal error) is encoded as a 200 envelope with the
``error`` member populated. **Transport-level failures** flip the HTTP
status away from 200 / 202; T1 has one such case: an unsupported
``MCP-Protocol-Version`` header on a non-``initialize`` call returns
HTTP 400 with a JSON-RPC error envelope in the body (spec §"Protocol
Version Header" MUST). T2 (#247) adds the OAuth 401 / 403 cases when
the Bearer chain lands.

The single-shot JSON shape is also what the AC list on #246 codifies:
no streaming, no chunked responses, no SSE. The transport spec's GET
support (clients opening an SSE stream pre-request) is unimplemented;
FastAPI auto-replies HTTP 405 to GET on this path because the route
only declares POST.

Audit + middleware interaction
==============================

The chassis-stage :class:`~meho_backplane.audit.AuditMiddleware` and
:class:`~meho_backplane.middleware.RequestContextMiddleware` wrap every
HTTP request, including ``/mcp``. Without Bearer auth in T1, no
``operator_sub`` is bound into structlog contextvars, so
``AuditMiddleware`` short-circuits its skip rule and forwards the
buffered response unchanged — there's no fail-closed 500 from an audit
write because no audit write is attempted. T5 (#250) replaces this
implicit pass-through with a fail-closed MCP-specific audit path that
runs on ``tools/call`` + ``resources/read``.

References
----------
* JSON-RPC 2.0 — https://www.jsonrpc.org/specification
* MCP 2025-06-18 lifecycle — https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle
* MCP 2025-06-18 transport — https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError

from meho_backplane import __version__
from meho_backplane.auth.operator import Operator
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.schemas import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    InitializeRequest,
    InitializeResponse,
    JsonRpcError,
    JsonRpcId,
    JsonRpcRequest,
    JsonRpcResponse,
    ServerCapabilities,
)

__all__ = ["McpInvalidParamsError", "register_method", "router"]


#: Stable ``serverInfo.name`` returned on every ``initialize``. Matches
#: the FastAPI app title in :mod:`meho_backplane.main` so MCP clients
#: and the HTTP API's OpenAPI document agree on the server's identity.
_SERVER_NAME: str = "meho-backplane"


# A handler receives the validated :class:`Operator` (so it can apply
# RBAC + tenant filtering — see G0.5-T3) and the parsed JSON-RPC
# ``params`` (``None`` when the request omits the field). Returns one of:
# * a :class:`pydantic.BaseModel` instance — serialized via
#   :meth:`~pydantic.BaseModel.model_dump` into the response's ``result``;
# * a plain ``dict`` — used verbatim as the ``result`` body;
# * ``None`` — sentinel meaning "no result body", which is only
#   meaningful for handlers registered against ``notifications/*``
#   methods. For request-shaped methods, a ``None`` return is treated as
#   :data:`INTERNAL_ERROR` (a handler bug).
_McpHandlerResult = BaseModel | dict[str, Any] | None
_McpHandler = Callable[
    [Operator, dict[str, Any] | None],
    Awaitable[_McpHandlerResult],
]


_DISPATCH: dict[str, _McpHandler] = {}


def register_method(name: str, handler: _McpHandler) -> None:
    """Register a JSON-RPC method handler against the dispatch table.

    Mirrors the :func:`~meho_backplane.health.register_probe` registry
    pattern. T3 (#248) builds the per-tool registry on top of this:
    ``register_mcp_tool`` registers handlers under ``tools/*`` method
    names and supplies its own RBAC / scope filter; T4 (#249) populates
    the table with the first reference tool ``meho.status``.

    Raises :class:`RuntimeError` on duplicate name — handlers should be
    registered exactly once per process, at import time. The error
    surface is a programmer-error class rather than a 4xx because the
    registry is configured at module load, not via a network call.
    """
    if name in _DISPATCH:
        raise RuntimeError(f"MCP method {name!r} already registered")
    _DISPATCH[name] = handler


class McpInvalidParamsError(Exception):
    """Handler-side sentinel mapped to JSON-RPC ``INVALID_PARAMS``.

    Raised by a handler when its params fail validation (e.g.
    :class:`~meho_backplane.mcp.schemas.InitializeRequest.model_validate`
    raises :class:`pydantic.ValidationError`). The dispatcher catches
    this distinctly from a generic :class:`Exception` so the wire
    response carries code ``-32602`` rather than ``-32603``.

    Re-exported from :mod:`meho_backplane.mcp` so T3 (#248) tool
    handlers — and any future MCP method handler — can raise it
    without reaching into a dunder-private symbol.
    """


# ---------------------------------------------------------------------------
# Built-in lifecycle handlers
# ---------------------------------------------------------------------------


async def _initialize(
    _operator: Operator,
    params: dict[str, Any] | None,
) -> InitializeResponse:
    """Handle the ``initialize`` method per MCP 2025-06-18 §Initialization.

    Returns a server-info + capabilities envelope. The spec requires the
    server to echo the client's ``protocolVersion`` when it supports it,
    or respond with another supported version otherwise; T1 supports
    only :data:`PROTOCOL_VERSION` and always responds with that.
    Negotiation past v0.2 (e.g. supporting an older revision for
    legacy clients) is a v0.3 concern.
    """
    # ``params or {}`` deliberately collapses ``None`` and ``{}``. Spec-
    # aligned: a missing ``params`` field on the JSON-RPC request and an
    # explicit empty params object are both equivalent to "no required
    # fields supplied" for our purposes — :class:`InitializeRequest`'s
    # validator will then surface a clean INVALID_PARAMS for the
    # required-but-missing ``protocolVersion``.
    try:
        InitializeRequest.model_validate(params or {})
    except ValidationError as exc:
        raise McpInvalidParamsError(
            f"initialize: {exc.error_count()} validation error(s)",
        ) from exc

    # T3 (#248) registers tools/list, tools/call, resources/list,
    # resources/templates/list, resources/read — so the capabilities
    # envelope now safely advertises both surfaces. ``listChanged: false``
    # because v0.2 doesn't emit notifications/tools/list_changed
    # (registry is populated at startup and never mutates at runtime).
    # ``subscribe: false`` on resources because v0.2 doesn't implement
    # resources/subscribe.
    return InitializeResponse(
        capabilities=ServerCapabilities(
            tools={"listChanged": False},
            resources={"listChanged": False, "subscribe": False},
        ),
        serverInfo={"name": _SERVER_NAME, "version": __version__},
    )


async def _ping(
    _operator: Operator,
    _params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Handle the ``ping`` utility method.

    Defined in MCP 2025-06-18 §Utilities/Ping as the canonical liveness
    probe between client and server. The response body is an empty
    object — the operator-facing signal is the success itself, not
    anything inside it.
    """
    return {}


async def _initialized_notification(
    _operator: Operator,
    _params: dict[str, Any] | None,
) -> None:
    """Acknowledge ``notifications/initialized`` — no response body.

    The MCP lifecycle requires the client to send this notification
    after the ``initialize`` request resolves successfully (§Initialization).
    Per JSON-RPC §4.1.2, notifications carry no ``id`` and the server
    MUST NOT reply with a response envelope; the Streamable HTTP
    transport encodes that contract as HTTP 202 Accepted with no body.
    The handler runs for side-effect signalling only — future revisions
    may flip a per-session "initialized" flag here.
    """
    return None


register_method("initialize", _initialize)
register_method("notifications/initialized", _initialized_notification)
register_method("ping", _ping)


# ---------------------------------------------------------------------------
# Wire serialization
# ---------------------------------------------------------------------------


def _serialize_response(resp: JsonRpcResponse) -> dict[str, Any]:
    """Serialize a :class:`JsonRpcResponse` to a wire-shape dict.

    JSON-RPC §5: the response carries either ``result`` or ``error``
    but never both, and the unset half MUST be omitted from the wire
    (not serialized as ``null``). Pydantic's ``exclude_none=True`` is
    too coarse — it would also strip the ``id`` field on parse-error
    responses, which spec §5 mandates is ``null`` and present. So the
    serialization is hand-rolled: ``id`` always serialized; exactly one
    of ``result`` / ``error`` serialized based on which is set.
    """
    out: dict[str, Any] = {"jsonrpc": resp.jsonrpc, "id": resp.id}
    if resp.error is not None:
        out["error"] = resp.error.model_dump(mode="json", exclude_none=True)
    else:
        # Result is guaranteed non-None by JsonRpcResponse's model validator.
        out["result"] = resp.result
    return out


def _error_response(
    request_id: JsonRpcId,
    code: int,
    message: str,
    *,
    status_code: int = 200,
) -> JSONResponse:
    """Build a JSON-RPC error envelope wrapped in the chosen HTTP status.

    JSON-RPC-level errors (parse, invalid request, method-not-found,
    invalid params, internal error) default to HTTP 200 with the failure
    encoded in the envelope. ``status_code`` overrides this for the
    narrow set of transport-level failures that MCP Streamable HTTP
    mandates flip the HTTP status:

    * The ``MCP-Protocol-Version`` validation arm sets ``status_code=400``
      per spec §"Protocol Version Header" — "If the server receives a
      request with an invalid or unsupported `MCP-Protocol-Version`, it
      MUST respond with `400 Bad Request`."

    The body still carries a JSON-RPC envelope because the MCP transport
    spec at §"Sending Messages to the Server" allows it ("The HTTP
    response body MAY comprise a JSON-RPC error response that has no
    id") and it gives the client a structured failure to render.
    """
    body = JsonRpcResponse(
        id=request_id,
        error=JsonRpcError(code=code, message=message),
    )
    return JSONResponse(content=_serialize_response(body), status_code=status_code)


# ---------------------------------------------------------------------------
# Router + dispatch
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/mcp", tags=["mcp"])

_log = structlog.get_logger()


def _coerce_request_id(payload: dict[str, Any]) -> JsonRpcId:
    """Best-effort extract the ``id`` from a malformed request payload.

    Used on the JSON-RPC validation-error path so an INVALID_REQUEST
    response can still echo the client's id when one was syntactically
    parseable (helps clients correlate failures). When the raw value
    isn't a String / Number / NULL, fall back to ``None`` per spec §5
    ("If there was an error in detecting the id ... it MUST be Null").

    ``bool`` is short-circuited explicitly because it subclasses ``int``
    in Python — without this check ``True`` / ``False`` would slip
    through the ``isinstance(raw, (int, str))`` arm and echo as ``1`` /
    ``0`` in the error response, which the client cannot correlate
    against its original ``true`` / ``false`` request id.
    """
    raw = payload.get("id")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, str)) or raw is None:
        return raw
    return None


def _parse_request_body(raw_body: bytes) -> dict[str, Any] | JSONResponse:
    """Decode ``raw_body`` to a JSON-RPC envelope dict or an HTTP error response.

    Three rejection arms map onto the spec-prescribed error codes:

    * Empty body → PARSE_ERROR (clearer message than ``json.loads(b"")``'s
      "Expecting value").
    * :class:`json.JSONDecodeError` → PARSE_ERROR with the parser's message.
    * Non-dict payload (array or scalar) → INVALID_REQUEST. JSON-RPC §6
      allows batch arrays at the protocol layer but the MCP Streamable
      HTTP transport mandates a single envelope per POST.

    On success returns the parsed ``dict``; on failure returns the
    :class:`JSONResponse` that the dispatcher should hand back to the
    client. The union return is the idiomatic "or-error-response" shape
    that lets the caller narrow with :func:`isinstance`.
    """
    if not raw_body:
        return _error_response(None, PARSE_ERROR, "parse error: empty body")
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        _log.warning("mcp_parse_error", error=exc.msg)
        return _error_response(None, PARSE_ERROR, f"parse error: {exc.msg}")
    if not isinstance(payload, dict):
        return _error_response(
            None,
            INVALID_REQUEST,
            "invalid request: expected JSON object, not array or scalar",
        )
    return payload


def _validate_protocol_version_header(
    request: Request,
    method: str,
    payload: dict[str, Any],
) -> JSONResponse | None:
    """Enforce MCP 2025-06-18 §"Protocol Version Header" on non-initialize calls.

    Per spec, clients MUST send ``MCP-Protocol-Version`` on every post-
    ``initialize`` request, and the server MUST respond with HTTP 400
    when the value is invalid or unsupported. ``initialize`` is exempted
    because clients don't know which version to send until the handshake
    completes. Absent header on a non-initialize call is accepted as
    transitional lenience: spec SHOULD-assume-2025-03-26 doesn't help —
    v0.2 doesn't support that revision — and tightening this in T1
    would break clients that don't yet emit the header. T6 (#251)
    acceptance tests will pin the strict-mode contract.

    Returns ``None`` on the OK path; a :class:`JSONResponse` (HTTP 400
    + JSON-RPC error envelope) on rejection so the caller can early-
    return it.
    """
    if method == "initialize":
        return None
    protocol_header = request.headers.get("mcp-protocol-version")
    if protocol_header is None or protocol_header == PROTOCOL_VERSION:
        return None
    _log.warning(
        "mcp_unsupported_protocol_version",
        method=method,
        header=protocol_header,
        supported=PROTOCOL_VERSION,
    )
    return _error_response(
        _coerce_request_id(payload),
        INVALID_REQUEST,
        (
            f"unsupported MCP-Protocol-Version: {protocol_header!r} "
            f"(server supports {PROTOCOL_VERSION!r})"
        ),
        status_code=400,
    )


def _build_success_response(
    request_id: JsonRpcId,
    result: _McpHandlerResult,
) -> Response:
    """Wrap a successful handler result in the JSON-RPC response envelope.

    Three handler return shapes are accepted: a :class:`BaseModel` (dumped
    with ``exclude_none=True`` so optional MCP fields like
    ``InitializeResponse.instructions`` are omitted), a plain ``dict``
    (used verbatim), and any other shape — which is treated as a handler
    bug and converted to INTERNAL_ERROR. The else-arm is load-bearing
    for the non-notification contract: returning ``None`` from a
    request-shaped handler would otherwise emit a wire-broken envelope
    that fails the spec's exactly-one-of(result, error) invariant.
    """
    if isinstance(result, BaseModel):
        result_body: dict[str, Any] = result.model_dump(mode="json", exclude_none=True)
    elif isinstance(result, dict):
        result_body = result
    else:
        return _error_response(
            request_id,
            INTERNAL_ERROR,
            "handler returned no result for a non-notification request",
        )
    response = JsonRpcResponse(id=request_id, result=result_body)
    return JSONResponse(content=_serialize_response(response))


async def _dispatch_to_handler(
    jrpc: JsonRpcRequest,
    is_notification: bool,
    operator: Operator,
) -> Response:
    """Look up and run the handler; map handler outcomes to wire responses.

    The MCP transport spec at §"Sending Messages to the Server" splits
    the notification response shape: "If the server accepts the input,
    the server MUST return HTTP status code 202" vs. "If the server
    cannot accept the input, it MUST return an HTTP error status code."
    The phrase "cannot accept" is intentionally narrow — it refers to
    *transport-level* rejection (malformed envelope, bad JSON, batch
    arrays, unsupported protocol version), not "the handler's logic
    couldn't process this notification." Spec language treats
    application-level failures of a notification as the server's problem
    to log, not the client's to fix (the client cannot retry a
    notification without violating §4.1.2). Every post-envelope
    notification arm here therefore returns HTTP 202 and logs the
    failure for operator triage — the transport-level rejections in
    :func:`_parse_request_body`,
    :class:`JsonRpcRequest.model_validate`, and
    :func:`_validate_protocol_version_header` already flip the status
    to HTTP 4xx because they run *before* the notification/request
    split.
    """
    handler = _DISPATCH.get(jrpc.method)
    if handler is None:
        if is_notification:
            _log.warning("mcp_unknown_notification", method=jrpc.method)
            return Response(status_code=202)
        return _error_response(
            jrpc.id,
            METHOD_NOT_FOUND,
            f"method not found: {jrpc.method}",
        )

    try:
        result = await handler(operator, jrpc.params)
    except McpInvalidParamsError as exc:
        if is_notification:
            _log.warning(
                "mcp_notification_invalid_params",
                method=jrpc.method,
                error=str(exc),
            )
            return Response(status_code=202)
        return _error_response(jrpc.id, INVALID_PARAMS, str(exc))
    except Exception as exc:
        _log.exception("mcp_handler_error", method=jrpc.method)
        if is_notification:
            return Response(status_code=202)
        return _error_response(
            jrpc.id,
            INTERNAL_ERROR,
            f"internal error: {type(exc).__name__}",
        )

    if is_notification:
        return Response(status_code=202)
    return _build_success_response(jrpc.id, result)


@router.post("")
async def mcp_dispatch(
    request: Request,
    operator: Operator = Depends(verify_mcp_jwt_and_bind),
) -> Response:
    """Dispatch a single JSON-RPC 2.0 request or notification.

    The Streamable HTTP body contract (§"Sending Messages to the Server"):

    * Input is a JSON object — batch arrays are unsupported in T1.
    * On a *request* (id present): return 200 + single JSON envelope.
    * On a *notification* (id absent, or method prefix
      ``notifications/``): return 202 with no body, regardless of
      whether the handler errored — JSON-RPC §4.1.2 forbids replying.

    Authentication (G0.5-T2, #247): every request to this route MUST
    carry a Bearer token whose ``aud`` claim equals the canonical MCP
    resource URI per RFC 8707 §2. Validation runs as a FastAPI
    dependency before the body is parsed, so a missing or invalid
    token short-circuits to 401 + ``WWW-Authenticate: Bearer
    resource_metadata=...`` per RFC 9728 §5.1 — the dispatch pipeline
    never sees an unauthenticated request. The :class:`Operator` is
    injected into this handler for downstream use even though T2
    itself doesn't consume the identity beyond the contextvar binding
    side effect; T3 / T4 / T5 will pull tenant / role data off it.

    The function is the orchestrator only; each phase of the dispatch
    pipeline lives in its own helper so the per-phase contract is grep-
    able and the cognitive complexity stays under the project's
    SonarCloud threshold:

    * :func:`_parse_request_body` — body → ``dict`` or transport error.
    * :func:`JsonRpcRequest.model_validate` + :func:`_coerce_request_id`
      — envelope shape validation.
    * :func:`_validate_protocol_version_header` — spec §"Protocol
      Version Header" enforcement.
    * :func:`_dispatch_to_handler` — handler lookup + execution + error
      mapping.

    GET requests on this path automatically return HTTP 405 (FastAPI's
    default for an unmatched method) which satisfies the spec's
    fallback when the server does not implement the GET-opens-SSE
    branch of the Streamable HTTP transport.
    """
    # ``operator`` flows through to handlers — T3 registry handlers
    # use it for RBAC filtering on tools/list / resources/templates/list
    # and for the call-time role re-check on tools/call / resources/read.
    # Built-in lifecycle handlers (initialize, ping, notifications/
    # initialized) accept the parameter but don't read it.
    raw_body = await request.body()
    parsed = _parse_request_body(raw_body)
    if isinstance(parsed, JSONResponse):
        return parsed
    payload = parsed

    try:
        jrpc = JsonRpcRequest.model_validate(payload)
    except ValidationError as exc:
        return _error_response(
            _coerce_request_id(payload),
            INVALID_REQUEST,
            f"invalid request: {exc.error_count()} validation error(s)",
        )

    protocol_error = _validate_protocol_version_header(request, jrpc.method, payload)
    if protocol_error is not None:
        return protocol_error

    # Notification detection: JSON-RPC §4.1.2 says a notification is a
    # request without an ``id`` member. Pydantic-side, ``jrpc.id``
    # collapses absent and explicit-null to ``None`` (spec discourages
    # the latter); the raw dict tells us which form arrived. The
    # method-prefix relaxation handles buggy clients that send a
    # ``notifications/*`` method with an id — the method semantics
    # are spec-defined as notification-only, so the server treats it
    # as such regardless of the envelope's id field.
    is_notification = "id" not in payload or jrpc.method.startswith("notifications/")
    return await _dispatch_to_handler(jrpc, is_notification, operator)
