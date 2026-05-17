<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Vault op surface onboarding — operator recipe

> Operator-facing recipe for the G3.3 `vault-1.x` op surface — the
> `meho vault kv/sys/auth …` verb tree, the agent meta-tool path, and
> the migration off the consumer's `_secret-read.sh` / `vault.sh`
> wrappers. The op handlers live in
> [`backend/src/meho_backplane/connectors/vault/`](../../backend/src/meho_backplane/connectors/vault/);
> the engineering-facing companion is
> [`docs/codebase/connectors-vault.md`](../codebase/connectors-vault.md).
> This doc is the cookbook every RDC operator reads when retiring the
> bash wrappers in favour of `meho vault …`.

This is **not** [`vault-provisioning.md`](./vault-provisioning.md).
That page is the federation-chain setup (JWT auth method, `meho-mcp`
role, policy, KV mount, federation-proof fixture) the backplane needs
to reach Vault at all. This page assumes that chain is already green
and is about the **operator op surface** layered on top of it.

## What this surface is

The `vault-1.x` connector is a **typed** connector: hand-coded handlers
against the `hvac` SDK, registered into the G0.6 `endpoint_descriptor`
table at backplane startup. It dispatches under the
`(product="vault", version="1.x", impl_id="vault")` registry triple —
the connector id `vault-1.x`.

