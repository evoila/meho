# Your own Keycloak

Use this when your company already runs Keycloak for its people. Your
Keycloak stays in front: it checks who the person is. MEHO's built-in
Keycloak sends people to yours, gets back their groups, and puts each
person into the matching MEHO group.

MEHO always ships its own Keycloak. You do not replace it with yours.

**What stays where:**

| On your side (your Keycloak) | On MEHO's side (realm `meho`) |
|---|---|
| Your people and your user directory | The five MEHO groups |
| Passwords, MFA, login checks | Tenant and role, from the MEHO group |
| Which of your groups may use MEHO | The tokens that MEHO accepts |

MEHO accepts only tokens from its own Keycloak. A token from your
Keycloak does not work at MEHO directly.

**Before you start:**

- The realm is set up and the five MEHO groups exist. See
  [Company login](index.md).
- You are an admin in your Keycloak.
- You can log in to the Keycloak admin console as the console admin
  (a user of realm `master`, default name `console-admin`; name and
  temporary password in the Secret `<release>-core-console-admin`) at
  `https://<meho-host>/auth/admin/master/console/#/meho`, then pick
  realm `meho`. The console needs
  `coreKeycloak.ingress.exposeAdmin: true`. See
  [The console admin](../keycloak-realm.md#the-console-admin).
- You know the address of your MEHO, for example
  `https://meho.example.com`. MEHO's Keycloak is at
  `https://<meho-host>/auth`.

Pick an alias for the identity provider now, for example
`customer-kc`. It goes into the redirect URI:

```text
https://<meho-host>/auth/realms/meho/broker/<alias>/endpoint
```

## 1. Make the client in your Keycloak

In your Keycloak admin console, in the realm of your people: **Clients
→ Create client**.

| Setting | Value |
|---|---|
| **Client type** | OpenID Connect |
| **Client ID** | `meho-broker` |
| **Client authentication** | On |
| **Authentication flow** | **Standard flow** only. Turn off **Direct access grants**. |
| **Valid redirect URIs** | `https://<meho-host>/auth/realms/meho/broker/<alias>/endpoint` |

Click **Save**. Then, on the client:

1. **Advanced → Proof Key for Code Exchange Code Challenge Method**:
   `S256`.
1. **Credentials**: copy the **Client Secret**.
2. **Client scopes → `meho-broker-dedicated` → Configure a new mapper
   → Group Membership**:

    | Setting | Value |
    |---|---|
    | **Name** | `groups` |
    | **Token Claim Name** | `groups` |
    | **Full group path** | Off |
    | **Add to ID token** | On |
    | **Add to access token** | On |
    | **Add to userinfo** | On |

The token now carries `groups`: a list of the person's group names,
without a leading `/`.

## 2. Make the groups in your Keycloak

Make one group per MEHO role you need. The names are yours to choose,
for example:

| Your group | MEHO group |
|---|---|
| `meho-readers` | `MEHO-Readers` |
| `meho-operators` | `MEHO-Operators` |
| `meho-sa-operators` | `MEHO-SA-Operators` |
| `meho-admins` | `MEHO-Admins` |
| `meho-approvers` | `MEHO-Approvers` |

Put each person who may use MEHO in **one** of them. People in none of
them cannot use MEHO.

## 3. Add your Keycloak to MEHO's Keycloak

In MEHO's Keycloak admin console, realm `meho`: **Identity providers →
OpenID Connect v1.0**.

| Setting | Value |
|---|---|
| **Alias** | the alias you picked, for example `customer-kc` |
| **Display name** | `Company account` |
| **Discovery endpoint** | `https://<your-keycloak>/realms/<your-realm>/.well-known/openid-configuration` |
| **Client authentication** | Client secret sent as post (Client secret as basic auth works too) |
| **Client ID** | `meho-broker` |
| **Client Secret** | the secret from step 1 |

Click **Add**. Then, on the provider:

- **Use PKCE**: On. **PKCE Method**: `S256`.
- **Scopes**: `openid profile email`.
- **Sync mode**: `Force`.
- **Trust Email**: On.

Check that **Redirect URI** at the top of the page matches the one in
your client.

!!! warning "Your Keycloak needs one fixed address"
    MEHO's Keycloak checks that every token from your Keycloak names the
    **Issuer** from the discovery endpoint. Your Keycloak must therefore
    use one fixed public address (`KC_HOSTNAME`), the same one MEHO's
    Keycloak reaches it on. If the address in the token changes with the
    caller, the login fails with "Unexpected error when authenticating
    with identity provider", and MEHO's Keycloak log says
    `Wrong issuer from token`.

**No extra form at the first login.** At a person's first login,
Keycloak runs the **First login flow** (default `first broker login`).
With the default settings it shows no form, as long as your token
carries the person's email, first name and last name. Keep the scopes
`openid profile email` and fill those fields in your Keycloak. The
person keeps the username from your Keycloak.

## 4. Map each of your groups to a MEHO group

On the provider → **Mappers** → **Add mapper**. Make one mapper per
MEHO group:

| Setting | Value |
|---|---|
| **Name** | `MEHO-Operators` (the MEHO group) |
| **Sync mode override** | `Force` |
| **Mapper type** | `Advanced Claim to Group` |
| **Claims** | Key `groups`, Value your group's name, for example `meho-operators` |
| **Regex Claim Values** | Off |
| **Group** | `/MEHO-Operators` |

All five:

| Claim key | Claim value | Group |
|---|---|---|
| `groups` | `meho-readers` | `/MEHO-Readers` |
| `groups` | `meho-operators` | `/MEHO-Operators` |
| `groups` | `meho-sa-operators` | `/MEHO-SA-Operators` |
| `groups` | `meho-admins` | `/MEHO-Admins` |
| `groups` | `meho-approvers` | `/MEHO-Approvers` |

With **Force**, Keycloak checks the groups at every login. Remove a
person from your group, and Keycloak removes them from the MEHO group
at their next login.

Never add a mapper that writes `tenant_id`, `tenant_role`, `approver`
or `platform_admin` on the person. The MEHO group gives those.

Your Keycloak's own checks stay in place: its password policy, MFA and
blocked accounts. They run when MEHO's Keycloak sends the person to
yours.

## 5. Optional: send people straight to your Keycloak

By default MEHO's login page shows a username and password form and a
**Company account** button. To skip it:

1. **Authentication → Flows** → **browser** → **Action → Duplicate**.
2. In the copy, open the settings (gear) of **Identity Provider
   Redirector**. Set **Default Identity Provider** to your alias.
3. **Action → Bind flow** → **Browser flow**.

Or add `kc_idp_hint=<alias>` to a login link. That skips the form for
that link only.

Do this only after a person from your Keycloak is in `MEHO-Admins`.
Local users in MEHO's Keycloak, such as the first admin, then no
longer see the password form.

## The rules

- **One MEHO group per person.** Put each person in one of your MEHO
  groups only. In two, MEHO refuses the login with
  `401 multiple_role_groups`.
- **Never map your claims to `tenant_id`, `tenant_role`, `approver` or
  `platform_admin`.** Only the MEHO group gives those.
- **Removal applies at the next login.** Remove a person on your side,
  and their next login through your Keycloak is refused. MEHO's
  Keycloak reads your groups only at that login: a session the person
  already has with MEHO's Keycloak keeps its group until it ends. To
  cut access at once, also end their sessions in MEHO's Keycloak
  (**Users → the person → Sessions → Sign out**).
- **MEHO accepts only tokens from its own Keycloak.** A token issued by
  your Keycloak is refused.

## Lab test

Use a test person from your Keycloak who is not an admin.

- [ ] `meho login https://<meho-host>` works with the account from your
      Keycloak. `meho status` shows the right tenant and role.
- [ ] The web UI (`https://<meho-host>/ui/`) logs in with the same
      account and shows the same role.
- [ ] Claude (MCP) logs in through the browser and can call a MEHO
      tool that fits the role.
- [ ] Your Keycloak's MFA ran during the login.
- [ ] No profile form showed at the first login.
- [ ] Remove the person from your group. The next login is refused
      with `401 missing_tenant_claim`.
- [ ] A token from your Keycloak itself, sent to MEHO, is refused with
      `401 invalid_token`.

An automated test covers the Keycloak part of this recipe:
`backend/tests/integration/test_keycloak_customer_idp_e2e.py`. It does
not replace the lab test.

Followed by a colleague who did not write this recipe: name, date,
result.
