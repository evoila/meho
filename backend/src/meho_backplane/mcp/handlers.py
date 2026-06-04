# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON-RPC method handlers for the registry-backed MCP surface (G0.5-T3).

This module wires the registry primitives in
:mod:`meho_backplane.mcp.registry` to five JSON-RPC methods on the
``/mcp`` route, registering each via
:func:`~meho_backplane.mcp.server.register_method` at import time:

* ``tools/list`` — :func:`handle_tools_list`. Returns the RBAC-filtered
  tool list per MCP 2025-06-18 §Listing Tools.
* ``tools/call`` — :func:`handle_tools_call`. Validates arguments
  against the tool's ``inputSchema`` (jsonschema), dispatches to the
  registered handler, packs the result into the MCP ``content`` array.
* ``resources/list`` — :func:`handle_resources_list`. Returns the
  list of concrete (non-templated) resources. v0.2 ships only templated
  resources, so this always returns an empty list — present for spec
  conformance.
* ``resources/templates/list`` —
  :func:`handle_resources_templates_list`. Returns the RBAC-filtered
  resource-template list per MCP 2025-06-18 §Resource Templates. This
  is where every v0.2 resource surfaces.
* ``resources/read`` — :func:`handle_resources_read`. Matches the
  requested URI against the registered templates, dispatches, packs
  the result into the MCP ``contents`` array. An unmatched URI is
  surfaced today as ``-32602`` "Invalid params" via
  :class:`McpInvalidParamsError`; the spec-correct ``-32002`` "Resource
  not found" mapping is recorded as a follow-up and discussed in
  :func:`handle_resources_read`'s own docstring.

Why ``resources/list`` and ``resources/templates/list`` are separate
====================================================================

The MCP 2025-06-18 spec defines two distinct list methods:

* ``resources/list`` returns ``Resource`` objects — items with a
  concrete ``uri`` field. Clients display them as a flat selectable
  list.
* ``resources/templates/list`` returns ``ResourceTemplate`` objects —
  items with a ``uriTemplate`` field carrying RFC 6570 ``{var}``
  placeholders. Clients render these as parameterised lookups, often
  with an auto-completion UI for the variables.

Issue #248's body conflated the two, asking for ``resources/list`` to
return the registered (templated) resources. Implementing per spec
instead is the right call: spec-conforming clients (MCP Inspector,
Claude Desktop) call ``resources/templates/list`` for templates and
``resources/list`` for concrete URIs; conflating them would break
client UIs that branch on the response shape. The cost is one extra
method handler; the benefit is full spec conformance.

RBAC + input validation
=======================

