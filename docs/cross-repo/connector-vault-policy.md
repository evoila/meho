<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Connector Vault policy — per-target secret reads (deploy runbook)

> Deploy-time prerequisite for **per-target connector credential reads**.
> Every MEHO REST/k8s connector resolves its vendor service-account
> credential from Vault *under the acting operator's identity* before it
> can call the vendor API
> ([decision: operator-context, `connector-auth.md`](../architecture/connector-auth.md)).
> That read only succeeds when the Vault `meho-mcp` role's policy grants
> the operator read on the target's secret path, and the operator's
> Keycloak→Vault Identity entity exists. This document is the contract
> the operator's Vault deployment must satisfy, plus the verification
> commands that prove a given operator JWT can read a given `secret_ref`.

## Why this lives in `evoila/meho`

The credential-read leaf of the dispatch chain is wired in
[`backend/src/meho_backplane/auth/vault.py`](../../backend/src/meho_backplane/auth/vault.py)
(the operator-context Vault client) and consumed by every connector's
session loader. The *shape* of "what Vault must grant" — which secret
subtree, which templated path, which identity claim — is a property of
that chassis code, so it is documented here and changes in lock-step
with it. The actual Vault configuration lives on the consumer side
(`evoila-bosnia/claude-rdc-hetzner-dc`
in the dogfood case); this page is the spec that side reads.

This runbook is the **per-target-secret** companion to
[`vault-provisioning.md`](./vault-provisioning.md). That doc provisions
the federation chain itself (JWT auth method, the `meho-mcp` role, the
KV mount, the federation-proof test path). This doc adds the one
surface a *connector* needs on top: read access to the per-target
vendor credential, scoped per operator through the single existing
role.

## How the read works (and why the policy is the gate)

