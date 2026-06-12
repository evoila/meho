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

## Startup advisory (unenforced state is visible)

Because the guard is default-off, a deploy whose Vault layout *is*
tenant-partitioned could leave the prefix empty and never know the guard
is silently a no-op. To make that state visible,
[`main._advise_vault_tenant_scope_unenforced`](../../backend/src/meho_backplane/main.py)
runs in the FastAPI lifespan and emits **exactly one** structured advisory
at startup when `vault_kv_tenant_scope_prefix` is empty:

```
vault_tenant_scope_unenforced  enable_via=VAULT_KV_TENANT_SCOPE_PREFIX  doc=docs/codebase/connectors-vault-tenant-scope.md
```

It is **observability-only** — loud-but-non-fatal, like the embedding
preload advisory: no dispatch change, no raise, the default is not flipped.
A deploy that relies solely on the per-`sub` Vault policy can ignore the
line; a tenant-partitioned deploy treats it as the cue to set the prefix
(see "Choosing a layout"). Once the prefix is set the advisory is silent.
The behaviour is covered by
[`backend/tests/test_vault_tenant_scope_advisory.py`](../../backend/tests/test_vault_tenant_scope_advisory.py)
(fires unset, silent when set, never blocks boot).

## Choosing a layout

Which Vault KV layout you run determines whether this guard does anything.
It is a deploy/infra decision, **not** a backplane default — the backplane
ships the prefix empty (#1673 only surfaces and documents the choice; it
does not make it):

- **Per-`sub` layout (the shipped default).** Secrets live under
  `secret/data/targets/<sub>/*` and isolation is enforced entirely by the
  templated `meho-mcp` Vault policy (`connector-vault-policy.md` §2). There
  is no `tenant-<id>/` partition to bind against, so the prefix stays
  **empty** and the app-layer guard is intentionally a no-op. The startup
  advisory fires; for this layout it is expected and can be ignored. Keep
  the policy template correct — it is the primary (and only) gate here.

- **Tenant-partitioned layout (opt-in).** Secrets are physically
  partitioned by tenant — e.g. mount `secret/tenant-<tenant_id>/...` or a
  path prefix `tenant-<tenant_id>/...`. Here the app-layer guard becomes a
  real backstop for a mis-provisioned policy, so you **enable** it.

**Enabling the prefix requires all of:**

1. A Vault KV layout actually partitioned by `tenant_id` (mount or path) —
   the prefix only denies; it never relocates secrets.
2. Setting `VAULT_KV_TENANT_SCOPE_PREFIX` to the matching `str.format`
   template with a single `{tenant_id}` placeholder, e.g.
   `tenant-{tenant_id}/` (path prefix) or `secret/tenant-{tenant_id}/`
   (mount-pinned). The rendered `tenant_id` is the canonical dashed
   lowercase UUID (see "The namespace convention").
3. Confirming every legitimate `vault.kv.*` caller's secrets already live
   **under** that prefix — once set, any in-namespace mismatch is denied
   with `exception_class=VaultTenantScopeError` *before* the hvac call.
   The system/shim operator (Nil-UUID tenant, `vault.sys.health` only) is
   exempt, so enabling the prefix does not break the health probe.

Because step 3 denies every per-`sub` call that is *not* under a
`tenant-<id>/` prefix, flipping this on against the shipped per-`sub`
layout would break dispatch — which is exactly why the default is empty
and the switch is left to the operator.

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
