# MCP server

The Model Context Protocol surface in `backend/src/meho_backplane/mcp/`
is MEHO's primary agent-facing transport. Spec-conforming clients
connect via Streamable HTTP at `POST /mcp`, complete the
`initialize` handshake, and then issue `tools/call` /
`resources/read` requests against the connector + reference tool
catalogue.

Most of the surface-level architecture (router mount, dispatch table,
audit-row shape, capability advertisement) lives in
[`backend.md`](backend.md) under the entries tagged
"MCP Streamable HTTP transport entrypoint", "MCP per-operation audit",
"MCP reference tool + resource", and the broadcast-feed resources.
This doc covers cross-cutting concerns that don't fit cleanly into a
single Task entry.

## Overview

* Entry-point module: `backend/src/meho_backplane/mcp/server.py`
* Wire schemas: `backend/src/meho_backplane/mcp/schemas.py`
* Tool / resource registries: `register_mcp_tool` /
  `register_mcp_resource` in `mcp/server.py`
* Audit hook: `backend/src/meho_backplane/mcp/audit.py`

The dispatch table lives at module scope and is populated at import
time; reloading via the test fixture
`tests/mcp_test_fixtures.isolated_registry` is the only supported way
to mutate it between requests.

## Protocol version negotiation

`PROTOCOL_VERSION` in
[`backend/src/meho_backplane/mcp/schemas.py`](../../backend/src/meho_backplane/mcp/schemas.py)
pins the MCP revision the server implements
(currently `"2025-06-18"`). It is a build-time constant — not a
settings field, not env-configurable. Bumping it is an explicit
release-cycle decision (CHANGELOG entry + release-body callout, per
the v0.6.0 honesty-callout precedent in PR #1159).

### Server behaviour

The server **always** responds with
`InitializeResponse.protocolVersion == PROTOCOL_VERSION` regardless
of what the client sent. From the MCP 2025-06-18 spec's perspective
this is compliant: "the server MUST respond with its own
`protocolVersion`. If the server does not support the requested
protocol version, it MUST respond with a version it does support
(typically the latest version supported by the server)." MEHO
supports exactly one revision at a time; that revision is the one
in the response.

What the spec does **not** require — and what MEHO historically did
not provide — is a server-side signal that a downgrade or upgrade
just occurred. A client pinned to `"2025-03-26"` receiving a
`"2025-06-18"` response had no way to know which revision the server
agreed to, and the operator's log stream stayed silent on the
mismatch. The consumer-side closed-loop reporting flagged this as
[`mcp-initialize-protocol-version-silent-upgrade`](https://github.com/evoila-bosnia/meho-internal/issues/697)
(signal 15), and the v0.6.0 release body was amended (PR #1159) to
call out the gap explicitly as observation-not-commitment.

### G0.14-T13: observability-only

[Task #1202](https://github.com/evoila/meho/issues/1202) lands the
observability half of the gap, deliberately scoped narrow:

* `_initialize` at
  [`mcp/server.py`](../../backend/src/meho_backplane/mcp/server.py)
  compares the validated client `protocolVersion` against
  `PROTOCOL_VERSION` after `InitializeRequest.model_validate(...)`
  succeeds. On mismatch it emits a single structured
  `WARNING` log event:

  ```
  _log.warning(
      "mcp_initialize_protocol_version_mismatch",
      client_protocol_version=<wire value>,
      server_protocol_version=PROTOCOL_VERSION,
      operator_sub=operator.sub,
  )
  ```

  The shape mirrors `mcp_unsupported_protocol_version` — the existing
  WARNING that `_validate_protocol_version_header` already emits when
  a non-`initialize` request carries a stale `MCP-Protocol-Version`
  header. Operators get a uniform event-name family for both
  handshake-time and post-handshake mismatches.

* `HealthResponse` at
  [`api/v1/health.py`](../../backend/src/meho_backplane/api/v1/health.py)
  carries `mcp_protocol_version: str = PROTOCOL_VERSION` (mirrors the
  `mcp_session_id_capture` precedent from G0.14-T6 #1147 — single
  field, single source of truth, surfaced on the authenticated GET).

* `/ready`'s `features` block at
  [`features.py`](../../backend/src/meho_backplane/features.py) gains
  an `mcp` entry of shape
  `{"configured": true, "protocol_version": PROTOCOL_VERSION, "missing_env": []}`.
  Mirrors the `audit_replay` block's shape (no missing env vars
  because the field is a build-time constant, not a deploy-time
  knob). Operators get a single unauthenticated GET that answers
  "which MCP revision does this deploy pin?".

The handshake response body itself is **unchanged**. This is
observability-only: a pinned client requesting `"2025-03-26"` still
gets `"2025-06-18"` back; the server just no longer keeps that
mismatch silent.

### Out of scope (deliberately)

What this task **does not** do:

* **Refusing mismatched clients.** Returning a JSON-RPC error on
  mismatch would break the broad install base of clients that
  currently negotiate against the older revision and silently
  upgrade-tolerate.
* **Down-negotiating to a supported older revision.** MEHO supports
  exactly one revision per server build. Multi-version
  capability-advertisement is a much larger surface change.
* **Version-conditional capability advertisement.** Some MCP
  capabilities (`resources.subscribe`, `prompts`) flip in/out across
  revisions; advertising them conditionally on the client's
  requested version is a per-revision compatibility matrix MEHO
  hasn't committed to maintaining yet.

Future work in any of these directions is gated on **demand
evidence** from real deployments — the observability landed here is
the precondition for measuring whether mismatch is actually
happening in the wild. Operators noticing recurring
`mcp_initialize_protocol_version_mismatch` events for the same
client identity should file a follow-up Task on
[`evoila/meho`](https://github.com/evoila/meho) describing the
mismatch pattern and what behaviour change would unblock them.

## Audit URI redaction for query-bearing resources

The `resources/read` dispatcher
(`handle_resources_read` in `mcp/handlers.py`) records the concrete
read URI in the audit row's `path` (`/mcp/resources/read/<uri>`) and
`payload.uri`. For most resources the URI variable is an opaque
identifier (a kb slug, a docs chunk id, a tenant UUID), so persisting
it is harmless and useful for forensic queries.

`meho://retrieve/{query}` is the exception: its variable is a
free-form retrieval query that leaks operator intent, so the raw query
must not land in the audit trail. The resource template opts into
`audit_redact_uri=True` (a field on
`ResourceTemplateDefinition`); when set, the dispatcher substitutes a
query-stripped sentinel — the template prefix up to the first `{var}`
plus `<redacted>`, i.e. `meho://retrieve/<redacted>` — for both the
audit `path` and `payload.uri`. The substitution runs only after the
URI matches a registered template, so an unmatched URI (404) still
records the attempted value.

Correlatability is preserved through the `audit_*` contextvar
convention: the retrieve handler binds `audit_query_hash` (the
SHA-256 of the decoded query, byte-for-byte identical to the
`POST /api/v1/retrieve` hash) plus `audit_hit_count` before/after the
retrieval call. `write_mcp_audit_row` merges those into the row's
`payload`, so the persisted row is fully attributable (tenant,
operator, `query_hash`, `hit_count`) without carrying the query text.
This mirrors the HTTP route's privacy posture documented in
[`retrieval.md`](retrieval.md).

The redaction helper is `redacted_audit_uri(template)` in
`mcp/registry.py`. New query-bearing resources reuse the same flag +
contextvar pattern rather than special-casing the dispatcher per
resource.

## Dependencies

* `structlog` — every MCP log line goes through
  `_log = structlog.get_logger()` at module scope, sharing
  contextvar-bound fields (`operator_sub`, request-id, MCP session id
  when present) with the rest of the request scope.
* `pydantic` v2 — the wire schemas (`JsonRpcRequest`,
  `InitializeRequest`, `InitializeResponse`, `ServerCapabilities`)
  are frozen `BaseModel`s with `extra="allow"` on the envelopes per
  MCP's forward-extensibility contract.
* `fastapi` — the router is mounted from `mcp/server.py::router`;
  the JWT verification + operator binding runs as a `Depends(...)`
  on `mcp_dispatch`.

## Known issues

* The MCP `initialize` flow currently does not negotiate
  capabilities (`tools`, `resources`) — they are advertised
  unconditionally based on build-time registry state. A client
  declining a capability on the request side has no effect; the
  capability still appears in the response. Tracking this is
  appropriate scope for a future Task once multi-version
  negotiation lands.
* `_validate_protocol_version_header` accepts an absent
  `MCP-Protocol-Version` header on post-handshake calls as
  transitional lenience. The strict-mode contract (header required,
  missing → HTTP 400) is gated behind a future settings flag whose
  rollout requires consumer migration ahead.

## References

* MCP spec (2025-06-18) §Initialization:
  https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle
* MCP spec (2025-06-18) §Streamable HTTP / Protocol Version Header:
  https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
* G0.14-T6 (#1147) — `mcp_session_id_capture` field on
  `HealthResponse` (the precedent G0.14-T13 mirrors).
* G0.14-T7 (#1148) — `/ready` `features` block (the home for
  feature-gate visibility).
* G0.14-T13 (#1202) — protocol-version mismatch observability
  (this section).
* v0.6.0 release-body amendment — PR #1159, G0.13-T6 #1136.
