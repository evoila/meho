# rate-limiting — per-principal / per-tenant dispatch limits (#3500)

## Overview

Before #3500 MEHO had **no application-level limiter on the dispatch
surface**. The only request rate limits were the event-ingest webhook
(`events/ingest/rate_limit.py`) and the agent broadcast-announce
(`broadcast/rate_limit.py`); the `identity_budget` / `budget_enforcement`
machinery caps only MEHO's **own internal** agent-run LLM spend, not a
client's `call_operation` / `search_operations` request volume. On a shared
instance hosting an untrusted-visitor tenant, one curious visitor — or a
runaway agent loop — could exhaust DB / connector pools, CPU, and (via
triggered internal agent runs) the server-side LLM budget, degrading every
tenant. There was no server-side backstop.

#3500 adds three controls, all **off by default** (every limit defaults to
`0` = unlimited), so existing tenants are unaffected until an operator opts
in. Each is a global default plus a per-tenant override expressed purely in
settings (no migration, mirroring `agent_runs_disabled_tenants`):

1. **Per-principal dispatch rate limit** — a per-`(tenant, principal)`
   fixed-window request cap (per minute). A burst above the cap is rejected
   with a `retry_after_seconds` hint.
2. **Per-principal concurrent-op cap** — a per-`(tenant, principal)` count
   of in-flight top-level dispatches, acquired before an op executes and
   released when it finishes.
3. **Per-tenant MCP session cap** — a per-tenant fixed-window cap on new
   MCP `initialize` handshakes (see "Session cap" below).

All three reuse the fixed-window `INCR` + `EXPIRE` shape the two pre-existing
limiters already proved on the shared Valkey (the broadcast client). A
token-bucket algorithm was considered and rejected for v1: the fixed-window
counter is atomic on a single `INCR` (no Lua / no read-modify-write race),
matches the codebase's two other limiters and their test harness, and
satisfies the "a burst is throttled" acceptance criterion. If burst-carryover
semantics are ever needed, that is a follow-up refinement.

## Key types

| Symbol | Where | Role |
|---|---|---|
| `check_dispatch_rate_limit(tenant_id, sub, limit)` | `operations/dispatch_limits.py` | Increments the window counter; returns `retry_after` seconds when over `limit`, else `None`. `limit<=0` → no Valkey round-trip. |
| `acquire_dispatch_slot(tenant_id, sub, cap, ttl)` | `operations/dispatch_limits.py` | Takes one in-flight slot; returns `(retry_after, None)` when over `cap` (increment rolled back), else `(None, key)`. |
| `release_dispatch_slot(key)` | `operations/dispatch_limits.py` | Releases a slot (`DECR`, deleting the key at zero); `None` is a no-op. |
| `rate_limited_read_envelope(operator)` | `operations/dispatch_limits.py` | Shared rate check for the read-path meta-tools; returns a `rate_limited` envelope dict or `None`. |
| `resolve_dispatch_rate_limit` / `resolve_dispatch_concurrency_cap` | `operations/dispatch_limits.py` | Resolve the effective limit for a tenant (per-tenant override wins over the global default). |
| `resolve_int_override(csv, tenant_id, default)` | `operations/dispatch_limits.py` | Parse a `<uuid>=<int>` CSV override map; malformed entries are skipped with a warning. |
| `check_mcp_session_cap(tenant_id, cap)` | `mcp/session_limit.py` | Per-tenant fixed-window cap on new `initialize` handshakes. |
| `result_rate_limited(...)` | `operations/_errors.py` | The `OperationResult` builder (`status='rate_limited'`, `extras.retry_after_seconds`). |

## Control flow

**Rate limit + concurrency cap** are enforced in
`operations/dispatcher.py::dispatch()` — the single entry point every
`call_operation` funnels through on **CLI, MCP and the REST dispatch route**
alike, so one seam covers all fronts (the CLAUDE.md postulate that CLI and
MCP share the dispatch path). The gate runs after descriptor lookup + param
validation and before the policy gate, and only for **top-level** requests:

- A composite's internal `dispatch_child` fan-out (`composite_depth_var > 0`)
  is one client request, not many — counting it would misattribute volume and
  risk a self-deadlock against the cap.
- An approval-resume (`_approved=True`) is the continuation of a request
  already counted at park time.

Both are exempt. On a rejection the dispatcher writes a synchronous audit row
(`audit_rejection_safe(result_status='rate_limited')`) — before any vendor
traffic — and returns `result_rate_limited(...)`. Unlike a policy denial, a
rate-limit rejection is audited **row-only, with no broadcast**: emitting one
broadcast per over-limit request would amplify the very load the limiter sheds
and spam the tenant feed. The audit write is fail-open (a rejected request
changed nothing). `status_code_for_result`
maps `rate_limited` → a synthetic `429` on the audit row. The concurrency slot
is released in the dispatcher's `finally` (best-effort: a release failure is
logged, not raised, and the slot's safety `ttl` is the backstop).

