<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Per-tenant templated Vault ACL policies (deploy runbook)

> Deploy-time prerequisite for the **per-tenant shared secret layout**
> (`secret/data/tenants/<tenant_id>/*`). This runbook authors the ≤3
> templated ACL policies that scope a tenant's operators onto their own
> tenant's secrets — keyed on `{{identity.entity.metadata.tenant_id}}`,
> by role — and the identity-store wiring (entity metadata + identity
> groups) that makes the template resolve. It supersedes the
> **per-operator** alias recipe in
> [`connector-vault-policy.md`](./connector-vault-policy.md) §2 for the
> case where operators in one tenant must **share** a target credential.

This is the T2 deliverable of
[Initiative #1685](https://github.com/evoila/meho/issues/1685) under
[Goal #221](https://github.com/evoila/meho/issues/221). It is **OSS-only**
— ACL policy templating on identity metadata is a core Vault feature, not
Enterprise namespaces
([Vault — ACL policy templating tutorial](https://developer.hashicorp.com/vault/tutorials/policies/policy-templating),
[Vault — identity ACL policy templates](https://developer.hashicorp.com/vault/docs/secrets/identity/deduplication/acl-policy-templates)).

## Why per-tenant templated, not per-operator copies

The shipped layout stored secrets **per operator `sub`**
(`secret/data/targets/<sub>/*`): credentials were duplicated per operator,
and two operators in one tenant could not cleanly share a target
credential — credentials belong to a tenant's *target*, not to a person.
The fix
([Initiative #1685](https://github.com/evoila/meho/issues/1685)) is a
**per-tenant shared** path with **one templated policy per role** (not N
per-tenant copies):

```text
secret/data/tenants/<tenant_id>/<target>
```

Vault substitutes the caller's `tenant_id` — lifted from the JWT claim
into the operator's Vault identity-entity **metadata** — into the policy
path at request time, so a single policy serves every tenant while still
isolating each tenant's secrets. **Roles are capabilities on the shared
path, not separate key spaces:** the same path template appears in all
three policies; only the granted capabilities differ.

The path-migration itself (relocating existing per-`sub` secrets) is
[#1723](https://github.com/evoila/meho/issues/1723) (T1); flipping the
[#1643](https://github.com/evoila/meho/issues/1643) guard default-on is
[#1725](https://github.com/evoila/meho/issues/1725) (T3). This runbook is
the policy + identity-wiring layer between them.

## 1. Role → capability table (authoritative)

The role names are the literal `TenantRole` values MEHO's JWT carries
(`read_only` / `operator` / `tenant_admin`,
[`auth/operator.py`](../../backend/src/meho_backplane/auth/operator.py)).
Each role binds to **exactly one** templated policy; the policy names are
the single source of truth in
[`connectors/vault/tenant_identity.py`](../../backend/src/meho_backplane/connectors/vault/tenant_identity.py)
(`TENANT_POLICY_FOR_ROLE`):

| Role (`TenantRole`) | Policy name | Capabilities on `secret/data/tenants/<id>/*` | Extra grants |
| --- | --- | --- | --- |
| `read_only` | `meho-tenant-read-only` | `read`, `list` | — |
| `operator` | `meho-tenant-operator` | `read`, `list` | `+ create,update` on the rotation sub-path `secret/data/tenants/<id>/rotation/*` |
| `tenant_admin` | `meho-tenant-admin` | `create`, `read`, `update`, `delete`, `list` (full CRUD) | `+ full CRUD` on the break-glass sub-path `secret/data/tenants/<id>/privileged/*` |

Notes:

- **KV-v2 splits `/data/` from `/metadata/`.** Each policy grants the
  matching capability on **both** subtrees — the backplane's read touches
  `/data/` (value) and `/metadata/` (version metadata); a `/data/`-only
  grant fails partially on the metadata leg
  ([`connector-vault-policy.md`](./connector-vault-policy.md) §2).
- **`list` is granted on `secret/metadata/...`**, not `secret/data/...`:
  KV-v2 authorises a LIST against the metadata path.
- **Admin break-glass lives under `privileged/`** and is reachable by the
  `tenant_admin` policy *alone* — `read_only`/`operator` policies never
  template that sub-path, so a non-admin operator cannot reach it even
  within their own tenant.
- The trailing `/*` is a **literal glob in the static portion** of the
  path, after the templated `{{...}}` segment. A glob must **never** be
  placed *inside* the `{{identity.entity.metadata.tenant_id}}` segment —
  Vault forbids it, and it is what keeps one tenant's rendered path from
  matching another's
  ([policy templating constraints](https://developer.hashicorp.com/vault/docs/concepts/policies)).

## 2. The three policy bodies

Attach all three to the `meho-mcp` role's identity groups (§4 binds
role → policy → operator). They are **additive** to the federation-chain
reads in [`vault-provisioning.md`](./vault-provisioning.md) §3 — keep
those.

### 2.1 `meho-tenant-read-only`

```hcl
# read_only: read+list a tenant's shared target secrets, nothing else.
path "secret/data/tenants/{{identity.entity.metadata.tenant_id}}/*" {
  capabilities = ["read"]
}
path "secret/metadata/tenants/{{identity.entity.metadata.tenant_id}}/*" {
  capabilities = ["read", "list"]
}
```

### 2.2 `meho-tenant-operator`

```hcl
# operator: read_only's grants, plus create/update on a rotation sub-path
# so an operator can rotate a target credential without full CRUD.
path "secret/data/tenants/{{identity.entity.metadata.tenant_id}}/*" {
  capabilities = ["read"]
}
path "secret/metadata/tenants/{{identity.entity.metadata.tenant_id}}/*" {
  capabilities = ["read", "list"]
}
path "secret/data/tenants/{{identity.entity.metadata.tenant_id}}/rotation/*" {
  capabilities = ["create", "read", "update"]
}
```

### 2.3 `meho-tenant-admin`

```hcl
# tenant_admin: full CRUD on the tenant's secrets, plus the privileged
# break-glass sub-path that only this policy templates.
path "secret/data/tenants/{{identity.entity.metadata.tenant_id}}/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
path "secret/metadata/tenants/{{identity.entity.metadata.tenant_id}}/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
path "secret/data/tenants/{{identity.entity.metadata.tenant_id}}/privileged/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
```

Write each via the approval-gated op
([`ops_sys_policy.py`](../../backend/src/meho_backplane/connectors/vault/ops_sys_policy.py),
`vault.sys.policy.write`) or `vault policy write` at provisioning time:

```bash
vault policy write meho-tenant-read-only meho-tenant-read-only.hcl
vault policy write meho-tenant-operator  meho-tenant-operator.hcl
vault policy write meho-tenant-admin     meho-tenant-admin.hcl
```

## 3. Entity-metadata wiring — make the template resolve

`{{identity.entity.metadata.tenant_id}}` renders **only when the
operator's Vault identity entity carries a `tenant_id` metadata key**. The
JWT carries the claim (`tenant_id` by default,
[`settings.py`](../../backend/src/meho_backplane/settings.py)
`tenant_id_claim`;
[`auth/jwt.py`](../../backend/src/meho_backplane/auth/jwt.py)
`_extract_tenant_id`), and MEHO materialises it on every
[`Operator`](../../backend/src/meho_backplane/auth/operator.py)
(`tenant_id: UUID`). The helper that maps an operator onto the entity
metadata is
[`connectors/vault/tenant_identity.py`](../../backend/src/meho_backplane/connectors/vault/tenant_identity.py)
(`tenant_entity_metadata`):

```python
from meho_backplane.connectors.vault.tenant_identity import tenant_entity_metadata

# Pass the result verbatim as the `metadata` param to vault.identity.entity.write.
metadata = tenant_entity_metadata(operator)   # {"tenant_id": "<uuid>"}
```

Set it on the operator's entity through the existing approval-gated op
([`ops_identity.py`](../../backend/src/meho_backplane/connectors/vault/ops_identity.py),
`vault.identity.entity.write`), which forwards `metadata` to hvac's
`create_or_update_entity(metadata=…)`:

```bash
# Equivalent CLI for a manual / out-of-band provision:
vault write identity/entity \
  name="<operator-sub>" \
  metadata=tenant_id="<tenant-uuid>"
```

> **The metadata value is the canonical dashed-lowercase UUID string**
> (`str(UUID)`), matching the tenant UUID's spelling in audit rows, JWT
> claims, and the [`tenant_scope`](../../backend/src/meho_backplane/connectors/vault/tenant_scope.py)
> guard's rendered prefix. A mismatch between the metadata value and the
> migrated path's `<tenant_id>` segment yields a Vault 403 — verify with §5.

### Why entity metadata, not the alias name

The per-operator recipe in
[`connector-vault-policy.md`](./connector-vault-policy.md) §2 templates on
`{{identity.entity.aliases.<accessor>.name}}` — the operator's JWT `sub`,
auto-populated on first login. That scopes per **person**. To scope per
**tenant** (so operators *share* a credential) the template must key on a
**tenant** attribute, which the auto-created alias does not carry. Entity
**metadata** is the OSS-supported place to attach an arbitrary identity
attribute Vault will template on — hence the explicit metadata write
above. Group metadata
(`{{identity.groups.names.<group>.metadata.<key>}}`) is an alternative
keying surface, but per-entity metadata keeps the binding closest to the
identity Vault already creates on JWT login.

## 4. Identity-group → policy binding

Bind **role → policy** through an identity **group** rather than editing
each entity's policies: every member entity inherits the group's policy,
so onboarding a new operator in a tenant is a membership add, not a policy
edit. One group per `(tenant, role)` pair; the group name is a flat
slash-free handle
([`tenant_identity.py`](../../backend/src/meho_backplane/connectors/vault/tenant_identity.py)
`tenant_group_name`):

```text
meho-tenant-<tenant_id>-<role>     # e.g. meho-tenant-3f8c…-operator
```

The `policies` value each role's group carries
(`tenant_group_policies(role)`):

| Role | Group name | `vault.identity.group.write` `policies` |
| --- | --- | --- |
| `read_only` | `meho-tenant-<id>-read_only` | `["meho-tenant-read-only"]` |
| `operator` | `meho-tenant-<id>-operator` | `["meho-tenant-operator"]` |
| `tenant_admin` | `meho-tenant-<id>-tenant_admin` | `["meho-tenant-admin"]` |

Provision through the existing approval-gated op
([`ops_identity.py`](../../backend/src/meho_backplane/connectors/vault/ops_identity.py),
`vault.identity.group.write`), forwarding `policies` and the tenant's
member entity ids:

```bash
# Equivalent CLI: one group per (tenant, role), carrying the role policy
# and the tenant's operator entity ids as members.
vault write identity/group \
  name="meho-tenant-<tenant-uuid>-operator" \
  policies="meho-tenant-operator" \
  member_entity_ids="<ent-id-A>,<ent-id-B>"
```

Membership is privilege plumbing — an entity in a policy-bearing group
inherits that policy
([`ops_identity.py`](../../backend/src/meho_backplane/connectors/vault/ops_identity.py)
docstring). An operator therefore needs: (a) a `tenant_id` entity metadata
(§3), and (b) membership in their tenant's role group (this §). The first
makes the path template render; the second grants the capability set.

## 5. Verification

Acquire the **operator's** Vault token by logging in with their Keycloak
JWT against the `meho-mcp` role (not a root token, which would mask a
missing grant — same procedure as
[`connector-vault-policy.md`](./connector-vault-policy.md) §4). Then:

```bash
# 1. The entity carries the tenant_id metadata the template needs.
vault read -format=json identity/entity/name/<operator-sub> \
  | jq '.data.metadata'
# Expect: {"tenant_id": "<tenant-uuid>"}

# 2. The operator can read a secret under their OWN tenant.
vault kv get -mount=secret -format=json "tenants/<tenant-uuid>/<target>" \
  | jq '.data.data | keys'
# Expect: ["password","username"]  (REST) or ["kubeconfig"]  (k8s)

# 3. The operator CANNOT read another tenant's secret (scoping holds).
vault kv get -mount=secret "tenants/<other-tenant-uuid>/<target>"
# Expect: permission denied  — the templated policy doing its job.

# 4. Capability probe for an operator's rotation write (operator role).
vault token capabilities "secret/data/tenants/<tenant-uuid>/rotation/<target>"
# Expect for operator: create read update
```

A `permission denied` on step 2 means either the entity metadata is
missing/mismatched (§3) or the group binding is absent (§4) — fix the
identity wiring, not the connector. A **success** on step 3 means a glob
leaked into the templated segment (§1) — every tenant could read every
secret; fix the policy body immediately.

## References

- Per-operator companion (superseded for shared-target access):
  [`connector-vault-policy.md`](./connector-vault-policy.md) §2
- Federation-chain provisioning (auth method, role, KV mount):
  [`vault-provisioning.md`](./vault-provisioning.md)
- Application-layer tenant-scope guard (#1643):
  [`connectors/vault/tenant_scope.py`](../../backend/src/meho_backplane/connectors/vault/tenant_scope.py),
  [`connectors-vault-tenant-scope.md`](../codebase/connectors-vault-tenant-scope.md)
- Role→policy + entity-metadata helper:
  [`connectors/vault/tenant_identity.py`](../../backend/src/meho_backplane/connectors/vault/tenant_identity.py)
- Identity ops (entity/group writes):
  [`connectors/vault/ops_identity.py`](../../backend/src/meho_backplane/connectors/vault/ops_identity.py)
- Policy ops (`vault.sys.policy.write`):
  [`connectors/vault/ops_sys_policy.py`](../../backend/src/meho_backplane/connectors/vault/ops_sys_policy.py)
- `tenant_id` JWT claim plumbing:
  [`auth/jwt.py`](../../backend/src/meho_backplane/auth/jwt.py) `_extract_tenant_id`,
  [`settings.py`](../../backend/src/meho_backplane/settings.py) (`tenant_id_claim`)
- Vault — [ACL policy templating tutorial](https://developer.hashicorp.com/vault/tutorials/policies/policy-templating)
- Vault — [identity ACL policy templates](https://developer.hashicorp.com/vault/docs/secrets/identity/deduplication/acl-policy-templates)
- Vault — [policy templating constraints](https://developer.hashicorp.com/vault/docs/concepts/policies)
- Vault — [KV v2 secrets engine API](https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2)
- Builds on #1643 (opt-in guard) + #1673 (startup advisory); Goal #221,
  Initiative #1685.
