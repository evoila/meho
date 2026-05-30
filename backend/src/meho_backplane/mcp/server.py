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
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Final

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
from meho_backplane.settings import get_settings

__all__ = [
    "RESOURCES_SUBSCRIBE_ENABLED",
    "McpInvalidParamsError",
    "mcp_session_id_capture_mode",
    "register_method",
    "router",
]


#: Stable ``serverInfo.name`` returned on every ``initialize``. Matches
#: the FastAPI app title in :mod:`meho_backplane.main` so MCP clients
#: and the HTTP API's OpenAPI document agree on the server's identity.
_SERVER_NAME: str = "meho-backplane"


#: Single source of truth for whether the server advertises
#: ``capabilities.resources.subscribe`` and emits
#: ``notifications/resources/updated`` on resource state changes.
#:
#: v0.2 ships ``False`` -- the server has no per-session subscriber
#: state and the long-poll / SSE bridge that would carry the
#: notifications is deferred to v0.2.next. Any write path that wants
#: to publish a ``notifications/resources/updated`` event (G7.1-T4
#: convention edits, future kb / memory invalidation) MUST gate the
#: emit on this constant -- emitting unconditionally would tell a
#: spec-conforming client "you can subscribe", which our
#: ``capabilities.resources.subscribe: False`` simultaneously denies.
#:
#: The constant is read by :func:`_initialize` (to declare the
#: capability) AND by every emit-side caller (to gate the notification).
#: Flipping it to ``True`` in v0.2.next lands the subscribe-channel
#: wiring in one place; the conditional-emit code paths already exist
#: and become live. See
#: ``docs/codebase/tenant_conventions.md`` for the conditional-emit
#: contract specifically as it lands in G7.1-T4 (#316).
RESOURCES_SUBSCRIBE_ENABLED: Final[bool] = False


