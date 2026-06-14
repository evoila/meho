<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Migration runbook: per-`sub` → per-tenant Vault KV layout

> Operator-driven relocation of target secrets from the retired
> per-operator-`sub` layout (`secret/data/targets/<sub>/*`) to the
> canonical **per-tenant shared** layout
> (`secret/data/tenants/<tenant_id>/<target>`). Companion to the
> app-layer convention in
> [`connectors/vault/tenant_paths.py`](../../backend/src/meho_backplane/connectors/vault/tenant_paths.py)
> and the tenant-scope guard in
> [`docs/codebase/connectors-vault-tenant-scope.md`](../codebase/connectors-vault-tenant-scope.md).
> Implemented for [#1723](https://github.com/evoila/meho/issues/1723)
> (Goal #221, Initiative #1685).

## Why migrate

The shipped layout stored each target's credential per operator `sub`
(`secret/data/targets/<sub>/*`, [`connector-vault-policy.md`](./connector-vault-policy.md) §2).
Two consequences:

- **Duplication.** A target shared by two operators in one tenant needed
  the same credential written under each operator's `sub` path. Rotating
  it meant rotating every copy.
- **The #1643 guard could not go default-on.** The tenant-scope guard
  ([`tenant_scope.py`](../../backend/src/meho_backplane/connectors/vault/tenant_scope.py))
  enforces a `{tenant_id}` prefix, but there was no universal
  tenant-partitioned layout to enforce against, so it shipped opt-in.

The per-tenant layout fixes both: one secret per `(tenant, target)`,
readable by every operator in that tenant, under a path the guard now
enforces **by default** (#1725) with the mount-pinned
`VAULT_KV_TENANT_SCOPE_PREFIX="secret/tenants/{tenant_id}/"`.

## The new convention

| | Retired (per-`sub`) | Canonical (per-tenant) |
| --- | --- | --- |
| Logical `secret_ref` | `targets/<sub>/<target>` | `tenants/<tenant_id>/<target>` |
| KV-v2 wire path | `secret/data/targets/<sub>/<target>` | `secret/data/tenants/<tenant_id>/<target>` |

`<tenant_id>` is the canonical dashed lowercase UUID (`str(UUID)`) —
the same rendering the guard's
[`rendered_tenant_prefix`](../../backend/src/meho_backplane/connectors/vault/tenant_scope.py)
produces and the same UUID that appears in audit rows and JWT claims.

New targets created or PATCH-homed after #1723 land on the per-tenant
path automatically (see "What the backplane does for you"). This runbook
covers the **existing** secrets that predate the change.

## What the backplane does for you

- **Create** (`POST /api/v1/targets`) with no explicit `secret_ref`
  derives `tenants/<tenant_id>/<name>` and stores it on the row.
- **Update** (`PATCH /api/v1/targets/{name}`) that does not touch
  `secret_ref`, on a row whose `secret_ref` is still unset, fills in the
  same derived path.
- An **explicitly-supplied** `secret_ref` (including a non-default mount
  layout, or an explicit `{"secret_ref": null}` to clear it) is always
  honoured verbatim — the backplane never silently re-homes a ref you set.

The backplane **never auto-relocates the secret material in Vault** —
RDC owns the Vault deployment. Moving the bytes is this runbook.

## Prerequisites

- `vault` CLI authenticated against the deployment, or the MEHO
  `vault.kv.*` ops reachable for an operator in the target tenant.
- The tenant's `tenant_id` (canonical dashed UUID). For an operator JWT
  this is the `tenant_id` claim; for a target row it is `targets.tenant_id`
  (`GET /api/v1/targets/{name}` returns it).
- A list of the targets to migrate and their current per-`sub`
  `secret_ref` values (`GET /api/v1/targets` returns `secret_ref` on the
  summary shape).

## Procedure (per target)

For each target with an old `targets/<sub>/<x>` secret_ref:

### 1. Relocate the secret material

The relocation moves the secret with a KV-v2 read → write, optionally
followed by a soft-delete of the old version. Two equivalent ways:

**A. Via the MEHO `relocate_target_secret` helper** (recommended — it runs
under the operator's OIDC identity and writes the Vault audit log
correctly, and derives the destination path for you):

```python
from meho_backplane.connectors.vault.tenant_paths import relocate_target_secret

# `operator` is the authenticated tenant operator (its tenant_id keys
# the destination path; its JWT authenticates both Vault legs).
new_ref = await relocate_target_secret(
    operator,
    old_ref="targets/<sub>/<target>",
    target="<target-name>",
    delete_old=False,   # leave the source intact until verified
)
# new_ref == "tenants/<tenant_id>/<target-name>"
```

**B. Via the `vault` CLI** (when running the migration outside MEHO):

```bash
TENANT_ID=<dashed-uuid>
TARGET=<target-name>
OLD=targets/<sub>/$TARGET
NEW=tenants/$TENANT_ID/$TARGET

# Read the latest version of the old secret and write it to the new path.
vault kv get -format=json secret/$OLD \
  | jq '.data.data' \
  | vault kv put secret/$NEW -
```

### 2. Verify the new path resolves

Confirm the relocated secret reads back through the new path before you
retire the source:

```bash
vault kv get secret/tenants/$TENANT_ID/$TARGET
```

Or, in MEHO, a `vault.kv.read` op (or any connector auth that resolves
`secret_ref`) against the rewritten target.

### 3. Rewrite the target's `secret_ref`

Point the target row at the new path:

```bash
curl -X PATCH https://<backplane>/api/v1/targets/$TARGET \
  -H "Authorization: Bearer $OPERATOR_JWT" \
  -H "Content-Type: application/json" \
  -d "{\"secret_ref\": \"tenants/$TENANT_ID/$TARGET\"}"
```

`secret_ref` is the **logical** KV-v2 path — no `secret/`, no `/data/`
prefix (hvac inserts the mount and the `/data/` segment itself; a
prefixed value double-resolves to a 404, see
[`_shared/vault_creds.py`](../../backend/src/meho_backplane/connectors/_shared/vault_creds.py)
`_is_api_path_shaped`).

### 4. Retire the old secret

Once every operator in the tenant resolves the target through the new
path, soft-delete the old version (reversible via Vault's undelete):

```bash
vault kv delete secret/$OLD
```

Or pass `delete_old=True` to `relocate_target_secret` in step 1 to do the
read → write → soft-delete in one call **after** you have verified the new
path resolves.

## The guard is default-on (disable while mid-migration)

As of #1725 the #1643 guard is **enforced by default** with the
mount-pinned prefix:

```text
VAULT_KV_TENANT_SCOPE_PREFIX=secret/tenants/{tenant_id}/
```

The mount segment is required: the guard matches a normalised
`<mount>/<path>` candidate and these secrets sit on the default `secret`
mount, so a path-only `tenants/{tenant_id}/` would deny every legitimate
per-tenant call. While a deploy still holds secrets under the retired
per-`sub` layout, **disable** the guard until the migration completes:

```text
VAULT_KV_TENANT_SCOPE_PREFIX=
```

Once every legitimate `vault.kv.*` caller's secrets are under
`tenants/<tenant_id>/`, drop the override and let the default-on guard
enforce.

See
[`connectors-vault-tenant-scope.md`](../codebase/connectors-vault-tenant-scope.md)
("The active (default) prefix and its preconditions") for the
preconditions and the startup advisory. Do **not** enable the prefix until
every legitimate `vault.kv.*` caller's secrets are under it — once set, any
in-namespace mismatch is denied with `exception_class=VaultTenantScopeError`
before the hvac call.

### Custom / non-standard layout (neither per-`sub` nor per-tenant)

The body of this runbook assumes you are moving **from** the retired
per-`sub` layout (`secret/data/targets/<sub>/*`) **to** the canonical
per-tenant layout (`secret/data/tenants/<tenant_id>/<target>`). Some
deploys run secrets under a **deliberate custom layout** that is *neither*
— e.g. an org-chosen mount or path scheme such as
`secret/data/<team>/<env>/<target>` set via explicit `secret_ref` values.
That is a supported configuration: the backplane honours an
explicitly-supplied `secret_ref` verbatim and never re-homes it (see
"What the backplane does for you").

**What the default-on guard enforces (#1725).** With
`VAULT_KV_TENANT_SCOPE_PREFIX` left at its mount-pinned default
`secret/tenants/{tenant_id}/`, every authenticated `vault.kv.*` call must
address a normalised `<mount>/<path>` that begins with
`secret/tenants/<your-tenant-id>/`. A path under any other scheme — your
custom layout included — is denied with
`exception_class=VaultTenantScopeError` **before** the hvac call. The
denial is local (no Vault round-trip) and reports the requested path
against the rendered tenant prefix.

**Upgrade action.** If your secrets are under neither `targets/<sub>/*`
nor `secret/tenants/{tenant_id}/`, the default-on guard will reject your
daily-driver `vault.kv.read` (and every other `vault.kv.*` op) on upgrade
to v0.15.0. Set the prefix **empty** to keep those calls working:

```text
VAULT_KV_TENANT_SCOPE_PREFIX=
```

An empty prefix makes the guard a no-op — behaviour is byte-for-byte what
it was before the guard existed; isolation falls back entirely to the
templated `meho-mcp` Vault policy
([`connector-vault-policy.md`](./connector-vault-policy.md) §2), which
must stay correct because it is then the only gate. The startup advisory
(`vault_tenant_scope_unenforced`) fires once at boot to keep that
unenforced state visible.

**Migrating later is optional, not required.** A custom layout does not
*have* to move to `secret/tenants/{tenant_id}/`. If you later choose to
adopt the per-tenant layout so the app-layer guard becomes a real
backstop, follow the per-target procedure above (relocate the bytes,
rewrite each `secret_ref`), then drop the empty-prefix override. Until
then, keeping the prefix empty is the correct steady state.

## Rollback

The relocation is non-destructive when `delete_old=False` (the default):
the source secret survives. To roll back, PATCH the target's `secret_ref`
back to the old `targets/<sub>/<target>` value. If you already
soft-deleted the source, undelete it first:

```bash
vault kv undelete -versions=<n> secret/$OLD
```
