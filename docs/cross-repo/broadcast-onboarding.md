<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Broadcast onboarding — consumer-side recipe

> Operator-facing recipe for subscribing to the MEHO activity
> broadcast. The implementation lives in
> [`backend/src/meho_backplane/broadcast/`](../../backend/src/meho_backplane/broadcast/)
> and `api/v1/feed.py`; this doc is the surface every consumer reads
> when wiring `meho status --watch`, an MCP client, or a custom
> downstream subscriber.

## What broadcast is

Every authenticated request that produces an audit row also produces
exactly one **broadcast event** on a per-tenant Valkey stream
(`meho:feed:<tenant_id>`). Subscribers tail the stream to get a
real-time view of what every operator in their tenant is doing —
the equivalent of `git log --follow` for inventory operations.

The audit row is the **canonical record** (durable, queryable via
G8's audit-query API). The broadcast feed is the **real-time view**
(in-memory in Valkey, capped at ~10 000 events per tenant, ~24 h
worth at moderate load). A subscriber that misses an event still
finds it in `audit_log`; a subscriber that needs ordering guarantees
queries audit_log by `audit_id` to reconcile gaps.

## Three transports

Every transport reads the same per-tenant stream. Pick by use case:

| Transport | Best for | Spec |
|---|---|---|
| `meho status --watch` CLI | Terminal-resident operator triage | G6.1-T5 (#311) |
| `GET /api/v1/feed` SSE | Custom dashboards, browser-side viewers, scripts that need live push | G6.1-T4 (#310) |
| `meho://tenant/{tenant_id}/feed` MCP resource | LLM clients (Claude, MCP-aware agents) that poll a snapshot | G6.1-T6 (this task) |

The Slack mirror (G6.2 #333) and any future web admin UI subscribe
the same way — XREAD against the per-tenant stream key.

## CLI: `meho status --watch`

The fastest path for an operator at a terminal:

```console
$ meho status --watch
[12:04:01] ok       op-alice   vsphere.vm.list      cluster=prod-vc-1
[12:04:02] denied   op-bob     vault.kv.read        (credential read — aggregate-only)
[12:04:05] ok       op-alice   k8s.pod.get          target=meho-prod
```

Filters (composable):

```console
$ meho status --watch --filter op=vault           # only vault ops
$ meho status --filter principal=op-alice         # only one operator
$ meho status --watch --filter target=meho-prod   # only one target
```

The CLI handles reconnect-with-replay automatically via SSE's
`Last-Event-Id`. A laptop closing its lid loses no events on
reconnect — the Valkey stream replays from the last seen entry id.

## HTTP SSE: `GET /api/v1/feed`

Standard WHATWG `EventSource` protocol. Use this when the CLI doesn't
fit (custom dashboard, headless script, third-party tool):

```javascript
const events = new EventSource(
  `${BACKPLANE_URL}/api/v1/feed?op_class=write`,
  { withCredentials: true },
);

events.addEventListener("broadcast", (msg) => {
  const event = JSON.parse(msg.data);
  console.log(event.op_id, event.principal_sub, event.result_status);
});
```

Query parameters (all optional, exact-match):

* `op_class` — one of `read`, `write`, `credential_read`,
  `audit_query`, `other`.
* `principal` — JWT `sub` claim (operator identifier).
* `target` — target name (when the op operates on a specific target).
* `since=<entry_id>` — replay cursor (server-side bridges; SSE
  clients use the `Last-Event-Id` header instead).

Heartbeats: the server emits `: heartbeat\n\n` (SSE comment line)
every 30 s of outbound silence so intermediaries (nginx, ALB,
CloudFront) don't idle-timeout the connection.

Disconnect handling: client disconnect propagates as cancellation
into the server-side generator; the next event-loop tick releases
the Valkey BLOCKing read.

## MCP resource: `meho://tenant/{tenant_id}/feed`

Designed for LLM clients that poll a snapshot rather than maintain
a live socket. The resource returns the **most recent 50 events** in
chronological order:

```console
$ mcp-inspector --uri "meho://tenant/<your-uuid>/feed"
{
  "tenant_id": "<uuid>",
  "count": 47,
  "events": [
    {"event_id": "...", "ts": "...", "op_id": "vault.kv.read", ...},
    {"event_id": "...", "ts": "...", "op_id": "k8s.pod.get",  ...},
    ...
  ]
}
```

The MCP server advertises **no subscribe capability**: clients that
need live updates re-read the resource on their own cadence. SSE is
the canonical live-push surface; the MCP resource exists for clients
that don't speak SSE (most pure JSON-RPC LLM tooling).

Cross-tenant reads (`meho://tenant/<someone-else>/feed`) reject
with JSON-RPC `INVALID_PARAMS` (-32602). The bound `tenant_id` must
match the operator's JWT-derived tenant.

## PII defaults (decision #3)

The publisher applies a sensitivity classifier to every event
**before** XADD onto the stream. The taxonomy:

| `op_class` | Examples | Payload visibility |
|---|---|---|
| `credential_read` | `vault.kv.read`, `vault.kv.list` | **Aggregate only.** Payload: `{op_id, target_name, result_status}`. Path / key names / values are stripped. |
| `audit_query` | G8 audit verbs | **Aggregate only.** Payload: `{op_id, result_status, row_count}`. Filter contents stripped. |
| `read` | `.list`, `.info`, `.get`, `.about`, `.ls` suffix | **Full detail.** Payload carries request params + structured response summary. |
| `write` | `.create`, `.update`, `.delete`, `.patch` suffix | **Full detail.** Same shape as `read`. |
| `other` | Anything else (chassis HTTP routes, unmapped ops) | **Full detail.** |

Operators verifying credential reads are correctly aggregate-only:

```console
$ meho status --watch --filter op=vault | head -1
[12:04:02] denied   op-bob     vault.kv.read   (no path / no key)
```

A `vault.kv.read` event whose payload includes a `path` field would
be a privacy regression — the integration test
[`test_broadcast_publisher.py`](../../backend/tests/test_broadcast_publisher.py)
explicitly negative-asserts this.

Future tightening (G6.3 #333-class follow-up): per-tenant
opt-in/opt-out flags will let operators flip `credential_read` /
`audit_query` to full-detail (e.g. for internal-only-trusted
tenants). v0.2 ships conservative defaults; the toggle is a separate
Initiative.

## Authentication + RBAC

All three transports honour the same gates:

* **JWT validation** — every request carries a Keycloak-minted JWT;
  the chassis `verify_jwt` chain validates issuer / audience / kid
  rotation / `exp` / `nbf`. See
  [`keycloak-tenant-claims.md`](keycloak-tenant-claims.md) for the
  claim contract.
* **Tenant scoping** — the stream key is derived from
  `operator.tenant_id` (the JWT claim, not a request parameter).
  Cross-tenant subscription is impossible by construction — there's
  no body field or URI fragment that accepts another tenant's ID,
  and the MCP resource rejects URI-bound mismatches with
  `INVALID_PARAMS`.
* **Role** — `operator` minimum on SSE + MCP feed. `read_only`
  operators have lower-friction surfaces (`meho status` snapshot,
  knowledge-base search) and don't get live activity. `tenant_admin`
  inherits operator privileges.

A `read_only` operator hitting `GET /api/v1/feed` receives `403
insufficient_role` + an `insufficient_role` structured log event for
operator triage.

## Troubleshooting

### `401 Unauthorized` from `/api/v1/feed` or `connection closed: 401`

Token expired or audience mismatch. Verify:

```console
$ meho auth whoami        # prints sub / tenant_id / expiry
$ meho status             # tests the federation chain end-to-end
```

If `meho status` works but `meho status --watch` doesn't, the issue
is the audience-binding split between `/api/v1/*` and `/mcp`. The
SSE feed uses the chassis audience (`KEYCLOAK_AUDIENCE`); the MCP
resource uses the MCP audience (`MCP_RESOURCE_URI`). A token minted
for one won't satisfy the other.

### `403 insufficient_role`

The operator's `tenant_role` JWT claim is `read_only`. Either
elevate to `operator` in Keycloak or switch to the snapshot surface
(`meho status` without `--watch`).

### Empty feed despite running operations

* Check `broadcast_publish_errors_total` on `/metrics` — a sustained
  nonzero rate indicates the publisher swallowed events (Valkey
  unreachable, redis-py teardown race). Audit rows still land; only
  the broadcast feed is degraded.
* Check the Valkey pod is reachable from the backplane:
  `/ready` returns 503 with detail `unreachable: <ExcClass>` when
  the `broadcast_readiness_probe` can't reach Valkey.

### SSE connection drops every 60 s

An intermediary (nginx, ALB, CloudFront) is idle-timing-out a quiet
stream. The server emits heartbeats every 30 s of outbound silence
specifically to defeat this. If you still see drops, check:

* The proxy honours SSE: `Cache-Control: no-cache` + `X-Accel-Buffering: no`
  response headers are set by the backplane; some proxies strip them.
* `proxy_read_timeout` / equivalent is ≥ 60 s.

### MCP `INVALID_PARAMS: cross-tenant access denied`

The URI bound a `tenant_id` that doesn't match the operator's JWT
claim. Use the operator's OWN `tenant_id` (visible via the
`meho://tenant/<id>/info` resource).

### Replay gaps after a Valkey pod restart

Valkey's RDB snapshot recovers most events on restart, but a
sub-second window of in-flight XADDs may be lost between snapshot
intervals. The audit_log row is always durable — a subscriber that
needs gap-free ordering queries `audit_log` by `audit_id` after
replay. The load-test acceptance for the chaos case ships in
[G6.1-T7](https://github.com/evoila/meho/issues/312#issuecomment-pushback-thread)
(separate task; see issue #312 pushback thread).

## What gets broadcast vs. what doesn't

**Broadcast:**

* Every authenticated HTTP request to `/api/v1/*` (audit-middleware
  publishes after the audit row commits).
* Every MCP `tools/call` and `resources/read` (MCP handlers publish
  in the same finally block as their audit row).

**Not broadcast:**

* Unauthenticated requests (`/healthz`, `/ready`, `/metrics`,
  `/.well-known/oauth-protected-resource`) — no operator to
  attribute, no broadcast row.
* 401 responses — same reason.
* The MCP `initialize` / `ping` / `notifications/initialized`
  JSON-RPC envelope itself — only the wrapped tool/resource calls
  generate broadcast events.
* Internal cron jobs and lifespan hooks (engine pre-warm, embedding
  preload) — no JWT, no operator.

## References

* Initiative: [#228](https://github.com/evoila/meho/issues/228).
* Component spec: [`docs/codebase/backend.md`](../codebase/backend.md)
  (Broadcast publish-on-write hook section).
* SSE endpoint:
  [`backend/src/meho_backplane/api/v1/feed.py`](../../backend/src/meho_backplane/api/v1/feed.py).
* MCP resource:
  [`backend/src/meho_backplane/mcp/resources/tenant_feed.py`](../../backend/src/meho_backplane/mcp/resources/tenant_feed.py).
* CLI verb:
  [`cli/internal/cmd/status_watch.go`](../../cli/internal/cmd/status_watch.go).
* Decision #3 (PII defaults): `docs/planning/v0.2-decisions.md`.
* Valkey Streams reference: <https://valkey.io/topics/streams-intro/>
* MCP 2025-06-18 Resources spec:
  <https://modelcontextprotocol.io/specification/2025-06-18/server/resources>
