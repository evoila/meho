<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Broadcast detail overrides — operator + admin recipe

> Operator-facing recipe for the two MEHO surfaces that flip the
> activity broadcast's PII discipline: the per-call
> `X-Broadcast-Detail` header (any operator, per request) and the
> durable `BroadcastOverride` rules (tenant admin, per tenant).
> Implementation: Initiative
> [#376](https://github.com/evoila/meho/issues/376); SSE / Slack /
> MCP subscriber side is covered by
> [`broadcast-onboarding.md`](./broadcast-onboarding.md).

## What this is

G6.1 ([#228](https://github.com/evoila/meho/issues/228)) ships an
aggregate-only-by-default broadcast classifier for two op classes:
`credential_read` (Vault KV reads + lists) and `audit_query`
(everything under the `audit.*` op namespace). The default is
deliberately conservative — a full-detail credential-read event
leaks the path operators were touching; a full-detail audit-query
event leaks the filter they were investigating and the rows it
returned. The decision lives in
[`docs/decisions/locked-decisions.md`](../decisions/locked-decisions.md)
under decision #3.

G6.3 ([#376](https://github.com/evoila/meho/issues/376)) ships **two
narrow escape valves** so the conservative default doesn't kill the
team-coordination signal:

1. **Per-call opt-in.** An on-call operator who *wants* colleagues
   to see which Vault paths they're touching during an incident
   adds `X-Broadcast-Detail: full` to one request — the broadcast
   event for that one call upgrades to full detail. Opt-in only:
   `aggregate` as a header value is silently ignored (you cannot
   weaken policy with a header).
2. **Tenant-convention opt-out.** A tenant admin configures a
   durable rule: "every `k8s.configmap.info` call against the
   `kube-system` namespace broadcasts aggregate-only" — and from
   that point every operator's call against `kube-system` collapses
   to the safe shape. No tenant boundary is crossed; other tenants
   are unaffected.

A third forensic discipline ships alongside: every broadcast
decision tags the audit row with `broadcast_detail_origin`
(`request_override` / `tenant_rule:<id>` / `default`) plus
`broadcast_detail_effective` (`full` / `aggregate`), so the G8.1
audit-query API
([`audit-query.md`](./audit-query.md), Initiative
[#465](https://github.com/evoila/meho/issues/465)) can answer
"who flipped this credential read to full, and which rule applied".

## When to opt-in via header

**Scenario:** credential-rotation incident on a production Vault
mount. The on-call operator is rotating five secrets across two
applications; colleagues need real-time visibility into *which*
paths are being touched so nobody steps on the in-flight work or
double-rotates a secret.

The `meho` CLI does not yet expose a per-call broadcast-detail
flag (tracked as a future enhancement). Until that lands, attach
the `X-Broadcast-Detail: full` header directly to the chassis
operations dispatcher (`POST /api/v1/operations/call`) via `curl`
or any programmatic HTTP client. Connector ops -- vault, k8s,
vsphere, bind9 -- all reach the backend through this one route;
there is no dedicated per-connector REST surface to target:

```console
$ curl -sS -X POST https://meho.example.com/api/v1/operations/call \
    -H "Authorization: Bearer $MEHO_TOKEN" \
    -H "Content-Type: application/json" \
    -H "X-Broadcast-Detail: full" \
    -d '{
      "connector_id": "vault",
      "op_id": "vault.kv.list",
      "target": {"name": "rdc-vault"},
      "params": {"path": "secret/prod/svc-payments"}
    }'

$ curl -sS -X POST https://meho.example.com/api/v1/operations/call \
    -H "Authorization: Bearer $MEHO_TOKEN" \
    -H "Content-Type: application/json" \
    -H "X-Broadcast-Detail: full" \
    -d '{
      "connector_id": "vault",
      "op_id": "vault.kv.read",
      "target": {"name": "rdc-vault"},
      "params": {"path": "secret/prod/svc-payments/db-password"}
    }'
```

The SSE feed for the tenant now shows:

```text
[14:23:01] ok       op-alice   vault.kv.list   path=secret/prod/svc-payments
[14:23:04] ok       op-alice   vault.kv.read   path=secret/prod/svc-payments/db-password
```

instead of the default aggregate-only shape:

```text
[14:23:01] ok       op-alice   vault.kv.list   (credential read — aggregate-only)
[14:23:04] ok       op-alice   vault.kv.read   (credential read — aggregate-only)
```

The audit row records the operator-side decision verbatim:

```jsonc
// SELECT payload FROM audit_log WHERE id = '<row-id>';
{
  "op_id": "vault.kv.read",
  "op_class": "credential_read",
  "broadcast_detail_origin": "request_override",  // ← header honoured
  "broadcast_detail_effective": "full"
}
```

Header rules (load-bearing):

* **Only `full` is honoured.** `X-Broadcast-Detail: aggregate` is
  parsed, logged at info under `broadcast_detail_invalid_header`,
  and dropped. The opt-in surface cannot weaken a default; durable
  downgrade is the tenant-rule surface below.
* **Case-insensitive.** `Full`, `FULL`, `full` all work — the
  middleware lower-cases the value.
* **Only flips sensitive classes.** A `full` header on a
  `vsphere.vm.list` (which already broadcasts full) is a no-op; the
  audit row keeps `broadcast_detail_origin = "default"`. Sensitive
  classes are `credential_read` + `audit_query` per decision #3.
* **MCP equivalent.** The MCP transport equivalent is
  `_meta.broadcast_detail = "full"` in the `tools/call` params per
  the MCP `_meta` envelope spec
  (<https://modelcontextprotocol.io/specification/2025-06-18/basic/utilities/_meta>).

## When to configure a tenant rule

Two recurring shapes the consumer-needs doc explicitly calls out:

1. **Lab-private VM names.** A tenant's vSphere inventory uses
   target names that encode customer identifiers. Every
   `vsphere.vm.list` broadcasts the full set of targets — leaking
   the customer roster to anyone with feed access. The fix: a
   tenant-wide rule on `vsphere.vm.*` downgrading to aggregate-only.
2. **`kube-system` configmaps.** A K8s configmap's `data` field can
   carry production secrets the consumer hasn't yet migrated to
   Vault (the
   [#324](https://github.com/evoila/meho/issues/324) call-out).
   The fix: a namespace-scoped rule on `k8s.configmap.info` matching
   `namespace = kube-system`.

Glob `op_id_pattern` matching uses
[`fnmatch.fnmatchcase`](https://docs.python.org/3/library/fnmatch.html#fnmatch.fnmatchcase)
— literal characters plus `*` only. Regex characters (`[`, `(`,
`\`, `+`, `?`, `|`, `^`, `$`) are rejected at the API layer with
422. The resolver applies most-specific-wins (a scoped rule beats
an op-wide rule); ties between equally-specific rules break on the
rule UUID's lexicographic order.

## Operator-side recipes

The operator-facing surface for the per-call upgrade is the
`X-Broadcast-Detail: full` HTTP header (or, on the MCP transport,
the `_meta.broadcast_detail = "full"` field on `tools/call` per the
[MCP `_meta` envelope spec](https://modelcontextprotocol.io/specification/2025-06-18/basic/utilities/_meta)).
The `meho` CLI does not yet thread either through as a flag —
adding one is tracked as a future enhancement. Today operators
attach the header directly from `curl` or any programmatic client
when they need the upgrade:

```console
# Audit-query against a backplane, asking for full detail on the broadcast event.
# This route DOES exist as a dedicated REST surface
# (backend/src/meho_backplane/api/v1/audit.py).
$ curl -sS -X POST https://meho.example.com/api/v1/audit/query \
    -H "Authorization: Bearer $MEHO_TOKEN" \
    -H "Content-Type: application/json" \
    -H "X-Broadcast-Detail: full" \
    -d '{"since": "24h", "limit": 50}'

# Vault KV read with full-detail opt-in -- dispatched through the
# generic operations route. No /api/v1/vault/* surface exists; the
# dispatcher resolves the connector + target + op id from the body.
$ curl -sS -X POST https://meho.example.com/api/v1/operations/call \
    -H "Authorization: Bearer $MEHO_TOKEN" \
    -H "Content-Type: application/json" \
    -H "X-Broadcast-Detail: full" \
    -d '{
      "connector_id": "vault",
      "op_id": "vault.kv.read",
      "target": {"name": "rdc-vault"},
      "params": {"path": "secret/prod/svc-payments/db-password"}
    }'
```

Verifying the header landed: after the call, the row's audit
payload carries `broadcast_detail_origin = "request_override"`
(see [Forensics](#forensics) below) and the published broadcast
event payload includes the `params` field instead of the default
aggregate-only `{op_class, result_status}` shape.

## Admin-side recipes — CLI, REST, MCP

Tenant-admin role required (`TenantRole.TENANT_ADMIN` claim on the
Keycloak JWT). Operators and read-only tokens get 403
`insufficient_role` from every CRUD verb on every transport.

### Create a downgrade rule (op-wide)

CLI:

```console
$ meho broadcast overrides set \
    --op-id-pattern 'vsphere.vm.*' \
    --detail aggregate
```

REST:

```console
$ curl -sS -X POST https://meho.example.com/api/v1/broadcast/overrides \
    -H "Authorization: Bearer $MEHO_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"op_id_pattern":"vsphere.vm.*","detail":"aggregate"}'
```

MCP (admin meta-tool, `tenant_admin` namespace):

```jsonc
// JSON-RPC POST to /mcp
{
  "jsonrpc": "2.0", "id": 1,
  "method": "tools/call",
  "params": {
    "name": "meho.broadcast.overrides.set",
    "arguments": {
      "op_id_pattern": "vsphere.vm.*",
      "detail": "aggregate"
    }
  }
}
```

The response shape is symmetric with `meho.broadcast.overrides.remove`
(v0.3.2 [G0.9.1-T7 #779](https://github.com/evoila/meho/issues/779)) —
the new rule's id is exposed at top-level `override_id` (matching the
`remove` arg shape) **and** nested under `override.id` (preserved so
v0.3.1 clients reading `.override.id` keep working):

```jsonc
{
  "override_id": "3a7e0c8c-7b3e-49cb-9b5d-9c1f5a2e3d4e",
  "override": {
    "id": "3a7e0c8c-7b3e-49cb-9b5d-9c1f5a2e3d4e",
    "tenant_id": "...",
    "op_id_pattern": "vsphere.vm.*",
    "scope_field": null,
    "scope_value": null,
    "detail": "aggregate",
    "created_by_sub": "op-admin",
    "created_at": "...",
    "updated_at": "..."
  }
}
```

An agent can therefore round-trip `set → remove` reading only
`result.override_id` from the `.set` response.

### Create a downgrade rule (scoped to one namespace / target)

CLI:

```console
$ meho broadcast overrides set \
    --op-id-pattern k8s.configmap.info \
    --scope-field namespace \
    --scope-value kube-system \
    --detail aggregate
```

`scope_field` is one of `namespace` or `target_name` (the v0.2
allowlist). The pair must be set together or omitted together — a
half-set pair is a 422.

### List the tenant's rules

CLI (default — human table):

```console
$ meho broadcast overrides list
id                                    op_id_pattern                   scope_field   scope_value           detail     created_by
  ---
3a7e0c8c-7b3e-49cb-9b5d-9c1f5a2e3d4e  vsphere.vm.*                    -             -                     aggregate  op-admin
b9d34ec0-4a78-4f0e-93e0-31aa7d5d2a05  k8s.configmap.info              namespace     kube-system           aggregate  op-admin
```

CLI (JSON for machine consumption):

```console
$ meho broadcast overrides list --json
[
  {"id":"3a7e...","op_id_pattern":"vsphere.vm.*","detail":"aggregate",...},
  ...
]
```

REST:

```console
$ curl -sS https://meho.example.com/api/v1/broadcast/overrides \
    -H "Authorization: Bearer $MEHO_TOKEN" | jq .
```

MCP:

```jsonc
{
  "jsonrpc": "2.0", "id": 2,
  "method": "tools/call",
  "params": { "name": "meho.broadcast.overrides.list", "arguments": {} }
}
```

### Remove a rule

CLI:

```console
$ meho broadcast overrides remove 3a7e0c8c-7b3e-49cb-9b5d-9c1f5a2e3d4e
# silent on success (chassis convention); use --json to surface error envelopes
```

REST:

```console
$ curl -sS -X DELETE \
    https://meho.example.com/api/v1/broadcast/overrides/3a7e0c8c-7b3e-49cb-9b5d-9c1f5a2e3d4e \
    -H "Authorization: Bearer $MEHO_TOKEN"
```

MCP:

```jsonc
{
  "jsonrpc": "2.0", "id": 3,
  "method": "tools/call",
  "params": {
    "name": "meho.broadcast.overrides.remove",
    "arguments": { "override_id": "3a7e0c8c-7b3e-49cb-9b5d-9c1f5a2e3d4e" }
  }
}
```

## Forensics

Every broadcast decision lands in the audit row's `payload` JSON
under two keys:

* `broadcast_detail_origin` — one of `request_override`,
  `tenant_rule:<uuid>`, `default`. The UUID tail of the
  `tenant_rule` form is the row id; cross-reference against the
  `broadcast_override` table.
* `broadcast_detail_effective` — the effective `full` / `aggregate`
  the broadcast event was rendered with.

Plus, every CRUD mutation against a rule writes its own audit row
with `op_id = meho.broadcast.overrides.{list,set,remove}` and a
diff fragment in `payload`:

```jsonc
// payload for a "set" mutation
{
  "op_id": "meho.broadcast.overrides.set",
  "op_class": "write",
  "override_op": "set",
  "override_id": "<new-rule-uuid>",
  "override_pattern": "vsphere.vm.*",
  "override_detail": "aggregate"
}
```

Concrete query — "who upgraded a credential read to full detail
in the last 24 h":

```console
$ meho audit query --since 24h --op-class credential_read --json | \
    jq '.rows[] | select(.payload.broadcast_detail_origin == "request_override")'
```

See [`audit-query.md`](./audit-query.md) for the full query surface
(filter grammar, cursor pagination, role-gated `audit.*` tools).

## What you can't do

Out of scope for G6.3 — restated for operators so the boundaries
are explicit:

* **Per-channel Slack detail override.** Permanently removed
  2026-05-14 with the G6.2 ([#333](https://github.com/evoila/meho/issues/333))
  `NOT_PLANNED` closure. No first-class Slack code in MEHO.
* **Field-level masking.** A rule cannot redact only `path` while
  keeping `target` in a `vault.kv.read` payload — overrides are
  op-level. Field-level masking is a v0.2.next refinement.
* **Per-principal overrides.** Rules scope to tenant, not to
  individual operators. RBAC already handles operator-shaped
  policy.
* **Audit-log redaction.** The audit row stays the canonical record
  at full detail per chassis convention (audit is fail-closed +
  write-mostly); overrides only shape the broadcast view.
* **Cross-tenant rules.** Tenant boundaries hold per Goal
  [#217](https://github.com/evoila/meho/issues/217)
  ("Cross-tenant visibility ... explicitly disallowed"). Each
  tenant's rules apply only to that tenant; there is no admin role
  that crosses the boundary.
* **Regex patterns.** Glob (`*` + literals) only — regex is a
  configuration footgun this Initiative deliberately avoids.

## Verification commands

### List a tenant's rules (operator-side smoke)

```console
$ meho broadcast overrides list
```

A clean tenant with no rules:

```text
(no broadcast-detail overrides in this tenant)
```

### MCP inspector — one-liner against a running backplane

The
[modelcontextprotocol/inspector](https://github.com/modelcontextprotocol/inspector)
project supports a non-interactive CLI mode for tool discovery and
invocation. Two commands prove the admin tools are reachable +
gated:

```console
# Discover the admin tool surface (tenant_admin token required to
# see meho.broadcast.overrides.* in the list).
$ npx -y @modelcontextprotocol/inspector \
    --cli https://meho.example.com/mcp \
    --transport http \
    --header "Authorization: Bearer $MEHO_TOKEN" \
    --method tools/list | jq '.tools[].name' | grep broadcast.overrides

"meho.broadcast.overrides.list"
"meho.broadcast.overrides.set"
"meho.broadcast.overrides.remove"

# Call list — admin scope. Returns {overrides: [...]}.
$ npx -y @modelcontextprotocol/inspector \
    --cli https://meho.example.com/mcp \
    --transport http \
    --header "Authorization: Bearer $MEHO_TOKEN" \
    --method tools/call \
    --tool-name meho.broadcast.overrides.list
```

A non-admin token (`operator` / `read_only` claim) running the
same `tools/list` call sees only the agent-surface tools
(`broadcast_recent` / `_announce` / `_watch`) — the
`meho.broadcast.overrides.*` namespace is hidden by the
registry-filter layer and the dispatcher rejects a direct call
with JSON-RPC `-32602` `forbidden:`.

### Audit-query forensics — "who flipped this in the last hour"

```console
$ meho audit query --since 1h --json | \
    jq '.rows[] | {ts:.ts, op:.op_id, who:.principal_sub,
       origin:.payload.broadcast_detail_origin,
       effective:.payload.broadcast_detail_effective}' | \
    head -10
```

## References

* Initiative: [#376](https://github.com/evoila/meho/issues/376).
* T1 schema:
  [`backend/src/meho_backplane/db/models.py`](../../backend/src/meho_backplane/db/models.py)
  (`BroadcastOverride`); migration
  [`0008_create_broadcast_override.py`](../../backend/alembic/versions/0008_create_broadcast_override.py).
* T2 resolver:
  [`backend/src/meho_backplane/broadcast/overrides.py`](../../backend/src/meho_backplane/broadcast/overrides.py).
* T3 per-call middleware:
  [`backend/src/meho_backplane/middleware.py`](../../backend/src/meho_backplane/middleware.py)
  (`BroadcastDetailMiddleware`).
* T4 REST + CLI:
  [`backend/src/meho_backplane/api/v1/broadcast_overrides.py`](../../backend/src/meho_backplane/api/v1/broadcast_overrides.py)
  +
  [`cli/internal/cmd/broadcast/`](../../cli/internal/cmd/broadcast/).
* T5 admin MCP tools:
  [`backend/src/meho_backplane/mcp/tools/broadcast_overrides.py`](../../backend/src/meho_backplane/mcp/tools/broadcast_overrides.py).
* T6 E2E + load tests + this doc:
  [`backend/tests/integration/test_broadcast_overrides_e2e.py`](../../backend/tests/integration/test_broadcast_overrides_e2e.py),
  [`backend/tests/integration/test_broadcast_overrides_load.py`](../../backend/tests/integration/test_broadcast_overrides_load.py).
* G6.1 SSE-feed onboarding (subscriber side):
  [`broadcast-onboarding.md`](./broadcast-onboarding.md).
* G3.2-T4 forward-reference for the K8s configmap example:
  [#324](https://github.com/evoila/meho/issues/324).
* G8.1 audit-query forensics:
  [`audit-query.md`](./audit-query.md) /
  [#334](https://github.com/evoila/meho/issues/334).
* MCP `_meta` envelope spec (header-equivalent on MCP transport):
  <https://modelcontextprotocol.io/specification/2025-06-18/basic/utilities/_meta>.
* MCP Inspector CLI:
  <https://github.com/modelcontextprotocol/inspector>.
* Decision #3 (aggregate-only-by-default classifier):
  [`docs/decisions/locked-decisions.md`](../decisions/locked-decisions.md).
