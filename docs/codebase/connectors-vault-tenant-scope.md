<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Vault KV tenant-scope guard (defense-in-depth)

> Application-layer tenant binding on the agent-supplied `vault.kv.*`
> mount/path, in front of the hvac call. Companion to the deploy-side
> contract in
> [`docs/cross-repo/connector-vault-policy.md`](../cross-repo/connector-vault-policy.md).
> Implemented for [#1643](https://github.com/evoila/meho/issues/1643)
> (cross-tenant isolation hardening, Goal #221).

## The problem

The KV-v2 handlers in
[`connectors/vault/ops.py`](../../backend/src/meho_backplane/connectors/vault/ops.py)
forward the agent-supplied `mount` / `path` to hvac **verbatim**. The
intended access boundary is the Vault `meho-mcp` role's ACL policy, which
scopes each operator to their own identity segment via policy templating
(`connector-vault-policy.md` §2). That is the *primary* gate and it is
sound — **as long as the policy is provisioned correctly.**

The failure mode this guard defends against is an **over-broad Vault
policy**: if a deploy drops the templated identity segment, or a wildcard
leaks into the rendered output (`connector-vault-policy.md` §2's "no glob
inside the rendered segment" constraint is violated), the single shared
`meho-mcp` role would let a tenant-A caller read `tenant-b/...` secrets.
Nothing in the backplane would have stopped it — the path was passed
straight through.

## The guard

[`connectors/vault/tenant_scope.py`](../../backend/src/meho_backplane/connectors/vault/tenant_scope.py)
adds `enforce_tenant_scope(operator, mount=..., path=...)`, called by
**every** KV-v2 handler (`read`, `list`, `versions`, `put`, `patch`,
`delete`) immediately after it extracts `mount`/`path` and **before** the
`vault_client_for_operator(...)` login. On a violation it raises
`VaultTenantScopeError`; the dispatcher's `connector_error` branch wraps
it into a structured `OperationResult` with
`extras["exception_class"] == "VaultTenantScopeError"` — distinct from the
`VaultClientError` family (a real Vault-side 403) so callers can tell a
*local* tenant denial apart from a Vault denial. No Vault round-trip
happens on a denied call.

`operator.tenant_id` (a `UUID`) is already threaded into every handler —
the dispatcher passes the real `Operator` to typed handlers
(`operations/_branches.py` `dispatch_typed`). **No operator-threading
refactor was needed.**

## The namespace convention

The rule is configured by one setting,
[`vault_kv_tenant_scope_prefix`](../../backend/src/meho_backplane/settings.py)
(env `VAULT_KV_TENANT_SCOPE_PREFIX`):

- It is a Python `str.format` template carrying a single `{tenant_id}`
  placeholder, e.g. `tenant-{tenant_id}/` or `secret/tenant-{tenant_id}/`.
- At call time the operator's `tenant_id` UUID is rendered into the
  template (canonical dashed lowercase form, matching audit rows and JWT
  claims).
- The requested address is normalised to `<mount>/<path>` (stray slashes
  trimmed) and must **equal the rendered prefix or begin with
  `<prefix>/`** — a path-segment-boundary match, so a `tenant-1` prefix is
  *not* satisfied by a `tenant-12/...` path or a `tenant-1extra/...` path.
- Because the candidate includes the mount segment, the prefix can pin the
  **mount** (`secret/tenant-{tenant_id}/`) or just a **path** prefix
  (`tenant-{tenant_id}/`) — a deploy partitions tenants by whichever it
  uses.

## Why opt-in (empty default)

The guard is **disabled by default** (empty prefix → `enforce_tenant_scope`
is a no-op, behaviour is byte-for-byte pre-#1643). This is deliberate:

The shipped Vault layout scopes secrets **per operator `sub`**
(`secret/data/targets/<sub>/*`, `connector-vault-policy.md` §2), **not per
tenant**. There is no universal `tenant-<id>/` partition to enforce against
out of the box, so turning on a hard tenant prefix unconditionally would
deny every existing `vault.kv.*` call. A deploy whose KV layout *is*
tenant-partitioned opts in by setting the env var; a deploy that relies
solely on the per-`sub` Vault policy leaves it empty and is unaffected.

The **system/shim operator** (the Nil-UUID `tenant_id` the vault connector
synthesises in `connector.py`, empty `raw_jwt`) is exempt even when the
guard is enabled: its only callers run the unauthenticated
`vault.sys.health` op and forward no token to Vault, so there is no tenant
identity to bind against.

## Scope notes

- This is **defense-in-depth, not the primary gate.** The guard never
  grants access the Vault policy denies — it only ever denies *earlier*.
  Keep the `meho-mcp` policy correct (`connector-vault-policy.md` §2/§6);
  this guard is the backstop for the day it is mis-provisioned.
- The **park-time write-capability preflight**
  (`vault_kv_write_capability_preflight`, #1504) is intentionally *not*
  guarded here: it is a non-authoritative early signal that probes
  capability *names* (no secret material) and already fails soft. The
  authoritative tenant check lives in the write handlers (`put`/`patch`/
  `delete`), which a parked write re-dispatches through on approval.

## Tests

[`backend/tests/test_connectors_vault_tenant_scope.py`](../../backend/tests/test_connectors_vault_tenant_scope.py):
unit boundary cases on `enforce_tenant_scope` (in-namespace pass,
out-of-namespace deny, look-alike sibling prefix, cross-mount, disabled
default, system-tenant exemption) plus dispatch-level coverage that **every**
KV-v2 handler denies a cross-tenant path (`exception_class=VaultTenantScopeError`,
no Vault login) and allows an in-namespace path unchanged.
