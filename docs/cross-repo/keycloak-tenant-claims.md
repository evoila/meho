<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Keycloak tenant-claims recipe — `tenant_id` + `tenant_role` protocol mappers

> Cross-repo handshake between `evoila/meho` (this repo, producer of the
> v0.2 backplane that **reads** `tenant_id` + `tenant_role` from the
> JWT) and the operator's Keycloak realm (consumer side; not a single
> repo — every MEHO deployment has its own realm).
>
> This page is the upstream-side **tracker** for the realm-side
> configuration each consumer must apply before deploying the v0.2
> backplane. The configuration itself is operator-applied in the
> Keycloak Admin Console (or the consumer's own IaC); what lives here
> is the recipe the operator follows and the verification commands
> either side can run to prove the contract holds.

## Why this doc exists

The v0.2 backplane (Initiative
[#222](https://github.com/evoila/meho/issues/222)) extracts two new
claims from the access token on every authenticated request:

- **`tenant_id`** — UUID of the tenant the operator acts on behalf of;
  used as the row-scoping key on `audit_log` and on every per-tenant
  retrieval / target / convention query that lands in G4–G9.
- **`tenant_role`** — one of `tenant_admin` / `operator` / `read_only`;
  used by the `require_role(min_role)` FastAPI dependency to gate
  routes that change state.

The v0.1 chassis-era access tokens carry only the OIDC standard claim
set (`sub`, `name`, `email`, `aud`, `iss`, `exp`, `iat`, `jti`).
Without `tenant_id` and `tenant_role` added by realm-side protocol
mappers, every v0.2 authenticated request returns
**`401 missing_tenant_claim`**, and operators upgrading from v0.1 to
v0.2 will not know why. This doc is the realm-side recipe that closes
that gap.

The backplane cannot enforce realm configuration; it can only
fail-closed when the claims are absent. This doc is the contract that
specifies what the realm must produce.

## Prerequisites

- Keycloak admin access to the realm where MEHO operators authenticate
  (the realm whose issuer URL is configured as
  `KEYCLOAK_ISSUER_URL` in the backplane's
  [`Settings`](../codebase/backend.md)).
- The realm already issues access tokens to a client whose `aud` claim
  matches the backplane's `KEYCLOAK_AUDIENCE`. v0.1 deployments meet
  this prerequisite — the chassis-era login flow already works.
- One UUID for the tenant. Mint it with `uuidgen` or
  `python3 -c "import uuid; print(uuid.uuid4())"`, and write it down.
  For now each site has one tenant.
- Keycloak version **22+**. This recipe uses only **stock** OIDC
  protocol mappers (no scripts). The Admin Console labels below match
  Keycloak 22 through 26.x.

## The recipe: five groups give the tenant and the role

A person gets their tenant and their role from their Keycloak group.
The full decision is in
[`docs/decisions/login-role-groups.md`](../decisions/login-role-groups.md).
In short:

- The realm has five top-level groups. Each group carries `tenant_id`
  and `tenant_role` as **group attributes** (key-value settings on the
  group). `MEHO-Approvers` also carries `approver` = `true`.
- Each human login client (`meho-cli`, `meho-mcp`) has three stock
  **User Attribute** mappers. They copy `tenant_id`, `tenant_role` and
  `approver` into the access token.
- Each person is in **exactly one** of the five groups.

| Group | `tenant_role` | `approver` |
| --- | --- | --- |
| `MEHO-Readers` | `read_only` | – |
| `MEHO-Operators` | `operator` | – |
| `MEHO-SA-Operators` | `operator` | – |
| `MEHO-Admins` | `tenant_admin` | – |
| `MEHO-Approvers` | `read_only` | `true` |

All five groups carry the same `tenant_id`.

**Why a User Attribute mapper can read a group attribute.** The stock
User Attribute mapper first looks at the person. When the person has no
value of their own, it takes the value from the person's groups (and
from a parent group when the group has none). This is
`KeycloakModelUtils.resolveAttribute`, which `UserAttributeMapper`
calls — see the
[Keycloak 26.8.1 source](https://github.com/keycloak/keycloak/blob/26.8.1/server-spi-private/src/main/java/org/keycloak/models/utils/KeycloakModelUtils.java#L768-L797)
and the
[mapper that calls it](https://github.com/keycloak/keycloak/blob/26.8.1/services/src/main/java/org/keycloak/protocol/oidc/mappers/UserAttributeMapper.java#L96-L104).
So you need no script and no fixed value. Earlier versions of this page
said the opposite; that was wrong.

### The fast way: `meho admin keycloak bootstrap-clients`

The CLI does Steps 1 and 2 for you, and puts the first admin user in
`MEHO-Admins` and `MEHO-Platform-Admins`. It also adds the
`meho_tenant` attribute and the `meho-tenants` mapper (see
[Several tenants](#several-tenants-the-meho_tenants-list)). It is safe
to run twice:

```bash
KEYCLOAK_ADMIN_PASSWORD=... KEYCLOAK_ADMIN_USER_PASSWORD=... \
meho admin keycloak bootstrap-clients \
  --keycloak-base-url https://keycloak.example.org \
  --realm <realm-name> \
  --admin-username admin \
  --mcp-resource-uri https://meho.example.org/mcp \
  --tenant-id <tenant-uuid> \
  --admin-user-username <first-admin>
```

Then do Step 3 for everyone else. The steps below are the manual path,
and they explain what the command does.

### Step 1 — Create the five groups

For each of the five groups in the table above:

1. **Groups** → **Create group** → **Name:** the group name, exactly
   as written (for example `MEHO-Operators`). Create it at the top
   level, not inside another group.
2. Open the group → **Attributes** → add:
   - `tenant_id` = your tenant UUID;
   - `tenant_role` = the value from the table (`read_only`, `operator`
     or `tenant_admin` — with an underscore, never a hyphen);
   - on `MEHO-Approvers` only: `approver` = `true`.
3. **Save.**

The same with `kcadm.sh` (the Keycloak admin command line). Run it
after `kcadm.sh config credentials`, with `REALM` and `TENANT_ID` set:

```bash
kcadm.sh create groups -r "$REALM" -s name=MEHO-Readers \
  -s "attributes.tenant_id=[\"$TENANT_ID\"]" -s 'attributes.tenant_role=["read_only"]'
kcadm.sh create groups -r "$REALM" -s name=MEHO-Operators \
  -s "attributes.tenant_id=[\"$TENANT_ID\"]" -s 'attributes.tenant_role=["operator"]'
kcadm.sh create groups -r "$REALM" -s name=MEHO-SA-Operators \
  -s "attributes.tenant_id=[\"$TENANT_ID\"]" -s 'attributes.tenant_role=["operator"]'
kcadm.sh create groups -r "$REALM" -s name=MEHO-Admins \
  -s "attributes.tenant_id=[\"$TENANT_ID\"]" -s 'attributes.tenant_role=["tenant_admin"]'
kcadm.sh create groups -r "$REALM" -s name=MEHO-Approvers \
  -s "attributes.tenant_id=[\"$TENANT_ID\"]" -s 'attributes.tenant_role=["read_only"]' \
  -s 'attributes.approver=["true"]'
```

The `tenant_id` on the groups is the source of truth for the tenant.
Renaming a group is safe (but the next login then needs the new name).
**Changing `tenant_id` is a breaking change:** every audit row keyed on
the old UUID becomes unreachable.

### Step 2 — Add the three User Attribute mappers

Add three mappers to **each** human login client (`meho-cli` and
`meho-mcp`). In the Admin Console: **Clients** → the client →
**Client scopes** → its **dedicated** scope → **Mappers** →
**Add mapper** → **By configuration** → **User Attribute**.

| Setting | `tenant_id` mapper | `tenant_role` mapper | `approver` mapper |
| --- | --- | --- | --- |
| Name | `tenant-id` | `tenant-role` | `approver` |
| User Attribute | `tenant_id` | `tenant_role` | `approver` |
| Token Claim Name | `tenant_id` | `tenant_role` | `approver` |
| Claim JSON Type | `String` | `String` | `boolean` |
| Add to ID token | off | off | off |
| Add to access token | **on** | **on** | **on** |
| Add to userinfo | on | on | on |
| Multivalued | off | off | off |
| Aggregate attribute values | off | off | off |

**Add to access token** must be on: the backplane reads the access
token. **Add to userinfo** on makes [Check 1](#verification) below
work. Keep **Multivalued** and **Aggregate attribute values** off, so
each claim is one value and not a list (a list is rejected).

The same with `kcadm.sh` for one client (repeat for each client and
each of the three mappers; change `name`, `user.attribute`,
`claim.name` and `jsonType.label`):

```bash
CID=$(kcadm.sh get clients -r "$REALM" -q clientId=meho-cli \
  --fields id --format csv --noquotes)
kcadm.sh create "clients/$CID/protocol-mappers/models" -r "$REALM" -f - <<'JSON'
{
  "name": "tenant-role",
  "protocol": "openid-connect",
  "protocolMapper": "oidc-usermodel-attribute-mapper",
  "config": {
    "user.attribute": "tenant_role",
    "claim.name": "tenant_role",
    "jsonType.label": "String",
    "id.token.claim": "false",
    "access.token.claim": "true",
    "userinfo.token.claim": "true",
    "introspection.token.claim": "true",
    "multivalued": "false",
    "aggregate.attrs": "false"
  }
}
JSON
```

Put the mappers on the clients that **people** use. Do not put them on
a client scope that service accounts or agent clients share: those get
their own claims on their own confidential clients.

### Step 3 — Put each person in one group

For each person who should use MEHO: **Users** → the person →
**Groups** → **Join group** → pick **one** of the five groups. Or with
`kcadm.sh`:

```bash
USER_ID=$(kcadm.sh get users -r "$REALM" -q username=alice \
  --fields id --format csv --noquotes)
GROUP_ID=$(kcadm.sh get groups -r "$REALM" -q search=MEHO-Operators \
  --fields id,name --format csv --noquotes | grep ',MEHO-Operators$' | cut -d, -f1)
kcadm.sh update "users/$USER_ID/groups/$GROUP_ID" -r "$REALM" -n
```

(Avoid the shell names `UID` and `GID`: they are read-only in bash and
zsh.)

The rules:

- **Exactly one group.** A person in two of the five groups is
  refused: the backplane answers `401 multiple_role_groups` and names
  the groups (#4594). Without that check, Keycloak would copy the role
  of whichever group it finds first.
- **No group, no access.** A person in none of the five groups gets no
  `tenant_id`, and the backplane answers `401 missing_tenant_claim`.
- **Leave the person's own `tenant_id` / `tenant_role` / `approver` /
  `meho_tenant` attributes empty.** A value on the person wins over the
  group, and a `meho_tenant` value on the person is added to their
  tenant list.
- **People must not be able to set these attributes on themselves.**
  Keep **Realm settings → General → Unmanaged Attributes** at
  **Disabled** (the Keycloak default), **Admin can view** or **Admin
  can edit**. With **Enabled**, a person can set their own
  `tenant_role` in the account console, and it wins over their group
  ([Keycloak docs](https://www.keycloak.org/docs/26.8.1/server_admin/#_understanding-managed-and-unmanaged-attributes)).
  Do not add these three names to the user profile as attributes a
  user can edit, and do not let a directory (LDAP) mapper write them.
- **To change a role, move the person to another group.** The change
  applies at the person's next login (or when their current token
  expires).

A company directory (Active Directory, LDAP) or a login system in front
of Keycloak (Entra ID) can fill the groups for you: map the directory
group to one of the five Keycloak groups. The recipes for that come
with [#3959](https://github.com/evoila/meho/issues/3959).

### Several tenants: the `meho_tenants` list

One person can be in role groups of several tenants, one role group per
tenant. The design is in
[2026-10-28-several-tenants-per-person.md](../decisions/2026-10-28-several-tenants-per-person.md).
The realm side:

- **The attribute.** Every role group carries `meho_tenant` =
  `<tenant-uuid>:<tenant>:<Role>`, for example
  `3f0c…:main:Operators`. `<tenant>` is a short name (lowercase letters,
  digits, dashes). `<Role>` is `Readers`, `Operators`, `SA-Operators`,
  `Admins` or `Approvers`. The five groups of the first tenant keep
  `tenant_id`, `tenant_role` and `approver` as well.
- **Extra tenants.** Each one gets five groups
  `MEHO-<tenant>-Readers|Operators|SA-Operators|Admins|Approvers` that
  carry **only** `meho_tenant`. No `tenant_id`, no `tenant_role`: an
  older MEHO refuses a person who is only in these groups.
- **The mapper** `meho-tenants`, on the clients people log in with:
  stock User Attribute mapper, **User Attribute** `meho_tenant`,
  **Token Claim Name** `meho_tenants`, **Claim JSON Type** `String`,
  **Multivalued** on, **Aggregate attribute values** on, access token
  and userinfo on, ID token off. Keycloak then lists the value of every
  group of the person.
- **The standard setups make all of this, safe to run twice.**
  `meho admin keycloak bootstrap-clients --tenant-name <name>` names the
  first tenant (default `main`), and `--add-tenant <name>
  [--add-tenant-id <uuid>]` adds one tenant's five groups. The built-in
  Keycloak reads `coreKeycloak.tenantName` and the list
  `coreKeycloak.tenants` (`{name, id}` each).
- **A site with one tenant** gets a list with one entry. Every other
  claim stays the same.

How MEHO reads the list is a separate step; until then MEHO keeps
reading `tenant_id` and `tenant_role` only.

### Upgrading a realm that still has fixed values

Older realms often have **Hardcoded claim** mappers that put the same
`tenant_id` and `tenant_role` into every token of the client. Remove
them:

- **Run `meho admin keycloak bootstrap-clients` again.** It turns the
  fixed-value `tenant-id` and `tenant-role` mappers it made earlier
  into User Attribute mappers, adds the `approver` mapper, creates the
  five groups, and puts the first admin user in `MEHO-Admins`. It
  prints `[updated] mapper tenant-id (replaced
  oidc-hardcoded-claim-mapper with oidc-usermodel-attribute-mapper)`.
  It does not delete old groups (for example `meho-admins`) or move
  other people. The first admin user stays in the old group too; take
  them out of it once nothing uses it.
- **Pass the same `--tenant-id` as before.** Without it the command
  writes the all-zero UUID to the five groups, which moves everyone to
  another tenant.
- **Or by hand:** delete every Hardcoded claim mapper for `tenant_id`
  or `tenant_role` on the client and on its client scopes, then do
  Steps 1–3.

Before you remove the fixed values, put every person in one of the
five groups. Otherwise they lose access at their next login.

## Verification

Three checks. Run them after applying the recipe and before
considering the realm "v0.2-ready". Checks 1 and 2 prove the realm
half (claims appear on userinfo and on the access token); Check 3
proves the realm + backplane contract end-to-end.

### Check 1 — Claims appear on the userinfo endpoint

The OIDC userinfo endpoint returns the same claims the access token
carries (when the mapper has **Add to userinfo: on**). Easiest
end-to-end check:

```bash
# Mint a token via the device-code flow or any other interactive flow
# the operator normally uses (the meho login CLI, kcadm, curl).
TOKEN="<the access token from the operator's login>"
ISSUER="https://keycloak.example.org/realms/<realm-name>"

curl -sS -H "Authorization: Bearer $TOKEN" \
  "$ISSUER/protocol/openid-connect/userinfo" | jq
```

Expected output (the `sub` / `name` / `email` claims will reflect
the operator; `tenant_id` and `tenant_role` are what this recipe
adds):

```json
{
  "sub": "f:1c4d3...:operator-alice",
  "email": "alice@example.org",
  "name": "Alice Operator",
  "tenant_id": "9b7c2e10-3d44-4f6a-91b5-1de8c7a92f04",
  "tenant_role": "operator"
}
```

If `tenant_id` or `tenant_role` is missing, either the person is in
none of the five groups, or the mapper for that claim is misconfigured
(most likely **Add to access token / userinfo** is off).

### Check 2 — Decoded access token carries the claims

Userinfo reflects the access token's claim set, but the backplane
reads from the access token directly. Spot-check by decoding:

```bash
# Decode the JWT body (no signature verification — we only want to
# confirm the claim shape). JWT payloads are base64url-encoded with
# padding stripped; plain `base64 -d` mishandles both the alphabet
# and the missing padding. Use Python's urlsafe_b64decode and pad
# back to a multiple of 4:
echo "$TOKEN" | cut -d. -f2 | python3 -c '
import base64, json, sys
p = sys.stdin.read().strip()
p += "=" * (-len(p) % 4)
print(json.dumps(json.loads(base64.urlsafe_b64decode(p)), indent=2))
'
```

Expected: a JSON object with `tenant_id` (UUID string) and
`tenant_role` (one of `tenant_admin` / `operator` / `read_only`) at
top level, alongside the standard OIDC claims.

If the access token does not carry the claims but the userinfo
endpoint does, the **Add to access token** toggle on the mapper is
off — fix it on the same screen as the mapper definition.

### Check 3 — `meho status` succeeds against the v0.2 backplane

End-to-end check against a deployed v0.2 backplane (the only check
that proves the contract end-to-end; the first two prove the realm
half).

```bash
meho login                  # interactive Keycloak device-code flow
meho status                 # authenticated probe
# Expected: 200 OK, body shows operator + tenant + role.
```

Failure modes:

- **`401 missing_tenant_claim`** — the access token Keycloak issued
  to `meho login` does not carry one of `tenant_id` / `tenant_role`.
  Re-check Check 1 against the same token.
- **`401 malformed_tenant_claim`** — `tenant_id` is not a UUID
  string. Re-check the `tenant_id` attribute on the group, and the
  mapper's **Claim JSON Type** (must be `String`) and **Multivalued** /
  **Aggregate attribute values** (both off).
- **`401 unknown_tenant_role`** — `tenant_role` is a string but not
  one of `tenant_admin` / `operator` / `read_only`. Most common
  causes: a typo in the group's `tenant_role` attribute (for example
  `read-only` with a hyphen), or a leftover **User Realm Role** mapper
  that forwards realm-role names such as `meho-operator`. Fix the
  attribute ([Step 1](#step-1--create-the-five-groups)) or delete the
  role mapper.

## The `tenant` row is seeded just-in-time — no manual step

There is **no** out-of-band tenant-seed step. You do **not** run a
`psql "INSERT INTO tenant …"` against a fresh deploy, and you do not
need a tenant-provisioning API call before the first write.

The backplane provisions the `tenant` row just-in-time from the
verified `tenant_id` claim. The seed runs in the
`verify_jwt_and_bind` middleware that **every** authenticated route
flows through (via `require_role`), so the **first authenticated
request** whose JWT carries a given `tenant_id` — any verb, reads
and `dry_run` included — triggers an idempotent
`INSERT INTO tenant (id, slug, name) … ON CONFLICT (id) DO NOTHING`
committed before the route runs (G0.8-T1, #628). The `slug` / `name`
default to `tenant-<full-uuid>` (the canonical hyphenated UUID, so
the slug is as unique as the primary key — `tenant.slug` has its own
`UNIQUE` index that `ON CONFLICT (id)` does not guard); that
placeholder is cosmetic and carries no FK — a future v0.3
tenant-provisioning API can rename it without touching any joined
data, and the just-in-time path never overwrites a row that already
exists.

Practical consequences for an operator standing up a v0.2 deploy:

- After the realm recipe above is applied and `meho login` mints a
  token carrying `tenant_id` / `tenant_role`, the **first
  authenticated request** of any kind — a read, a `dry_run` ingest,
  or a real write — seeds the tenant in middleware before the route
  runs. Before #628 the first real write failed with an asyncpg
  `documents_tenant_id_fkey` violation because the `tenant` table
  was empty; that failure mode is gone.
- Because the seed is in middleware, a `dry_run` ingest seeds the
  tenant just like any other authenticated request. A green
  acceptance smoke — even one that only exercises `dry_run` or a
  read endpoint — therefore **does** prove the row exists.
- Concurrent first requests for the same tenant are safe — the
  `ON CONFLICT DO NOTHING` means exactly one row lands regardless of
  how many requests race.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `401 missing_tenant_claim` on every request | Mapper not on the access token | On the mapper screen, toggle **Add to access token** on |
| `401 missing_tenant_claim` for one person only | The person is in none of the five groups | Put the person in one group ([Step 3](#step-3--put-each-person-in-one-group)) |
| `401 malformed_tenant_claim` | The `tenant_id` mapper has **Aggregate attribute values** or **Multivalued** on (emits a JSON array instead of a single string), or **Claim JSON Type** `int` / `JSON` instead of `String` | Set both toggles off and the type to `String`, then log in again |
| `401 unknown_tenant_role` | The group's `tenant_role` attribute is not exactly `tenant_admin` / `operator` / `read_only` (for example `read-only` with a hyphen), or a leftover **User Realm Role** mapper forwards realm-role names like `meho-operator` | Fix the group attribute ([Step 1](#step-1--create-the-five-groups)); delete the User Realm Role mapper |
| `401 multiple_role_groups` for one person only | The person is in two or more of the five groups (the message names them) | Keep the person in exactly one group, then log in again |
| A person gets the wrong role, or a different role from one login to the next | The person has their own `tenant_role` attribute (which wins over the group) | Clear the person's own `tenant_id` / `tenant_role` / `approver` attributes |
| Every person gets the same role, whatever their group | A **Hardcoded claim** mapper for `tenant_role` or `tenant_id` is still on the client or one of its client scopes | Delete it, or re-run `meho admin keycloak bootstrap-clients` ([upgrading](#upgrading-a-realm-that-still-has-fixed-values)) |
| `403 Forbidden` on a route that worked before | The person's `tenant_role` is now below what the route requires (for example they moved to `MEHO-Readers`) | Move the person to the right group. Tokens already issued keep the old role until they expire (or the person runs `meho login` again). |
| Claims appear on userinfo but not on the access token | Mapper has **Add to userinfo: on** but **Add to access token: off** | Toggle both on |
| Mapper changes don't take effect for already-logged-in operators | Cached access tokens are still being sent until they expire | Wait for token expiry (or invoke `meho login` again to mint a fresh token) |

## Side note — the `platform_admin` cross-tenant flag (#1638)

`tenant_role` is scoped **within** a single tenant. A separate,
**optional** boolean claim — `platform_admin` — marks a principal that
holds the cross-tenant *platform* capability (e.g. an MSP operator
managing every tenant). The backplane reads it into
`Operator.platform_admin` and defaults it to **`False`** when the claim
is absent or malformed, so every existing token — and every agent /
service principal — is non-platform-admin unless the realm explicitly
grants the claim (fail-closed). The backplane reads the claim under the
name `platform_admin` by default, overridable with
`JWT_PLATFORM_ADMIN_CLAIM_NAME`.

Recommended mapper shape: a **Hardcoded claim** mapper
(`Token Claim Name: platform_admin`, `Claim value: true`,
`Claim JSON Type: boolean`, *Add to access token: on*) attached to a
dedicated realm role (e.g. `meho-platform-admin`), so only users granted
that role carry `"platform_admin": true`. Users without the role carry no
claim and resolve to `False`. A per-user boolean attribute + User
Attribute mapper is the script-free alternative (the same mapper type
the `tenant_role` recipe uses). The backplane also accepts the string forms `"true"` /
`"false"` for realms whose mapper emits the claim as a string.

**Alternative source — a realm role, no mapper (`JWT_PLATFORM_ADMIN_ROLE_NAME`).**
Realms that already express platform authority as a **realm role** (the
role list Keycloak emits under `realm_access.roles` via the default
roles scope) need not add the Hardcoded-claim mapper above. Set
`JWT_PLATFORM_ADMIN_ROLE_NAME` (settings field
`Settings.jwt_platform_admin_role_name`) to the role's name and the
backplane grants `platform_admin` when **either** the boolean claim
says true **or** that exact role name appears in `realm_access.roles`.
The setting is **unset by default** — behaviour is then exactly the
boolean-claim path above, so no existing deployment changes. The match
is **exact string equality** (no prefix / substring: `meho-admin` never
matches `meho-admins`), and the source stays **fail-closed** — a
malformed `realm_access` shape (`realm_access` not an object, `roles`
not a list of strings) or an absent role resolves to `False`, and a
malformed shape is logged under `malformed_platform_admin_realm_access`.

> Status: the backplane primitive (the `platform_admin` field + claim
> extraction) landed under [#1638]. Both standard setups now make the
> group `MEHO-Platform-Admins` (attribute `platform_admin` = `true`) and
> a User Attribute mapper `platform-admin`, and put the first admin in
> the group: the built-in Keycloak since #4043, and
> `meho admin keycloak bootstrap-clients` since #5237.

## The `approver` approve-only capability — group-based recipe (#3243)

`tenant_role` gates *dispatch* (who may run operations). A separate,
**optional** boolean claim — `approver` — gates the *approvals plane*
(list / show / approve / reject / decide) **without** granting dispatch.
[#3243](https://github.com/evoila/meho/issues/3243) (backplane side
[#3270](https://github.com/evoila/meho/issues/3270)) lifts it onto
`Operator.approver` so a **dedicated approver** can be provisioned as
`read_only` role **+** `approver=true`: it clears a four-eyes gate yet is
denied `POST /api/v1/operations/call` (dispatch stays
`require_role(operator)`, and `read_only` ranks below `operator`). This
recipe is the realm-side provisioning path — without it the capability is
code that reads a claim nothing mints.

### What the backplane reads

- **Claim name:** `approver` by default, overridable with
  `JWT_APPROVER_CLAIM_NAME` (settings field
  `Settings.jwt_approver_claim_name`). The Token Claim Name the mapper
  emits **must** equal this value.
- **Accepted shapes** (`auth/jwt._extract_approver`): a JSON boolean
  `true` / `false` (the canonical mapper output for
  **Claim JSON Type: boolean**) **or** the strings `"true"` / `"false"`
  (case-insensitive) for realms whose mapper emits the claim as a string.
- **Fail-closed:** an **absent** claim, `null`, a number, an object, or
  any unrecognised string resolves to **`False`** — so every existing
  token, and every principal whose token does not carry the claim, is a
  non-approver unless the realm explicitly grants it. A
  present-but-malformed value is logged `malformed_approver_claim` and
  still fails closed.
- **Gate:** `auth/rbac.require_approvals_access` admits a principal that
  is `operator`-or-higher **or** carries `approver=true`. Dispatch
  (`POST /api/v1/operations/call`) is untouched — it stays
  `require_role(operator)`, and the `sub`-keyed self-approval refusal
  (#1401) still applies (an approver cannot approve a request it parked).

### Recipe — put the person in `MEHO-Approvers`

The `approver` claim comes from the same group model as the tenant and
the role ([the recipe](#the-recipe-five-groups-give-the-tenant-and-the-role)):

- The `MEHO-Approvers` group carries `approver` = `true`, plus
  `tenant_id` and `tenant_role` = `read_only`
  ([Step 1](#step-1--create-the-five-groups)).
- The `approver` User Attribute mapper on each human login client
  copies it into the token, with **Claim JSON Type** `boolean`
  ([Step 2](#step-2--add-the-three-user-attribute-mappers)). Keycloak
  turns the string `"true"` into the JSON boolean `true`.
- To make a person an approver, put them in `MEHO-Approvers`
  ([Step 3](#step-3--put-each-person-in-one-group)), and in no other
  of the five groups.

```bash
USER_ID=$(kcadm.sh get users -r "$REALM" -q username=approver-bob \
  --fields id --format csv --noquotes)
GROUP_ID=$(kcadm.sh get groups -r "$REALM" -q search=MEHO-Approvers \
  --fields id,name --format csv --noquotes | grep ',MEHO-Approvers$' | cut -d, -f1)
kcadm.sh update "users/$USER_ID/groups/$GROUP_ID" -r "$REALM" -n
```

The other four groups carry no `approver` attribute, so their members
get no `approver` claim and are not approvers. Operators and admins do
not need it: `operator` and higher already pass the approvals gate.
`meho admin keycloak bootstrap-clients` creates the group and the
mapper.

### Verification

Two operator checks against a live realm, plus a code-side proof that
runs without one.

**Check A — decoded access token carries the claim.** Mint a token for a
member and decode its body (reuse the base64url decoder from
[Check 2](#check-2--decoded-access-token-carries-the-claims) above):

```bash
echo "$TOKEN" | cut -d. -f2 | python3 -c '
import base64, json, sys
p = sys.stdin.read().strip(); p += "=" * (-len(p) % 4)
print(json.loads(base64.urlsafe_b64decode(p)).get("approver"))
'
# Member  → True ;  non-member → None (claim absent → backplane reads false)
```

**Check B — the decouple #3243 promises (operator execution step).**
Requires a deployed backplane and a parked `requires_approval` request.
With a `read_only` **+** `approver=true` token, the approvals decide route
clears the gate while dispatch stays refused:

```bash
# Clears require_approvals_access (200, decision applied):
curl -sS -X POST -H "Authorization: Bearer $APPROVER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"decision":"approved"}' \
  "$BASE/api/v1/approvals/$REQUEST_ID/decide"

# Refused before the dispatcher runs (HTTP 403 insufficient_role — the
# require_role(operator) dependency rejects read_only):
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  -H "Authorization: Bearer $APPROVER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"connector_id":"...","op_id":"...","params":{}}' \
  "$BASE/api/v1/operations/call"
# → 403
```

**Code-side proof (no live realm — runs in CI).** The claim-extraction
and decouple behaviour the two live checks confirm is pinned by the
#3270 unit suite; run it to prove the semantics the mapper feeds into:

```bash
cd backend && uv run pytest \
  tests/test_auth_approver.py \
  tests/test_auth_rbac.py \
  tests/test_api_v1_approvals.py -q
```

`test_auth_approver.py` pins the accepted shapes (JSON `true`/`false`,
string `"true"`/`"false"`, **absent → false**, malformed → false);
`test_auth_rbac.py::test_approvals_access_read_only_with_approver_passes`
and `::test_approver_flag_does_not_grant_dispatch` pin the gate decouple;
`test_api_v1_approvals.py` pins `read_only + approver` → **403
`insufficient_role`** on `POST /api/v1/operations/call`.

### Operator console admits the same capability (#3282)

The complete human approver workflow needs **both** the claim provisioned
(this recipe) **and** a surface that admits it. Both surfaces are live:

- **REST** — the routes above, gated by `require_approvals_access`.
- **Operator console** — `/ui/approvals*` reconstructs the `Operator` via
  the same `_operator_from_claims` (which lifts the `approver` claim) and
  delegates to the shared `approve_request` / `reject_request`, so a
  `read_only + approver` operator can approve / reject from the console
  bell and the full-page queue. This parity is pinned by
  [#3282](https://github.com/evoila/meho/issues/3282) /
  PR [#3285](https://github.com/evoila/meho/pull/3285) (merged
  `b03ad98e`); the 403 banner names **both** levers ("operator role OR
  the approver capability"). No separate console flag is needed.

Ordering: the console admission (#3282) already merged, so the only
remaining gap this recipe closes is realm-side provisioning. Once a member
of `MEHO-Approvers` logs in, the REST decide path and the console are both
immediately usable by that approver.

### Rollback / removal

Remove the person from `MEHO-Approvers`
(`kcadm.sh delete "users/$USER_ID/groups/$GROUP_ID" -r "$REALM"`), and,
if they still need access, put them in another of the five groups.

This takes effect on the **next** token mint. An **already-minted**
access token keeps `approver: true` until it expires — the capability
rides the token, not a per-request DB lookup, so removal is not
retroactive. For immediate revocation, end the user's SSO sessions
(`kcadm.sh create "users/$USER_ID/logout" -r "$REALM"`) so the next request
must re-authenticate (`USER_ID` resolved as in the recipe above), and/or
keep the realm's access-token lifespan short (the Keycloak default is 5
minutes). Same in-flight-token caveat as the `tenant_role`
[troubleshooting](#troubleshooting) rows.

## Status

| Item | Side | State |
| --- | --- | --- |
| Recipe (this doc) | producer | landed in this PR ([`./keycloak-tenant-claims.md`](./keycloak-tenant-claims.md)) |
| Backplane reads `tenant_id` from JWT | producer | tracked at [#222](https://github.com/evoila/meho/issues/222) (T2 / T3) |
| `require_role(min_role)` enforces `tenant_role` | producer | tracked at [#222](https://github.com/evoila/meho/issues/222) (T4) |
| Per-tenant audit-row isolation test | producer | tracked at [#222](https://github.com/evoila/meho/issues/222) (T6) |
| Five role groups + User Attribute mappers | producer | `meho admin keycloak bootstrap-clients` creates them ([#4590](https://github.com/evoila/meho/issues/4590)); decision in [`../decisions/login-role-groups.md`](../decisions/login-role-groups.md) |
| End-to-end `meho status` against v0.2 returns 200 | consumer | pending — the closing-comment artefact on the parent Initiative |
| Backplane reads `approver` from JWT + `require_approvals_access` decouple | producer | landed — [#3270](https://github.com/evoila/meho/issues/3270) (`auth/jwt._extract_approver`, `auth/rbac.require_approvals_access`); console parity [#3282](https://github.com/evoila/meho/issues/3282) |
| `MEHO-Approvers` group + `approver` mapper | producer | `meho admin keycloak bootstrap-clients` creates them ([#4590](https://github.com/evoila/meho/issues/4590)); a realm admin puts each approver in the group |

## References

- Parent Initiative: [#222 — G0.1 Tenant model](https://github.com/evoila/meho/issues/222) — JWT `tenant_id` claim extraction, tenant table, audit_log scoping, role-enum RBAC primitive
- Parent Goal: [#221 — G0 Foundational substrate](https://github.com/evoila/meho/issues/221)
- Decision: [`../decisions/login-role-groups.md`](../decisions/login-role-groups.md) — the five login groups ([#4590](https://github.com/evoila/meho/issues/4590))
- Sibling handshake: [`./targets-yaml.md`](./targets-yaml.md) — `targets.yaml` `rdc-meho` entry; the per-tenant UUID minted in Step 1 lands here in v0.2.next
- Sibling handshake: [`./rke2-infra-coordination.md`](./rke2-infra-coordination.md) — per-PR ephemeral smoke + `repository_dispatch`
- Backend codebase walkthrough: [`../codebase/backend.md`](../codebase/backend.md) — `Settings`, `verify_jwt`, `Operator` model
- Approve-only capability: [#3243](https://github.com/evoila/meho/issues/3243) / [#3270](https://github.com/evoila/meho/issues/3270) — `auth/jwt._extract_approver`, `auth/rbac.require_approvals_access`; approvals surface in [`../codebase/approvals.md`](../codebase/approvals.md)
- Keycloak Server Admin Guide — [Protocol Mappers](https://www.keycloak.org/docs/latest/server_admin/index.html#protocol-mappers)
- Keycloak OIDC layers — [UserInfo endpoint](https://www.keycloak.org/securing-apps/oidc-layers)
- Consumer's Keycloak realm: see `evoila-bosnia/claude-rdc-hetzner-dc/rdc-hetzner-dc/INVENTORY.md` Keycloak section
