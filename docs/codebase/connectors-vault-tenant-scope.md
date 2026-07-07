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
adds `enforce_tenant_scope(operator, mount=..., path=..., read_only=...)`,
called by **every** KV-v2 handler (`read`, `list`, `versions`, `put`,
`patch`, `delete`) immediately after it extracts `mount`/`path` and
**before** the `vault_client_for_operator(...)` login. `read_only` is
`True` for the read/list/versions handlers and `False` for
put/patch/delete; it gates the platform-path exemption (below) to
read-only verbs. On a violation it raises
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

## The canonical layout is per-tenant (#1723)

As of [#1723](https://github.com/evoila/meho/issues/1723) (Goal #221,
Initiative #1685) the **canonical** KV layout for target secrets is
**per-tenant shared**:

```text
secret/data/tenants/<tenant_id>/<target>
```

`<tenant_id>` is the canonical dashed lowercase UUID — the exact rendering
`rendered_tenant_prefix` produces. New targets land on this path
automatically:
[`connectors/vault/tenant_paths.py`](../../backend/src/meho_backplane/connectors/vault/tenant_paths.py)'s
`tenant_secret_ref(tenant_id, target)` derives
`tenants/<tenant_id>/<target>`, and `api/v1/targets.py`'s `create_target`
(no explicit `secret_ref`) and `update_target` (PATCH not touching
`secret_ref` on an unset row) apply it. An explicitly-supplied
`secret_ref` is always honoured verbatim.

This replaces the retired **per operator `sub`** layout
(`secret/data/targets/<sub>/*`, `connector-vault-policy.md` §2), which
duplicated a target's credential per operator and gave the guard no
universal `{tenant_id}` partition to enforce against. Existing per-`sub`
secrets are relocated by the operator-driven runbook
[`docs/cross-repo/vault-per-tenant-migration.md`](../cross-repo/vault-per-tenant-migration.md)
(read → write → soft-delete via the `vault_kv_*` ops, then rewrite each
target's `secret_ref`).

## The guard is default-on (as of #1725)

The guard ships **enforced by default** with the mount-pinned prefix
`secret/tenants/{tenant_id}/`. With the per-tenant layout now canonical
(#1723), tenant isolation is enforced at the app layer out of the box — no
per-deploy opt-in.

**Why the mount segment is in the default.** The guard matches a normalised
`<mount>/<path>` candidate (see "The namespace convention"), and the KV-v2
handlers default `mount="secret"` (`ops.py` `_DEFAULT_KV_MOUNT`). The
canonical layout addresses secrets as mount `secret`, path
`tenants/<tenant_id>/<target>`, so the candidate is
`secret/tenants/<tenant_id>/<target>`. A *path-only* prefix
`tenants/{tenant_id}/` would render to `tenants/<id>/` and never match that
candidate — it would deny **every** legitimate per-tenant call. The default
is therefore the mount-pinned `secret/tenants/{tenant_id}/`.

**Adopter action on upgrade (#1725).** The default-on guard denies any
authenticated `vault.kv.*` call whose normalised `<mount>/<path>` does not
begin with `secret/tenants/<tenant_id>/`. Two kinds of deploy must act
before upgrading to keep their daily-driver `vault.kv.read` path working:

- A deploy upgrading from a pre-#1723 release that still holds *existing*
  secrets under the retired per-`sub` layout (`secret/data/targets/<sub>/*`).
  Relocate them with the migration runbook
  ([`vault-per-tenant-migration.md`](../cross-repo/vault-per-tenant-migration.md)).
- A deploy running a **deliberate custom layout** under neither
  `targets/<sub>/*` nor `secret/tenants/{tenant_id}/` (e.g. an org-chosen
  mount/path scheme set via explicit `secret_ref` values, which the
  backplane honours verbatim and never re-homes).

In **both** cases, **explicitly disable** the guard before upgrading by
setting `VAULT_KV_TENANT_SCOPE_PREFIX=""` — otherwise the default-on guard
denies every not-yet-conforming `vault.kv.*` call with
`exception_class=VaultTenantScopeError`. A per-`sub` deploy drops the
override once the migration completes; a custom-layout deploy may keep it
empty as its steady state (migrating to `secret/tenants/{tenant_id}/` later
is optional — see the runbook's "Custom / non-standard layout" subsection).
A fresh deploy (all secrets under `tenants/<tenant_id>/`) needs no action —
the default already enforces.

**Platform-path exemption.** A closed allow-list of fixed, shared,
non-tenant platform secrets (`PLATFORM_EXEMPT_PATHS` in `tenant_scope.py`)
is exempt regardless of operator. The only entry today is the
federation-proof health secret `secret/meho/test/federation` that
`GET /api/v1/health` reads under the *real* request operator's identity to
prove the JWT→OIDC→Vault chain — it carries no tenant data and is
provisioned shared per the Goal #11 cross-repo contract, so the default-on
guard must not deny the platform's own probe. The exemption is an
**exact-match** set, never a prefix or glob, so a caller cannot smuggle a
tenant escape through it; a regression test pins it equal to the health
route's path. It is also scoped to **read-only** verbs (gated on the
`read_only` argument): the health route only ever reads this path, so a
`put`/`patch`/`delete` to it under a non-owning operator is still
tenant-scoped and denied.

**Prefix template validation.** `vault_kv_tenant_scope_prefix` is validated
at `Settings` construction (a pydantic `@field_validator`): a non-empty
value must contain the `{tenant_id}` placeholder and be a clean
`str.format` template (no unbalanced braces, no positional `{0}`, no extra
named placeholder). The empty string (explicit-disable) is accepted
verbatim. A malformed override therefore fails the pod start with an
actionable message rather than failing at first `vault.kv.*` call (or, for
a placeholder-less value, silently collapsing every operator to one shared
namespace).

The **system/shim operator** (the Nil-UUID `tenant_id` the vault connector
synthesises in `connector.py`, empty `raw_jwt`) is exempt even when the
guard is enabled: its only callers run the unauthenticated
`vault.sys.health` op and forward no token to Vault, so there is no tenant
identity to bind against.

## Startup advisory (unenforced state is visible)

Because the guard is now default-on, the advisory is **silent on the common
deploy**. It fires only when an operator has *explicitly disabled* the guard
(`VAULT_KV_TENANT_SCOPE_PREFIX=""`) — e.g. while still mid-migration with
secrets under the retired per-`sub` layout — so that running unenforced is
never silent.
[`main._advise_vault_tenant_scope_unenforced`](../../backend/src/meho_backplane/main.py)
runs in the FastAPI lifespan and emits **exactly one** structured advisory
at startup when `vault_kv_tenant_scope_prefix` is empty:

```
vault_tenant_scope_unenforced  enable_via=VAULT_KV_TENANT_SCOPE_PREFIX  doc=docs/codebase/connectors-vault-tenant-scope.md
```

It is **observability-only** — loud-but-non-fatal, like the embedding
preload advisory: no dispatch change, no raise. A deploy that has
deliberately opted out (mid-migration) can ignore the line; otherwise it is
the cue that tenant isolation is not being enforced at the app layer. Once
the prefix is restored to a non-empty value (or left at the default) the
advisory is silent. The behaviour is covered by
[`backend/tests/test_vault_tenant_scope_advisory.py`](../../backend/tests/test_vault_tenant_scope_advisory.py)
(silent on default, silent when set, fires when explicitly emptied, never
blocks boot).

## Choosing a layout

The guard is default-on (`secret/tenants/{tenant_id}/`), matching the
canonical per-tenant layout. Any deploy whose secrets are **not** under
`secret/tenants/{tenant_id}/` — the retired per-`sub` layout or a
deliberate custom layout — must touch the prefix:

- **Per-tenant layout (canonical since #1723; guard default-on).** Secrets
  are partitioned by tenant under `tenants/<tenant_id>/<target>` on the
  default `secret` mount. New targets land here automatically; existing
  per-`sub` secrets are moved by the migration runbook. The default-on guard
  is a real backstop for a mis-provisioned Vault policy — **no action
  required** (leave `VAULT_KV_TENANT_SCOPE_PREFIX` unset).

- **Per-`sub` layout (retired; pre-#1723 deploys mid-migration).** Secrets
  live under `secret/data/targets/<sub>/*` and isolation is enforced
  entirely by the templated `meho-mcp` Vault policy
  (`connector-vault-policy.md` §2). There is no `tenant-<id>/` partition to
  bind against, so the default-on guard would deny every not-yet-relocated
  call. **Explicitly disable** the guard with
  `VAULT_KV_TENANT_SCOPE_PREFIX=""` until the migration runbook has run; the
  startup advisory fires to keep that unenforced state visible. Keep the
  policy template correct — it is the primary (and only) gate while the
  guard is disabled. Relocate to the per-tenant layout via
  [`vault-per-tenant-migration.md`](../cross-repo/vault-per-tenant-migration.md),
  then drop the override.

- **Custom / non-standard layout (neither per-`sub` nor per-tenant).**
  Secrets live under a deploy-chosen mount/path scheme set via explicit
  `secret_ref` values (the backplane honours these verbatim and never
  re-homes them), so the candidate never begins with
  `secret/tenants/<tenant_id>/` and the default-on guard would deny every
  `vault.kv.*` call. **Explicitly disable** the guard with
  `VAULT_KV_TENANT_SCOPE_PREFIX=""` before upgrading; isolation then rests
  entirely on the `meho-mcp` Vault policy, which must stay correct. Unlike
  the per-`sub` case, a migration is **optional** — keeping the prefix empty
  is a valid steady state. The runbook's
  [Custom / non-standard layout](../cross-repo/vault-per-tenant-migration.md)
  subsection covers the upgrade action and the optional path to adopting the
  per-tenant layout later.

**The active (default) prefix and its preconditions:**

1. The canonical Vault KV layout is per-tenant: mount `secret`, path
   `tenants/<tenant_id>/<target>`. The prefix only denies; it never
   relocates secrets.
2. The default `VAULT_KV_TENANT_SCOPE_PREFIX` is the mount-pinned
   `secret/tenants/{tenant_id}/` — a `str.format` template with a single
   `{tenant_id}` placeholder. The mount segment is required because the
   guard matches a normalised `<mount>/<path>` candidate on the default
   `secret` mount; a path-only `tenants/{tenant_id}/` would never match and
   would deny every legitimate call. The rendered `tenant_id` is the
   canonical dashed lowercase UUID (see "The namespace convention").
3. Every legitimate `vault.kv.*` caller's secrets must already live
   **under** that prefix — any in-namespace mismatch is denied with
   `exception_class=VaultTenantScopeError` *before* the hvac call. The
   system/shim operator (Nil-UUID tenant, `vault.sys.health` only) is
   exempt, so the default does not break the health probe.

Because step 3 denies every call that is *not* under a `tenants/<id>/`
prefix, any deploy whose secrets sit elsewhere — the retired per-`sub`
layout or a deliberate custom layout — must disable the guard with
`VAULT_KV_TENANT_SCOPE_PREFIX=""` before upgrading. That is exactly what
the empty-prefix opt-out is for: a per-`sub` deploy drops it once the
migration completes, while a custom-layout deploy may keep it empty
indefinitely.

## Write-time sibling: the target `secret_ref` gate (#2091)

The guard above protects the *runtime* `vault.kv.*` ops. The same
subtree definition also gates the **targets write surface**
(`POST`/`PATCH /api/v1/targets*`, which `meho targets import` drives):
an explicitly supplied `secret_ref` outside the operator's rendered
tenant prefix is rejected at write time with a structured 422
(`kind="secret_ref_outside_tenant_scope"`,
`_enforce_secret_ref_tenant_scope` in `api/v1/targets.py`) instead of
importing clean and failing every dispatch with an opaque Vault
`permission denied`. Semantics deliberately mirror
`enforce_tenant_scope`: the match candidate is the normalised
mount-pinned `<mount>/<secret_ref>` (the dispatch-time credential read
addresses the default `secret` mount), matching is on a path-segment
boundary, and the empty-prefix opt-out makes the gate a no-op. Only an
*explicit* ref is checked — the derived per-tenant default (#1723) and
an explicit-null clear pass untouched.

A target that predates the gate (or a genuine Vault-policy drift) still
surfaces at dispatch as the structured `connector_vault_forbidden`
error (`result_connector_vault_forbidden` in `operations/_errors.py`,
caught by the dispatcher's `except hvac.exceptions.Forbidden` arm),
which names the target's `secret_ref`, the `tenants/<tenant_id>/<name>`
convention, and the exact expected path — rather than the bare
`connector_error: Forbidden` that read like a missing Vault grant and
invited widening the deploy-owned policy (the wrong fix). See
`error-message-shape.md` for both rows.

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

The #2091 write-time gate is covered in
[`backend/tests/test_api_v1_targets.py`](../../backend/tests/test_api_v1_targets.py)
(the "#2091 — secret_ref tenant-scope fail-fast" cluster: POST/PATCH
reject, segment boundary, derived-default pass, guard-disabled no-op);
the dispatch-time `connector_vault_forbidden` mapping in
[`backend/tests/test_operations_connector_vault_forbidden.py`](../../backend/tests/test_operations_connector_vault_forbidden.py).
