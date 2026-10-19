# People get their tenant and role from one of five login groups (decision)

**Status:** decided — product owner, 2026-10-19
**Issue:** [#4590](https://github.com/evoila/meho/issues/4590)
**Initiative:** [#3959](https://github.com/evoila/meho/issues/3959) — people log in with their company account, and their group decides their tenant and role
**Used by:** `meho admin keycloak bootstrap-clients`, the built-in Keycloak setup ([#4040](https://github.com/evoila/meho/issues/4040)), the company login recipes under #3959

## The decision

MEHO has one way to give a person their tenant and their role: the
person's group in Keycloak.

- There are five top-level groups.
- Each group carries the values as **group attributes** (key-value
  settings stored on the group).
- The stock Keycloak **User Attribute** mapper copies them into the
  login token. A mapper is a Keycloak setting that puts a value into
  the token.

| Group | `tenant_role` | Extra attribute | What the person can do |
| --- | --- | --- | --- |
| `MEHO-Readers` | `read_only` | – | Read only. |
| `MEHO-Operators` | `operator` | – | Run actions. Risky changes wait until a second person approves. |
| `MEHO-SA-Operators` | `operator` | – | Same as Operators for now. "SA" means self-approval. That rule comes later with [#3325](https://github.com/evoila/meho/issues/3325). |
| `MEHO-Admins` | `tenant_admin` | – | Manage the tenant. |
| `MEHO-Approvers` | `read_only` | `approver` = `true` | Approve other people's changes. Cannot run actions. |

Every group also carries `tenant_id`: the UUID of the tenant. For now
each site has one tenant, so all five groups carry the same
`tenant_id`.

## The rules

1. **One group per person.** A person is in exactly one of the five
   groups. If a person is in two, Keycloak puts one of the two values
   into the token, and you cannot tell which one. A separate Task
   ([#4594](https://github.com/evoila/meho/issues/4594)) makes MEHO
   refuse such a login.
2. **No group, no access.** A person in none of the five groups gets
   no `tenant_id`. MEHO refuses the login with
   `401 missing_tenant_claim`.
3. **Stock mapper only.** The human login clients (`meho-cli`,
   `meho-mcp`, and later `meho-web`) each have three User Attribute
   mappers: `tenant_id`, `tenant_role` and `approver`. No scripts. No
   fixed values on a client that many people share.
4. **Leave the person's own attributes empty.** The mapper looks at the
   person first. Only when the person has no value of their own does it
   look at the person's groups. So a `tenant_role` set on the person
   wins over the group.

## How the mapper reads a group attribute

Older MEHO docs said stock Keycloak has no mapper that reads a group
attribute. That was wrong. The User Attribute mapper falls back to the
person's groups:

- `UserAttributeMapper.setClaim` calls
  `KeycloakModelUtils.resolveAttribute(user, name, aggregateAttrs)`
  ([source, Keycloak 26.8.1](https://github.com/keycloak/keycloak/blob/26.8.1/services/src/main/java/org/keycloak/protocol/oidc/mappers/UserAttributeMapper.java#L96-L104)).
- `resolveAttribute` returns the person's own values when there are
  any. Otherwise it returns the values of the first group that has the
  attribute, and a group without the value asks its parent group
  ([source, Keycloak 26.8.1](https://github.com/keycloak/keycloak/blob/26.8.1/server-spi-private/src/main/java/org/keycloak/models/utils/KeycloakModelUtils.java#L768-L797)).
- When nothing is found, the list is empty, and Keycloak adds no claim
  at all
  ([source, Keycloak 26.8.1](https://github.com/keycloak/keycloak/blob/26.8.1/services/src/main/java/org/keycloak/protocol/oidc/mappers/OIDCAttributeMapperHelper.java#L162-L186)).
  That is why a person in no group gets `401 missing_tenant_claim`.

Keycloak 26.0 has the same code. MEHO's CI tests this against a real
Keycloak 26.0
(`backend/tests/integration/test_keycloak_bootstrap_mcp_login.py`).

The three mappers use these settings:

| Setting | `tenant_id` | `tenant_role` | `approver` |
| --- | --- | --- | --- |
| Mapper type | User Attribute (`oidc-usermodel-attribute-mapper`) | same | same |
| User Attribute (`user.attribute`) | `tenant_id` | `tenant_role` | `approver` |
| Token Claim Name (`claim.name`) | `tenant_id` | `tenant_role` | `approver` |
| Claim JSON Type (`jsonType.label`) | `String` | `String` | `boolean` |
| Add to access token | on | on | on |
| Add to ID token | off | off | off |
| Add to userinfo | on | on | on |
| Multivalued | off | off | off |
| Aggregate attribute values (`aggregate.attrs`) | off | off | off |

We set **Aggregate attribute values** off. Then the person's own value
wins, else the first group's value. With it on, Keycloak merges the
values of the person and all their groups, and then still puts only
one of them into the token. That is not better, only harder to
explain.

## Later: more than one tenant

Each new tenant gets its own five groups with its own `tenant_id`, for
example `MEHO-<tenant>-Readers`, `MEHO-<tenant>-Operators`,
`MEHO-<tenant>-SA-Operators`, `MEHO-<tenant>-Admins` and
`MEHO-<tenant>-Approvers`. The client mappers do not change: they read
whatever group the person is in. One person in several tenants is a
separate topic ([#3978](https://github.com/evoila/meho/issues/3978)).

## Why not the other ways

- **A fixed value on the client** (the Hardcoded claim mapper). Every
  person who logs in through that client gets the same tenant and role.
  On a shared client, everyone becomes an admin. The older
  `bootstrap-clients` did exactly this, with `tenant_admin`.
- **A script mapper.** It needs a Keycloak feature that is off by
  default, and many sites do not allow it. A script is also harder to
  check than a setting.
- **A value on each person.** Every role change is an edit on the
  person. With groups, an admin moves the person to another group. It
  also fits company directories: a directory group (Active Directory,
  LDAP, Entra ID) maps to one of the five Keycloak groups.
- **Realm roles.** The stock role mapper copies the role names (for
  example `meho-operator`), not the value MEHO reads (`operator`).
  Turning one into the other needs a script.

## Where this lives

- `meho admin keycloak bootstrap-clients` creates the five groups and
  the three mappers, and puts the first admin user in `MEHO-Admins`.
  On a realm made by the older version, it turns the fixed-value
  `tenant-id` / `tenant-role` mappers into User Attribute mappers.
- [`docs/cross-repo/keycloak-tenant-claims.md`](../cross-repo/keycloak-tenant-claims.md)
  is the full recipe for a realm set up by hand.
- [`docs-site/install/keycloak-realm.md`](../../docs-site/install/keycloak-realm.md)
  is the install guide.