Tool inputs are validated against the tool's ``inputSchema`` via the
:mod:`jsonschema` library; a schema violation surfaces as JSON-RPC
``-32602`` "Invalid params" through
:class:`~meho_backplane.mcp.server.McpInvalidParamsError`. The RBAC
filter on ``tools/list`` and ``resources/templates/list`` is delegated
to :func:`~meho_backplane.mcp.registry.all_tools_for` /
:func:`~meho_backplane.mcp.registry.all_resource_templates_for`. The
RBAC enforcement on ``tools/call`` / ``resources/read`` is a *second*
check, gating the actual invocation — listing the tool does not by
itself authorise calling it. (A future polish would compute the
filter once and cache it per-request; v0.2 calls the filter twice and
that's fine.)
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import jsonschema
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast import (
    BroadcastEvent,
    compute_effective_broadcast_detail,
    publish_event,
    redact_payload,
)
from meho_backplane.mcp.audit import compute_params_hash, write_mcp_audit_row
from meho_backplane.mcp.registry import (
    ResourceTemplateDefinition,
    ToolDefinition,
    all_resource_templates_for,
    all_tools_for,
    get_resource_for_uri,
    get_tool,
    role_at_least,
)
from meho_backplane.mcp.server import McpInvalidParamsError, register_method

__all__ = [
    "handle_resources_list",
    "handle_resources_read",
    "handle_resources_templates_list",
    "handle_tools_call",
    "handle_tools_list",
]

_log = structlog.get_logger(__name__)


#: MCP-defined error code for "Resource not found" per spec
#: §Resources/Error Handling. Distinct from the generic JSON-RPC
#: ``METHOD_NOT_FOUND`` because ``resources/read`` is the method that
#: succeeded — it's the *resource URI* that didn't resolve. Clients use
#: this code to distinguish "I tried to read a resource that doesn't
#: exist" from "I called a method the server doesn't support".
_RESOURCE_NOT_FOUND: int = -32002


def _read_mcp_broadcast_detail(raw_params: dict[str, Any]) -> Literal["full"] | None:
    """Pull ``_meta.broadcast_detail`` out of an MCP method's ``params``.

    G6.3-T3 (#380): MCP spec §Common/Utilities/_meta blesses ``_meta``
    as the per-call metadata envelope. An operator opts into full
    detail on a sensitive-class tool call (e.g. ``vault.kv.read``) by
    sending::

        {
          "method": "tools/call",
          "params": {
            "name": "vault.kv.read",
            "arguments": {"path": "secret/foo"},
            "_meta": {"broadcast_detail": "full"}
          }
        }

    Opt-in only -- per Initiative #376 DoD, only ``"full"`` is honored.
    Any other value (including ``"aggregate"``, which would be a
    "weaken via channel" request) is logged at ``info`` level under
    ``mcp_broadcast_detail_invalid_meta`` and dropped silently -- the
    request still succeeds and the broadcast uses the default detail.

    Defensive accessors: a malformed ``_meta`` (not a dict, missing
    entirely, contains the key with a wrong-type value) returns
    ``None`` without raising. The fail-open contract is the same as
    :func:`~meho_backplane.broadcast.publisher.publish_event` -- the
    broadcast layer never converts a benign client mistake into an
    operation failure.

    Returns ``"full"`` when the operator opted in to full detail,
    ``None`` otherwise. MCP path passes this value directly to
    :func:`compute_effective_broadcast_detail` rather than threading
    it through a contextvar -- handlers already pass :class:`Operator`
    explicitly, so a parameter is more idiomatic than the structlog
    contextvar shim the HTTP path uses.
    """
    meta = raw_params.get("_meta")
    if not isinstance(meta, dict):
        return None
    raw = meta.get("broadcast_detail")
    if raw == "full":
        return "full"
    if raw is not None:
        _log.info("mcp_broadcast_detail_invalid_meta", value=raw)
    return None


# ---------------------------------------------------------------------------
# tools/list + tools/call
# ---------------------------------------------------------------------------


async def handle_tools_list(
    operator: Operator,
    _params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return tools the operator's role admits, in MCP wire shape.

    No pagination in v0.2 — the registry is small (single-digit tools
    through G0.5-T4, growing into tens through G3+). The MCP spec allows
    a server to return all tools in one response and omit the
    ``nextCursor`` field, which is what this handler does.
    """
    visible = [defn.to_wire() for defn in all_tools_for(operator)]
    return {"tools": visible}


# code-quality-allow: pre-existing oversized MCP envelope dispatcher (>100
# lines / C901 / PLR0915 on main before #1481); the #1481 audit-status fix
# adds a small except arm, not the size — refactor is out of scope here.
async def handle_tools_call(
    operator: Operator,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Dispatch a ``tools/call`` request to the registered handler.

    Three failure modes:

    * **Unknown tool** → :class:`McpInvalidParamsError` mapping to
      ``-32602``. The MCP spec example for "Unknown tool" uses
      ``-32602`` (see §Tools/Error Handling), distinct from
      ``-32601`` ("Method not found" — used when the JSON-RPC method
      itself doesn't exist). The dispatcher's
      :data:`~meho_backplane.mcp.schemas.METHOD_NOT_FOUND` is correct
      for the latter; this handler raises the former.
    * **Insufficient role** → :class:`McpInvalidParamsError` with a
      ``forbidden`` detail. The RBAC filter on ``tools/list``
      already hides the tool from this operator's listing; reaching
      this branch means the client tried to call a tool it didn't
      see, which is a client bug or an attack. Surface as invalid
      params rather than a 403 — JSON-RPC doesn't have an HTTP-403
      analogue and the spec's audience-binding rule already 401s at
      the transport.
    * **Schema-invalid arguments** → :class:`McpInvalidParamsError`
      via :mod:`jsonschema`. The tool's ``inputSchema`` is the
      contract; a violation is invalid params.

    Handler return value is packed into the MCP ``content`` array as
    a single ``text`` block containing the JSON-serialised dict, per
    spec §Tools/Tool Result. Structured content (``structuredContent``
    field) is a future polish.

    Audit row writing
    -----------------

    Per G0.5-T5 (#250), every ``tools/call`` invocation produces exactly
    one :class:`~meho_backplane.db.models.AuditLog` row via
    :func:`~meho_backplane.mcp.audit.write_mcp_audit_row`. The write
    runs inside the ``finally`` block so the failure paths (unknown
    tool, forbidden, schema-invalid arguments, handler exception) all
    produce an audit row attributing the *attempted* operation —
    matching the chassis :class:`~meho_backplane.audit.AuditMiddleware`
    semantics for HTTP routes. ``status_code`` is the audit-side
    projection of the JSON-RPC outcome (200 / 400 / 403 / 404 / 500)
    so dashboards that group HTTP and MCP traffic on ``status_code``
    see one consistent axis.

    Fail-closed: if the audit write itself raises, the in-flight return
    value (or in-flight exception) is replaced by the audit exception,
    which the dispatcher maps to JSON-RPC ``-32603`` Internal Error.
    The MCP client therefore sees the operation as failed; the audit
    row's absence is the operator's signal to investigate the audit
    layer specifically.
    """
    raw_params = params or {}
    name = raw_params.get("name")
    arguments = raw_params.get("arguments", {})
    request_override = _read_mcp_broadcast_detail(raw_params)
    start = time.monotonic()
    audit_payload: dict[str, Any] = {
        "op_id": name if isinstance(name, str) else "",
        "params_hash": "",
        "op_class": "unknown",
    }
    status_code = 500
    audit_name = name if isinstance(name, str) and name else "<empty>"

    try:
        if not isinstance(name, str) or not name:
            status_code = 400
            raise McpInvalidParamsError("tools/call: missing or empty 'name'")
        if not isinstance(arguments, dict):
            status_code = 400
            raise McpInvalidParamsError("tools/call: 'arguments' must be an object")

        audit_payload["params_hash"] = compute_params_hash(arguments)

        entry = get_tool(name)
        if entry is None:
            status_code = 404
            raise McpInvalidParamsError(f"unknown tool: {name!r}")
        defn, handler = entry
        audit_payload["op_class"] = defn.op_class

        # RBAC: the tool's required_role gates *invocation*, not just listing.
        # The list filter already hides tools the operator can't call, but
        # a client that knows the name could try to call anyway.
        if not _operator_meets_required_role(operator, defn):
            _log.warning(
                "mcp_tool_call_forbidden",
                tool=name,
                required=defn.required_role,
                actual=operator.tenant_role,
            )
            status_code = 403
            raise McpInvalidParamsError(
                f"forbidden: {name!r} requires a higher role",
            )

        # Validate arguments against the tool's inputSchema. ``cls`` is
        # pinned to :class:`jsonschema.Draft202012Validator` to make the
        # "JSON Schema 2020-12" contract called out in :class:`ToolDefinition`
        # load-bearing rather than incidental: jsonschema 4.26 happens to
        # pick the 2020-12 validator as the default when a schema lacks
        # ``$schema``, but the default is the library's "latest known"
        # pointer and would slide forward on a future major bump. Pinning
        # here decouples MEHO's schema dialect from the library's release
        # cadence. ``jsonschema.validate`` raises ``ValidationError`` on the
        # first failure; we surface it as INVALID_PARAMS.
        try:
            jsonschema.validate(
                instance=arguments,
                schema=defn.inputSchema,
                cls=jsonschema.Draft202012Validator,
            )
        except jsonschema.ValidationError as exc:
            status_code = 400
            raise McpInvalidParamsError(
                f"tools/call {name!r}: arguments failed inputSchema: {exc.message}",
            ) from exc

        result = await handler(operator, arguments)
        status_code = 200

        # MCP §Tool Result: every successful tools/call response carries a
        # ``content`` array. v0.2 ships unstructured content only — a single
        # text block with the JSON-serialised result. Structured content
        # (``structuredContent``) lands when a downstream tool needs it.
        return {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "isError": False,
        }
    except McpInvalidParamsError:
        # Class-wide audit-status correction (#1481). A tool handler can
        # raise ``McpInvalidParamsError`` *after* all the explicit
        # pre-dispatch gates (name/arguments/unknown-tool/RBAC/schema)
        # — e.g. ``_approve_handler`` rejecting a self-approval,
        # ``approval_request_not_found``, or ``approval_unauthorized``.
        # Those raises bypass every branch that set ``status_code``, so
        # it is still the init 500 — a server-fault projection of what is
        # actually a JSON-RPC ``-32602`` parameter/policy rejection on
        # the wire. Project the whole class onto a 403 "denied" status so
        # the audit row and the broadcast event
        # (:func:`_classify_mcp_status`) classify the rejection
        # consistently with the wire outcome instead of mis-reporting a
        # crash. Explicit pre-dispatch branches already set 400/403/404,
        # so they flow through here unchanged; only the residual init 500
        # is corrected.
        if status_code == 500:
            status_code = 403
        raise
    finally:
        duration_ms = (time.monotonic() - start) * 1000
        audit_id = uuid.uuid4()
        # G6.3-T2 (#379): resolve broadcast detail BEFORE the audit
        # row commits so ``broadcast_detail_origin`` lands on the
        # row's payload. The op_id is the tool name verbatim so
        # :func:`classify_op` matches credential / audit / read /
        # write suffixes correctly (e.g. ``vault.kv.read`` →
        # ``credential_read`` → aggregate-only redacted payload by
        # default).
        #
        # ``resolver_params`` merges the raw tool ``arguments`` on top
        # of ``audit_payload`` so scope-matched override rules
        # (``scope_field="namespace"`` keys into ``raw_params["namespace"]``,
        # ``scope_field="target_name"`` keys into ``raw_params["target"]``)
        # can match against the actual MCP call. The hashed
        # ``audit_payload`` (``params_hash``) stays the persistence
        # shape; the resolver-visible view also carries the unhashed
        # arguments. Arguments win on key collision -- if a tool
        # happens to define an ``op_class`` argument, the operator
        # value would take precedence, but that overlap is a tool-
        # naming bug T4's API layer can warn on later.
        resolver_params: dict[str, Any] = dict(audit_payload)
        if isinstance(arguments, dict):
            resolver_params.update(arguments)
        # #93: ``call_operation`` is a wrapper tool — its name does not
        # carry any sensitivity signal, so ``classify_op("call_operation")``
        # falls through to ``"other"`` → ``"full"`` detail and ships raw
        # ``params`` (including secret-bearing ``params.data`` /
        # ``params.password``) onto the per-tenant feed. The inner
        # ``arguments["op_id"]`` is the real operation the agent dispatched
        # and must be used for the redaction classification instead.
        # This mirrors the inner DISPATCH row's precedent at
        # ``operations/_audit.py:443`` where ``classify_op(descriptor.op_id)``
        # is called on the real op id so credential_write/credential_mint/
        # credential_read ops collapse to aggregate-only. The outer envelope's
        # ``op_id`` broadcast field and the audit row's ``path`` column are
        # intentionally left as ``audit_name`` (the wrapper tool name) so
        # ``meho audit query`` cardinality and path-based correlations are
        # unchanged (AC #5).
        _broadcast_op_id = audit_name
        if (
            audit_name == "call_operation"
            and isinstance(arguments, dict)
            and isinstance(arguments.get("op_id"), str)
            and arguments["op_id"]
        ):
            _broadcast_op_id = arguments["op_id"]
        (
            broadcast_op_class,
            broadcast_detail,
            broadcast_origin,
        ) = await compute_effective_broadcast_detail(
            op_id=_broadcast_op_id,
            tenant_id=operator.tenant_id,
            raw_params=resolver_params,
            request_override=request_override,
        )
        # Snapshot the broadcast-visible params BEFORE injecting the
        # audit-only ``broadcast_detail_origin`` /
        # ``broadcast_detail_effective`` keys. The audit row gets the
        # augmented ``audit_payload``; the broadcast event reads
        # ``broadcast_params`` so audit-internal metadata (origin,
        # ``tenant_rule:<uuid>``, effective-detail enum) never reaches
        # the broadcast feed.
        broadcast_params = dict(resolver_params)
        audit_payload["broadcast_detail_origin"] = broadcast_origin
        # G6.3-T3 (#380): effective detail joins origin as an audit-
        # only forensic field. Subscribers see ``detail`` through the
        # rendered ``redact_payload`` shape; the audit row needs the
        # raw enum for ``meho audit query`` filtering.
        audit_payload["broadcast_detail_effective"] = broadcast_detail
        # #704 strip-and-merge, applied to MCP path. Per-tool handlers
        # bind ``audit_*`` contextvars (e.g. ``audit_override_op`` from
        # G6.3-T5's broadcast-overrides verbs); the chassis HTTP route
        # path merges them via :func:`audit._resolve_audit_payload` and
        # the typed-op dispatcher path via
        # :func:`operations._audit._build_audit_payload`. MCP was the
        # third audit-write path missing the merge — the test
        # ``test_set_via_mcp_writes_audit_row_with_override_diff``
        # expects ``override_op``/``override_id``/``override_pattern``/
        # ``override_detail`` to land on the row's payload.
        _audit_prefix = "audit_"
        for _k, _v in structlog.contextvars.get_contextvars().items():
            if not _k.startswith(_audit_prefix) or _v is None:
                continue
            _stripped = _k[len(_audit_prefix) :]
            if _stripped:
                audit_payload.setdefault(_stripped, _v)
        try:
            await write_mcp_audit_row(
                audit_id=audit_id,
                operator=operator,
                method="MCP",
                path=f"/mcp/tools/call/{audit_name}",
                status_code=status_code,
                duration_ms=duration_ms,
                payload=audit_payload,
            )
        except Exception:
            # Fail-closed: an audit-write failure invalidates the call.
            # The finally's bare raise replaces any in-flight return or
            # exception with the audit-write exception; the dispatcher
            # then maps it to JSON-RPC -32603 Internal Error. Detail
            # strings stay scrubbed (only the exception class lands in
            # the structlog payload below).
            _log.exception(
                "mcp_audit_write_failed",
                method="MCP",
                path=f"/mcp/tools/call/{audit_name}",
                status_code=status_code,
            )
            raise
        # G6.1-T3 (#309) publish-on-write hook — runs AFTER the audit
        # commit succeeds. ``publish_event`` is fail-open by contract,
        # so a Valkey wobble never converts an OK tool call into a
        # JSON-RPC -32603. Audit row is the canonical record; broadcast
        # is the real-time view.
        await _publish_mcp_event(
            audit_id=audit_id,
            operator=operator,
            op_id=audit_name,
            op_class=broadcast_op_class,
            detail=broadcast_detail,
            audit_path=f"/mcp/tools/call/{audit_name}",
            status_code=status_code,
            audit_payload=broadcast_params,
        )


def _operator_meets_required_role(
    operator: Operator,
    defn: ToolDefinition | ResourceTemplateDefinition,
) -> bool:
    """Re-check the role ranking from :mod:`~meho_backplane.mcp.registry`.

    Mirrors the inline check in
    :func:`~meho_backplane.mcp.registry.all_tools_for` but is needed
    here because ``tools/call`` reaches the registry by direct
    :func:`~meho_backplane.mcp.registry.get_tool` lookup, bypassing
    the list-time RBAC filter. The shared rule lives in the registry
    module; this helper is a thin re-application at the call site.
    """
    return role_at_least(operator.tenant_role, defn.required_role)


# ---------------------------------------------------------------------------
# resources/list + resources/templates/list + resources/read
# ---------------------------------------------------------------------------


async def handle_resources_list(
    _operator: Operator,
    _params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return concrete (non-templated) resources.

    v0.2 registers only templated resources (every URI carries at least
    one ``{var}``), so this always returns an empty list. The method is
    present for MCP spec conformance: clients that call ``resources/list``
    expect an empty array, not ``METHOD_NOT_FOUND``. Templated resources
    surface via :func:`handle_resources_templates_list`.
    """
    return {"resources": []}


async def handle_resources_templates_list(
    operator: Operator,
    _params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return resource templates the operator's role admits.

    Same RBAC-filter + no-pagination shape as :func:`handle_tools_list`.
    """
    visible = [defn.to_wire() for defn in all_resource_templates_for(operator)]
    return {"resourceTemplates": visible}


# code-quality-allow: pre-existing oversized resources/read handler (>100
# lines on main before #1481); the #1481 audit-status fix adds a small
# except arm, not the size — refactor is out of scope here.
async def handle_resources_read(
    operator: Operator,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Dispatch a ``resources/read`` request to the matching template handler.

    Per MCP spec §Resources/Error Handling, a URI that doesn't match any
    registered resource returns code ``-32002`` "Resource not found"
    (not the generic ``-32601``). We map this through
    :class:`McpInvalidParamsError` because the dispatcher's framework
    catches that distinct exception; the dispatcher then assigns the
    correct numeric code at the JSON-RPC envelope layer.

    Actually — the dispatcher only knows about INVALID_PARAMS (-32602)
    and INTERNAL_ERROR (-32603) at the moment, not -32002. To surface
    the spec-correct code we'd need either:
    (a) a new exception sentinel in
    :mod:`~meho_backplane.mcp.server` (e.g.
    ``McpResourceNotFoundError``) that the dispatcher catches and
    maps to ``-32002``;
    (b) build the wire-shape error envelope directly here, bypassing
    the McpInvalidParamsError path.

    v0.2 takes path (a) is cleaner but requires a server.py change.
    For now this handler uses the dispatcher's existing generic-error
    path via :class:`McpInvalidParamsError` (mapping to ``-32602``).
    The spec-correct ``-32002`` is recorded as an adjacent finding —
    landing it cleanly needs a small dispatcher extension that's
    outside T3's surface.

    Audit row writing
    -----------------

    Per G0.5-T5 (#250), every ``resources/read`` invocation produces
    exactly one :class:`~meho_backplane.db.models.AuditLog` row.
    ``op_class`` is hardcoded ``"read"`` — resources are passive in v0.2
    (the registry currently exposes no write-shape resources; future
    write-shape patterns would surface as tools, not resources).
    Fail-closed semantics match :func:`handle_tools_call`.
    """
    raw_params = params or {}
    uri = raw_params.get("uri")
    request_override = _read_mcp_broadcast_detail(raw_params)
    start = time.monotonic()
    audit_uri = uri if isinstance(uri, str) and uri else "<empty>"
    audit_payload: dict[str, Any] = {
        "uri": audit_uri,
        "op_class": "read",
    }
    status_code = 500

    try:
        if not isinstance(uri, str) or not uri:
            status_code = 400
            raise McpInvalidParamsError("resources/read: missing or empty 'uri'")

        match = get_resource_for_uri(uri)
        if match is None:
            # Per spec this should be -32002. See docstring for the deferral.
            status_code = 404
            raise McpInvalidParamsError(f"resource not found: {uri!r}")
        defn, handler, bound_params = match

        # RBAC: same call-time re-check as tools.
        if not _operator_meets_required_role(operator, defn):
            _log.warning(
                "mcp_resource_read_forbidden",
                uri=uri,
                required=defn.required_role,
                actual=operator.tenant_role,
            )
            status_code = 403
            raise McpInvalidParamsError(
                f"forbidden: resource {uri!r} requires a higher role",
            )

        body = await handler(operator, bound_params)
        status_code = 200

        # MCP §Resources/Reading Resources: response.contents is an array;
        # each entry carries `uri`, `mimeType`, and one of `text` or `blob`.
        # v0.2 serialises the handler's dict as a JSON text block, mirroring
        # the tool-result shape — handlers that need binary (`blob`) return
        # value can override by emitting their own contents-array structure
        # in a later task.
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": defn.mimeType,
                    "text": json.dumps(body),
                },
            ],
        }
    except McpInvalidParamsError:
        # Class-wide audit-status correction (#1481), mirroring
        # :func:`handle_tools_call`. A resource handler that raises
        # ``McpInvalidParamsError`` after the explicit gates leaves
        # ``status_code`` at the init 500; the wire outcome is a
        # ``-32602`` rejection, so project it onto a 403 "denied" status
        # for the audit row and the broadcast event. Pre-dispatch
        # branches (400/404/403) already set ``status_code`` and pass
        # through untouched.
        if status_code == 500:
            status_code = 403
        raise
    finally:
        duration_ms = (time.monotonic() - start) * 1000
        audit_id = uuid.uuid4()
        # G6.3-T2 (#379): resolve broadcast detail BEFORE the audit
        # row commits. The broadcast op_id is the generic
        # ``mcp.resource.read`` (resource URIs are per-request unique
        # and would explode the metric cardinality) so the resolver
        # falls through to ``other`` op_class by default; tenant rules
        # can still match via ``op_id_pattern="mcp.resource.*"``.
        (
            broadcast_op_class,
            broadcast_detail,
            broadcast_origin,
        ) = await compute_effective_broadcast_detail(
            op_id="mcp.resource.read",
            tenant_id=operator.tenant_id,
            raw_params=audit_payload,
            request_override=request_override,
        )
        # Snapshot the broadcast-visible params BEFORE injecting the
        # audit-only ``broadcast_detail_origin`` /
        # ``broadcast_detail_effective`` keys -- same separation of
        # audit-row and broadcast-event payloads as the tools/call
        # path above.
        broadcast_params = dict(audit_payload)
        audit_payload["broadcast_detail_origin"] = broadcast_origin
        audit_payload["broadcast_detail_effective"] = broadcast_detail
        try:
            await write_mcp_audit_row(
                audit_id=audit_id,
                operator=operator,
                method="MCP",
                path=f"/mcp/resources/read/{audit_uri}",
                status_code=status_code,
                duration_ms=duration_ms,
                payload=audit_payload,
            )
        except Exception:
            _log.exception(
                "mcp_audit_write_failed",
                method="MCP",
                path=f"/mcp/resources/read/{audit_uri}",
                status_code=status_code,
            )
            raise
        # G6.1-T3 publish-on-write hook for the resources/read path.
        await _publish_mcp_event(
            audit_id=audit_id,
            operator=operator,
            op_id="mcp.resource.read",
            op_class=broadcast_op_class,
            detail=broadcast_detail,
            audit_path=f"/mcp/resources/read/{audit_uri}",
            status_code=status_code,
            audit_payload=broadcast_params,
        )


# ---------------------------------------------------------------------------
# G6.1-T3 publish-on-write helper
# ---------------------------------------------------------------------------


def _classify_mcp_status(status_code: int) -> str:
    """Map an MCP audit status_code to the broadcast result-status trichotomy.

    The MCP handlers project JSON-RPC outcomes onto HTTP-shaped codes
    (200 OK, 400 INVALID_PARAMS, 403 forbidden-via-RBAC, 404
    unknown-tool, 500 internal). Same split as
    :func:`meho_backplane.audit._classify_http_status` so subscribers
    see one taxonomy across HTTP and MCP traffic.
    """
    if status_code == 403:
        return "denied"
    if status_code >= 400:
        return "error"
    return "ok"


async def _publish_mcp_event(
    *,
    audit_id: uuid.UUID,
    operator: Operator,
    op_id: str,
    op_class: str,
    detail: Literal["full", "aggregate"],
    audit_path: str,
    status_code: int,
    audit_payload: dict[str, Any],
) -> None:
    """Build the MCP-side :class:`BroadcastEvent` and publish it.

    Identity is pulled from the validated :class:`Operator` (the
    dispatcher resolved it via
    :func:`~meho_backplane.mcp.auth.verify_mcp_jwt_and_bind`); the
    chassis ``operator_sub`` / ``tenant_id`` contextvars are also
    bound on the same path but reading them here would duplicate the
    indirection :class:`Operator` exists to eliminate. The
    ``audit_path`` argument is the chassis audit row's path column
    (``/mcp/tools/call/{name}`` or ``/mcp/resources/read/{uri}``);
    used only for the log line on a publish failure so operators
    chasing ``broadcast_publish_failed`` events can correlate to the
    exact audit row that triggered the (failed) publish.

    ``op_id`` differs from ``audit_path`` deliberately: the audit row
    keeps the per-URI path for forensic queries, while the broadcast
    event uses the tool-name (tools/call) or a stable
    ``mcp.resource.read`` constant (resources/read) so
    :func:`~meho_backplane.broadcast.events.classify_op` matches the
    sensitivity-class taxonomy correctly without per-URI cardinality
    blowup on the ``broadcast_events_published_total`` metric.

    As of G6.3-T2 (#379) the *(op_class, detail)* pair is resolved
    upstream by :func:`compute_effective_broadcast_detail`; this helper
    no longer calls :func:`classify_op` or decides the redaction
    branch. The split lets the resolver inject its decision-origin
    into the audit row's payload *before* the audit write commits.
    """
    result_status = _classify_mcp_status(status_code)
    event = BroadcastEvent(
        event_id=uuid.uuid4(),
        ts=datetime.now(UTC),
        tenant_id=operator.tenant_id,
        principal_sub=operator.sub,
        principal_name=operator.name,
        op_id=op_id,
        op_class=op_class,
        result_status=result_status,
        audit_id=audit_id,
        payload=redact_payload(op_class, audit_payload, result_status, detail=detail),
    )
    # publish_event is itself fail-open; the wrap here is belt-and-
    # suspenders against an exception in BroadcastEvent construction
    # (e.g. a future tightening of the schema). The audit row is
    # already committed by the time we reach this line.
    try:
        await publish_event(event)
    except Exception:
        _log.exception(
            "mcp_broadcast_construction_failed",
            audit_path=audit_path,
            op_id=op_id,
            status_code=status_code,
        )


# ---------------------------------------------------------------------------
# Register on import (side effect)
# ---------------------------------------------------------------------------


register_method("tools/list", handle_tools_list)
register_method("tools/call", handle_tools_call)
register_method("resources/list", handle_resources_list)
register_method("resources/templates/list", handle_resources_templates_list)
register_method("resources/read", handle_resources_read)