The v0.2 op surface (Initiative
[#366](https://github.com/evoila/meho/issues/366)) is the working set
the consumer's wrappers exercise daily:

| Group | Ops | Class |
| --- | --- | --- |
| `kv` | `vault.kv.read`, `vault.kv.list`, `vault.kv.put`, `vault.kv.versions`, `vault.kv.delete` | reads + 2 writes |
| `sys` | `vault.sys.health`, `vault.sys.seal_status`, `vault.sys.mounts.list`, `vault.sys.auth.list` | read-only diagnostics |
| `auth` | `vault.auth.userpass.list`, `vault.auth.userpass.read`, `vault.auth.approle.list`, `vault.auth.approle.read` | read-only identity browse |

Every op dispatches through the same `POST /api/v1/operations/call`
route the agent surface uses — auth, policy, audit, broadcast, and
JSONFlux all run as documented in [CLAUDE.md](../../CLAUDE.md) §6. The
CLI verb tree is operator ergonomics over that one route; it is **not**
a separate data path and is **not** mirrored on the MCP surface
(CLAUDE.md postulate 5).

## Prerequisites

- **A registered Vault target.** The CLI verbs take `--target <slug>`
  (e.g. `--target rdc-vault`); the slug resolves server-side to a row
  in the `targets` table. The target carries `product="vault"`,
  `host`, `secret_ref` (nullable), and `auth_model`.
- **`auth_model = shared_service_account`** — the DB-column default and
  the model the shipped connector implements. See the next section for
  what this means for tokens and rotation; it is **not** the
  long-lived-token-in-`secret_ref` shape the bash wrappers used.
- **An operator session.** `meho login <backplane-url>` writes the
  session token the CLI reuses across every verb. `meho vault …` needs
  `operator` role minimum (same gate as every dispatch verb);
  `read_only` callers get HTTP 403 on the write ops.
- **The federation chain is green.** `meho vault sys health
  --target rdc-vault` is the fastest end-to-end smoke; if it fails,
  fix the chain via [`vault-provisioning.md`](./vault-provisioning.md)
  before reading further here.

## Target + auth model

The shipped connector's auth model is **`shared_service_account` via
OIDC JWT forwarding**, not a long-lived token stored in `secret_ref`:

- Every operator's Keycloak JWT (minted by `meho login`) is forwarded
  to Vault's JWT/OIDC auth method, bound to the `meho-mcp` role
  (`vault-provisioning.md` surfaces 1–3).
- The Vault token Vault mints in exchange is **per-request** and
  revoked on context exit. No client token is persisted by the
  backplane; the connector never holds a standing Vault credential.
- The one exception is `vault.sys.health`, which Vault serves
  unauthenticated (it works against a sealed Vault).

What this means for the `secret_ref` / token-rotation question in the
Initiative framing:

- The target's `secret_ref` column is **unused by the shipped
  `shared_service_account` Vault connector** — there is no operator-
  managed long-lived Vault token for MEHO to rotate. The credential of
  record is the operator's Keycloak identity; rotation is Keycloak
  token-lifetime + the `meho-mcp` role's `token_ttl`, both
  operator/realm config, not a `secret_ref` value.
- This is the deliberate replacement for the bash wrappers'
  `$HOME/.vault-token` pattern. `_secret-read.sh` / `vault.sh` read a
  token the operator populated with `vault login -method=userpass`
  once per session and managed by hand. Under MEHO that whole
  responsibility collapses into `meho login` against Keycloak: the
  per-request Vault token is minted and revoked by the federation
  chain, never written to disk, never the operator's to rotate.
- Per-operator userpass impersonation and approle secret-id generation
  (the write-side identity surfaces) are explicitly **future**
  (v0.2.next) — not shipped here.

If a future target ever needs the long-lived-token shape, that is the
`per_user` / `impersonation` `auth_model` and a separate Initiative;
v0.2 ships only `shared_service_account`.

## The CLI verb surface

Every verb pre-bakes `connector_id="vault-1.x"` so operators never
type the connector id. All verbs accept `--target <slug>` (required
for the ops that resolve a Vault target), `--json` (emit the full
`OperationResult` envelope for `jq`), and `--backplane <url>` (override
the URL from the last `meho login`). Exit codes mirror
`meho operation call`.

### KV-v2 — `meho vault kv …`

`<mount>` is the KV-v2 engine mount (e.g. `secret`); `<path>` is the
secret path relative to the mount root — **no leading slash, no mount
prefix, no `/data/` or `/metadata/` infix**. The connector handles the
KV-v2 `data/` vs `metadata/` split internally (the bash wrappers
hand-built `/v1/<mount>/data/<rest>`; you no longer do that).

```console
$ meho vault kv read    --target rdc-vault secret meho/test/federation
$ meho vault kv read    --target rdc-vault secret app/db --json | jq .result.data
$ meho vault kv list    --target rdc-vault secret meho
$ meho vault kv versions --target rdc-vault secret app/db
$ meho vault kv put     --target rdc-vault secret app/db --data '{"password":"s3cr3t"}'
$ meho vault kv put     --target rdc-vault secret app/db --data @secret.json --cas 3
$ meho vault kv delete  --target rdc-vault secret app/db --versions 3,4,5
```

| Verb | op_id | Notes |
| --- | --- | --- |
| `kv read <mount> <path>` | `vault.kv.read` | Latest version. Result: `{data: {...}, version: <int>}`. `safety_level=safe`, classified `credential_read`. |
| `kv list <mount> <path>` | `vault.kv.list` | Child key names only (never values). Set-shaped — see the JSONFlux section. `credential_read`. |
| `kv put <mount> <path> --data <json> [--cas N]` | `vault.kv.put` | New version (wholesale replace, no merge). `--data` is inline JSON or `@<file>`. `--cas 0` asserts must-not-exist; `--cas N` asserts current version `== N`. `safety_level=caution`, `op_class=write`. |
| `kv versions <mount> <path>` | `vault.kv.versions` | Version metadata (created/deleted/destroyed timestamps). Read-only metadata browse — **not** `credential_read` (no secret values). |
| `kv delete <mount> <path> --versions <N,M>` | `vault.kv.delete` | Soft-delete (reversible — Vault retains the data). `--versions` is a comma-separated int list. `safety_level=dangerous`, `op_class=write`. |

`--cas` distinguishes "explicitly `--cas 0`" (a real must-not-exist
assertion) from "flag absent" — pass `--cas 0` only when you mean it.

`requires_approval` is registered `false` for `kv.put` / `kv.delete`
in v0.2 (the shipped G0.6 substrate has no per-path approval
predicate). `safety_level` (`caution` / `dangerous`) is the
load-bearing signal the future G7/G10 production-path approval gate
keys on.

### System diagnostics — `meho vault sys …`

No args, no params, all read-only:

```console
$ meho vault sys health      --target rdc-vault
$ meho vault sys seal-status --target rdc-vault
$ meho vault sys mounts-list --target rdc-vault
$ meho vault sys auth-list   --target rdc-vault
```

| Verb | op_id | Result |
| --- | --- | --- |
| `sys health` | `vault.sys.health` | `{ok: <bool>, detail: <str>}` + raw `sys/health` fields. Unauthenticated upstream — works against a sealed Vault. |
| `sys seal-status` | `vault.sys.seal_status` | `{sealed, initialized, ...}`. |
| `sys mounts-list` | `vault.sys.mounts.list` | Enabled secret-engine mount map. |
| `sys auth-list` | `vault.sys.auth.list` | Enabled auth-method map. |

Note the verb spellings hyphenate (`seal-status`, `mounts-list`,
`auth-list`) while the op_ids use dots/underscores
(`vault.sys.seal_status`, `vault.sys.mounts.list`,
`vault.sys.auth.list`).

### Identity browse — `meho vault auth …`

Read-only. The `read` verbs take a single positional identifier:

```console
$ meho vault auth userpass-list                  --target rdc-vault
$ meho vault auth userpass-read --target rdc-vault svc-deploy
$ meho vault auth approle-list                   --target rdc-vault
$ meho vault auth approle-read  --target rdc-vault ci-runner
```

| Verb | op_id | Result |
| --- | --- | --- |
| `auth userpass-list` | `vault.auth.userpass.list` | Userpass roster (set-shaped — JSONFlux applies). |
| `auth userpass-read <user>` | `vault.auth.userpass.read` | One user's policies + token TTLs. |
| `auth approle-list` | `vault.auth.approle.list` | AppRole role-name roster. |
| `auth approle-read <role>` | `vault.auth.approle.read` | One role's policies + token/secret-id TTLs. |

AppRole **secret-id generation** is deliberately out of scope for v0.2
(high-risk write with policy implications; deferred to v0.2.next behind
a policy gate). These four verbs are read-only.

## The agent meta-tool path

Agents never see `meho vault …` — those are operator-only CLI
ergonomics. Per [CLAUDE.md](../../CLAUDE.md) postulate 5, an agent
reaches every Vault op through the narrow-waist meta-tools:

```text
search_connectors(query="vault secrets")        → finds vault-1.x
list_operation_groups(connector_id="vault-1.x") → kv / sys / auth
search_operations(connector_id="vault-1.x", query="read a secret", group="kv")
call_operation(connector_id="vault-1.x",
               operation_id="vault.kv.read",
               target={"name": "rdc-vault"},
               params={"mount": "secret", "path": "app/db"})
```

The agent's flow is always: pick connector → list operation groups →
search operations (optionally scoped to a group) → `call_operation`.
The CLI verb table above and the `call_operation` params are
1:1 — `meho vault kv read --target rdc-vault secret app/db` and the
`call_operation` call above dispatch the identical route, audit row,
and broadcast event. Each op's `llm_instructions` payload (registered
at `register_typed_operation()` time) is what `search_operations`
surfaces to rank and guide the agent; it is reviewable in
[`backend/src/meho_backplane/connectors/vault/ops.py`](../../backend/src/meho_backplane/connectors/vault/ops.py)
(KV) / `ops_sys.py` / `ops_auth.py`.

## JSONFlux handle behaviour for `vault.kv.list`

`vault.kv.list` is the only set-shaped op on the v0.2 Vault surface (it
returns `{"keys": [...]}`; every other op returns a bounded scalar or a
single-secret dict). Per v0.1-spec §4 / CLAUDE.md postulate 6, a set
larger than the JSONFlux threshold (~50 rows / 4 KB) must come back as
a sample + result handle, never the raw list.

The wrapping is the **dispatcher's** job, not the handler's: the
handler returns `{"keys": [...]}` verbatim and `dispatch` passes it
through the configured `Reducer` before audit/broadcast.

**v0.2 ships only `PassThroughReducer`, so the v0.2 default is
pass-through** — `vault.kv.list` returns the full inline key list with
no handle, regardless of key count. The threshold-aware reducer (and
the `result_query` / `result_aggregate` / `result_describe` /
`result_export` meta-tools that drill into a handle) ship in a
follow-on Initiative; swapping it in touches one `set_default_reducer`
call, not the Vault handler. `tests/test_vault_kv_list_jsonflux.py`
(G3.3-T4 #548/#566) pins both halves of the contract: ≤50 keys stays
inline with no handle (shipped default), and — with a threshold-aware
reducer installed via the test seam — >50 keys produces a `sample` +
`ResultHandle`. Operationally: in v0.2 expect the full key list inline;
when the real reducer lands, large `meho vault kv list` results return
a handle and you drill in with the `meho operation` result verbs
exactly as for any other connector's set-shaped op.

## The `credential_read` PII guarantee

`vault.kv.read` and `vault.kv.list` are the canonical
`credential_read` ops (locked [decision
#3](../planning/v0.2-decisions.md)). The broadcast publisher's
sensitivity classifier maps them via an explicit allowlist —
`{vault.kv.read, vault.kv.list}` in
[`backend/src/meho_backplane/broadcast/events.py`](../../backend/src/meho_backplane/broadcast/events.py)
— **not** by `vault.kv.` prefix (a future `vault.kv.stats` that reads
no secret content must not over-match).

For those two ops the broadcast event payload is **aggregate-only**:

```json
{"op_class": "credential_read", "result_status": "ok"}
```

No mount, no path, no key names, no values. The mere fact that an
operator read a credential is broadcast on the per-tenant feed; the
*what* never leaves the audit row. A `vault.kv.read` broadcast event
carrying a `path` field would be a privacy regression — the broadcast
publisher test negative-asserts this.

Sensitivity-class boundaries on the Vault surface, so operators know
exactly what the live feed shows:

| op_id | Broadcast class | Why |
| --- | --- | --- |
| `vault.kv.read` | `credential_read` (aggregate-only) | Reads secret values. |
| `vault.kv.list` | `credential_read` (aggregate-only) | Key names leak structure. |
| `vault.kv.versions` | `read` (full detail) | Metadata only, no secret values. |
| `vault.kv.put` | `write` (full detail) | `.put` write suffix — full params, including the written secret, are NOT aggregate-only. Operators must treat `kv.put` params as sensitive on the feed. |
| `vault.kv.delete` | `write` (full detail) | `.delete` write suffix. |
| `vault.sys.health` / `vault.sys.seal_status` | `read` (full detail) | Cluster-state diagnostics, no secret content. |
| `vault.auth.*.list` / `.read` | `read` (full detail) | Identity config, no secret values. |

The full audit row is always durable and queryable (via the G8 audit
surface) by anyone with the appropriate role on the tenant —
`credential_read` only governs the *live broadcast feed*, not the
audit log. Per-tenant opt-in to flip `credential_read` to full-detail
is a future G6.3-class follow-up; v0.2 ships the conservative default.

> **`kv.put` caveat.** `vault.kv.put` broadcasts at `op_class=write`
> with **full params** — the written secret body is visible on the
> per-tenant feed and in the audit row. This is intentional (writes
> are full-detail per decision #3) but means a `kv.put` is not
> credential-redacted the way a `kv.read` is. Treat `meho vault kv put`
> as a feed-visible operation.

## Migrating off the bash wrappers

The consumer's [`scripts/_secret-read.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/_secret-read.sh)
(sourced KV-read helper) and
[`scripts/vault.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/vault.sh)
(generic Vault HTTP wrapper) retire as follows. The wrappers built
raw `/v1/<mount>/data/<rest>` paths and read `$HOME/.vault-token` by
hand; the `meho vault` verbs take `<mount> <path>` and use the
`meho login` Keycloak session — no token file, no `/data/` infix.

| Wrapper invocation | `meho vault …` replacement | Notes |
| --- | --- | --- |
| `source _secret-read.sh; secret_read secret/rdc-hetzner-dc/<area>/<item> <field>` | `meho vault kv read --target rdc-vault secret rdc-hetzner-dc/<area>/<item> --json \| jq -r '.result.data.<field>'` | Wrapper echoes one field; `kv read` returns the whole `data` dict — pick the field with `jq`. Drop the `secret/` mount prefix into the positional `<mount>` arg. |
| `vault.sh --target rdc-vault GET /v1/sys/health` | `meho vault sys health --target rdc-vault` | |
| `vault.sh --target rdc-vault --probe` | `meho vault sys seal-status --target rdc-vault` (+ `sys health`) | The wrapper's `--probe` combined unauthenticated `/sys/health` + `/sys/seal-status`; `seal-status` carries the richer detail (threshold, shares, seal type). |
| `vault.sh --target rdc-vault GET /v1/sys/mounts` | `meho vault sys mounts-list --target rdc-vault` | |
| `vault.sh --target rdc-vault GET /v1/sys/auth` | `meho vault sys auth-list --target rdc-vault` | |
| `vault.sh --target rdc-vault GET /v1/auth/userpass/users` | `meho vault auth userpass-list --target rdc-vault` | |
| `vault.sh --target rdc-vault GET /v1/auth/userpass/users/<u>` | `meho vault auth userpass-read --target rdc-vault <u>` | |
| `vault.sh --target rdc-vault GET /v1/auth/approle/role` | `meho vault auth approle-list --target rdc-vault` | |
| `vault.sh --target rdc-vault GET /v1/auth/approle/role/<r>` | `meho vault auth approle-read --target rdc-vault <r>` | |
| `vault.sh --target rdc-vault GET /v1/<mount>/metadata/<path>?list=true` | `meho vault kv list --target rdc-vault <mount> <path>` | |
| `vault.sh --target rdc-vault PUT /v1/<mount>/data/<path> -d '{"data":{…}}'` | `meho vault kv put --target rdc-vault <mount> <path> --data '{…}'` | `kv put` takes the secret body directly — no `{"data": …}` envelope; the connector wraps it. |
| `vault.sh --target rdc-vault GET /v1/<mount>/metadata/<path>` | `meho vault kv versions --target rdc-vault <mount> <path>` | Version metadata browse. |
| `vault.sh --target rdc-vault DELETE /v1/<mount>/data/<path>` (latest) | `meho vault kv delete --target rdc-vault <mount> <path> --versions <N>` | The wrapper's bare DELETE soft-deleted the latest version; the op is explicit about which versions — read `kv versions` first to pick `<N>`. |

What the wrappers did that `meho vault` deliberately does **not** do
(out of scope for v0.2 — keep the wrapper for these until a future
Initiative lands them):

- `vault.sh POST /v1/auth/userpass/users/<u>` and other identity
  **writes** — v0.2 auth surface is read-only.
- AppRole **secret-id generation** — deferred to v0.2.next behind a
  policy gate.
- Secret-engine writes beyond KV-v2 (database, PKI, transit), token /
  lease / policy management, cubbyhole, response-wrapping, KV-v1 —
  all explicitly out of scope.
- The wrappers' `--host <fqdn>` registry-less mode — `meho vault`
  always resolves a registered `--target`.

Migration discipline: run the `meho vault` form alongside the wrapper
for an overlap window, diff the outputs, then retire the wrapper call
site. The MEHO path adds the full audit row + broadcast event the bash
pattern never had — that audit coverage is the point of migrating.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `no backplane URL configured` (exit 2) | Never logged in / no `--backplane`. | `meho login <url>` or pass `--backplane <url>`. |
| `auth_expired` / stored token rejected | Keycloak token expired; refresh failed. | `meho login <url>` again. |
| `status=error … operation not found` | op_id drift or the typed-op registrar didn't run. | Verify the backplane started cleanly; check the connector registered (`meho connector list` shows `vault-1.x`). |
| `status=denied` on `kv put` / `kv delete` | `read_only` role, or a policy gate denied the write. | Use an `operator`-role token; confirm the Vault policy grants write on the path. |
| `kv read` returns 404 / "InvalidPath" | Wrong `<mount>`/`<path>`, or you passed a `secret/` prefix / `/data/` infix in `<path>`. | `<mount>` is the engine mount alone; `<path>` is mount-relative with no `/data/`. `meho vault kv list <mount> <parent>` to confirm the path. |
| `meho vault sys health` fails | The federation chain (not this surface) is broken. | Diagnose via [`vault-provisioning.md`](./vault-provisioning.md) failure-modes table. |
| A `vault.kv.read` appears on the feed with a `path` | Privacy regression — must never happen. | File a bug; the classifier allowlist + redaction contract is load-bearing (decision #3). |

## References

- Initiative: [#366 G3.3 `vault-1.x` typed op surface](https://github.com/evoila/meho/issues/366); Goal [#214](https://github.com/evoila/meho/issues/214) (G3 connector parity).
- Tasks that shipped this surface: [#545](https://github.com/evoila/meho/issues/545) (KV-v2 ops), [#546](https://github.com/evoila/meho/issues/546) (sys ops), [#547](https://github.com/evoila/meho/issues/547) (auth ops), [#550](https://github.com/evoila/meho/issues/550) (CLI verbs).
- Engineering companion: [`docs/codebase/connectors-vault.md`](../codebase/connectors-vault.md).
- Federation-chain setup (prerequisite): [`vault-provisioning.md`](./vault-provisioning.md).
- PII default (decision #3): [`docs/planning/v0.2-decisions.md`](../planning/v0.2-decisions.md); classifier [`backend/src/meho_backplane/broadcast/events.py`](../../backend/src/meho_backplane/broadcast/events.py).
- Broadcast feed onboarding: [`broadcast-onboarding.md`](./broadcast-onboarding.md). Audit query: [`audit-query.md`](./audit-query.md).
- Op handlers: [`backend/src/meho_backplane/connectors/vault/`](../../backend/src/meho_backplane/connectors/vault/) (`ops.py` KV, `ops_sys.py`, `ops_auth.py`). CLI verbs: [`cli/internal/cmd/vault/`](../../cli/internal/cmd/vault/).
- Consumer wrappers retired: [`scripts/_secret-read.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/_secret-read.sh), [`scripts/vault.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/vault.sh).
- Vault HTTP API: <https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2> (KV-v2), <https://developer.hashicorp.com/vault/api-docs/system> (sys), <https://developer.hashicorp.com/vault/api-docs/auth/userpass> (userpass), <https://developer.hashicorp.com/vault/api-docs/auth/approle> (approle).