#: Module-level structlog logger. Defined up here (rather than next to
#: the router in the dispatch section below) because
#: :func:`_initialize` references it for the over-budget warning the
#: preamble assembler emits on dropped slugs.
_log = structlog.get_logger()


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

    The optional ``data`` attribute lands on the JSON-RPC ``error.data``
    member (spec §5.1: "A Primitive or Structured value that contains
    additional information about the error"). Tool handlers that have
    structured diagnostic detail — e.g. the connector-ingest MCP path
    surfacing expected-vs-received versions for
    :class:`~meho_backplane.operations.ingest.VersionMismatchError`
    via :func:`~meho_backplane.operations.ingest.build_version_mismatch_detail`
    (G0.9.1-T5 #777) — pass it as ``data=`` so the operator-facing
    agent gets a self-correcting envelope rather than just a string.

    Re-exported from :mod:`meho_backplane.mcp` so T3 (#248) tool
    handlers — and any future MCP method handler — can raise it
    without reaching into a dunder-private symbol.
    """

    def __init__(
        self,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.data = data


# ---------------------------------------------------------------------------
# Built-in lifecycle handlers
# ---------------------------------------------------------------------------


def _log_protocol_version_mismatch(
    client_request: InitializeRequest,
    operator: Operator,
) -> None:
    """Emit the G0.14-T13 mismatch breadcrumb when client/server revisions differ.

    Extracted out of :func:`_initialize` to keep the handler under the
    code-quality function-size budget and to give the observability
    semantics a single, grep-friendly home. The log shape mirrors
    ``mcp_unsupported_protocol_version`` (the existing WARNING that
    :func:`_validate_protocol_version_header` emits when a non-
    ``initialize`` request carries a stale ``MCP-Protocol-Version``
    header) — operators get a uniform event-name family for both
    handshake-time and post-handshake mismatches.

    The response body is unchanged: this helper is observability-only
    (G0.14-T13 #1202). Multi-version negotiation (refusing, down-
    negotiating, version-conditional capability advertisement) is
    deliberately deferred until concrete operator demand evidence
    accumulates from the events this WARNING surfaces.

    Same failure-mode genus as the v0.6.0 ``add_to_memory`` ``content``
    → ``body`` silent rename — operators and pinned clients shouldn't
    have to read CHANGELOGs to discover when their assumed contract no
    longer applies.
    """
    if client_request.protocolVersion == PROTOCOL_VERSION:
        return
    _log.warning(
        "mcp_initialize_protocol_version_mismatch",
        client_protocol_version=client_request.protocolVersion,
        server_protocol_version=PROTOCOL_VERSION,
        operator_sub=operator.sub,
    )


async def _initialize(
    operator: Operator,
    params: dict[str, Any] | None,
) -> InitializeResponse:
    """Handle the ``initialize`` method per MCP 2025-06-18 §Initialization.

    Returns a server-info + capabilities envelope, plus -- when the
    operator's tenant has any ``kind='operational'`` conventions -- the
    assembled session preamble in the spec-optional ``instructions``
    field (G7.1-T4 #316). The preamble is built by
    :func:`meho_backplane.conventions.preamble.assemble_preamble`:
    deterministic highest-``priority``-first packing wrapped in a
    delimited lower-trust block; when the packer drops entries to fit
    the token budget, the dropped slugs are logged at WARNING so the
    omission is loud (silent truncation of an operational rule is a
    safety bug per the issue body).

    MEHO supports only :data:`PROTOCOL_VERSION` and always responds
    with that — spec-compliant on the response side, but
    indistinguishable from a silent upgrade for a client pinned to an
    older revision. G0.14-T13 (#1202) closes the observability gap
    via :func:`_log_protocol_version_mismatch`: a mismatched client
    revision triggers a structured ``mcp_initialize_protocol_version_mismatch``
    WARNING, while the response body stays unchanged. Multi-version
    negotiation behaviour is tracked as explicit follow-up work,
    gated on concrete operator demand.
    """
    # ``params or {}`` deliberately collapses ``None`` and ``{}``. Spec-
    # aligned: a missing ``params`` field on the JSON-RPC request and an
    # explicit empty params object are both equivalent to "no required
    # fields supplied" for our purposes — :class:`InitializeRequest`'s
    # validator will then surface a clean INVALID_PARAMS for the
    # required-but-missing ``protocolVersion``.
    try:
        client_request = InitializeRequest.model_validate(params or {})
    except ValidationError as exc:
        raise McpInvalidParamsError(
            f"initialize: {exc.error_count()} validation error(s)",
        ) from exc

    _log_protocol_version_mismatch(client_request, operator)

    # G7.1-T4 (#316): assemble the operator's tenant session preamble
    # from ``kind='operational'`` conventions and ship it as
    # ``instructions`` per MCP 2025-06-18 §Initialization. An empty
    # tenant returns ``("", [])``; the empty-string text falls through
    # to ``None`` below so the wire serializer drops the field rather
    # than emitting a literal empty string (which would still count as
    # a non-null ``instructions`` value to a spec-conforming client).
    # Imported inside the function to break the import cycle (mcp.server
    # → conventions.preamble → db → ... → mcp.server). The cost of one
    # function-local import per ``initialize`` call is negligible (the
    # module is already loaded by the time any handshake arrives).
    from meho_backplane.conventions.preamble import assemble_preamble

    # G12.4-T2 (#1316): pass the operator's ``sub`` so the assembler
    # can append per-run priming text for any ``in_progress`` runs
    # assigned to this operator. An operator with no in-progress runs
    # sees a byte-identical preamble to the pre-T2 shape (the priming
    # helper returns ``text=""`` and the assembler omits the section).
    preamble = await assemble_preamble(operator.tenant_id, operator.sub)
    if preamble.dropped_slugs:
        # Loud, not silent -- the dropped-slug list is part of the
        # contract per the issue body's acceptance criterion. WARNING
        # rather than ERROR because the preamble still degrades
        # gracefully (the *highest*-priority entries are the ones
        # kept; the operator-facing surface that flags the overflow
        # is the CLI's ``meho conventions list`` non-zero exit, T3).
        _log.warning(
            "mcp_preamble_over_budget",
            tenant_id=str(operator.tenant_id),
            dropped_slugs=preamble.dropped_slugs,
        )

    # T3 (#248) registers tools/list, tools/call, resources/list,
    # resources/templates/list, resources/read — so the capabilities
    # envelope safely advertises both surfaces. ``listChanged: false``
    # because v0.2 doesn't emit notifications/tools/list_changed
    # (registry is populated at startup and never mutates at runtime).
    # ``subscribe`` is read from :data:`RESOURCES_SUBSCRIBE_ENABLED` so
    # the capability declaration and the conditional-emit gate on the
    # write paths (G7.1-T4 conventions edits) share one source of truth.
    return InitializeResponse(
        capabilities=ServerCapabilities(
            tools={"listChanged": False},
            resources={
                "listChanged": False,
                "subscribe": RESOURCES_SUBSCRIBE_ENABLED,
            },
        ),
        serverInfo={"name": _SERVER_NAME, "version": __version__},
        instructions=preamble.text or None,
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
    data: dict[str, Any] | None = None,
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

    Optional ``data`` lands on the JSON-RPC ``error.data`` member per
    spec §5.1 ("A Primitive or Structured value that contains
    additional information about the error"). Used by handlers that
    have structured diagnostic detail to surface (G0.9.1-T5 #777, the
    connector-ingest typed envelopes).
    """
    body = JsonRpcResponse(
        id=request_id,
        error=JsonRpcError(code=code, message=message, data=data),
    )
    return JSONResponse(content=_serialize_response(body), status_code=status_code)


# ---------------------------------------------------------------------------
# Router + dispatch
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/mcp", tags=["mcp"])


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


def mcp_session_id_capture_mode() -> str:
    """Report whether MCP session-id capture is ``"always"`` or ``"enforced"``.

    Capture is **unconditional**: whenever a request carries a
    parseable ``Mcp-Session-Id`` header the server binds it to the
    structlog contextvar regardless of any env var, so the
    ``audit_log.agent_session_id`` column populates automatically for
    every client that sends one (G8.2 audit replay then has rows to
    walk in the default deploy). The
    :attr:`~meho_backplane.settings.Settings.mcp_require_session_id`
    knob (``MCP_REQUIRE_SESSION_ID`` env) is **strictly about
    enforcement** — whether a missing header is a 400 reject. It does
    not gate capture.

    Crucially, the capture chain is only useful **when clients actually
    send the header**. Per the MCP 2025-06-18 Streamable HTTP transport
    §"Session Management" the client only emits ``Mcp-Session-Id`` on
    subsequent requests when the server assigned one in an
    ``Mcp-Session-Id`` **response header** on the ``InitializeResult``
    (spec rule 2: *"If an ``Mcp-Session-Id`` is returned by the server
    during initialization, clients … **MUST** include it"*). G0.15-T4
    (#1213) closes the regression where MEHO captured the header end
    of the chain but never issued one — leaving every Claude Code MCP
    audit row's ``agent_session_id`` as NULL despite this helper
    reporting ``"always"``. See :func:`_issue_mcp_session_id` for the
    issuance side.

    Operators can introspect the current mode via
    :func:`~meho_backplane.api.v1.health.authenticated_health` (Task
    G0.14-T6 #1147) so the deploy-time observability story for the
    audit-replay feature gate (G8.2) is a single GET away. Task
    G0.14-T7 #1148's ``/ready`` features block reads this helper too
    so both surfaces stay consistent.

    Returns ``"enforced"`` when ``MCP_REQUIRE_SESSION_ID=true`` (every
    MCP call must carry a header or the server rejects it before
    dispatch); ``"always"`` otherwise (header is captured when sent,
    otherwise ``agent_session_id`` lands as NULL — which is fine: the
    G8.2 replay route filters NULLs out of session walks naturally).
    """
    return "enforced" if get_settings().mcp_require_session_id else "always"


#: Lowercase HTTP header name for the MCP session id, used on both the
#: inbound request (read by :func:`_bind_mcp_session_id`) and the
#: outbound ``initialize`` response (set by :func:`_issue_mcp_session_id`).
#: Defining it once keeps the wire spelling consistent and grep-able.
_MCP_SESSION_HEADER: Final[str] = "mcp-session-id"


def _issue_mcp_session_id(response: Response) -> uuid.UUID:
    """Stamp a fresh ``Mcp-Session-Id`` response header on an ``initialize`` reply.

    Per MCP 2025-06-18 Streamable HTTP §"Session Management" rule 1, a
    server **MAY** assign a session id at initialization by returning
    it in an ``Mcp-Session-Id`` response header on the
    ``InitializeResult``; rule 2 then requires the client to echo that
    id on every subsequent HTTP POST to the MCP endpoint. The handshake
    is therefore strictly **server-driven** — clients do not invent
    session ids, they only relay one the server gave them.

    G0.15-T4 (#1213) closed the regression where MEHO's chain captured
    the inbound header into the structlog contextvar (and from there
    into ``audit_log.agent_session_id``) but never **issued** one to
    begin with. The visible symptom on `claude-rdc-hetzner-dc#753`
    finding 2 was eight Claude Code MCP rows with
    ``agent_session_id: null`` despite ``meho_status`` /
    ``/ready.features.audit_replay`` advertising ``capture_mode:
    "always"`` — both surfaces correctly reported the **capture**
    config; nothing populated the column because no client had a
    server-assigned session id to send back.

    The issued value is a fresh :func:`uuid.uuid4` rendered as the
    canonical UUID string (the same shape :func:`_bind_mcp_session_id`
    parses on subsequent requests, so the round-trip lands cleanly on
    ``audit_log.agent_session_id``). MEHO holds **no stateful session
    store** in v0.2 — the id exists purely for audit correlation, so
    no registry-side allocation is needed: the server assigns + emits +
    forgets, and the audit-log linkage is the only persistence. The
    spec's optional terminate-via-DELETE flow (rule 5) and the
    404-on-stale-id rejection (rule 3) are therefore both out of
    scope; MEHO accepts any well-formed UUID the client returns,
    which is also the lenient posture the v0.2 capture path already
    documents.

    Returns the issued :class:`uuid.UUID` so the caller can mirror it
    into the structlog contextvar — useful for structlog log lines
    emitted from inside the ``initialize`` handler (the handler itself
    writes no audit row, but its log entries get the same
    correlation key the subsequent ``tools/call`` audit rows will
    carry).
    """
    issued = uuid.uuid4()
    response.headers[_MCP_SESSION_HEADER] = str(issued)
    return issued


def _response_is_jsonrpc_error(response: Response) -> bool:
    """Return ``True`` when *response* carries a JSON-RPC ``error`` envelope.

    JSON-RPC-level errors (parse / invalid-request / method-not-found /
    invalid-params / internal) ride on HTTP 200 by design — the failure
    is encoded inside the body's ``error`` member, not on the HTTP
    status. :func:`mcp_dispatch`'s post-dispatch session-id-issuance
    gate (G0.15-T4 #1213) therefore can't filter on
    ``response.status_code`` alone: a JSON-RPC failure of ``initialize``
    (e.g. an invalid ``protocolVersion`` body) returns HTTP 200 with
    ``error.code``, and the spec wants no session pinned to that
    degenerate exchange.

    The implementation peeks at the rendered body bytes. ``JSONResponse``
    fixes ``response.body`` at construction time (Starlette renders the
    content into bytes in ``JSONResponse.__init__``), so this read is
    safe pre-stream. A non-``JSONResponse`` shape (the spec-driven HTTP
    202 notifications path returns a bare :class:`Response`) carries
    no JSON-RPC envelope and is treated as "not an error" — the caller
    further gates on ``is_notification`` anyway.

    Defensive: any decode failure (truncated body, unexpected encoding)
    is treated as "looks like an error" so the issuance side stays
    fail-closed — a malformed body is not a valid initialize result and
    must not seed a session id either.
    """
    body = getattr(response, "body", None)
    if not body:
        return False
    try:
        envelope = json.loads(body)
    except (ValueError, TypeError):
        return True
    return isinstance(envelope, dict) and "error" in envelope


def _bind_mcp_session_id(
    request: Request,
    payload: dict[str, Any],
) -> JSONResponse | None:
    """Capture the ``Mcp-Session-Id`` header into a structlog contextvar.

    Per the MCP 2025-06-18 Streamable HTTP transport §"Session
    Management", a server MAY assign a session id at ``initialize`` time
    that the client echoes in the ``Mcp-Session-Id`` header on every
    later request. MEHO runs no stateful session store in v0.2 — it
    only needs the id for **audit correlation** so per-session replay
    (``meho audit replay <session-id>``, G8.2) can reconstruct one
    agent's full operation trace. The header is bound as a structlog
    contextvar (stored as the canonical UUID string, mirroring how
    :func:`~meho_backplane.targets.resolver.resolve_target` binds
    ``target_id``); :func:`~meho_backplane.mcp.audit.write_mcp_audit_row`
    reads it back and writes ``audit_log.agent_session_id``. The
    contextvar propagates down the request's async call chain, so the
    write picks it up without threading the value through every handler
    signature.

    **Capture is independent of enforcement (G0.14-T6 #1147).** Capture
    fires whenever the client sent a parseable UUID header, regardless
    of :attr:`~meho_backplane.settings.Settings.mcp_require_session_id`.
    Enforcement — the missing-header reject — is the only behaviour
    that env var gates. This split lets G8.2 audit-replay light up
    automatically on any deploy whose MCP clients include the header
    (Claude Code does, by default) without operators having to flip a
    second env var.

    Resolution rules:

    * Present and a parseable UUID → bind that id.
    * Present but not a UUID → don't bind. A non-UUID id can't go in
      the ``uuid`` column, and a malformed *client* header must not
      500 the call (the client is in the wrong, not the server). The
      audit row's ``agent_session_id`` lands as NULL — same as a row
      from a client that never sent the header. A warning is logged so
      the malformation is observable in structlog.
    * Absent / empty → don't bind. The row's ``agent_session_id``
      lands as NULL. The G8.2 replay route's session walk treats NULLs
      as "not part of any session", which is correct for a call from a
      stateless client.

    When :attr:`~meho_backplane.settings.Settings.mcp_require_session_id`
    is ``True`` (``MCP_REQUIRE_SESSION_ID`` env), a missing/empty header
    short-circuits to a JSON-RPC ``-32600`` Invalid Request **before**
    dispatch, mirroring the early-return shape of
    :func:`_validate_protocol_version_header`. A present-but-malformed
    header is *not* a rejection in require-mode: the client did send a
    session id (just a malformed one), so the require-a-session
    contract is satisfied at the transport layer; the audit row gets a
    NULL ``agent_session_id`` the same way it does in the default
    capture-only mode, and the structured warning lets the operator
    see which client is misbehaving.

    Returns ``None`` on the OK path (contextvar bound or deliberately
    unbound); a :class:`JSONResponse` (HTTP 200 + JSON-RPC ``-32600``
    envelope) on the require-mode rejection so the caller can
    early-return it before any audit row is written.
    """
    session_header = request.headers.get("mcp-session-id")
    has_header = session_header is not None and session_header != ""

    if not has_header and get_settings().mcp_require_session_id:
        _log.warning("mcp_session_id_required_but_missing")
        return _error_response(
            _coerce_request_id(payload),
            INVALID_REQUEST,
            "invalid request: Mcp-Session-Id header is required",
        )

    if not has_header:
        return None

    try:
        session_id = uuid.UUID(session_header)
    except ValueError:
        _log.warning("mcp_malformed_session_id", header=session_header)
        return None

    structlog.contextvars.bind_contextvars(mcp_session_id=str(session_id))
    return None


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
        return _error_response(jrpc.id, INVALID_PARAMS, str(exc), data=exc.data)
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

    # Capture the Mcp-Session-Id header (G8.2-T2 #1010 + G0.14-T6 #1147
    # decouple) so the audit writer can correlate every row of this
    # call to one agent session. Capture-if-present is unconditional;
    # MCP_REQUIRE_SESSION_ID only gates the missing-header reject
    # (-32600 before any dispatch). Bound for both requests and
    # notifications — notifications still write audit rows downstream.
    session_error = _bind_mcp_session_id(request, payload)
    if session_error is not None:
        return session_error

    # Notification detection: JSON-RPC §4.1.2 says a notification is a
    # request without an ``id`` member. Pydantic-side, ``jrpc.id``
    # collapses absent and explicit-null to ``None`` (spec discourages
    # the latter); the raw dict tells us which form arrived. The
    # method-prefix relaxation handles buggy clients that send a
    # ``notifications/*`` method with an id — the method semantics
    # are spec-defined as notification-only, so the server treats it
    # as such regardless of the envelope's id field.
    is_notification = "id" not in payload or jrpc.method.startswith("notifications/")
    response = await _dispatch_to_handler(jrpc, is_notification, operator)
    _maybe_issue_initialize_session_id(request, jrpc.method, is_notification, response)
    return response


def _maybe_issue_initialize_session_id(
    request: Request,
    method: str,
    is_notification: bool,
    response: Response,
) -> None:
    """Stamp an ``Mcp-Session-Id`` response header on a successful initialize.

    G0.15-T4 (#1213): closes the regression on the v0.7.0 release-body's
    G0.14-T6 #1147 promise — the capture chain (header → contextvar →
    ``audit_log.agent_session_id``) already worked; what was missing is
    the **issuance** half, since MCP 2025-06-18 Streamable HTTP
    §"Session Management" rule 2 says clients only emit the header when
    the server first sent one. Gates:

    * Method is ``initialize`` and it's a *request*, not a notification
      — the spec scopes session assignment to the handshake exchange.
    * Response is HTTP 2xx **and** the JSON-RPC envelope has no
      ``error`` member — a failed initialize (transport-level reject or
      JSON-RPC ``error``) must not leak a session id, since the spec
      wants the id pinned to a real session, not a degenerate one.
    * The client did not already send an ``Mcp-Session-Id`` header
      inbound — a resume / replay attempt where the client carries an
      id is accepted lenient (MEHO holds no session-state to validate
      against in v0.2), and we do not overwrite the client's
      correlation key on the response.

    On the success path the issued id is also bound into structlog
    contextvars so any post-issue log line in the same async task
    (e.g. the ``request_completed`` log from
    :class:`~meho_backplane.middleware.RequestContextMiddleware`)
    carries the same correlation key the client will echo on the next
    request. The initialize call itself writes no audit row (chassis
    ``AuditMiddleware`` skips ``/mcp`` and the MCP-side audit path only
    fires for ``tools/call`` / ``resources/read``), so this binding is
    purely log-side. See :func:`_issue_mcp_session_id` for the
    issuance contract.
    """
    if method != "initialize" or is_notification:
        return
    if not (200 <= response.status_code < 300):
        return
    if _response_is_jsonrpc_error(response):
        return
    if request.headers.get(_MCP_SESSION_HEADER) not in (None, ""):
        return
    issued = _issue_mcp_session_id(response)
    structlog.contextvars.bind_contextvars(mcp_session_id=str(issued))
