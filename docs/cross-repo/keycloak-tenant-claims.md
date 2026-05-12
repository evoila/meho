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
value the backplane sees is the unprefixed snake_case enum
(`tenant_admin` / `operator` / `read_only`). Shape A's Script Mapper
translates the prefix away (and converts hyphens to underscores) —
see [Step 4](#step-4--configure-the-tenant_role-protocol-mapper).
Under Shape B the realm roles are RBAC bookkeeping only; the per-user
`tenant_role` attribute carries the literal enum value directly.

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
attribute mapper because **Keycloak has no built-in mapper that
emits a fixed scalar claim value gated by a single realm role.**
The built-in **User Realm Role** mapper writes the user's filtered
realm-role *names* (a string or JSON array) into the claim — not a
per-mapper-configured constant. The backplane's `tenant_role` enum
needs the constant value (`tenant_admin` / `operator` /
`read_only`), not the role name. Two viable shapes meet the
contract:

#### Shape A (recommended) — one Script Mapper

If the realm has the **Script Mapper** feature enabled, a single
mapper picks the most-privileged role and emits the
backplane-shaped enum value:

```javascript
// Script Mapper body — token-mapper-script.js
// Outputs the highest of the three meho-* roles the user holds, mapped
// to the backplane's tenant_role enum (snake_case). The realm-side role
// names use hyphens (meho-tenant-admin, meho-read-only) but the
// backplane's TenantRole StrEnum is tenant_admin / operator / read_only
// — see backend/src/meho_backplane/auth/operator.py. The replace() +
// whitelist below bridges the two naming conventions and fail-closes
// on anything unexpected (emits null, which Keycloak omits from the
// claim, which the backplane rejects as 401 missing_tenant_claim).
var roles = user.getRoleMappings();
var ranked = ['meho-tenant-admin', 'meho-operator', 'meho-read-only'];
var allowed = {tenant_admin: true, operator: true, read_only: true};
for (var i = 0; i < ranked.length; i++) {
  for (var j = 0; j < roles.size(); j++) {
    if (roles.get(j).getName() == ranked[i]) {
      // Strip the meho- prefix and convert hyphens to underscores so
      // the value matches the TenantRole enum exactly.
      var role = ranked[i].substring('meho-'.length).replace('-', '_');
      exports = allowed[role] ? role : null;
      break;
    }
  }
  if (typeof exports !== 'undefined') break;
}
```

Configure with **Token Claim Name:** `tenant_role`, **Claim JSON
Type:** `String`, **Add to access token:** on, **Add to userinfo:**
on, **Add to ID token:** off.

A token minted by this mapper for an operator with `meho-operator`
assigned carries `"tenant_role": "operator"`; for `meho-read-only`
it carries `"tenant_role": "read_only"` (underscore, not hyphen —
the backplane's enum is snake_case, the realm-role name is
kebab-case, and the script is what bridges them).

Script Mapper requires the script-mappers feature flag enabled
(`--features=scripts` on the `kc.sh start` command line — disabled
by default since Keycloak 18) and a JS engine on the classpath
(GraalJS bundled with current Keycloak distributions). Realms that
allow scripts land here: one mapper, declarative, easy to audit.

#### Shape B (no scripts) — `tenant_role` as a per-user attribute

For realms that cannot enable the scripts feature (most hardened
production deployments), drop the realm-role-driven claim entirely
and stamp `tenant_role` directly on each user as a custom attribute.
A single built-in **User Attribute** protocol mapper then copies
that attribute into the access token — no scripts, no role-to-claim
indirection, no UI surfaces that don't exist.

The realm roles (`meho-tenant-admin` / `meho-operator` /
`meho-read-only`) created in Step 2 are still useful as RBAC
bookkeeping on the realm side (audit, group queries, future
realm-side policies), but they do **not** drive the `tenant_role`
claim under Shape B — the per-user attribute does.

1. On each operator user, set the `tenant_role` attribute:
   **Users** → select the user → **Attributes** tab → add
   - **Key:** `tenant_role`
   - **Value:** exactly one of `tenant_admin` / `operator` /
     `read_only` (the literal enum value the backplane reads;
     hyphens are wrong and will be rejected as
     `401 unknown_tenant_role`).

2. Add a single **User Attribute** mapper to the `meho-mcp` client
   scope (the same scope the `tenant_id` mapper lives on, from
   [Step 3](#step-3--configure-the-tenant_id-protocol-mapper)):
   **Client scopes** → `meho-mcp` → **Mappers** tab → **Add mapper**
   → **By configuration** → **User Attribute**.

   Configure:
   - **Name:** `tenant_role`
   - **User Attribute:** `tenant_role` (the source — matches the
     attribute key set in step 1)
   - **Token Claim Name:** `tenant_role` (the target — what the
     backplane reads)
   - **Claim JSON Type:** `String`
   - **Add to ID token:** off
   - **Add to access token:** **on** (load-bearing)
   - **Add to userinfo:** on
   - **Aggregate attribute values:** off (each user has one role;
     aggregation would emit a JSON array, which the backplane
     rejects as `malformed_tenant_claim`)
   - **Multivalued:** off (same reason)

3. Save.

The Script Mapper shape (Shape A) does the gating automatically
from the user's role mappings; Shape B keeps role bookkeeping in
realm roles but moves the claim source to a per-user attribute,
trading "one mapper, role-driven" for "one mapper plus a per-user
attribute edit on every role change". Both produce the same
on-the-wire claim shape.

> **Why not the built-in User Realm Role mapper?** Its `setClaim()`
> reads `RoleResolveUtil.getResolvedRealmRoles(...)` and emits the
> user's actual filtered role *names* (e.g.
> `["meho-operator","meho-read-only"]` or
> `"meho-operator"`) — there is no per-mapper "fixed value" field
> on its config schema (verified against
> [`UserRealmRoleMappingMapper.java`](https://github.com/keycloak/keycloak/blob/main/services/src/main/java/org/keycloak/protocol/oidc/mappers/UserRealmRoleMappingMapper.java)
> on `main`, cross-checked against Keycloak 22.x and 26.x
> javadocs; behaviour is unchanged on the relevant code path).
> Tokens minted by that mapper carry e.g.
> `tenant_role: "meho-operator"`, which the backplane rejects as
> `401 unknown_tenant_role` (wrong prefix, not in the enum).
>
> **Why not Hardcoded-Claim mappers in per-role client scopes?** A
> per-user gate would require Keycloak to support per-user client-
> scope assignment, which it does not — client scopes are bound at
> the client level (Default + Optional), with no per-user
> override surface in the Admin Console. Open feature requests
> ([keycloak#37804](https://github.com/keycloak/keycloak/discussions/37804),
> [keycloak#8486](https://github.com/keycloak/keycloak/issues/8486))
> track this gap; until they land, the no-scripts path is the
> per-user attribute approach above.

### Step 5 — Assign users

For each operator who should authenticate against MEHO:

1. **Users** → select the user → **Groups** tab → **Join Group** →
   pick the tenant group from [Step 1](#step-1--create-the-tenant-group).
   Users must belong to **exactly one** tenant group; multiple
   memberships make the group attribute mapper produce ambiguous
   `tenant_id` values.
2. **(Shape A only.)** **Users** → select the user → **Role
   mapping** tab → **Assign role** → filter by **Realm roles** →
   pick exactly one of `meho-tenant-admin` / `meho-operator` /
   `meho-read-only`. Holding more than one of the three
   `meho-*` roles is supported — the script in Shape A picks the
   most-privileged (admin > operator > read-only) — but
   single-role assignment is the simpler invariant.
3. **(Shape B only.)** **Users** → select the user → **Attributes**
   tab → set the `tenant_role` attribute to exactly one of
   `tenant_admin` / `operator` / `read_only` (matching the
   [Step 4 Shape B](#shape-b-no-scripts--tenant_role-as-a-per-user-attribute)
   recipe). Realm-role assignment is optional under Shape B; only
   the attribute drives the claim.
4. Save.

Operators that are members of zero tenant groups, or that (under
Shape A) hold none of the three `meho-*` roles, or that (under
Shape B) have no `tenant_role` attribute set, will be rejected by
the backplane with `401 missing_tenant_claim` on every
authenticated request.

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

If `tenant_id` or `tenant_role` is missing, the protocol mapper for
that claim is misconfigured — most likely **Add to access token /
userinfo** is off, or the user is not assigned to the
group / role the mapper sources from.

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
  string, or `tenant_role` is not in the enum. Re-check the mapper's
  **Claim JSON Type** (must be `String`, not `int` or array) and the
  attribute / role values.
- **`401 unknown_tenant_role`** — `tenant_role` is a string but not
  one of `tenant_admin` / `operator` / `read_only`. Most common
  cause: a built-in **User Realm Role** mapper is forwarding the
  user's realm-role names verbatim (e.g. `meho-operator` or
  `meho-read-only`) instead of the backplane's snake_case enum
  values. Replace it with the recipe in
  [Step 4](#step-4--configure-the-tenant_role-protocol-mapper) —
  Shape A (Script Mapper, which strips the `meho-` prefix and
  converts hyphens to underscores) or Shape B (User Attribute
  mapper sourcing a per-user `tenant_role` attribute set to the
  literal enum value) — both emit the constant snake_case enum the
  backplane expects.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `401 missing_tenant_claim` on every request | Mapper not on the access token | On the mapper screen, toggle **Add to access token** on |
| `401 missing_tenant_claim` for one user only | User is not in a tenant group; or (Shape A) holds none of the three `meho-*` realm roles; or (Shape B) has no `tenant_role` user attribute | Assign the user (Step 5) |
| `401 malformed_tenant_claim` | `tenant_id` mapper has **Aggregate attribute values** on (emits a JSON array instead of a single string) | Toggle **Aggregate attribute values** off and re-issue the token |
| `401 malformed_tenant_claim` (continued) | `tenant_id` mapper has **Claim JSON Type:** `int` or `JSON` instead of `String` | Set **Claim JSON Type** to `String`; UUIDs are strings, not ints |
| `401 unknown_tenant_role` | The token's `tenant_role` is a string but not one of the three enum values — usually because the operator left a built-in **User Realm Role** mapper in place (it forwards realm-role names like `meho-operator`, not the constant enum values), or under Shape B set the user attribute to a hyphenated value (`read-only` instead of `read_only`) | Remove the User Realm Role mapper; use Shape A (Script Mapper, which converts `meho-read-only` → `read_only` automatically) or Shape B (User Attribute mapper sourcing the literal snake_case enum value the operator stamped on the user). Either way the on-the-wire value must be exactly `tenant_admin` / `operator` / `read_only` |
| Two operators in the same tenant get different `tenant_id` values | One of them is in two tenant groups; the mapper picks one non-deterministically | Each user belongs to exactly one tenant group |
| `403 Forbidden` on a route that worked before | User has been assigned a lower role than the route requires (e.g. demoted to `meho-read-only`) | Re-assign the appropriate role on the **Role mapping** tab |
| Claims appear on userinfo but not on the access token | Mapper has **Add to userinfo: on** but **Add to access token: off** | Toggle both on |
| Mapper changes don't take effect for already-logged-in operators | Cached access tokens are still being sent until they expire | Wait for token expiry (or invoke `meho login` again to mint a fresh token) |

## Side note: alternative claim sources

The recipe above is the recommended shape (groups for `tenant_id`,
realm roles for `tenant_role`). Step 4 already covers the two
viable `tenant_role` mapper shapes (Shape A: Script Mapper; Shape
B: User Attribute mapper). One smaller variation on the
`tenant_id` half is worth recording for the single-tenant case:

### `tenant_id` as a per-user attribute

Skip the group plumbing when every user is permanently in one
tenant. Stamp `tenant_id` directly on each user as an attribute and
replace the Group Attribute mapper with a **User Attribute** mapper
(User Attribute: `tenant_id`, Token Claim Name: `tenant_id`, Claim
JSON Type: `String`, Add to access token: on).

Step 1 and Step 3 of the recipe drop out; Step 2 (realm roles —
optional under Shape B), Step 4, and Step 5 still apply.

Tradeoff: avoids one Admin Console object class but does not
remove maintenance — every per-tenant UUID rotation still touches
every user. The recommended (groups + group attribute) shape is
strictly better once a tenant has more than one operator.

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
