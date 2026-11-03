# Company login with authentik

This page connects [authentik](https://goauthentik.io/) in front of
MEHO's Keycloak. When you are done:

- People log in to MEHO with their authentik account and pass
  authentik's second login step (MFA).
- They come back with a MEHO token from Keycloak.
- Their authentik group decides their MEHO group, and with it their
  tenant and their role.
- You manage people only in authentik. Nobody needs the Keycloak admin
  console for daily work.

MEHO still trusts only Keycloak. Keycloak works here as a **broker**: a
go-between for two login systems. It sends people on to authentik, and
then it writes the MEHO token. Service accounts (agents, check runners,
add-ons) stay in Keycloak and do not use authentik.

```text
 person (meho CLI, web console, Claude Code)
     |  1. "log me in to MEHO"
     v
 Keycloak (realm meho) ------ 2. sends the person on ----->  authentik
 (no passwords, no MFA here) <-- 3. "this is Jane, groups: MEHO-Operators"
     |  4. MEHO token: tenant_id, tenant_role, approver
     v
 MEHO
```

There are two ways to run Keycloak, and this page covers both:

- **The built-in Keycloak** that the MEHO Helm chart installs
  (`coreKeycloak.enabled: true`). You set up the broker with a few Helm
  values. Follow [Part 1](#part-1-authentik) and
  [Part 2](#part-2-meho-built-in-keycloak).
- **Your own Keycloak**, set up with
  [Keycloak realm setup](keycloak-realm.md). You set up the broker by
  hand. Follow [Part 1](#part-1-authentik) and
  [Your own Keycloak](#your-own-keycloak).

Other company login systems that speak OpenID Connect (Entra ID, Okta,
ADFS) work the same way. Only the clicks in Part 1 differ.

## Before you start

- A MEHO install (or a planned one) with a DNS name, for example
  `meho.example.com`. This page writes it as `<host>`.
- An authentik that people already use, for example
  `authentik.example.com`. The menu names below come from authentik
  2026.8.
- Admin access to authentik.
- If a private CA signs authentik's certificate: that CA as a PEM file.
  Keycloak calls authentik directly to get the login result, so it must
  trust this certificate.
- Every person who will use MEHO has an email address, a first name and
  a last name in authentik. If one is missing, Keycloak shows the person
  a short form to fill it in at their first login.

## How the groups work

MEHO has five role groups. Each group gives a role:

| MEHO group | Role in MEHO (`tenant_role`) | What the person can do |
|---|---|---|
| `MEHO-Readers` | `read_only` | Read only. |
| `MEHO-Operators` | `operator` | Run actions. Risky changes wait until a second person approves. |
| `MEHO-SA-Operators` | `operator` | Same as Operators for now. |
| `MEHO-Admins` | `tenant_admin` | Manage the tenant. |
| `MEHO-Approvers` | `read_only`, plus `approver: true` | Approve other people's changes. Cannot run actions. |

The rule: **put each person in exactly one of these groups.**

- A person in no group gets no tenant. MEHO refuses them with
  `401 missing_tenant_claim`.
- A person in two groups is refused with `401 multiple_role_groups`.
- Operators and admins can already approve other people's changes, so
  nobody needs two groups.

The easiest setup is to give the authentik groups the same five names.
You can also use groups you already have and map them (see `groupMap`
in Part 2).

## Part 1: authentik

### 1. Create the groups

In authentik, open **Directory > Groups > New Group**. Create the five
groups from the table above, or pick five groups you already have. Put
each person in exactly one of them.

### 2. Create the application and the provider

Open **Applications > Applications > New Application**, and choose
**with New Provider...** from the arrow menu.

- **Application Name**: `MEHO`. **Slug**: `meho`. The slug is part of
  the addresses below. This page uses `meho`.
- Provider type: **OAuth2/OpenID Provider**.
- **Authorization Flow**: `default-provider-authorization-implicit-consent`.
  People then see no extra "allow this app?" page.
- **Client Type**: **Confidential** (a client that has a secret). Copy
  the **Client ID** and the **Client Secret**. You need both in Part 2.
- **Redirect URIs/Origins (RegEx)**: **Add entry**, mode **Strict**,
  URL:

    - built-in Keycloak:
      `https://<host>/auth/realms/meho/broker/company/endpoint`
    - your own Keycloak:
      `https://<keycloak-host>/realms/<realm>/broker/company/endpoint`

    This is the address authentik sends people back to. `company` is
    the short name of the login in Keycloak. With the built-in Keycloak,
    the install notes print the exact address.

- **Signing Key**: pick an RSA certificate, for example the
  self-signed certificate that authentik makes. This is required.
  Without a signing key, authentik signs with HS256 and the client
  secret, and it publishes no public key. Keycloak then cannot check
  the token, and the login fails.
- **Grant Types**: **Authorization Code** must be on.
- Under **Advanced protocol settings**:
    - **Scopes**: `openid`, `profile` and `email`. The `profile` scope
      carries the `groups` list (the names of the person's groups).
    - **Include claims in id_token**: keep it on (the default).
      Keycloak reads the groups from the ID token.
    - **Issuer mode**: keep the default, "Each provider has a different
      issuer, based on the application slug".
- **Bindings** (recommended): **Bind existing policy/group/user**, and
  bind the five groups. Then authentik refuses people who are in none
  of them. Without bindings, such a person reaches Keycloak, and MEHO
  refuses them with `401 missing_tenant_claim`.

### 3. Note the discovery address

The discovery address of the application is:

```text
https://authentik.example.com/application/o/meho/.well-known/openid-configuration
```

Check that it works and that it has signing keys:

```bash
curl -fsS https://authentik.example.com/application/o/meho/.well-known/openid-configuration \
  | jq '{issuer, jwks_uri, id_token_signing_alg_values_supported}'
```

`jwks_uri` must be set, and the list of signing methods must hold
`RS256`.

## Part 2: MEHO (built-in Keycloak)

This part is for an install with `coreKeycloak.enabled: true`. The keys
of the built-in Keycloak are described in the chart's
[`values.yaml`](https://github.com/evoila/meho/blob/main/deploy/charts/meho/values.yaml)
under `coreKeycloak`.

### 1. Store the client secret

Put the authentik client secret into a Kubernetes Secret in MEHO's
namespace. The chart reads it by name and never prints it.

```bash
read -rs CLIENT_SECRET     # paste the authentik client secret; it is not shown
kubectl create secret generic meho-company-login -n meho \
  --from-literal=client_secret="$CLIENT_SECRET"
unset CLIENT_SECRET
```

If a private CA signs authentik's certificate, also store the CA in a
ConfigMap:

```bash
kubectl create configmap company-login-ca -n meho --from-file=ca.crt=<authentik-ca.pem>
```

### 2. Add the Helm values

Add this to your values file, next to your other `coreKeycloak`
values:

```yaml
coreKeycloak:
  enabled: true
  hostname: meho.example.com          # the same as ingress.host
  firstTenantId: "<your tenant UUID>"
  companyLogin:
    enabled: true
    alias: company                    # part of the redirect address
    displayName: authentik            # the button name on the Keycloak login page
    discoveryUrl: https://authentik.example.com/application/o/meho/.well-known/openid-configuration
    clientId: "<the authentik Client ID>"
    clientSecret:
      secretName: meho-company-login
      secretKey: client_secret
    scopes: openid profile email      # keep profile: it carries the groups
    groupsClaim: groups
    groupMap:                         # authentik group -> MEHO group
      MEHO-Readers: MEHO-Readers
      MEHO-Operators: MEHO-Operators
      MEHO-SA-Operators: MEHO-SA-Operators
      MEHO-Admins: MEHO-Admins
      MEHO-Approvers: MEHO-Approvers
    autoRedirect: true                # go straight to authentik
    caBundle:                         # only for a private CA; else leave the name empty
      configMapName: company-login-ca
      key: ca.crt
```

What the keys mean:

- `groupMap`: one line per group. The left side is the authentik group
  name. The right side is the MEHO group. **Only the groups you list
  here give access.** With an empty `groupMap`, nobody gets a MEHO
  group. If your authentik groups have other names, write those names
  on the left, for example `Infra-Team: MEHO-Operators`.
- `autoRedirect: true`: Keycloak never shows its own login page. It
  sends everyone straight to authentik.
- `caBundle`: Keycloak trusts this CA for its calls to authentik, and
  still trusts the usual public CAs.

### 3. Install or upgrade

```bash
helm upgrade --install meho oci://ghcr.io/evoila/meho-chart \
  --namespace meho --create-namespace \
  -f values.yaml --wait --wait-for-jobs
```

After every install and every upgrade, the bootstrap Job sets up the
broker in Keycloak. A second run with the same values changes nothing.
The Job:

- reads authentik's discovery address and creates or updates the
  login. It refuses a login system that publishes no signing keys;
- makes one group rule per `groupMap` line, and removes the rules of
  lines you deleted;
- turns the redirect to authentik on or off, as `autoRedirect` says.

The install notes then print the redirect address. Check that it is the
same as the one in authentik:

```text
Company login (built-in Keycloak):
  In the company login system, set this redirect address on the client <client id>:
       https://meho.example.com/auth/realms/meho/broker/company/endpoint
  People in a company group listed in coreKeycloak.companyLogin.groupMap get that MEHO group at every company login.
  Keycloak sends everyone straight to the company login. To use the local accounts above
  (for example when the company login is down), set coreKeycloak.companyLogin.autoRedirect=false and run helm upgrade.
```

To change a group rule later, edit `groupMap` and run the same
`helm upgrade`. People who came in through authentik lose the MEHO
group of a removed or changed line.

!!! note "You turn on the company login on an existing install"

    The Job needs two Keycloak rights to set up the broker:
    `manage-identity-providers` and `manage-realm`. The chart gives them
    only when the company login is on at the **first** install, because
    Keycloak reads its realm file only at its first start. If you turn
    the company login on later, the Job stops with a message that names
    the two rights. Then add them once by hand: in the Keycloak admin
    console, realm `meho`, open **Clients > `meho-admin`**, tab
    **Service accounts roles**, assign the two `realm-management` roles, and run
    `helm upgrade` again. For a Keycloak admin login, see
    [Reach the Keycloak admin console](#reach-the-keycloak-admin-console).

### 4. Check that it works

1. **Command line.** Run `meho login https://meho.example.com`. Open the
   link it prints. Keycloak sends you to authentik. Log in with your
   password and MFA. Keycloak may ask once "Grant Access to …": click
   **Yes**. Then run `meho status`. It must show your tenant and role.
2. **Web console.** Open `https://meho.example.com/ui/`. You land on
   authentik, then on the MEHO dashboard with your name and role.
3. **Claude Code.** Log in with the MEHO plugin
   ([Claude Code](../clients/claude-code.md)). The browser goes to
   authentik. Then `meho_status` returns your `tenant_id` and
   `tenant_role`.
4. **Token fields.** Read your raw token as shown in
   [Keycloak realm setup, Verify](keycloak-realm.md#verify). Expect:
    - `iss` is `https://meho.example.com/auth/realms/meho`;
    - `aud` holds `meho-backplane` and `https://meho.example.com/mcp`;
    - `tenant_id` is your tenant UUID, and `tenant_role` is the role of
      your group;
    - `groups` lists your MEHO group;
    - `approver: true` appears only for `MEHO-Approvers`.

## Your own Keycloak

Use this section if your site runs its own Keycloak instead of the
built-in one. You set up the same broker by hand. The result is the
same as with the Helm values above.

**First:** set up the realm with [Keycloak realm setup](keycloak-realm.md),
including the five groups and the mappers that read them. Check that a
local user in a group can log in. This page writes your Keycloak as
`<keycloak-host>` and your realm as `<realm>`.

### 1. Let Keycloak trust authentik

Skip this step if a public CA signs authentik's certificate.

Otherwise, give Keycloak the authentik CA as a PEM file, and point the
Keycloak setting `truststore-paths` at it. In a container, that is the
environment variable `KC_TRUSTSTORE_PATHS`, for example
`/opt/keycloak/conf/tls/authentik-ca.pem`
([Keycloak truststore guide](https://www.keycloak.org/server/keycloak-truststore)).
Restart Keycloak.

Without it, the login fails after authentik with "Unexpected error when
authenticating with identity provider", and the Keycloak log says
`PKIX path building failed`.

### 2. Add authentik as a login in Keycloak

You can click it in the admin console or run it with `kcadm.sh`, the
Keycloak admin command line. Both give the same result.

**In the admin console** (realm `<realm>`):

1. **Identity providers > OpenID Connect v1.0**.
    - **Alias** `company`, **Display name** `authentik`.
    - **Use discovery endpoint** on. **Discovery endpoint**: the
      address from Part 1, step 3.
    - **Client authentication**: "Client secret sent in the request
      body". **Client ID** and **Client Secret** from authentik.
      Click **Add**.
2. On the provider page, check or set:
    - **Validate Signatures** on, **Use JWKS URL** on.
    - Under **Advanced**: **Scopes** `openid profile email`.
      **Use PKCE** on, **PKCE Method** `S256`.
    - **Trust Email** off. **First login flow override**
      `first broker login`. **Sync mode** `force`. Click **Save**.
3. **Mappers** tab > **Add mapper**, once for each of the five groups:
    - **Name** `meho-group-MEHO-Operators`, **Sync mode override**
      Force, **Mapper type** Advanced Claim to Group.
    - **Claims**: key `groups`, value `MEHO-Operators` (the authentik
      group name). **Regex Claim Values** off.
    - **Group**: `MEHO-Operators` (the MEHO group).

    With Force, Keycloak checks the `groups` field at every authentik
    login. It adds the person to the group, or takes them out when the
    field no longer matches.
4. **Authentication > browser**, then the gear icon on the
   **Identity Provider Redirector** row. Set an **Alias** (for example
   `redirect-to-company`) and **Default Identity Provider** `company`.
   People now never see the Keycloak login page.

**The same with `kcadm.sh`.** Log in first, for example with
`kcadm.sh config credentials --server https://<keycloak-host> --realm master --user <admin>`.
Save this as `company-idp.json`, with your authentik address and Client
ID:

```json
{
  "alias": "company", "displayName": "authentik", "providerId": "oidc", "enabled": true,
  "trustEmail": false, "hideOnLogin": false, "firstBrokerLoginFlowAlias": "first broker login",
  "config": {
    "issuer": "https://authentik.example.com/application/o/meho/",
    "authorizationUrl": "https://authentik.example.com/application/o/authorize/",
    "tokenUrl": "https://authentik.example.com/application/o/token/",
    "userInfoUrl": "https://authentik.example.com/application/o/userinfo/",
    "jwksUrl": "https://authentik.example.com/application/o/meho/jwks/",
    "useJwksUrl": "true", "validateSignature": "true",
    "clientId": "<the authentik Client ID>", "clientAuthMethod": "client_secret_post",
    "defaultScope": "openid profile email",
    "pkceEnabled": "true", "pkceMethod": "S256", "syncMode": "FORCE"
  }
}
```

The authentik issuer ends with a `/`. Keep it. Then run:

```bash
REALM=<realm>
read -rs CLIENT_SECRET     # paste the authentik client secret; it is not shown
jq --arg s "$CLIENT_SECRET" '.config.clientSecret = $s' company-idp.json \
  | kcadm.sh create identity-provider/instances -r "$REALM" -f -
unset CLIENT_SECRET

for G in MEHO-Readers MEHO-Operators MEHO-SA-Operators MEHO-Admins MEHO-Approvers; do
  jq -n --arg g "$G" '{name: ("meho-group-" + $g), identityProviderAlias: "company",
      identityProviderMapper: "oidc-advanced-group-idp-mapper",
      config: {syncMode: "FORCE", "are.claim.values.regex": "false", group: ("/" + $g),
               claims: ([{key: "groups", value: $g}] | tojson)}}' \
    | kcadm.sh create identity-provider/instances/company/mappers -r "$REALM" -f -
done

EID=$(kcadm.sh get authentication/flows/browser/executions -r "$REALM" \
        --fields id,providerId --format csv --noquotes \
      | awk -F, '$2=="identity-provider-redirector" {print $1}')
kcadm.sh create "authentication/executions/$EID/config" -r "$REALM" \
  -b '{"alias":"redirect-to-company","config":{"defaultProvider":"company"}}'
```

If your authentik groups have other names, change `value` in `claims`
(the authentik name) and keep `group` (the MEHO group). Run the block
once. A second run fails, because the objects exist already.

### 3. Point MEHO at your Keycloak

Nothing changes in MEHO. MEHO still trusts your Keycloak, with the
values from [the install trail, Step 6](index.md#step-6-write-your-values-file).

### 4. Check that it works

Run the checks of [Part 2, step 4](#4-check-that-it-works). The only
difference: `iss` is `https://<keycloak-host>/realms/<realm>`.

## What admins must know

- **Removal takes effect at the next company login.** Keycloak checks
  the groups only when the person logs in through authentik. While the
  person's Keycloak session lasts, a new MEHO login reuses it and does
  not ask authentik. A login that is still running keeps working, and so
  does a long login for Claude Code (an offline login, up to 48 hours
  without use). To cut
  access now, also sign the person out in Keycloak, or disable the user
  there: open **Users >** the person **> Sessions**, click
  **Logout all sessions**, then **Revoke** on any row of type
  **Offline**. A MEHO token already issued stays valid until it
  expires, which takes a few minutes.
- **MFA is authentik's job.** Keycloak does not check it again. Turn on
  MFA in authentik for everyone who uses MEHO.
- **The emergency way in when authentik is down.** The local Keycloak
  accounts still work. With the built-in Keycloak, these are the two
  people the install made (their passwords are in the Secret
  `<release>-core-first-login`). Two ways:
    - Set `coreKeycloak.companyLogin.autoRedirect: false` and run
      `helm upgrade`. Keycloak shows its own login form again. Set it
      back to `true` when authentik works again. With your own
      Keycloak, clear **Default Identity Provider** on the
      **Identity Provider Redirector** instead (Part "Your own
      Keycloak", step 2.4).
    - For one login only: open the Keycloak login address with an
      empty `kc_idp_hint=` added at the end. Keycloak then shows its
      login form instead of sending you to authentik. MEHO's own login
      screens do not add this, so do it by hand: start the web console
      login, copy the Keycloak address (it starts with
      `https://<host>/auth/realms/meho/protocol/openid-connect/auth?`)
      from the browser's developer tools, add `&kc_idp_hint=`, and
      open it in the same browser.
- **A company user with the same name as a local account.** If an
  authentik user has the user name or email of a local Keycloak account
  (for example `admin`), Keycloak does not link the two by itself. It
  asks the person to prove that they own the local account first, for
  example with its password.
- **Group changes:** to move a person to another role, move them to
  another group in authentik. The next company login gives the new
  role.
- **Turning the company login off.** Setting
  `coreKeycloak.companyLogin.enabled: false` does not remove the login
  from Keycloak. To show the local login form again, keep `enabled` on
  and set `autoRedirect: false`.

### Reach the Keycloak admin console

The steps above that sign a person out, or add the two rights, need the
Keycloak admin console. With the built-in Keycloak:

- The console is not on the ingress by default. Set
  `coreKeycloak.ingress.exposeAdmin: true` to reach it at
  `https://<host>/auth/admin/`.
- The install makes no admin account for people. Make one with
  Keycloak's
  [bootstrap admin recovery](https://www.keycloak.org/server/bootstrap-admin-recovery)
  command, and remove it when you are done.

## If something fails

| What you see | What it means | Fix |
|---|---|---|
| authentik shows a redirect URI error | The address in authentik does not match. | Copy the redirect address from the install notes into authentik, mode Strict. |
| The bootstrap Job stops: "publishes no signing keys" | authentik has no signing key. | Pick an RSA **Signing Key** in the authentik provider (Part 1, step 2). |
| The bootstrap Job stops and names `manage-identity-providers` and `manage-realm` | The company login was off at the first install. | See "You turn on the company login on an existing install" above. |
| The bootstrap Job stops: "groupMap names the group …, which the realm does not have" | A right side in `groupMap` is not a MEHO group. | Use one of the five MEHO group names. |
| Keycloak shows "Unexpected error when authenticating with identity provider" | Keycloak does not trust authentik's certificate. | Set `caBundle` (built-in) or `KC_TRUSTSTORE_PATHS` (your own Keycloak). |
| `401 missing_tenant_claim` | The person is in no MEHO group. | Check the person's authentik group, the `groupMap` line, and that `profile` is in the scopes. Log in again. |
| `401 multiple_role_groups` | The person is in two MEHO groups. | Keep one group per person in authentik. |
| Keycloak asks for a name or an email | authentik did not send that field. | Give the person a first name, a last name and an email in authentik. |
