# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON-RPC 2.0 + MCP 2025-06-18 wire-shape models.

Pydantic v2 envelopes for the MCP server entrypoint (G0.5-T1). T1 covers
only the lifecycle surface: ``initialize`` / ``ping`` /
``notifications/initialized``. The tool + resource registries (T3, #248)
and the OAuth-RS metadata document (T2, #247) layer their own request /
response models on top of these JSON-RPC envelopes.

The wire forms here follow two specs that the MCP 2025-06-18 revision
chains together:

* JSON-RPC 2.0 — https://www.jsonrpc.org/specification — the envelope,
  the request / response / error shapes, and the notification rule
  (request without an ``id`` member; server MUST NOT reply).
* MCP 2025-06-18 lifecycle —
  https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle
  — the ``initialize`` request / result and ``serverInfo`` /
  ``capabilities`` payloads.

Field names that the wire defines as camelCase (``protocolVersion``,
``clientInfo``, ``serverInfo``) keep that shape on the Python side too;
the ``# noqa: N815`` markers exist because pep8-naming would otherwise
push them to snake_case, breaking interop with every MCP client in the
wild.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "PROTOCOL_VERSION",
    "InitializeRequest",
    "InitializeResponse",
    "JsonRpcError",
    "JsonRpcId",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "ServerCapabilities",
]


#: The MCP spec revision this server implements. Returned verbatim in
#: ``InitializeResponse.protocolVersion`` when the client requests this
#: version; otherwise the server responds with the same value anyway and
#: the client decides whether to disconnect (spec §Version Negotiation).
PROTOCOL_VERSION: str = "2025-06-18"


#: A JSON-RPC 2.0 id is "a String, Number, or NULL value if included"
#: (spec §4.1). A request *without* an id is a notification (§4.1.2);
#: the absent / explicit-null distinction is normatively discouraged on
#: the request side, so this server treats both as the same shape.
JsonRpcId = int | str | None


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 envelopes
# ---------------------------------------------------------------------------


class JsonRpcRequest(BaseModel):
    """Inbound JSON-RPC 2.0 request envelope.

    Notifications carry no ``id`` (spec §4.1.2). Because Pydantic v2 cannot
    distinguish "field absent" from "field present and ``null``" once a
    default is supplied, the dispatcher consults the raw ``payload`` dict
    for the ``"id"`` key when deciding whether to emit a response — see
    :func:`~meho_backplane.mcp.server.mcp_dispatch`.

    ``extra="allow"`` lets the model accept MCP-spec extensions that
    revision 2025-06-18 doesn't itself surface (the spec is intentionally
    forward-extensible at the envelope layer); the dispatcher only reads
    the four canonical fields.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    jsonrpc: Literal["2.0"]
    id: JsonRpcId = None
    method: str
    params: dict[str, Any] | None = None


class JsonRpcError(BaseModel):
    """JSON-RPC 2.0 error object (the ``response.error`` payload).

    ``data`` is optional per spec §5.1 and is omitted on the wire when
    unset; callers that need to attach structured context (e.g. an
    "unsupported protocol version" body with ``supported`` + ``requested``
    keys) pass it through here.
    """

    model_config = ConfigDict(frozen=True)

    code: int
    message: str
    data: dict[str, Any] | None = None


class JsonRpcResponse(BaseModel):
    """Outbound JSON-RPC 2.0 response envelope.

    Spec §5: "Either the result member or error member MUST be included,
    but both members MUST NOT be included." The
    :meth:`_exactly_one_of_result_or_error` validator enforces this
    invariant on construction so a misbehaving handler can't emit a
    wire-broken response that some clients accept and others reject.

    Serialization to the wire goes through
    :func:`~meho_backplane.mcp.server._serialize_response`, which drops
    the unset half of ``result`` / ``error`` (Pydantic's ``exclude_none``
    would also strip ``id`` when it's the spec-mandated ``null`` on a
    parse-error response — see spec §5).
    """

    model_config = ConfigDict(frozen=True)

    jsonrpc: Literal["2.0"] = "2.0"
    id: JsonRpcId
    result: dict[str, Any] | None = None
    error: JsonRpcError | None = None

    @model_validator(mode="after")
    def _exactly_one_of_result_or_error(self) -> JsonRpcResponse:
        if (self.result is None) == (self.error is None):
            raise ValueError(
                "JsonRpcResponse must have exactly one of `result` or `error`",
            )
        return self


# ---------------------------------------------------------------------------
# MCP 2025-06-18 lifecycle payloads
# ---------------------------------------------------------------------------


class InitializeRequest(BaseModel):
    """``initialize`` request params per MCP 2025-06-18 §Initialization.

    The spec example body carries ``protocolVersion``, ``capabilities``,
    and ``clientInfo`` as required keys; defensively, ``capabilities``
    and ``clientInfo`` are tolerated as missing (default to empty dict)
    because some client SDKs omit empty objects. Missing
    ``protocolVersion`` is the only required-field shape that hits
    the ``INVALID_PARAMS`` path.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    protocolVersion: str  # noqa: N815 — wire field is camelCase per MCP spec
    capabilities: dict[str, Any] = Field(default_factory=dict)
    clientInfo: dict[str, Any] = Field(default_factory=dict)  # noqa: N815


class ServerCapabilities(BaseModel):
    """Server-side capability declaration returned on ``initialize``.

    T1 advertises an **empty** capabilities envelope — ``tools`` /
    ``resources`` / ``logging`` all stay :data:`None`, which the
    hand-rolled wire serializer drops. Advertising a capability ahead
    of the dispatch table being able to honor it would tell a
    spec-conforming client it can call ``tools/list`` / ``resources/list``
    immediately after the handshake, which would then ``-32601`` and
    likely cause the client to disconnect per spec §Capability
    Negotiation ("Only use capabilities that were successfully
    negotiated"). T3 (#248) is where ``tools`` and ``resources`` flip
    back on, paired with the dispatch-table additions. ``prompts`` /
    ``roots`` / ``sampling`` / ``completions`` are out of scope for
    v0.2 and stay absent.
    """

    model_config = ConfigDict(frozen=True)

    tools: dict[str, Any] | None = None
    resources: dict[str, Any] | None = None
    logging: dict[str, Any] | None = None


class InitializeResponse(BaseModel):
    """``initialize`` result per MCP 2025-06-18 §Initialization.

    ``instructions`` stays ``None`` in T1; G7.1 will populate it with
    the assembled tenant-aware session preamble once tenancy lands.
    Until then the field is serialized as omitted (the wire view drops
    ``None``-valued ``instructions``).
    """

    model_config = ConfigDict(frozen=True)

    protocolVersion: str = PROTOCOL_VERSION  # noqa: N815
    capabilities: ServerCapabilities
    serverInfo: dict[str, str]  # noqa: N815
    instructions: str | None = None


# ---------------------------------------------------------------------------
# Standard JSON-RPC error codes (spec §5.1)
# ---------------------------------------------------------------------------

#: "Invalid JSON was received by the server." (spec §5.1)
PARSE_ERROR: int = -32700

#: "The JSON sent is not a valid Request object." (spec §5.1)
INVALID_REQUEST: int = -32600

#: "The method does not exist / is not available." (spec §5.1)
METHOD_NOT_FOUND: int = -32601

#: "Invalid method parameter(s)." (spec §5.1)
INVALID_PARAMS: int = -32602

#: "Internal JSON-RPC error." (spec §5.1)
INTERNAL_ERROR: int = -32603