The read-path meta-tools `search_operations` and `preview_operation` (which do
not go through `dispatch()`) call `rate_limited_read_envelope` at entry and
return the same structured envelope. They share the one per-principal dispatch
rate bucket (a caller's total working-surface request volume is bounded), and
do no vendor traffic, so they carry no concurrency cap or dedicated audit row.
`result_query` is intentionally **not** gated — see "Known issues / limits".

**Session cap.** MEHO holds **no stateful MCP session store** — a session id
is issued on `initialize` purely for audit correlation and then forgotten
(`mcp/server.py::_issue_mcp_session_id`). A precise "concurrent sessions" count
is therefore unknowable without building a session registry, which #3500 did
not do. The cheap, deterministic backstop is a per-tenant cap on **new session
handshakes per window**: every `initialize` is one new session, so bounding
`initialize` volume bounds session creation. `_initialize` calls
`_enforce_mcp_session_cap` first; over the cap it raises `McpRateLimitedError`
(JSON-RPC `-32000`) with a `retry_after_seconds` hint.

## Settings

All default to disabled. Per-tenant override CSV is `<tenant-uuid>=<int>`
pairs (case-insensitive UUID, whitespace ignored); a tenant absent from the
map uses the global default.

| Setting / env | Default | Meaning |
|---|---|---|
| `dispatch_rate_limit_per_minute` / `DISPATCH_RATE_LIMIT_PER_MINUTE` | `0` | Global per-principal dispatch requests/min. |
| `dispatch_rate_limit_per_minute_overrides` / `..._OVERRIDES` | `""` | Per-tenant override map. |
| `dispatch_max_concurrent_ops` / `DISPATCH_MAX_CONCURRENT_OPS` | `0` | Global per-principal in-flight-dispatch cap. |
| `dispatch_max_concurrent_ops_overrides` / `..._OVERRIDES` | `""` | Per-tenant override map. |
| `dispatch_concurrency_slot_ttl_seconds` / `DISPATCH_CONCURRENCY_SLOT_TTL_SECONDS` | `3600` | Safety TTL so a slot leaked by a crashed worker self-heals. |
| `mcp_session_start_limit_per_minute` / `MCP_SESSION_START_LIMIT_PER_MINUTE` | `0` | Global per-tenant new-MCP-sessions/min. |
| `mcp_session_start_limit_per_minute_overrides` / `..._OVERRIDES` | `""` | Per-tenant override map. |

The lab enables tight caps for the untrusted-visitor (Envision) tenant only,
leaving production tenants at the unlimited default.

## Dependencies

- The shared broadcast Valkey via `broadcast.client.get_broadcast_client` —
  the same client the announce + ingest limiters use.
- Key namespaces (distinct from the announce/ingest/feed keys):
  `meho:ratelimit:dispatch:{tenant}:{principal}:{bucket}`,
  `meho:concurrency:dispatch:{tenant}:{principal}`,
  `meho:ratelimit:mcpsession:{tenant}:{bucket}`.
- Prometheus counters: `dispatch_rate_limited_total`,
  `dispatch_concurrency_limited_total`, `mcp_session_limited_total`.

## Known issues / limits

- **Fail-loud on a Valkey outage** — a redis-py failure propagates verbatim
  (consistent with the two existing limiters). Failing the dispatch closed is
  the safe posture for an abuse control; failing open would let a principal
  bypass the cap during a Valkey wobble.
- The concurrency cap is a counter, not a lease: a slot leaked by a crashed
  worker (release never runs) self-heals only when the safety `ttl` expires.
- `result_query` (the JSONFlux handle drill-in) is deliberately not
  separately rate-limited: it is a bounded read over the caller's own
  already-spilled, tenant+principal-isolated handle (low abuse value), it
  follows a `call_operation` that already spent a rate token, and gating its
  pure query cores would couple them to full `Settings` construction. The
  discovery/execution paths (`call_operation`, `search_operations`,
  `preview_operation`) carry the limit.
- The session cap is a **new-sessions-per-window** proxy, not a live
  concurrent-session count — MEHO holds no session registry to count against.

## References

- Task: `evoila/meho#3500`; finding: `evoila-bosnia/meho-internal#321`.
- Prior art: `broadcast/rate_limit.py`, `events/ingest/rate_limit.py`.
- Not this: `operations/budget_enforcement.py` / `identity-budget.md`
  (internal agent-run LLM spend only, not client request volume).