When an operator calls an op against a target, the dispatcher forwards
the operator's already-validated Keycloak JWT to
[`vault_client_for_operator(operator)`](../../backend/src/meho_backplane/auth/vault.py#L198),
which logs in to Vault's JWT/OIDC auth method on the `meho-mcp` role and
reads the target's `secret_ref` as a KV-v2 secret. The role and mount
are not hard-coded — they come from settings
([`settings.py`](../../backend/src/meho_backplane/settings.py)):

| Setting | Env var | Default | Meaning |
| --- | --- | --- | --- |
| `vault_oidc_role` | `VAULT_OIDC_ROLE` | `meho-mcp` | Vault role the JWT logs in against |
| `vault_oidc_mount_path` | `VAULT_OIDC_MOUNT_PATH` | `jwt` | Mount of the JWT/OIDC auth method (no `auth/` prefix) |
| `vault_addr` | `VAULT_ADDR` | — (required) | Vault base URL |
| `vault_namespace` | `VAULT_NAMESPACE` | unset (OSS) | Enterprise namespace, omitted on OSS |

The login is bound to the operator's identity, so the read runs under
**the operator's Vault policy**, not a broad backplane policy. That is
the whole point of the operator-context decision: per-operator RBAC
*and* per-operator audit through one role. The flip side is the
deploy prerequisite — **if the policy attached to `meho-mcp` does not
grant the operator read on the target's path, the read returns Vault
403 and the connector raises
[`VaultRoleDeniedError`](../../backend/src/meho_backplane/auth/vault.py#L99)**
(surfaced to the caller as a clean auth failure, never a 5xx).

## 1. Secret layout — where per-target vendor credentials live

Each target row carries a `secret_ref`: a **string** Vault path (KV-v2)
holding that target's vendor service-account credential
([`targets/schemas.py`](../../backend/src/meho_backplane/targets/schemas.py),
`secret_ref: str | None`). It is a *reference*, never an embedded value
— the operator and the agent never see the cleartext; the backplane
reads and uses it server-side.

Recommended convention: put per-target secrets under a `targets/`
subtree of the existing KV-v2 mount (default mount `secret`,
[`connectors/vault/ops.py`](../../backend/src/meho_backplane/connectors/vault/ops.py#L92)),
keyed by the operator identity segment the templated policy scopes on
(see §2):

```text
secret/data/targets/<operator-identity>/<target-name>
```

The secret's **field shape** depends on the connector family:

| Connector family | Secret fields at `secret_ref` | Read by |
| --- | --- | --- |
| vSphere REST, NSX, SDDC Manager, Harbor, VCF Operations/Logs/Fleet/Automation | `username`, `password` | the connector's session loader → `POST /api/session` (or vendor login) |
| Kubernetes | `kubeconfig` (the full kubeconfig YAML as one field) | `load_kubeconfig_from_vault` → builds the API client |

This matches the `{"username","password"}` contract the REST loaders
expect and the `kubeconfig` field name the k8s loader expects
([`kubernetes-onboarding.md`](./kubernetes-onboarding.md), "Target +
secret"). KV-v2 stores the value under `/data/` and its metadata under
`/metadata/`; a policy that grants the read must cover **both**
(see §2).

> **Do not embed the credential in `secret_ref`.** `secret_ref` is a
> `str` path, not a dict. The embedded-dict shape (a credential value
> living inside the target row) is the anti-pattern the Vault-broker
> model replaces — config holds references, Vault holds values.

## 2. Templated ACL policy — per-operator scoping through one role

> **Superseded for shared-target access (v0.14.0+, #1724).** The
> per-operator recipe below scopes secrets per **person**
> (`secret/data/targets/<sub>/*`), which duplicates a target credential
> across operators and prevents two operators in one tenant from sharing
> it. For the **per-tenant shared** layout
> (`secret/data/tenants/<tenant_id>/*`) — where a tenant's operators read
> one shared target credential, scoped by role — use the
> **per-tenant templated** policies in
> [`connector-vault-tenant-policy.md`](./connector-vault-tenant-policy.md).
> Those policies key on `{{identity.entity.metadata.tenant_id}}` instead
> of the alias name and bind capabilities by `TenantRole`. The
> per-operator recipe here remains valid for the `per_user` model (each
> operator has their own vendor credential; see the scope note below) and
> as background for how templating works.

A single Vault role (`meho-mcp`) enforces **per-operator** path scoping
via **ACL policy templating**: Vault renders the operator's identity
attributes into the policy path at evaluation time, so each operator can
only read secrets under their own identity segment — without a
per-operator role.
([Vault ACL policy templating](https://developer.hashicorp.com/vault/docs/concepts/policies))

Attach this policy to the `meho-mcp` role (it is *additive* to the
federation-chain reads in
[`vault-provisioning.md`](./vault-provisioning.md) §3 — keep those):

```hcl
# Per-target connector secrets, scoped to the operator's own identity.
# <ACCESSOR> is the mount accessor of the JWT auth method (see below).
path "secret/data/targets/{{identity.entity.aliases.<ACCESSOR>.name}}/*" {
  capabilities = ["read"]
}
path "secret/metadata/targets/{{identity.entity.aliases.<ACCESSOR>.name}}/*" {
  capabilities = ["read"]
}
```

What the template parts mean:

- `{{identity.entity.aliases.<ACCESSOR>.name}}` renders to the
  operator's **alias name on the JWT mount** — i.e. the value of the
  role's `user_claim` (configured as `sub` in
  [`vault-provisioning.md`](./vault-provisioning.md) §2). Each operator
  therefore sees a path scoped to *their* identity and no one else's.
- `<ACCESSOR>` is the **mount accessor** of the JWT auth method, not the
  mount path. Resolve it once at provisioning time:

  ```bash
  vault auth list -format=json | jq -r '."jwt/".accessor'
  # e.g. auth_jwt_0a1b2c3d  — substitute this literal value for <ACCESSOR>
  ```

  (Use your chosen mount path if you didn't mount the JWT method at
  `jwt/` per `vault-provisioning.md` §1.)

- Both `secret/data/...` and `secret/metadata/...` are required: KV-v2
  splits the value (`/data/`) from its metadata (`/metadata/`); the
  backplane reads `/data/` via hvac's
  `secrets.kv.v2.read_secret_version`, and the read also touches
  `/metadata/`. Granting only `/data/` produces a read that succeeds
  partially then fails on the metadata leg.

### Constraint: no wildcard inside the rendered identity segment

Vault does **not** permit a glob (`*`) or `+` *inside a template's
rendered output* — the identity-derived segment must resolve to a
literal, which is what prevents one operator's rendered path from
matching another's.
([policy templating constraints](https://developer.hashicorp.com/vault/docs/concepts/policies))
The trailing `/*` in the policy above is a **literal glob in the
static portion of the path** (after the templated segment), which *is*
allowed — it lets one operator read any target *under their own*
identity segment. Never try to put the `*` inside the `{{...}}`.

> **Tighter scoping (optional).** If an operator should only reach a
> known set of targets, drop the trailing `/*` and template the target
> name from entity metadata instead
> (`{{identity.entity.metadata.allowed_targets}}`-style), or list each
> `secret/data/targets/<op>/<target>` path explicitly. The day-1 model
> is the `/*`-under-own-identity shape above; per-target allow-lists are
> an operator policy choice, not a chassis requirement.

## 3. Keycloak → Vault identity prerequisite

The templated policy renders `identity.entity.aliases.<ACCESSOR>.name`,
which is populated **only when the operator has a Vault Identity entity
with an alias on the JWT mount**. With JWT/OIDC auth, Vault creates that
entity-alias automatically on first successful login from the operator's
JWT, keyed by the role's `user_claim`. Two prerequisites make that work:

1. **JWT auth method config maps a stable identity claim.** The
   `meho-mcp` role's `user_claim` must be the JWT claim that uniquely
   and stably identifies the operator. MEHO uses `sub`
   ([`vault-provisioning.md`](./vault-provisioning.md) §2):

   ```bash
   vault read -format=json auth/jwt/role/meho-mcp \
     | jq '{user_claim, groups_claim, bound_audiences, role_type}'
   # Expect: user_claim="sub", role_type="jwt",
   #         bound_audiences matching KEYCLOAK_AUDIENCE
   ```

   `groups_claim` (e.g. `groups`) is optional here — it populates
   `identity.groups.*`, which a *group*-scoped variant of the §2 policy
   could template on
   (`{{identity.groups.names.<group>.id}}`). The day-1 per-operator
   shape uses only `user_claim`.

2. **The operator's entity-alias exists after first login.** The alias
   `name` is the rendered identity segment in §2's path, so an operator
   who has never logged in to Vault has no entity yet, and their first
   connector call self-provisions it. To verify (or pre-create) it:

   ```bash
   # List entity-aliases on the JWT mount and find the operator's `sub`.
   vault list -format=json identity/entity-alias/id \
     | jq -r '.[]' \
     | while read -r id; do
         vault read -format=json "identity/entity-alias/id/$id" \
           | jq -r '.data | "\(.name)\t\(.mount_path)"';
       done
   # The `name` column for the operator must equal their JWT `sub`,
   # on mount_path "auth/jwt/".
   ```

   If the operator's secret path was created *before* their entity
   exists (e.g. an admin pre-seeds `secret/data/targets/<sub>/...`), the
   `<sub>` segment must match the JWT `sub` exactly — the templated
   policy renders that exact string and a mismatch yields a 403.

For the realm-side JWT-claim configuration (how Keycloak emits the
claims Vault reads), see
[`keycloak-tenant-claims.md`](./keycloak-tenant-claims.md) and
[`mcp-client-setup.md`](./mcp-client-setup.md) (audience).

## 4. Verification — can a given operator read a given `secret_ref`?

Run from a host with the **operator's** Vault token. Acquire one by
logging in with the operator's Keycloak JWT against the same role the
backplane uses (do not use a root/admin token — that would mask a
missing operator policy):

```bash
# Obtain the operator's JWT out-of-band (e.g. from Keycloak token
# endpoint), then log in to Vault exactly as the backplane does:
OP_JWT="<operator-keycloak-jwt>"        # placeholder — never commit a real JWT
vault write -field=token auth/jwt/login role=meho-mcp jwt="$OP_JWT" \
  | vault login -            # bind the issued token to this shell
```

Then prove the read against the target's `secret_ref`:

```bash
# Replace <op-sub> with the operator's JWT `sub` and <target> with the
# target name; this is the exact path the connector's loader reads.
vault kv get -mount=secret -format=json "targets/<op-sub>/<target>" \
  | jq '.data.data | keys'
# Expect for a REST target: ["password","username"]
# Expect for a k8s target:  ["kubeconfig"]
```

Expected outcomes:

- **Success** → `keys` lists `username`/`password` (or `kubeconfig`).
  The connector's live read will succeed; the target is at rubric
  State 2 for this operator.
- **`permission denied` (HTTP 403)** → the §2 policy is missing,
  mis-scoped, or the operator's entity-alias `name` does not match the
  `<op-sub>` segment. This is the exact failure that surfaces as
  [`VaultRoleDeniedError`](../../backend/src/meho_backplane/auth/vault.py#L99)
  from the connector. Fix §2 (policy) or §3 (identity), not the
  connector.
- **`No value found at secret/data/targets/...`** → the secret itself
  was never written. Provision it (§1) with the vendor service-account
  credential.

> **Negative check (prove the scoping holds).** As operator A, attempt
> to read operator B's path
> (`vault kv get -mount=secret "targets/<other-op-sub>/<target>"`). It
> **must** return `permission denied` — that is the templated policy
> doing its job. A success here means the path was templated wrong (a
> glob leaked into the identity segment) and every operator can read
> every secret.

## 5. Audit + least privilege

- **Per-operator attribution, for free.** Because the read runs under
  the operator's JWT, Vault's own audit device attributes every secret
  read to the operator's Identity entity (the `sub`), HMAC-hashing the
  values so the cleartext is never in Vault's logs. MEHO's synchronous
  audit row already records the operator + `target_id`. Dual
  attribution without extra machinery.
  ([Vault audit devices](https://developer.hashicorp.com/vault/docs/audit))
- **Least privilege.** The templated policy is the *only* grant a
  connector needs; the backplane never holds a god-mode Vault token.
  A compromised backplane can read no more than the *currently-acting
  operator* could — the blast-radius property the operator-context
  decision buys.
- **Never disable HMAC** on the audit device in production; never log
  the credential value. The chassis keeps `{username,password}` /
  kubeconfig out of every log event and every `OperationResult` — the
  policy doc's job is to keep it out of *Vault's* logs too (HMAC).

## 6. KV-v2 write surface — the `meho-mcp` write policy (v0.10.0+)

§2–§5 cover the **read** path every connector needs. v0.10.0 added a
KV-v2 **write** surface — `vault.kv.put` (new version, wholesale
replace), `vault.kv.patch` (merge fields onto the current version), and
`vault.kv.delete` (soft-delete versions)
([`connectors/vault/ops.py`](../../backend/src/meho_backplane/connectors/vault/ops.py)).
`vault.kv.metadata.delete` (evoila/meho#3424) adds the permanent removal of
a whole secret: every version and its metadata.
These ops register `requires_approval=True`, so a human/operator write
parks for a four-eyes review before it executes. **The review is wasted
if the write then hits Vault `permission denied`** — the templated §2
policy grants only `read`, with no `create`/`update` on the write path,
so a freshly-provisioned `meho-mcp` role denies every KV write.

This section documents the write-capability stanza the role needs, and a
copy-paste `sys/capabilities-self` command to verify a token can write a
path **before** an operator approves the parked write.

### 6.1 Write-capability policy stanza

> **Check the target's `vault_role` first.** A write runs under the
> operator's token, logged in with the `extras.vault_role` of the vault
> target it is dispatched on. When the target sets none, the role comes
> from the deployment's fallback chain: for a check run, the check-runner
> role or, when that is unset, the global default; for every other
> operator, the per-tenant role, then the global default (see §6.3 for
> the exact order). So before
> you add the stanza below, check which target the write ran on. Another
> registered vault target may already have a role that can write this
> path. If so, run the same write on that target. No policy change is
> needed.

Like the §2 reads, KV-v2 writes are scoped per operator through ACL
policy templating. KV-v2 authorizes each write against a **different**
path prefix: `vault.kv.put` / `vault.kv.patch` write the value under
`<mount>/data/<path>` (`create`/`update` there for `put`; `read`/`update`
for `patch`, which reads the secret before it writes), while `vault.kv.delete`'s
version soft-delete (`POST <mount>/delete/<path>`) is authorized by
`update` on the **`/delete/`** path — *not* the data path (evoila/meho#3274,
lab-verified in claude-rdc-hetzner-dc#2814). Add **both** stanzas below
alongside the §2 read grants (they are *additive* — keep all):

```hcl
# Per-target connector secret WRITES, scoped to the operator's own
# identity. <ACCESSOR> is the JWT mount accessor (resolve it as in §2).
# `create` covers a first write to a path; `update` covers a subsequent
# write (new version). Vault requires BOTH for an unconditional
# `vault.kv.put` on the /data/ path. `vault.kv.patch` reads the current
# version first and then writes to the existing secret, so it needs
# `read` + `update` here (never `create`, evoila/meho#4831).
path "secret/data/targets/{{identity.entity.aliases.<ACCESSOR>.name}}/*" {
  capabilities = ["create", "read", "update"]
}

# The version soft-delete (`vault.kv.delete`) POSTs to <mount>/delete/<path>
# and Vault authorizes it with `update` on that /delete/ path — a SEPARATE
# grant from the /data/ stanza above. Omit this and every delete is denied
# post-approval even though /data/ carries `update` (evoila/meho#3274).
path "secret/delete/targets/{{identity.entity.aliases.<ACCESSOR>.name}}/*" {
  capabilities = ["update"]
}

# Only if this identity may PERMANENTLY remove secrets
# (`vault.kv.metadata.delete`, evoila/meho#3424). The op sends
# DELETE <mount>/metadata/<path> and reads the same path before and after,
# so it needs `read` and `delete` on the /metadata/ path. Leave this out to
# keep permanent removal impossible for this identity.
path "secret/metadata/targets/{{identity.entity.aliases.<ACCESSOR>.name}}/*" {
  capabilities = ["read", "delete"]
}
```

Notes:

- **`create` *and* `update` are both required** for `vault.kv.put`.
  Vault treats a first write to a non-existent path as `create` and a
  write to an existing path as `update`; an unconditional write (no CAS
  guard) can be either, so the policy must grant both or the write fails
  the first time the path's existence flips.
- **`read` *and* `update` are required** for `vault.kv.patch`
  (evoila/meho#4831, evoila/meho#3897). The connector calls hvac's
  `secrets.kv.v2.patch`, which reads the current version (`GET`, needs
  `read`), merges the fields, and writes the result back to the existing
  secret with a version check (`POST`, needs `update`). A patch never
  writes a missing secret, so it never needs `create`. A role with
  `create` + `update` but no `read` fails at the read step. The
  park-time check (§6.2) asks for exactly `read` + `update` for a patch.
  Keeping `read` in the stanza above also lets the §2 read grant and this
  write grant collapse into one path block.
- **`vault.kv.delete` needs `update` on the `/delete/` path** — the
  version soft-delete POSTs to `<mount>/delete/<path>`, which Vault
  authorizes with `update` on *that* path, **not** `/data/`
  (evoila/meho#3274). This is why the second stanza above is required; the
  `/data/` grant alone does **not** authorize a soft-delete, so a policy
  that ships only `/data/` denies every delete post-approval. It is not a
  `delete` capability and not a `/metadata/` grant.
- **No `/metadata/` write grant is needed** for `put`, `patch` and the
  soft `delete`. `put` / `patch` operate on `/data/`; the version
  soft-delete operates on `/delete/`.
- **`vault.kv.metadata.delete` needs `read` and `delete` on the
  `/metadata/` path** (the third stanza above). `delete` there is what lets
  Vault remove every version and the metadata. `read` lets the op check the
  path before the delete (nothing there → nothing is changed) and after it
  (the op reports `deleted` only when the read finds nothing). Grant it only
  to an identity that should be able to purge secrets, for example a
  dedicated teardown role ([vault-provisioning.md](vault-provisioning.md)
  §8). Destroying single versions (`PUT <mount>/destroy/<path>`) is still
  **not** a wired op and needs no grant.
- The **same per-operator templating constraints** from §2 apply: no
  glob inside the rendered `{{identity...}}` segment; the trailing `/*`
  is a literal glob in the static path portion.

### 6.2 Verify a token can write a path (`sys/capabilities-self`)

The backplane runs this exact check at **park time** — when a
`vault.kv.put`/`patch`/`delete`/`metadata.delete` parks for approval, the
dispatcher probes `POST sys/capabilities-self` on the path the op
authorizes against (`secret/data/<path>` for `put`/`patch`,
`secret/delete/<path>` for `delete` — evoila/meho#3274,
`secret/metadata/<path>` for `metadata.delete` — evoila/meho#3424) and
stores the result as `permission_preflight` on the approval row. When the
token lacks the required capability (`will_be_denied: true`), MEHO does not
park the op at all: the caller gets `denied` / `write_capability_denied`
(evoila/meho#3542), so an operator is **not** asked to approve a write
that Vault will then reject
([`operations/dispatcher.py`](../../backend/src/meho_backplane/operations/dispatcher.py)
`_handle_needs_approval` →
[`connectors/vault/ops.py`](../../backend/src/meho_backplane/connectors/vault/ops.py)
`vault_kv_write_capability_preflight`). The probe returns only
**capability names** — never a secret value — so it sidesteps the
credential-class redaction rule that bars a value-revealing dry-run for
a credential write.

Run the same check by hand from a host holding the **operator's** Vault
token (acquire it as in §4 — do *not* use a root/admin token, which
would mask a missing operator grant):

```bash
# Replace <op-sub> with the operator's JWT `sub` and <target> with the
# target name. put/patch authorize on the /data/ path; the soft-delete
# authorizes on the /delete/ path (evoila/meho#3274).
vault token capabilities "secret/data/targets/<op-sub>/<target>"
# Expect for a writable put/patch path: create read update
vault token capabilities "secret/delete/targets/<op-sub>/<target>"
# Expect for a soft-delete-capable path: update
vault token capabilities "secret/metadata/targets/<op-sub>/<target>"
# Expect for a path this identity may permanently remove: delete read
```

Or, equivalently, against the raw API (what the backplane preflight
calls — `POST /v1/sys/capabilities-self`):

```bash
vault write -format=json sys/capabilities-self \
  paths="secret/data/targets/<op-sub>/<target>" \
  | jq '.data.capabilities'
# Expect: ["create","read","update"]  → the write will succeed
# A read-only role returns: ["read"]  → the write WILL be denied
```

Expected outcomes:

- **`create` + `update` present** (for `put`), or **`read` + `update`
  present** (for `patch`) → the parked write will execute on approval;
  the approval row's `permission_preflight.will_be_denied` is `false`.
- **Only `read` (or `deny`)** → the write will be denied. MEHO refuses to
  park it and returns `write_capability_denied` with `granted` and
  `required`. Fix §6.1 (add the write stanza) or §3 (identity-alias
  mismatch), not the connector, then call the operation again.

> **Reviewer-token caveat.** An *approved* re-dispatch executes under the
> **reviewing** operator's token, not the original dispatcher's
> ([`approvals.py`](../../backend/src/meho_backplane/api/v1/approvals.py)
> → `resume_dispatch_after_approval`). The park-time preflight runs under
> the **dispatching** operator's token (the only identity available at
> park time). Both operators authenticate against the same `meho-mcp`
> role, so they share this policy and the preflight is the right early
> signal — but the reviewer's token must carry the §6.1 write grant too,
> or the write fails post-approval despite a clean preflight. Verify the
> reviewer's token with the same command above when in doubt.

### 6.3 The write-identity contract (which identity writes, and its two signals)

The read path (§2–§5) and the write path use the **same** federated
identity model: every KV op runs under the acting **operator's**
OIDC-federated Vault token, never a shared god-mode backplane token. The
role the token logs in under is picked in this order (first match wins):

1. the dispatch target's `extras.vault_role`, when the target sets one
   (evoila/meho#3274);
2. for the synthetic background-dispatch operator (a check run) only: the
   dedicated check-runner role (`VAULT_CHECK_RUNNER_ROLE`,
   evoila/meho#2757). When that variable is unset, a check run uses the
   deployment-global `VAULT_OIDC_ROLE` directly and **skips step 3**;
3. a per-tenant role from `VAULT_OIDC_ROLE_BY_TENANT` matching the
   operator's tenant (evoila/meho#3852). A check run never reaches this
   step;
4. the deployment-global `VAULT_OIDC_ROLE` (usually `meho-mcp`).

So the write identity is the operator's JWT together with the role this
chain selects. When you diagnose a denied write on a target without a
`vault_role`, check steps 2–4 for the acting operator before you edit a
policy: the role was not necessarily `meho-mcp`. For a check run, the
per-tenant map does not matter; only the check-runner role or the global
default can apply. There is no separate
service-account identity for agent
writes in this model — an *agent-initiated* `vault.kv.put` still parks and
executes under a human operator's token (the dispatcher's at park time,
the reviewer's on approved re-dispatch; see the §6.2 reviewer-token
caveat). The contract a deploy must satisfy is therefore:

| Path | Acting identity | Required Vault capabilities on the templated path |
|------|-----------------|---------------------------------------------------|
| Read (`vault.kv.read`, credential resolution) | Operator's OIDC-federated token | `read` on `secret/data/…/{{identity…}}/*` (§2) |
| Write (`vault.kv.put`) | Operator's OIDC-federated token (dispatcher at park; **reviewer** on approved re-dispatch) | `create` + `update` on `secret/data/…/{{identity…}}/*` (§6.1) |
| Write (`vault.kv.patch`) | same | `read` + `update` on `secret/data/…/{{identity…}}/*` (§6.1); it reads before it writes and never needs `create` |
| Write (`vault.kv.delete` version soft-delete) | same | `update` on the data path (§6.1) |
| Write (`vault.kv.metadata.delete` permanent removal) | same | `read` + `delete` on `secret/metadata/…/{{identity…}}/*` (§6.1) |
| Write (`vault.token.role.write` create or change a token role) | same | `create` + `update` on `auth/token/roles/<role>` (§6.4) |
| Read (`vault.token.role.read` / `vault.token.role.list`) | same | `read` on `auth/token/roles/<role>`; `list` on `auth/token/roles` (§6.4) |

A deploy that follows only the §2 read grant provisions **read-only** and
discovers the missing write grant *after* an operator approves a parked
write. Two product signals make that gap visible without reading Vault's
logs:

- **Park-time refusal (before Approve).** The dispatcher runs the §6.2
  `sys/capabilities-self` probe under the dispatching operator's token.
  When the token lacks a right the write needs, MEHO does **not** park the op
  (evoila/meho#3542). The caller gets `status: denied` with
  `error_code: "write_capability_denied"`, `path`, `granted`, `required`
  and a `doc_ref` pointing back at this §6. No approval row is created, so
  no person spends an approval on a write that cannot land. If the write
  policy is landing in the same change, apply it first, then call the
  operation again. (Before #3542 this was only a warning,
  `write_capability_warning`, and the op parked anyway.)
- **Post-approval structured error (after Approve).** If the write is
  approved and Vault still denies it, the op result is **not** a bare
  `permission denied` or the read-oriented `connector_vault_forbidden`; it
  is the write-specific `error_code: "vault_write_identity_forbidden"` with
  `path` (the denied `secret/data/<path>`), `identity_hint` (the acting
  operator's `sub`), and `doc_ref` pointing back at this §6. It names the
  acting target and its `vault_role` when known, and lists two fixes in
  order: first, run the same write on a vault target whose `vault_role`
  can already write the path (no policy change); second, add the §6.1
  write stanza. It repeats the do-NOT-widen-the-shared-policy warning
  (the grant is per-operator templated, not role-wide).

To verify the contract for a given operator + target before wiring an
approval flow, run the §6.2 `sys/capabilities-self` probe under **that
operator's** token (and, once a reviewer is designated, theirs too).
Both must return `["create","read","update"]` on the target's
`secret/data/…` path for an approved write to land.

### 6.4 Token roles (`vault.token.role.write` / `.read` / `.list`)

A token role is a saved set of rules in Vault for making tokens: which
policies a token may get, whether it has a parent token, and how long it
lives between renewals. MEHO manages them with three operations on the
core `token` auth backend. They run under the same identity chain as §6.3
(the target's `vault_role`, else the deployment's fallback roles), so the
grants below go into the policy of the role that will manage token roles,
not into the shared read policy of every operator:

```hcl
# List the token role names (`vault.token.role.list`).
path "auth/token/roles" {
  capabilities = ["list"]
}

# Create or change one token role (`vault.token.role.write`) and read it
# (`vault.token.role.read`). Replace <role> with the role name this
# identity may manage, or end the path with a `*` glob to cover every
# token role.
path "auth/token/roles/<role>" {
  capabilities = ["create", "update", "read"]
}
```

Notes:

- **`create` *and* `update` are both required** for `vault.token.role.write`.
  Vault asks for `create` when the role does not exist yet and `update`
  when it does; the write is unconditional, so it can be either.
- **Park-time check.** Before MEHO asks for approval, it runs the §6.2
  `sys/capabilities-self` probe on `auth/token/roles/<role>` under the
  target's role. When `create` or `update` is missing, the call is refused
  with `error_code: "write_capability_denied"` and no approval request is
  written, like a KV write. Add the stanza above, then call the operation
  again.
- **Only the fields given are sent.** A write that names one setting leaves
  the other settings of an existing role as they are. Check the whole role
  with `vault.token.role.read` (needs `read` on the role path).
- **Nothing here lets the identity make tokens.** Making a token from a
  role is a separate operation with its own grant on `auth/token/create`
  (and `auth/token/create/<role>`); this section only covers the role
  settings.

### 6.5 Staging a Kubernetes Secret (`k8s.secret.read_to_ref`)

`k8s.secret.read_to_ref` reads one value from a Kubernetes Secret and
writes it to Vault. It is the way to register a guest cluster from its
kubeconfig without the kubeconfig reaching the agent. The write goes
through the same handler as `vault.kv.put`, so it needs the same
`create` + `update` grant. What is different is **where** it writes.

**The path is fixed.** The op always writes to

```text
secret/data/tenants/<tenant_id>/<register_as>
```

`<tenant_id>` is the caller's tenant. `<register_as>` is the target name
the caller passes. You cannot choose another path, for two reasons:

- `meho targets create --name <register_as>` without a `secret_ref`
  reads this same path. That is how the staged kubeconfig reaches the new
  target.
- The tenant-scope guard (on by default) refuses a Vault write outside
  `secret/tenants/<tenant_id>/`. A path such as `secret/meho/...` would be
  refused by MEHO before it reaches Vault.

**Which role writes.** The op writes with no Vault target. So step 1 of
the §6.3 chain (a target's `vault_role`) never applies. The write uses
the per-tenant role from `VAULT_OIDC_ROLE_BY_TENANT`, or else the global
`VAULT_OIDC_ROLE` (usually `meho-mcp`). Running the op on another vault
target does not change the role.

**The grant it needs.** The policies of that role, or the identity-group
policies of the caller, must give `create` + `update` on the tenant
subtree:

```hcl
path "secret/data/tenants/{{identity.entity.metadata.tenant_id}}/*" {
  capabilities = ["create", "read", "update"]
}
```

The per-tenant policies in
[`connector-vault-tenant-policy.md`](./connector-vault-tenant-policy.md)
already handle this:

- `meho-tenant-admin` grants it. A `tenant_admin` can run the op.
- `meho-tenant-operator` does **not** grant it. That policy can write
  only under `tenants/<tenant_id>/rotation/*`. An `operator` gets
  `write_capability_denied` at park time.
- `meho-tenant-read-only` does not grant it.

Do not add this grant to the shared `meho-mcp` policy for everyone. Give
it through the per-tenant policy of the people who register clusters.

**The tenant metadata must be set.** The templated path above only works
when the caller's Vault entity carries the `tenant_id` metadata
([`connector-vault-tenant-policy.md`](./connector-vault-tenant-policy.md)
§3). Without it, the grant does not apply and Vault denies the write.

**Both people need the grant.** The op needs approval. MEHO checks the
grant for the person who calls the op, at park time. After approval, the
write runs with the token of the person who approves (see the §6.2 note
on the reviewer token). So the approver needs the same grant. Check both
with §6.2, using the path above:

```bash
vault token capabilities "secret/data/tenants/<tenant-uuid>/<register_as>"
# Expect: create read update
```

## Scope note — `shared_service_account` only; `per_user`/`impersonation` deferred

This runbook covers the **`shared_service_account`** auth model: one
vendor service-account credential per target, stored in Vault, read
under the operator's identity. That is the only model the connectors
wire today; their `auth_headers` raise a clear `NotImplementedError`
naming the target for `per_user` and `impersonation`
([research §4](../research/214-connector-credential-broker.md)).

- **`per_user`** (each operator has their own vendor credential) is a
  natural extension of §2's templating — the secret path is *already*
  per-operator, so a future `per_user` loader keys on
  `operator.sub` with no policy change. Deferred until a concrete need
  exists.
- **`impersonation`** (the backplane authenticates as a privileged
  account and asks the vendor to act *as* the operator) needs
  vendor-side support and likely RFC 8693 token exchange. Deferred.

System-initiated calls (background/scheduled work with no operator JWT)
**cannot** perform an operator-context read and are out of scope for
v0.x — see the carve-out in
[`connector-auth.md`](../architecture/connector-auth.md).

## References

- Decision (operator-context Vault read):
  [`docs/architecture/connector-auth.md`](../architecture/connector-auth.md)
- Research (audit/least-privilege §5, templated-policy §2):
  [`docs/research/214-connector-credential-broker.md`](../research/214-connector-credential-broker.md)
- Federation-chain provisioning (auth method, role, KV mount):
  [`vault-provisioning.md`](./vault-provisioning.md)
- Implementation that consumes this config:
  [`backend/src/meho_backplane/auth/vault.py`](../../backend/src/meho_backplane/auth/vault.py#L198)
  (role/mount from `vault_oidc_role` / `vault_oidc_mount_path`)
- Connector secret-ref convention (k8s `kubeconfig` field):
  [`kubernetes-onboarding.md`](./kubernetes-onboarding.md)
- First connector wired against this runbook (vSphere `{username,password}`
  read, rubric State 2): [`vmware-rest-onboarding.md`](./vmware-rest-onboarding.md)
  (G3.9-T3 #942)
- Vault — [ACL policy templating](https://developer.hashicorp.com/vault/docs/concepts/policies)
- Vault — [ACL policy capabilities (`create`/`update`/`read`/`delete`)](https://developer.hashicorp.com/vault/docs/concepts/policies#capabilities)
- Vault — [`POST sys/capabilities-self`](https://developer.hashicorp.com/vault/api-docs/system/capabilities-self)
  (returns a token's capabilities on a path; no secret material)
- Vault — [KV v2 secrets engine API](https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2)
  (the `/data/` write path the §6 stanza grants)
- Vault — [JWT/OIDC auth method](https://developer.hashicorp.com/vault/docs/auth/jwt)
- Vault — [Audit devices](https://developer.hashicorp.com/vault/docs/audit)
- Park-time preflight implementation:
  [`backend/src/meho_backplane/operations/dispatcher.py`](../../backend/src/meho_backplane/operations/dispatcher.py)
  (`_handle_needs_approval`) +
  [`backend/src/meho_backplane/connectors/vault/ops.py`](../../backend/src/meho_backplane/connectors/vault/ops.py)
  (`vault_kv_write_capability_preflight`)
