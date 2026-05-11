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
- A generated UUID per tenant the deployment will host. v0.2 deploys
  with a single tenant (`rdc-internal` for the dogfooding lab); the
  UUID is recorded on the consumer side in `targets.yaml` for
  per-target tenancy.
- Keycloak version **22+**. The mapper type names in this recipe
  (`Group Attribute`, `User Realm Role`) match the Admin Console as
  shipped in Keycloak 22 through current (26.x at time of writing);
  the 19.x and earlier consoles use slightly different labels.

## Recommended path: groups + realm roles

The recommended configuration sources `tenant_id` from a **group
attribute** and `tenant_role` from a **realm role**. This shape scales
to tenants with many operators (one group membership +  one role
assignment per operator; the group attribute carries the UUID once).

The alternative — both claims as user attributes — is covered as a
[side note](#side-note-alternative-claim-sources) at the bottom; it is
simpler when each user is in exactly one tenant and never moves, but
does not scale.

### Step 1 — Create the tenant group

Each MEHO tenant maps 1:1 to a Keycloak group. The group's
`tenant_id` attribute carries the UUID the backplane scopes by.

In the Admin Console (logged in as a realm admin):

1. Navigate to **Groups** → **Create group**.
2. **Name:** the tenant slug (lowercase, hyphenated). Example:
   `rdc-internal`.
3. After the group is created, open **Attributes** on that group.
4. Add an attribute:
   - **Key:** `tenant_id`
   - **Value:** a generated UUID (run `python -c "import uuid;
     print(uuid.uuid4())"` or `uuidgen` to mint one). Record this
     value — it goes into `targets.yaml` on the consumer side as the
     per-tenant UUID for v0.2.next.

The group attribute is the source of truth for `tenant_id`. Renaming
the group is safe; **changing the `tenant_id` attribute value is a
breaking change** (every audit row keyed on the old UUID becomes
unreachable).

### Step 2 — Create the three realm roles

The `tenant_role` enum has exactly three values matching the v0.2
RBAC primitive:

- `tenant_admin` — manages the tenant: targets, conventions, role
  assignments. Can perform any state-changing operation.
- `operator` — runs MCP operations against tenant infrastructure.
  Read + write on the tenant's surface; cannot manage RBAC.
- `read_only` — reads audit history, targets, conventions. No
  state-changing operations.

In the Admin Console:

1. Navigate to **Realm roles** → **Create role**.
2. Create three roles, named exactly:
   - `meho-tenant-admin`
   - `meho-operator`
   - `meho-read-only`
3. The `meho-` prefix scopes the roles to MEHO inside the realm and
   keeps them visually distinct from other client / realm roles the
   operator may already use.

The role names on the realm side carry the `meho-` prefix; the claim
value the backplane sees is the unprefixed enum (`tenant_admin` /
`operator` / `read_only`). The mapper translates the prefix away —
see [Step 4](#step-4--configure-the-tenant_role-protocol-mapper).

### Step 3 — Configure the `tenant_id` protocol mapper

This mapper copies the group's `tenant_id` attribute into every access
token issued for a member of that group.

Where to add it:

- **Preferred:** add to the **Client scope** the backplane's client
  uses (typically `meho-mcp` or whichever scope is bound to the
  `KEYCLOAK_AUDIENCE` client). Mapper-on-scope means every client that
  shares the scope inherits the mapper — clean for multi-client
  deployments.
- **Alternative:** add directly to the **Client** itself. Use this
  when the realm has only one MEHO client and you prefer mapper
  ownership tied to the client lifecycle.

In the Admin Console:

1. Navigate to **Client scopes** → `meho-mcp` (or your chosen scope) →
   **Mappers** tab → **Add mapper** → **By configuration**.
2. Select **Group Attribute** from the mapper-type list.
3. Configure:
   - **Name:** `tenant_id`
   - **Group Attribute:** `tenant_id` (the source — matches the
     attribute key from [Step 1](#step-1--create-the-tenant-group))
   - **Token Claim Name:** `tenant_id` (the target — what the
     backplane reads via `JWT_TENANT_CLAIM_NAME`, which defaults to
     `tenant_id`)
   - **Claim JSON Type:** `String`
   - **Add to ID token:** off (the backplane validates the access
     token, not the ID token)
   - **Add to access token:** **on** (load-bearing — without this the
     mapper is a no-op for the backplane)
   - **Add to userinfo:** on (recommended — makes the
     [verification snippet](#verification) below work without
     decoding the access token by hand)
   - **Aggregate attribute values:** off (each operator is in exactly
     one tenant group; aggregation would produce a JSON array, which
     the backplane rejects as `malformed_tenant_claim`)
4. Save.

### Step 4 — Configure the `tenant_role` protocol mapper

The realm-role-to-claim mapping is more nuanced than the group
attribute mapper because Keycloak's built-in **User Realm Role**
mapper emits *all* of a user's realm roles as a JSON array, not a
single role string. Two viable shapes:

#### Shape A (recommended) — one mapper per role, single-value String

Three mappers, one per role, each writing the same claim name with a
fixed value when the user has that role. Order is significant — list
the most-privileged mapper first so the highest role wins when
multiple are present.

For each of the three roles (`meho-tenant-admin` first,
`meho-operator` second, `meho-read-only` third), add a **User Realm
Role** mapper:

1. **Client scopes** → `meho-mcp` → **Mappers** → **Add mapper** →
   **By configuration** → **User Realm Role**.
2. Configure (example for the admin mapper):
   - **Name:** `tenant_role-admin`
   - **Realm Role prefix:** (leave empty)
   - **Multivalued:** **off** (single-value String, not an array)
   - **Token Claim Name:** `tenant_role`
   - **Claim JSON Type:** `String`
   - **Add to ID token:** off
   - **Add to access token:** **on**
   - **Add to userinfo:** on
3. Save.
4. Repeat for `meho-operator` (claim value `operator`) and
   `meho-read-only` (claim value `read_only`).

The mapper writes `tenant_role` only when the user has the matching
role; the three mappers do not collide because Keycloak processes
them in **Priority order** (lower priority number = higher
precedence). Set:

- `tenant_role-admin` priority **10**
- `tenant_role-operator` priority **20**
- `tenant_role-read-only` priority **30**

Result: an operator with both `meho-operator` and `meho-read-only`
gets `tenant_role: operator` (the higher-priority mapper wins).

#### Shape B (simpler, requires Script Mapper feature) — one Script Mapper

If the realm has the **Script Mapper** feature enabled (off by
default since Keycloak 18; some operators run it on a hardened build
with a curated script library), a single mapper picks the
most-privileged role:

```javascript
// Script Mapper body — token-mapper-script.js
// Outputs the highest of the three meho-* roles the user holds.
var roles = user.getRoleMappings();
var ranked = ['meho-tenant-admin', 'meho-operator', 'meho-read-only'];
for (var i = 0; i < ranked.length; i++) {
  for (var j = 0; j < roles.size(); j++) {
    if (roles.get(j).getName() == ranked[i]) {
      // Strip the meho- prefix and emit; map admin -> tenant_admin.
      var role = ranked[i].substring('meho-'.length);
      exports = (role == 'tenant-admin') ? 'tenant_admin' : role;
      break;
    }
  }
  if (typeof exports !== 'undefined') break;
}
```

Configure with **Token Claim Name:** `tenant_role`, **Claim JSON
Type:** `String`, **Add to access token:** on. Script Mapper is a
single mapper instead of three; it depends on the realm having the
script-mappers feature flag enabled (`--features=token-exchange,
scripts` on the `kc.sh start` command line) and a suitable JS engine
(GraalJS bundled with current Keycloak distributions).

Most operators land on Shape A — fewer feature flags, no JavaScript
in the realm config, easier to audit.

### Step 5 — Assign users

For each operator who should authenticate against MEHO:

1. **Users** → select the user → **Groups** tab → **Join Group** →
   pick the tenant group from [Step 1](#step-1--create-the-tenant-group).
   Users must belong to **exactly one** tenant group; multiple
   memberships make the group attribute mapper produce ambiguous
   `tenant_id` values.
2. **Users** → select the user → **Role mapping** tab → **Assign
   role** → filter by **Realm roles** → pick exactly one of
   `meho-tenant-admin` / `meho-operator` / `meho-read-only`.
3. Save.

Operators that are members of zero tenant groups, or that hold none
of the three `meho-*` roles, will be rejected by the backplane with
`401 missing_tenant_claim` on every authenticated request.

## Verification

Two checks. Run them after applying the recipe and before considering
the realm "v0.2-ready".

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

If `tenant_id` or `tenant_role` is missing, the protocol mapper for
that claim is misconfigured — most likely **Add to access token /
userinfo** is off, or the user is not assigned to the
group / role the mapper sources from.

### Check 2 — Decoded access token carries the claims

Userinfo reflects the access token's claim set, but the backplane
reads from the access token directly. Spot-check by decoding:

```bash
# Decode the JWT body (no signature verification — we only want to
# confirm the claim shape).
echo "$TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null | jq
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
  string, or `tenant_role` is not in the enum. Re-check the mapper's
  **Claim JSON Type** (must be `String`, not `int` or array) and the
  attribute / role values.
- **`401 unknown_tenant_role`** — `tenant_role` is a string but not
  one of `tenant_admin` / `operator` / `read_only`. The user holds a
  realm role outside the three the mappers gate on, **and** the
  mappers are configured to forward that role's name verbatim. Either
  remove the stray role from the user, or harden the mappers to emit
  only the three enum values.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `401 missing_tenant_claim` on every request | Mapper not on the access token | On the mapper screen, toggle **Add to access token** on |
| `401 missing_tenant_claim` for one user only | User is not in a tenant group, or holds none of the three realm roles | Assign the user (Step 5) |
| `401 malformed_tenant_claim` | `tenant_id` mapper has **Aggregate attribute values** on (emits a JSON array instead of a single string) | Toggle **Aggregate attribute values** off and re-issue the token |
| `401 malformed_tenant_claim` (continued) | `tenant_id` mapper has **Claim JSON Type:** `int` or `JSON` instead of `String` | Set **Claim JSON Type** to `String`; UUIDs are strings, not ints |
| `401 unknown_tenant_role` | User holds a non-`meho-*` realm role and the mapper forwards it | Use Shape A (one mapper per role) instead of forwarding all roles via a single multivalued mapper |
| Two operators in the same tenant get different `tenant_id` values | One of them is in two tenant groups; the mapper picks one non-deterministically | Each user belongs to exactly one tenant group |
| `403 Forbidden` on a route that worked before | User has been assigned a lower role than the route requires (e.g. demoted to `meho-read-only`) | Re-assign the appropriate role on the **Role mapping** tab |
| Claims appear on userinfo but not on the access token | Mapper has **Add to userinfo: on** but **Add to access token: off** | Toggle both on |
| Mapper changes don't take effect for already-logged-in operators | Cached access tokens are still being sent until they expire | Wait for token expiry (or invoke `meho login` again to mint a fresh token) |

## Side note: alternative claim sources

The recipe above is the recommended shape. Two simpler alternatives
exist; both trade scalability for fewer realm objects.

### Both claims as user attributes

Skip the group, skip the realm roles. Stamp `tenant_id` and
`tenant_role` directly on each user as attributes:

1. **Users** → select the user → **Attributes** tab → add
   `tenant_id` = `<uuid>` and `tenant_role` = `operator` (or one of
   the three values).
2. Replace the Group Attribute mapper with a **User Attribute**
   mapper (User Attribute: `tenant_id`, Token Claim Name:
   `tenant_id`, Claim JSON Type: `String`, Add to access token: on).
3. Replace the User Realm Role mappers with another **User
   Attribute** mapper for `tenant_role`.

Tradeoff: simplest possible config; per-user maintenance burden
(every user re-stamped when a tenant's UUID rotates, every role
change is a per-user attribute edit). Suitable for single-tenant
single-operator demo deployments. Not suitable for the dogfooding
lab once it grows past one operator.

### `tenant_id` as user attribute, `tenant_role` as realm role

Hybrid: per-user `tenant_id` attribute (avoids the group plumbing
when each user is permanently in one tenant), realm-role mappers per
the recipe above. Skips Step 1 and Step 3; keeps Step 2 and Step 4.

Tradeoff: avoids one Admin Console object class but does not actually
remove maintenance — every per-tenant rotation still touches every
user. The recommended (groups + roles) shape is strictly better once
a tenant has more than one operator.

## Status

| Item | Side | State |
| --- | --- | --- |
| Recipe (this doc) | producer | landed in this PR ([`./keycloak-tenant-claims.md`](./keycloak-tenant-claims.md)) |
| Backplane reads `tenant_id` from JWT | producer | tracked at [#222](https://github.com/evoila/meho/issues/222) (T2 / T3) |
| `require_role(min_role)` enforces `tenant_role` | producer | tracked at [#222](https://github.com/evoila/meho/issues/222) (T4) |
| Per-tenant audit-row isolation test | producer | tracked at [#222](https://github.com/evoila/meho/issues/222) (T6) |
| Realm groups + roles + mappers configured on `evba.lab` | consumer | pending — applied by the dogfooding lab operator before deploying v0.2 |
| End-to-end `meho status` against v0.2 returns 200 | consumer | pending — the closing-comment artefact on the parent Initiative |

## References

- Parent Initiative: [#222 — G0.1 Tenant model](https://github.com/evoila/meho/issues/222) — JWT `tenant_id` claim extraction, tenant table, audit_log scoping, role-enum RBAC primitive
- Parent Goal: [#221 — G0 Foundational substrate](https://github.com/evoila/meho/issues/221)
- Sibling handshake: [`./targets-yaml.md`](./targets-yaml.md) — `targets.yaml` `rdc-meho` entry; the per-tenant UUID minted in Step 1 lands here in v0.2.next
- Sibling handshake: [`./rke2-infra-coordination.md`](./rke2-infra-coordination.md) — per-PR ephemeral smoke + `repository_dispatch`
- Backend codebase walkthrough: [`../codebase/backend.md`](../codebase/backend.md) — `Settings`, `verify_jwt`, `Operator` model
- Keycloak Server Admin Guide — [Protocol Mappers](https://www.keycloak.org/docs/latest/server_admin/index.html#protocol-mappers)
- Keycloak OIDC layers — [UserInfo endpoint](https://www.keycloak.org/securing-apps/oidc-layers)
- Consumer's Keycloak realm: see `evoila-bosnia/claude-rdc-hetzner-dc/rdc-hetzner-dc/INVENTORY.md` Keycloak section
