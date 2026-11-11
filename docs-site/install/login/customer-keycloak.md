# Your own Keycloak

Use this when your company already runs Keycloak for its people. Your
Keycloak stays in front: it checks who the person is. MEHO's built-in
Keycloak sends people to yours, gets back their groups, and puts each
person into the matching MEHO group.

In this recipe, MEHO keeps its built-in Keycloak, and yours stays in
front of it. To use your Keycloak in place of the built-in one
(`coreKeycloak.enabled: false`), follow
[Keycloak realm setup](../keycloak-realm.md) instead.

**What stays where:**

| On your side (your Keycloak) | On MEHO's side (realm `meho`) |
|---|---|
| Your people and your user directory | The five MEHO groups |
| Passwords, MFA, login checks | Tenant and role, from the MEHO group |
| Which of your groups may use MEHO | The tokens that MEHO accepts |

In this setup, MEHO accepts only tokens from its built-in Keycloak. A
token from your Keycloak does not work at MEHO directly.

On MEHO's side you set this up with Helm values
(`coreKeycloak.companyLogin`), not by hand in the Keycloak admin
console. The install sets it up again at every `helm upgrade`. The
page [Company login with authentik](../company-login-authentik.md)
explains the same values in more detail.

**Before you start:**

- The realm is set up and the five MEHO groups exist. See
  [Company login](index.md).
- You are an admin in your Keycloak.
- You know the address of your MEHO, for example
  `https://meho.example.com`. MEHO's Keycloak is at
  `https://<meho-host>/auth`.

The Helm values use the alias `company` by default. The alias is part
of the redirect URI:

```text
https://<meho-host>/auth/realms/meho/broker/company/endpoint
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
| **Valid redirect URIs** | `https://<meho-host>/auth/realms/meho/broker/company/endpoint` |

Click **Save**. Then, on the client:

1. **Advanced → Proof Key for Code Exchange Code Challenge Method**:
   `S256`.
2. **Credentials**: copy the **Client Secret**.
3. **Client scopes → `meho-broker-dedicated` → Configure a new mapper
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

## 3. Store the client secret

Put the secret from step 1 into a Kubernetes Secret in MEHO's
namespace. The chart reads it by name and never prints it.

```bash
read -rs CLIENT_SECRET     # paste the client secret; it is not shown
kubectl create secret generic meho-company-login -n meho \
  --from-literal=client_secret="$CLIENT_SECRET"
unset CLIENT_SECRET
```

If a private CA signs your Keycloak's certificate, also store the CA
in a ConfigMap and set `caBundle` (see
[Company login with authentik](../company-login-authentik.md#1-store-the-client-secret)).

## 4. Add the Helm values

Add this to your values file, next to your other `coreKeycloak`
values:

```yaml
coreKeycloak:
  companyLogin:
    enabled: true
    alias: company
    displayName: Company account
    discoveryUrl: https://<your-keycloak>/realms/<your-realm>/.well-known/openid-configuration
    clientId: meho-broker
    clientSecret:
      secretName: meho-company-login
      secretKey: client_secret
    scopes: openid profile email
    groupsClaim: groups
    groupMap:                         # your group -> MEHO group
      meho-readers: MEHO-Readers
      meho-operators: MEHO-Operators
      meho-sa-operators: MEHO-SA-Operators
      meho-admins: MEHO-Admins
      meho-approvers: MEHO-Approvers
    autoRedirect: false               # true later: go straight to your Keycloak
```

- `groupMap`: the left side is your group's name, the right side is
  the MEHO group. **Only the groups you list here give access.**
- `autoRedirect`: with `true`, MEHO's Keycloak sends everyone straight
  to yours and never shows its own password form. Start with `false`,
  so the first admin can still log in with a password. Switch to
  `true` once a person from your Keycloak is in `MEHO-Admins`.

Then run `helm upgrade` as in
[Install or upgrade](../company-login-authentik.md#3-install-or-upgrade).
The install notes print the redirect URI. Check that it is the same as
the one in your client. If you turn the company login on for an
install that already exists, read the note in that section first.

The install sets up the login with PKCE (`S256`). It makes one group
rule per `groupMap` line. MEHO's Keycloak checks the groups at every
login: remove a person from your group, and MEHO's Keycloak removes
them from the MEHO group at their next login.

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

Your Keycloak's own checks stay in place: its password policy, MFA and
blocked accounts. They run when MEHO's Keycloak sends the person to
yours.

**Passkeys.** With a company login, the person signs in at your
Keycloak, so passkeys (Face ID, Touch ID, a fingerprint or Windows
Hello) are turned on there, not in MEHO's realm. In your Keycloak 26.4
or newer, in the realm of your people, set the passkey rows of the
table in
[Remember the browser, and passkeys](../keycloak-realm.md#remember-the-browser-and-passkeys):
**Enable Passkeys** on, and in the **Webauthn Passwordless Policy**
**User verification requirement** and **Discoverable credential** at
**Required**. A realm made before Keycloak 26.4 also needs the step that
skips the one-time code after a passkey (see
[A realm that already runs](../keycloak-realm.md#a-realm-that-already-runs)).
Your people then add a passkey on your Keycloak's account page. For a
company login of another kind (Entra ID, authentik), turn passkeys on in
that system. When you use your Keycloak in place of MEHO's, set the same
rows in the realm `meho`.

## 5. Check the approval sign-in (required)

`meho approvals approve` signs in through its own login flow in realm
`meho`: `meho approval sign-in`, bound to the client `meho-cli-approve`.
The flow asks for the password, and for the one-time code when the person
has set one up. It has no other step, so it does not send the person to
your Keycloak. Every install needs this flow and its binding.

- **MEHO's built-in Keycloak in front of yours (this page):** the install
  makes the flow and binds it at every `helm upgrade`. On an older install
  the install may stop and ask you to give `meho-admin` the role
  `manage-realm` once
  ([The bootstrap Job needs `manage-realm`](../upgrades.md#the-bootstrap-job-needs-manage-realm)).
- **Your Keycloak in place of MEHO's (`coreKeycloak.enabled: false`):**
  set it up yourself, in one of two ways:
    - by hand, as
      [The approval client `meho-cli-approve`](../keycloak-realm.md#the-approval-client-meho-cli-approve)
      says ("Its own login flow");
    - with the operation `keycloak.authflow.ensure_approval_flow`
      ([Add the approval flow to a running realm through MEHO](../keycloak-realm.md#add-the-approval-flow-to-a-running-realm-through-meho)).
      It waits for approval.

Then check it. `meho doctor` shows the line `approval sign-in`, and
`GET /api/v1/health` shows `approvals.sign_in.state`. Both must say `ok`.
`flow_missing`, `not_bound` or `flow_differs` mean the flow or its
binding is not right; `meho doctor` names the fix. `not_checked` with
`no_right_to_read_login_flows` means MEHO's Keycloak admin client needs
the realm-management role `view-realm`.

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
  (**Users → the person → Sessions → Sign out**), and revoke any session
  of type **Offline**. An offline login, for example of Claude Code,
  lasts up to 48 hours without use.
- **MEHO accepts only tokens from its built-in Keycloak.** A token
  issued by your Keycloak is refused.

## Lab test

Use a test person from your Keycloak who is not an admin.

- [ ] `meho login https://<meho-host>` works with the account from your
      Keycloak. `meho status` shows the right tenant and role.
- [ ] `meho doctor` shows `approval sign-in` as OK.
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
