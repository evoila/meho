# authentik

Use this when your people log in with
[authentik](https://goauthentik.io/). It is the same pattern as
[Entra ID](entra-id.md): Keycloak sends people to authentik, authentik
sends back their groups, and Keycloak puts each person into the
matching MEHO group. Tracked in
[#4593](https://github.com/evoila/meho/issues/4593).

**Before you start:**

- The realm is set up and the five MEHO groups exist. See
  [Company login](index.md).
- You can log in to the Keycloak admin console of realm `meho` as the
  Keycloak admin (`keycloak-admin`, temporary password in the Secret
  `<release>-core-keycloak-console`) at
  `https://<meho-host>/auth/admin/meho/console/`. The console needs
  `coreKeycloak.ingress.exposeAdmin: true`. See
  [The Keycloak admin](../keycloak-realm.md#the-keycloak-admin).
- You are an admin in authentik.

Pick an alias for the identity provider, for example `authentik`. The
redirect URI is:

```text
https://<keycloak>/realms/meho/broker/<alias>/endpoint
```

## 1. Make the authentik groups

In authentik: **Directory → Groups**. Make one group per MEHO group,
with the same name, for example `MEHO-Operators`. Put each person in
**one** of them.

## 2. Create the provider and the application

In the authentik admin interface: **Applications → Providers → Create →
OAuth2/OpenID Provider**.

| Setting | Value |
|---|---|
| **Name** | `MEHO Keycloak` |
| **Authorization flow** | `default-provider-authorization-implicit-consent` |
| **Client type** | Confidential |
| **Redirect URIs/Origins** | Strict, `https://<keycloak>/realms/meho/broker/<alias>/endpoint` |
| **Signing Key** | any certificate, for example the authentik self-signed one |
| **Scopes** (advanced) | `openid`, `email`, `profile` |

Copy the **Client ID** and **Client Secret**.

The default `profile` scope adds a `groups` claim: a list of the
person's authentik group names. Check it on the provider's **Preview**
tab with a test person.

Then **Applications → Applications → Create**: name `MEHO`, slug
`meho`, provider `MEHO Keycloak`. To keep other people out, bind the
MEHO groups to the application (**Policy / Group / User Bindings**).

The discovery URL is:

```text
https://<authentik>/application/o/meho/.well-known/openid-configuration
```

## 3. Add authentik to Keycloak

Same as [Entra ID, step 3](entra-id.md#3-add-entra-to-keycloak), with:

- **Alias**: `authentik`;
- **Discovery endpoint**: the URL above;
- **Client ID** and **Client Secret**: from authentik;
- **Sync mode**: `Force`.

## 4. Map each authentik group to a MEHO group

Same as [Entra ID, step 4](entra-id.md#4-map-each-entra-group-to-a-meho-group):
one **Advanced Claim to Group** mapper per MEHO group, **Sync mode
override** `Force`. The claim value is the authentik **group name**,
not an ID:

| Claim key | Claim value | Group |
|---|---|---|
| `groups` | `MEHO-Operators` | `/MEHO-Operators` |

Never add a mapper that writes `tenant_id`, `tenant_role`, `approver`
or `platform_admin` on the person.

authentik's own checks stay in place: the MFA stages in its
authentication flow run when Keycloak sends the person to authentik.

## Lab test

Use a test person from authentik who is not an admin.

- [ ] `meho login https://<meho-host>` works with the authentik
      account. `meho status` shows the right tenant and role.
- [ ] The web UI (`https://<meho-host>/ui/`) logs in with the same
      account and shows the same role.
- [ ] Claude (MCP) logs in through the browser and can call a MEHO
      tool that fits the role.
- [ ] Remove the person from the authentik group. The next login is
      refused with `401 missing_tenant_claim`.

Followed by a colleague who did not write this recipe: name, date,
result.
