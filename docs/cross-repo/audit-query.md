<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Querying the MEHO audit log — operator runbook

> Operator-facing runbook for the G8.1 audit query surface. Architecture sits in [`docs/architecture/audit.md`](../architecture/audit.md); this doc is the cookbook every operator reads when answering "who did X to Y and when?".

## What this surface answers

Today, answering "who patched the NSX firewall on Tuesday and why?" requires stitching a Slack thread, a ticket, a PR, and maybe a knowledge-base entry. That's archaeology, not investigation.

After G8.1, `meho audit query --target rdc-nsx --since 7d --op-class write` returns the rows in under 30 seconds. Same data, structured rows, tenant-scoped automatically.

The audit log is **WORM-grade** (write-once, read-many) — written synchronously on every authenticated request by the backplane's chassis middleware. There is no operator-facing edit path; queries here read what was logged, never mutate it.

## Prerequisites

- **Role.** Every audit verb requires `operator` role minimum. `read_only` callers get HTTP 403 (CLI exit code 5); `tenant_admin` is also accepted (same tenant scope as `operator` — there is no admin escape hatch).
- **A running backplane.** `meho login <backplane-url>` writes a session token the CLI reuses across every verb. Override per-call with `--backplane <url>` when needed.
- **Filter intent stays local.** Per [decision #3](../planning/v0.2-decisions.md), the broadcast event emitted for every `query_audit` call is **aggregate-only** — `{op_id, result_status, row_count}`. Slack subscribers and the SSE feed never see which principal you queried, which target, or which op_id pattern. Your investigation intent is yours.

## The five CLI verbs

The G8.1-T3 CLI ships five verbs under `meho audit ...`. All five wrap the [G8.1-T2 REST routes](../../backend/src/meho_backplane/api/v1/audit.py) under `/api/v1/audit/*` and produce identical results to the equivalent HTTP request — the CLI is a render surface, not a separate data path.

| Verb | REST route | Filter shape |
|---|---|---|
| `meho audit query` | `POST /api/v1/audit/query` | Full filter — every field below. |
| `meho audit recent` | `POST /api/v1/audit/query` with `since="24h"` bound at the CLI | Convenience: most recent N rows of operator's tenant. |
| `meho audit show <audit-id>` | `GET /api/v1/audit/show/{audit_id}` | Single-row detail. Cross-tenant id returns "audit row not found" (the backend returns 404, never 403, so existence never leaks). |
| `meho audit who-touched <target>` | `GET /api/v1/audit/who-touched/{target}` | Pre-canned: target name + since window. |
| `meho audit my-recent` | `GET /api/v1/audit/my-recent` | Pre-canned: `principal_sub` = your operator JWT sub. |

Every verb accepts `--json` to print the raw `AuditQueryResult` envelope (rows + cursor) for piping into `jq`. Without `--json` the verbs print a tabular summary plus a `NEXT: --cursor=...` line when more pages remain.

## Common forensic questions

### "Who patched rdc-nsx on Tuesday?"

```bash
meho audit query --target rdc-nsx --since 7d --op-class write
```

`--op-class write` narrows to the destructive verbs (POST / PUT / PATCH / DELETE and the typed-connector write ops). Drop it to see reads too.

### "What did Damir do across all targets in the last 24 hours?"

If you are Damir:

```bash
meho audit my-recent --since 24h
```

If you are auditing someone else's activity:

```bash
meho audit query --principal damir --since 24h
```

`--principal` is a partial-match on `audit_log.operator_sub` (case-insensitive `ILIKE %damir%`). The pre-canned `my-recent` infers the principal from your own JWT subject and never accepts a `--principal` override — it always reports on you.

### "All denied operations across the tenant in the last week"

```bash
meho audit query --result-status denied --since 7d
```

`--result-status` accepts one of `ok` (200–399), `denied` (401, 403), or `error` (every other 4xx / 5xx). The classifier mirrors the broadcast publisher's status-code trichotomy.

### "Every vSphere VM operation in the last hour"

```bash
meho audit query --op-id "vsphere.vm.*" --since 1h
```

`--op-id` accepts a glob; `*` translates to SQL `LIKE %` while literal `%` and `_` in your input are escaped so they match verbatim. The pattern matches against `payload->>'op_id'` for MCP / typed-op rows and against the derived `http.{method.lower()}:{path}` for chassis HTTP rows in the same query.

### "Show one audit row in full detail"

```bash
meho audit show 11111111-1111-1111-1111-111111111111
```

The cross-tenant probe semantic: if the audit id exists but belongs to another tenant, the backplane returns 404 ("audit row not found") — not 403. The substrate's first WHERE clause is always `audit_log.tenant_id = <operator JWT tenant>`, so a cross-tenant id resolves to zero rows; the router surfaces 404 deliberately so a probing operator cannot distinguish "row never existed" from "row exists in another tenant".

### "Trace one agent session"

The `agent_session_id` column does not exist on `audit_log` in v0.2 and there is no `--agent-session-id` CLI flag. The substrate's filter type carries the field as a forward-compatibility hook — the REST `POST /api/v1/audit/query` body and the MCP `query_audit` tool both accept it on the wire, but the substrate raises `UnsupportedFilterError` (rendered as HTTP 400 by the REST router; `-32602` by the MCP dispatcher) until the column lands with a future schema migration.

For now the closest approximation from the CLI is `--json` plus `jq` over a wider window keyed on `request_id` (the chassis-bound `X-Request-Id` propagates into the audit row):

```bash
meho audit query --since 24h --json | jq '.rows[] | select(.request_id=="<request-uuid>")'
```

Full session-graph traversal is the [G8.2 audit replay](https://github.com/evoila/meho/issues/377) Initiative's job (`meho audit replay <session-id>`); G8.1 ships the substrate `parent_audit_id` filter that G8.2's recursive-CTE traversal builds on.

## Filter semantics

| Flag | Type | Notes |
|---|---|---|
| `--target` | string (target name or alias) | Matches against `targets.name` scoped to your tenant. Unknown name returns zero rows, not an error. |
| `--principal` | string | Substring `ILIKE` match on `operator_sub`. Empty string is treated as no filter. |
| `--op-id` | glob | `*` → SQL `%`. Literal `%` / `_` escaped. Matches both `payload->>'op_id'` (MCP / typed) and `http.{method}:{path}` (chassis). |
| `--op-class` | enum | One of `read` / `write` / `credential_read` / `audit_query` / `other`. Exact match against `payload->>'op_class'`. |
| `--result-status` | enum | One of `ok` / `error` / `denied`. Maps to status-code ranges. |
| `--since` / `--until` | duration shorthand or ISO-8601 | Shorthand grammar: `<N><unit>` where unit ∈ `{s, m, h, d, w}` and N ≤ 9999. ISO-8601 accepted via `datetime.fromisoformat` (Python 3.11+, including the `Z` suffix). |
| `--audit-id` | UUID | Exact-id lookup. Combined with the tenant boundary, produces 0 or 1 row. |
| `--parent-audit-id` | UUID | Composite-op subtree filter. **Returns `-32602` in v0.2** — the column lands with G0.6-T7. |
| `--limit` | int 1..1000 | Default 100, max 1000. |
| `--cursor` | string (opaque) | Forward-only base64 cursor from a prior page's `next_cursor`. Tampered cursors return 400. |

All filters are AND'd. Empty-string string filters are still applied at the substrate (so passing `--principal ""` explicitly matches every row, by virtue of `ILIKE %%`).

## Pagination

Default `--limit 100`, maximum 1000. The substrate orders rows `(occurred_at DESC, id DESC)` and uses an `N+1` fetch to detect whether more pages exist. When more rows are available, the response includes `next_cursor` (opaque base64 of `{ts, id}`); the tabular CLI output prints `NEXT: --cursor=<value>`.

Page forward by passing that cursor back in:

```bash
meho audit query --since 7d --cursor "<value-from-previous-page>"
```

The cursor is **forward-only** — there is no `--prev-cursor`. To go back to "page 1" of a query, re-run without `--cursor`. Rows inserted between two cursor-paginated pages are visible only on a fresh page-1 call; they do not appear inside a cursor-paginated continuation. This is correctness-by-design: a snapshot of the rows older than the cursor's `(ts, id)` is what the operator asked for.

## Cross-tenant boundary

Every verb is tenant-scoped via your operator JWT. There is **no admin escape hatch**; cross-tenant queries are structurally impossible:

- The backplane's substrate enforces `tenant_id = <JWT tenant>` as the first SQL WHERE clause. The `tenant_id` argument is keyword-only on the handler and the model has no `tenant_id` field at all.
- The REST `POST /api/v1/audit/query` body's Pydantic model has no `tenant_id` field, so any client-supplied `tenant_id` is silently dropped by Pydantic's default `extra="ignore"`.
- The MCP tool's `inputSchema` has `additionalProperties: false`, so an agent that tries to smuggle `tenant_id` in `arguments` is rejected by the dispatcher's jsonschema layer with `-32602` before reaching the handler.
- The `show` route returns 404 (not 403) for any audit-id that exists in another tenant.

If you need to query another tenant's audit log, you need an operator JWT for that tenant — there is no cross-tenant capability.

## Audit-on-audit-query (decision #3)

Every `query_audit` call is itself an authenticated request and writes its own audit row. That row's broadcast event ships under `op_class="audit_query"` per the [G6.1 sensitivity classifier](https://github.com/evoila/meho/issues/228), which means subscribers to the per-tenant Valkey stream and Slack relay see:

```json
{"op_id": "meho.audit.query", "result_status": "ok", "row_count": 47}
```

**Filter contents are never broadcast.** Which principal you investigated, which target, which op_id glob — all of that stays in the audit row itself and is invisible to anyone watching the live feed. The full audit row is still queryable (via this same surface) by anyone with the appropriate role on the appropriate tenant.

The REST surface emits the canonical op_id `meho.audit.query` for these audit rows. The MCP surface emits `op_id = "query_audit"` (the tool name verbatim, per the MCP dispatcher convention) — the same logical operation, different identifier per dispatch path. A v0.2.next unification is on the roadmap; operators querying for "all audit-query calls regardless of surface" today need to OR both op_ids.

## MCP agent surface

Agents see exactly one tool for audit data: `query_audit(filters)`. The narrow-waist contract ([CLAUDE.md](../../CLAUDE.md) postulate 5) collapses the per-shape CLI conveniences (`show` / `who-touched` / `my-recent`) into filter combinations on the same tool:

| CLI verb | Equivalent MCP call |
|---|---|
| `meho audit query --target X --since 24h` | `tools/call query_audit {"target": "X", "since": "24h"}` |
| `meho audit show <uuid>` | `tools/call query_audit {"audit_id": "<uuid>", "limit": 1}` |
| `meho audit who-touched X` | `tools/call query_audit {"target": "X", "since": "24h"}` |
| `meho audit my-recent` | `tools/call query_audit {"principal": "<your sub>", "since": "24h"}` |

The MCP tool's input schema, full description, and RBAC posture are documented in [`docs/codebase/audit_query.md`](../codebase/audit_query.md#mcp-tool-surface-g81-t4-468) (the engineering-facing companion to this runbook).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `audit row not found` on a row you know exists | Row belongs to another tenant — the 404 is a deliberate cross-tenant probe defence. | Use the operator JWT for the tenant that owns the row. |
| `unrecognised duration '<value>': expected '<N>{s\|m\|h\|d\|w}' or an ISO-8601 datetime` | `--since` / `--until` shorthand failed to parse. | Use one of the grammars above (`24h`, `7d`, `2026-04-01T00:00:00Z`). |
| `parent_audit_id filter not supported in v0.2` | The column lands with G0.6-T7. | Drop the flag; if you need composite-op-tree traversal today, wait for [G8.2](https://github.com/evoila/meho/issues/377). |
| `cursor` returns 400 | The cursor was tampered with, truncated, or copy-pasted across queries with incompatible filter shapes. | Re-run the query without `--cursor` to get a fresh page-1 cursor. |
| Empty result on a filter you know should match | Filter ANDs with the tenant boundary first. A row in another tenant won't appear under any filter. Also check `--since` — the default is **no** time bound for `meho audit query`, but each pre-canned shortcut defaults `since=24h`. | Widen `--since` (`--since 7d`); confirm the tenant matches; if `--op-id` is involved, double-check the glob (literal segment boundaries matter — `vsphere.vm.*` does not match `vsphere.vmware.foo`). |

## Related

- [`docs/architecture/audit.md`](../architecture/audit.md) — canonical architecture reference for the audit-query module (substrate, surfaces, decision #3 alignment).
- [`docs/codebase/audit_query.md`](../codebase/audit_query.md) — engineering-facing internal doc covering the schemas, the cursor format, control flow, and known v0.2 gaps.
- [Initiative #334 (G8.1)](https://github.com/evoila/meho/issues/334) — the issues that delivered the surface (substrate, REST, CLI, MCP, acceptance, docs).
- [Initiative #377 (G8.2)](https://github.com/evoila/meho/issues/377) — the audit replay sibling Initiative (`meho audit replay <session-id>`) that builds on the `parent_audit_id` filter shipped here.
- [CLAUDE.md](../../CLAUDE.md) postulate 5 (narrow-waist agent surface) — the rule that collapses the per-shape MCP tools into a single `query_audit`.
