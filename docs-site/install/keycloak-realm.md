# Keycloak realm setup

MEHO always ships with a built-in Keycloak. Set
`coreKeycloak.enabled: true` in the Helm values and the chart makes
the realm `meho` at install, with everything on this page. You can
then skip to [Company login](login/index.md).

This page configures a realm so that `meho login` (the CLI's
device-code flow), the web UI and MCP clients (browser OAuth) can
authenticate. It is the promoted, outsider-readable version of the
recipe MEHO's own labs deploy from.

A person gets their tenant and role from **their group only**. No
login client carries a fixed tenant or role. To connect your company
accounts (Active Directory, Entra ID, …) to the groups, follow
[Company login](login/index.md) after this page.

**Where this fits:** you arrived from
[the install trail, Step 4](index.md#step-4-set-up-the-keycloak-realm).
Work through this page top to bottom, run the verification block at
the end, then return to the trail.

## What you are creating

| Object | Kind | Used by |
|---|---|---|
| `meho-backplane` | Confidential client | The backplane itself — the audience it validates tokens for. If your realm does not have it yet, create it first: a confidential client with ID `meho-backplane` and no login flows enabled — it exists as the resource-server identity whose client ID is the token audience (`keycloak.audience` in the chart values). |
| `meho-cli` | **Public** client, device-code grant | `meho login` |
| `meho-cli-approve` | **Public** client, authorization-code + PKCE on a loopback address only, with its own login flow | The fresh sign-in of `meho approvals approve`. Only its tokens can approve on the command line. |
| `meho approval sign-in` | Login flow, bound to `meho-cli-approve` | Always asks for the password (and the one-time code, when the person has one) |
| `meho-mcp` | **Public** client, authorization-code + PKCE | Browser-capable MCP clients (Claude Desktop, MCP Inspector, …) |
| `meho-web` | Confidential client, authorization-code + PKCE | The MEHO web UI. Redirect URI `https://<host>/ui/auth/callback`. |
| `meho-admin`, `meho-check-runner` | Confidential service clients | MEHO manages clients and users in the realm (`meho-admin`), and runs scheduled checks (`meho-check-runner`) |
| 8 protocol mappers | On the login clients (`meho-cli-approve` without `meho-mcp-audience`) | Make issued tokens carry the claims the backplane validates |
| 4 default client scopes | On the login clients | Including the `basic` scope that carries the mandatory `sub` claim |
| `mcp:read`, `mcp:execute`, `mcp:admin` | Client scopes, optional on `meho-mcp` | The scopes MEHO's MCP clients ask for at login |
| `offline_access` | Built-in client scope, optional on both public clients | The long login that renews itself |
| `MEHO-Readers`, `MEHO-Operators`, `MEHO-SA-Operators`, `MEHO-Admins`, `MEHO-Approvers` | Top-level groups with attributes | Give each person their tenant and their role |
| `MEHO-Platform-Admins` | Top-level group with one attribute | Rights across all tenants, for the people who run MEHO |
| A user in `MEHO-Admins` and `MEHO-Platform-Admins` | Realm user | The first admin: approves device-code logins and operates MEHO |
| A user in `MEHO-SA-Operators` | Realm user | A second person, so a change the first admin asks for can be approved |
| A user of realm `master` with every role of the client `meho-realm` except `impersonation` | `master` user (not in realm `meho`) | The console admin: administers realm `meho` in the Keycloak admin console. MEHO refuses its tokens. |

!!! tip "There is an automation for this"

    The CLI ships an idempotent helper — `meho admin keycloak
    bootstrap-clients` — that creates the three public clients, the eight
    mappers, the four default scopes, the three `mcp:*` scopes, the
    five groups and `MEHO-Platform-Admins`, and puts the first admin
    user in `MEHO-Admins` and `MEHO-Platform-Admins`, in one
    invocation. This page is the
    manual reference path (and what the helper encodes); use the helper
    once you have an admin credential wired up, use this page to
    understand or verify what it did.

    Use the helper only for a **new realm**. On a realm that already
    works, update the sign-in clients one setting at a time, for
    example with the operation `keycloak.client.update` or in the admin
    console. The helper sets its values again on every run: it
    overwrites the redirect URIs, web origins and flow switches of the
    three clients, and the eight mappers, also `tenant-id` and
    `tenant-role`. If you do run it on an existing realm, run it with
    `--dry-run` first: it reads the realm and prints each change, with
    the old and the new value, and changes nothing.

## Step 1 — Trust the deployment's CA on your workstation

Skip this step if your backplane and Keycloak present certificates
from a public CA.

If they are signed by an internal CA, install that CA into your
workstation's **operating-system trust store** before anything else —
`meho login` and most MCP clients verify TLS against the OS store, not
against environment variables. Platform commands and the reasons are
on [TLS and ingress](tls-ingress.md#your-workstation-os-trust-store).
`meho login` contacts **both** hosts, so verify both from a fresh shell
before continuing:

```bash
curl -sf https://<backplane-host>/healthz
curl -sf https://<keycloak-host>/realms/<realm>/.well-known/openid-configuration
```

An `x509: certificate signed by unknown authority` from `meho login`
later means one of these two was skipped.

## Step 2 — Create the public `meho-cli` client

In the realm that hosts `meho-backplane`, create a client with:

| Setting | Value | Why |
|---|---|---|
| Client ID | `meho-cli` (suggested — any identifier works) | Must match `config.keycloakCliClientId` in your chart values. |
| Client authentication | **Off** (public client) | The CLI has nowhere to store a secret; the device grant against a confidential client fails with `unauthorized_client`. |
| Standard flow | **On** | Older `meho` CLI versions open the approval sign-in on this client. The current CLI uses `meho-cli-approve` (below). `meho login` uses the device flow. |
| PKCE method (Advanced → Proof Key for Code Exchange) | (none), or `S256` once every person runs CLI 0.44.0 or later | From CLI 0.44.0, `meho login` sends PKCE (`S256`) at the device login. Older CLI versions send none, and Keycloak checks PKCE at the device login too, so a required method refuses their `meho login`. The browser sign-in of older CLI versions sends a challenge, and Keycloak accepts it. |
| Direct access grants | Off | Password grant is out of scope by design. |
| Implicit flow | Off | Deprecated by OAuth 2.1. |
| Service accounts roles | Off | Public clients cannot hold credentials. |
| **OAuth 2.0 Device Authorization Grant** | **On** | This *is* `meho login` ([RFC 8628](https://www.rfc-editor.org/rfc/rfc8628)). |
| Valid redirect URIs | `http://127.0.0.1:*` | Only for the browser sign-in of older CLI versions. The CLI listens on 127.0.0.1 with a random port ([RFC 8252](https://www.rfc-editor.org/rfc/rfc8252#section-7.3)). Do not add other addresses. |
| Web origins | (none) | The CLI is not a web page. |
| `offline_access` client scope | Assign as **Optional** | `meho login` asks for it by default, so the login renews itself. See [How long each login lasts](#how-long-each-login-lasts). |
| Client Offline Session Idle (attribute `client.offline.session.idle.timeout`) | **`172800`** seconds (48 hours) | The login ends after 48 hours without use. Without it, the realm's Offline Session Idle applies (30 days by default). Set it on the **Advanced** tab of the client. |

### The approval client `meho-cli-approve`

An approval is a person's decision. On the command line and REST, MEHO
accepts an approve only with a token from the person's own approval
sign-in:

- the token was made for the approval client (its `azp` claim is
  `meho-cli-approve`). A `meho login` token or an MCP token never
  approves, even right after a login;
- the sign-in is at most 15 minutes old (the `auth_time` claim). Inside
  these 15 minutes one sign-in may approve several waiting changes. Each
  approval is still a separate decision about one request;
- the sign-in asked for the password (the `acr` claim is set and is not
  `0`). Keycloak gives `acr` `1` to a sign-in that asked for the
  password, and `0` to one that did not;
- an operation marked `destructive` needs a sign-in of its own, at most
  5 minutes old. After it, that sign-in approves nothing more.

`meho approvals approve` opens the browser and asks for the password
again (it sends `prompt=login` and `max_age=0`). It keeps the token in
memory for this one run and never stores it. The approval client gives
no refresh token, and its access token lasts as long as the approval
window. When the token runs out, the CLI says so and asks for a new
sign-in. Reject needs no new sign-in.

The web interface applies the same age rules to the sign-in behind the
console session, and it checks that this sign-in asked for the password.
When the sign-in is too old, the console shows **Sign in to approve**
and asks for the password again (see
[The web console session](#the-web-console-session)).

Create a second public client with these settings:

| Setting | Value | Why |
|---|---|---|
| Client ID | `meho-cli-approve` (suggested) | Must match `config.keycloakCliApproveClientId` in your chart values (default `meho-cli-approve`). |
| Client authentication | **Off** (public client) | The CLI has nowhere to store a secret. |
| Standard flow | **On** | The browser sign-in of an approval. |
| PKCE method | `S256` | The CLI sends a PKCE challenge. |
| OAuth 2.0 Device Authorization Grant | Off | Not used. |
| Direct access grants, implicit flow, service accounts | Off | Not used. |
| Valid redirect URIs | `http://127.0.0.1:*` | The CLI listens on 127.0.0.1 with a random port. Do not add other addresses. |
| Web origins | (none) | The CLI is not a web page. |
| `offline_access` | Not assigned | The approval sign-in never gives a long login. |
| Use refresh tokens (**Advanced → OpenID Connect Compatibility Modes**) | **Off** | The sign-in gives one access token and no refresh token. |
| Access Token Lifespan (**Advanced → Advanced settings**) | 15 minutes | As long as the approval window. If you change `config.freshSignInMaxAgeSeconds`, set the same time here. |

Give it the default scopes of Step 4 and the mappers of Step 3, except
`meho-mcp-audience`: its token is only for the approve call. The `basic`
scope puts `auth_time` into the access token, and the `acr` scope puts
`acr` into it. Without both, every approve on the command line is
refused.

**Its own login flow.** The approval client signs in with its own flow,
which always asks for the password. Create it under **Authentication →
Create flow**:

1. Name `meho approval sign-in`, type **Basic flow**.
2. Add the step **Username Password Form**. Set it to **Required**.
3. Add a sub-flow `meho approval sign-in - one-time code`. Set it to
   **Conditional**. In it, add **Condition - user configured** and
   **OTP Form**, both **Required**. A person who has set up a one-time
   code then has to enter it.
4. Add no other step. The flow must not have the **Cookie**,
   **Kerberos** or **Identity Provider Redirector** step.
5. Open **Clients → `meho-cli-approve` → Advanced → Authentication flow
   overrides**. Set **Browser Flow** to `meho approval sign-in`.

People who sign in with a company login see its button on the form and
click it.

**On an existing realm.** Create `meho-cli-approve` by hand as above,
or with the operation `keycloak.client.create`, and set the two token
settings on an existing `meho-cli-approve` with
`keycloak.client.update`. Add its login flow as the next section says.
Do not
run `meho admin keycloak bootstrap-clients` for this: on an existing
realm it also rewrites the `meho-cli` and `meho-mcp` clients and their
tenant and role mappers. Use it only for a new realm. On a new realm,
a run that stopped half-way leaves a flow with missing steps; the next
run finishes it. A flow `meho approval sign-in` with other steps than
the ones above is left as it is, and the client is not bound to it. The
Keycloak that ships with the MEHO chart creates the client on the next
upgrade (the realm finishing job adds a client that is missing, and sets
the two token settings on an existing one). The job makes
the login flow only when its account `meho-admin` has the
`manage-realm` role, which it has when the company login is on.
Otherwise add the flow through MEHO (see
[Add the approval flow to a running realm through MEHO](#add-the-approval-flow-to-a-running-realm-through-meho))
or by hand as above.

**Bind the flow before people approve on the command line.** A
command-line approval must always sign in through `meho approval
sign-in`, also on a realm that was made before this flow existed. So on
an upgraded install, add the flow and bind it to `meho-cli-approve`
first, with the operation above or by hand. Until the client exists and
uses this flow, approve in the web interface. This matters most on a
realm whose login goes straight to a company login: the approval flow
does not use that login, so the person always types the password (and
the code) at Keycloak. Check the binding with
`keycloak.client.get` (`authenticationFlowBindingOverrides.browser` must
name the flow) and the realm's login with `keycloak.identity_provider.list`.

Two backplane settings control the check:

| Setting | Default | Meaning |
|---|---|---|
| `FRESH_SIGN_IN_MAX_AGE_SECONDS` (chart `config.freshSignInMaxAgeSeconds`) | `900` | The approval window: how old the sign-in may be, in seconds (30 to 3600). One sign-in may approve several requests inside it. |
| `PER_ACTION_SIGN_IN_MAX_AGE_SECONDS` (chart `config.perActionSignInMaxAgeSeconds`) | `300` | How old the sign-in may be for an action that needs a fresh sign-in each time: approving a `destructive` operation, and turning approval off or lowering a risk level for an operation. |
| `KEYCLOAK_CLI_APPROVE_CLIENT_ID` (chart `config.keycloakCliApproveClientId`) | `meho-cli-approve` | The only client whose tokens can approve on the command line or REST. Empty: only the web interface can approve. |
| `APPROVAL_ONE_TIME_CODE_ACR_VALUES` (chart `config.approvalOneTimeCodeAcrValues`) | empty (off) | Comma-separated `acr` values that mean "the person entered a one-time code". |

**Optional one-time code step.** You can ask for a code from an
authenticator app (a Keycloak one-time password) on top of the password.
The command line does not ask Keycloak for a level itself. So the login
flow of the approval client must ask for the code at every sign-in:

1. In the flow `meho approval sign-in`, make **OTP Form** a required step.
2. Under **Realm settings → General → ACR to LoA mapping**, map the level
   that flow reaches to an `acr` value, for example `otp`.
3. Put the same value into `APPROVAL_ONE_TIME_CODE_ACR_VALUES`.

MEHO then records such an approval as a one-time code approval. The
sign-in must still be recent: an `acr` value alone never approves.

#### Add the approval flow to a running realm through MEHO

The operation `keycloak.authflow.ensure_approval_flow` adds the flow
`meho approval sign-in` to the realm of a Keycloak target (its managed
realm) and makes it the browser flow of `meho-cli-approve`. A second
person must approve it.

```bash
meho operation call keycloak-admin-26.x keycloak.authflow.ensure_approval_flow \
  --target <keycloak-target>
```

- The flow is missing: MEHO makes it with the steps above and binds it.
- The flow is there with the same steps: MEHO changes nothing. When the
  client already uses it, the result says `flow_created: false` and
  `binding_changed: false`. So the call is safe to run twice.
- The flow is there with other steps: MEHO stops, changes nothing, and
  names the first difference. Fix the flow by hand, then run it again.
- `meho-cli-approve` is missing: MEHO stops before it changes anything.
  Create the client first.

MEHO reads the client back after the change. `browser_flow_id` in the
result is the flow the client now uses. Its other settings stay as they
were.

To check how people sign in to the realm through an outside login (a
company login, for example), list the realm's identity providers:

```bash
meho operation call keycloak-admin-26.x keycloak.identity_provider.list \
  --target <keycloak-target>
```

Each row has the alias, the type, whether it is on, whether it is hidden
on the login page, and `redirect_flows`: the login flows that send people
straight to it. The approval flow must not be in `redirect_flows`. The
list never shows the provider's secret.

## Step 3 — Add the protocol mappers

Tokens minted by `meho-cli` must carry the same claim shape the
backplane validates — otherwise tokens are issued cleanly and then
rejected on every call. Add these eight mappers to the client:

| Mapper name | Type | Output claim | Without it |
|---|---|---|---|
| `audience-meho-backplane` | Audience mapper | adds `meho-backplane` to `aud` | Every call rejected with `invalid_audience`. |
| `meho-mcp-audience` | Audience mapper (**Included Custom Audience** = `https://<backplane-host>/mcp`, **no trailing slash**) | adds the MCP resource URI to `aud` | Tokens work for the REST API but not for `/mcp`. |
| `tenant-id` | User Attribute (**User Attribute** = `tenant_id`, **Claim JSON Type** `String`) | `tenant_id`, from the person's group | Rejected with `missing_tenant_claim`. |
| `tenant-role` | User Attribute (**User Attribute** = `tenant_role`, **Claim JSON Type** `String`) | `tenant_role`, from the person's group | Rejected with `missing_tenant_role_claim`. |
| `approver` | User Attribute (**User Attribute** = `approver`, **Claim JSON Type** `boolean`) | `approver`, only for `MEHO-Approvers` | Nobody with the `read_only` role can approve. |
| `platform-admin` | User Attribute (**User Attribute** = `platform_admin`, **Claim JSON Type** `boolean`) | `platform_admin`, only for `MEHO-Platform-Admins` | Nobody gets rights across tenants. |
| `meho-tenants` | User Attribute (**User Attribute** = `meho_tenant`, **Token Claim Name** = `meho_tenants`, **Claim JSON Type** `String`, **Multivalued** on, **Aggregate attribute values** on) | `meho_tenants`, a list with one entry per role group of the person | The token does not list the person's tenants. One tenant still works. |
| `groups-claim` | Group-membership mapper | `groups` | Group-gated surfaces silently return empty results. |

On the four User Attribute mappers `tenant-id`, `tenant-role`,
`approver` and `platform-admin`, set **Token Claim Name** to the same
name as the attribute, **Add to access token** on, **Add to ID token**
off, **Add to userinfo** on, and keep **Multivalued** and **Aggregate
attribute values** off. On `meho-tenants`, set the same token switches,
but turn **Multivalued** and **Aggregate attribute values** on: then
Keycloak collects the `meho_tenant` value of every group of the person.

These mappers hold **no fixed value**. They read the attributes of the
person's group ([Step 5](#step-5-create-the-five-groups)): when a person
has no value of their own, the stock User Attribute mapper takes it
from the person's group. Never put a fixed `tenant_id` or `tenant_role`
(a Hardcoded claim mapper) on these clients: everyone who logs in
through them would get the same tenant and role. The full recipe and
the reasons are in the
[tenant claims recipe](https://github.com/evoila/meho/blob/main/docs/cross-repo/keycloak-tenant-claims.md)
and the
[decision record](https://github.com/evoila/meho/blob/main/docs/decisions/login-role-groups.md).

## Step 4 — Assign the four default client scopes

Explicitly assign these as **default** client scopes on the client:

| Scope | Why |
|---|---|
| `basic` | Carries the **`sub`** and **`auth_time`** claims. An approve on the command line needs `auth_time` (see Step 2). Since Keycloak 25, `sub` is emitted by a protocol mapper inside this scope rather than unconditionally — and a JWT access token without `sub` is (correctly) rejected. This is the single most opaque failure in the whole setup. |
| `roles` | Realm and client roles in the token. |
| `web-origins` | CORS origins for browser flows. |
| `acr` | Authentication context class; cheap to ship, needed for step-up flows. |

!!! warning "Creating clients via the admin API? Read this"

    Clients created through Keycloak's **admin REST API** (or
    `kcadm.sh`) do **not** auto-inherit the realm's default client
    scopes the way the admin-console "Create" button does. If you
    scripted Step 2, you must set
    `defaultClientScopes: ["basic","roles","web-origins","acr"]`
    explicitly in the request body — or add the scopes in the console
    afterwards. The symptom of missing `basic` is a token that *looks*
    complete but has no `sub`, and a wall of `invalid_token` /
    `missing_sub` responses.

## Step 5 — Create the five groups

A person gets their tenant and their role from their group. Create
these five groups at the **top level** (not inside another group), and
give each one these **attributes** (open the group → **Attributes**):

| Group | `tenant_id` | `tenant_role` | `approver` | `meho_tenant` |
|---|---|---|---|---|
| `MEHO-Readers` | your tenant UUID | `read_only` | – | `<uuid>:<name>:Readers` |
| `MEHO-Operators` | your tenant UUID | `operator` | – | `<uuid>:<name>:Operators` |
| `MEHO-SA-Operators` | your tenant UUID | `operator` | – | `<uuid>:<name>:SA-Operators` |
| `MEHO-Admins` | your tenant UUID | `tenant_admin` | – | `<uuid>:<name>:Admins` |
| `MEHO-Approvers` | your tenant UUID | `read_only` | `true` | `<uuid>:<name>:Approvers` |

All five groups carry the same tenant UUID (mint one with `uuidgen`).
`tenant_role` uses an underscore (`read_only`), never a hyphen. In
`meho_tenant`, `<uuid>` is the same tenant UUID and `<name>` is the
tenant's short name (lowercase letters, digits and dashes, for example
`main`).

Also create the group **`MEHO-Platform-Admins`** with one attribute,
`platform_admin` = `true`. Its members get rights across all tenants.
It is not a role group: it holds no tenant and no role, so a person in
it is also in one of the five groups.

Then put each person in **exactly one** of the five groups:

- A person in **no** group gets no tenant. MEHO refuses the login with
  `401 missing_tenant_claim`.
- A person in **two** groups is refused. MEHO answers
  `401 multiple_role_groups` and names the groups it found, because
  Keycloak would otherwise pick one of the two roles at random.
- To change a person's role, move them to another group. The change
  applies at their next login.
- Leave the person's own `tenant_id`, `tenant_role`, `approver` and
  `meho_tenant` attributes empty: a value on the person wins over the
  group, and a `meho_tenant` value on the person is added to their
  tenant list. Keep
  **Realm settings → General → Unmanaged Attributes** at **Disabled**
  (the default), **Admin can view** or **Admin can edit**, so people
  cannot set these values on themselves
  ([Keycloak docs](https://www.keycloak.org/docs/26.8.1/server_admin/#_understanding-managed-and-unmanaged-attributes)).

To add another tenant later, follow [Add a tenant](../guides/tenant-bootstrap.md).

`meho admin keycloak bootstrap-clients --tenant-id <uuid>` creates the
five groups for you. On a later run, pass the same `--tenant-id`:
without it, the command writes the all-zero UUID to the five groups.
`--tenant-name` sets the short name in `meho_tenant` (default `main`).

### More than one tenant

A person can be in role groups of several tenants, one group per
tenant. Each extra tenant gets its own five groups,
`MEHO-<name>-Readers`, `MEHO-<name>-Operators`,
`MEHO-<name>-SA-Operators`, `MEHO-<name>-Admins` and
`MEHO-<name>-Approvers`. These groups carry **only** `meho_tenant`
(for example `<uuid>:lab-b:Operators`), with the extra tenant's own
UUID. They carry no `tenant_id` and no `tenant_role`.

`meho admin keycloak bootstrap-clients --add-tenant lab-b` creates the
five groups of the tenant `lab-b`. Give its UUID with
`--add-tenant-id <uuid>`. Without it, the command reuses the UUID the
groups already carry, or makes a new one and prints it. Keep it.

The token of a person in `MEHO-Readers` and `MEHO-lab-b-Operators`
lists both tenants in `meho_tenants`. A site with one tenant gets a
list with one entry, and nothing else changes. The design is in the
[decision record](https://github.com/evoila/meho/blob/main/docs/decisions/2026-10-28-several-tenants-per-person.md).
The steps for an admin, from the new tenant to the switch between
tenants, are in [One person in several tenants](../guides/several-tenants.md).

Also create the group **`MEHO-Platform-Admins`** at the top level, with
one attribute: `platform_admin` = `true`. It gives no tenant and no
role. Only the platform admin (usually the first admin) is in it, on
top of one of the five groups.

To fill the groups from your company directory instead of by hand, see
[Company login](login/index.md).

## Step 6 — Create an operator user

Device-code login requires a real user to approve the request — a
fresh realm prepared only for service-to-service traffic may not have
one. Create at least one user with:

- a password (whatever your realm policy requires), and
- membership in the **`MEHO-Admins`** group, so the user gets the
  `tenant_admin` role, and in **`MEHO-Platform-Admins`**.

Create a second person in **`MEHO-SA-Operators`**. A risky change
waits until a second person approves it, and nobody approves their own
request. So the first admin needs this second person (or a member of
`MEHO-Approvers`) to approve their changes.

The user's email does not need to be verified for device-code login to
complete.

## The console admin

The built-in Keycloak makes a third user at install: the console
admin, default name `console-admin` (values key
`coreKeycloak.firstLogin.consoleAdminUsername`). It administers realm
`meho` in the Keycloak admin console. Use it for the recipes
[Local users](login/local-users.md) and [AD / LDAP](login/ad-ldap.md).
A company login through OpenID Connect (Entra ID, authentik, your own
Keycloak) is set up with Helm values instead.

The console admin is a user of realm `master`, not of realm `meho`. It
holds every role of the client `meho-realm` except `impersonation`:
the client Keycloak makes in `master` to administer realm `meho`. That
gives it the powers of `realm-admin` over realm `meho`, but it cannot log
in as a person of realm `meho` directly. It can still reset a person's
password and then log in as that person. So treat it as a full admin of
realm `meho`, and give it only to people you trust with that. At its
first login it sets a new password and an
authenticator code, because realm `master` does not ask for a code by
default. It lives in `master` for a reason:
`meho-admin` has `manage-users` in realm `meho`, so it can reset the
password of any user there and take the account over. It cannot see
users of realm `master`. MEHO refuses the console admin's tokens, because
they do not come from realm `meho`.

1. Route the admin console: set `coreKeycloak.ingress.exposeAdmin: true`
   and run `helm upgrade`. This routes `/auth/admin` and
   `/auth/realms/master`. A port-forward alone is not enough: Keycloak
   sends the console to its public address `https://<host>/auth`, so the
   console's calls go through the ingress anyway.
2. Read the user name and the temporary password from the Secret. Read
   the name from the Secret, not from your values: the Secret keeps the
   name of the first run.

    ```bash
    kubectl -n <namespace> get secret <release>-core-console-admin \
      -o jsonpath='{.data.username}' | base64 -d; echo
    kubectl -n <namespace> get secret <release>-core-console-admin \
      -o jsonpath='{.data.password}' | base64 -d; echo
    ```

3. Open `https://<host>/auth/admin/master/console/#/meho` and log in
   with that name. Keycloak asks for a new password, a name and an
   email. Then pick realm `meho` in the realm list, if the console does
   not show it already.
4. When you are done, set `exposeAdmin` back to `false` if you do not
   want the console on the internet.

Only the first run of the bootstrap Job can make the console admin. That
run uses the temporary first-start account, which is a `master` admin.
Later runs use `meho-admin`, which cannot make users in realm `master`:
they make nothing and only say in the log whether the Secret exists.

Two names are refused:

- A name that starts with `service-account-`. Keycloak uses that prefix
  for its service accounts. This holds for all three users: the first
  admin, the approver and the console admin.
- A name a `master` user already has, when the Secret does not exist
  yet. That user was not made by this install. The Job fails and asks
  you to set `coreKeycloak.firstLogin.consoleAdminUsername` to another
  name.

### Console admin on an older install

An install made before the console admin existed has no console admin,
and the bootstrap Job cannot make one: the temporary first-start account
was removed on the first run, and `meho-admin` cannot make users in realm
`master`. The Job does not fail. It says in its log that there is no
console admin and points here. Make one once by hand
([Keycloak: recovering the admin account](https://www.keycloak.org/server/bootstrap-admin-recovery)):

1. Make a temporary admin in realm `master`. Skip this if you already
   have a `master` admin. In the running Keycloak pod, which already has
   the database settings (port 9001, because the server uses 9000):

    ```bash
    kubectl -n <namespace> exec -it <release>-keycloak-0 -- \
      /opt/keycloak/bin/kc.sh bootstrap-admin user \
      --username temp-admin --http-management-port 9001
    ```

    It asks for a password. This starts a second Java process in the
    pod, so the pod needs some spare memory.

2. With `exposeAdmin: true` (see above), open
   `https://<host>/auth/admin/master/console/` and log in as `temp-admin`.
3. Stay in realm `master`. **Users → Add user**, user name
   `console-admin` (any name that does not start with
   `service-account-`), **Create**.
4. **Credentials → Set password**. Turn **Temporary** on, so the user
   picks a new password at the first login. On **Details → Required user
   actions**, add **Configure OTP**, so the user also sets up an
   authenticator code. **Save**.
5. **Role mapping → Assign role → Filter by clients**, search for
   `meho-realm`, select all of its roles except `impersonation`,
   **Assign**.
6. Optional: keep the login in a Secret, as a new install does:

    ```bash
    kubectl -n <namespace> create secret generic <release>-core-console-admin \
      --from-literal=username=console-admin \
      --from-literal=password='<the temporary password>'
    ```

    The next `helm upgrade` then prints `console admin: kept`.

7. **Users → temp-admin → Delete.** Skip this if you used an existing
   `master` admin.

Then log in as in [The console admin](#the-console-admin).

## The MCP client (`meho-mcp`)

Browser-capable MCP clients authenticate with authorization-code +
PKCE, which needs a second public client. Repeat Steps 2–4 with these
differences:

| Setting | `meho-mcp` | Why it differs |
|---|---|---|
| Standard flow (authorization-code + PKCE) | **On** | The MCP specification mandates OAuth 2.1 authorization-code + PKCE. |
| Device grant | Off | Not used by MCP clients. |
| Valid redirect URIs | **Loopback only** — `http://localhost:<port>/callback` (Claude Code) plus `http://localhost:<port>/oauth/callback` and its `http://127.0.0.1:<port>/oauth/callback` twin (the `mcp-remote` shim) | MEHO is internal-only, so there is **no** public `claude.ai` Custom-Connector redirect. `<port>` matches the client's configured callback port; the exact per-client values are in [Connect clients](../clients/index.md). |
| PKCE challenge method | `S256` | Spec-required for public clients. |
| `offline_access` client scope | Assign as **Optional** | The MEHO Claude Code plugin, the Claude Desktop bundle, and Claude Code's own HTTP client ask for it, so the login lasts past the realm's SSO idle limit. Keycloak refuses a login that asks for a scope the client does not offer: it answers `invalid_scope` and issues no token. |
| `mcp:read`, `mcp:execute`, `mcp:admin` client scopes | Create each, then assign as **Optional** | The Claude Code plugin and the Claude Desktop bundle ask for `mcp:read mcp:execute` at every login, and for `mcp:admin` when you turn on the operator tools. Keycloak does not ship these scopes. Create each one under **Client scopes → Create client scope**: protocol **OpenID Connect**, **Include in token scope** On. Then add all three to `meho-mcp` as **Optional**. If one is missing, Keycloak refuses the login with `invalid_scope`. `meho-cli` does not get them. |
| Client Offline Session Idle (attribute `client.offline.session.idle.timeout`) | **`172800`** seconds (48 hours) | Sets how long the MCP login lasts with no use. Without it, the realm's Offline Session Idle applies (30 days by default). If you set up the client by hand, set it on the **Advanced** tab of the client, or with `kcadm.sh update clients/<client-uuid> -r <realm> -s 'attributes."client.offline.session.idle.timeout"=172800'`. |

The eight mappers and four default scopes apply identically. Which MCP
client uses which flow — and everything else client-side — is the
[Connect clients](../clients/index.md) section's territory.

## How long each login lasts

| Client | Who uses it | Login | It ends |
|---|---|---|---|
| `meho-cli` | `meho login` (the default) | Long login (`offline_access`). It renews itself. | After 48 hours without use, or after 7 days at most. |
| `meho-cli` | `meho login --offline=false` | Short login | At the realm's SSO Session Idle limit (30 minutes by default). |
| `meho-mcp` | The Claude Code plugin, the Claude Desktop bundle, a plain HTTP entry in Claude Code's `.mcp.json` | Long login (`offline_access`). It renews itself. | After 48 hours without use, or after 7 days at most. |
| `meho-web` | The web console | Browser session | At the realm's SSO Session limits. |

The 7-day maximum is set in MEHO's built-in login setup (realm setting
**Offline Session Max Limited** On, **Offline Session Max** 7 days). On
your own Keycloak, turn on **Offline Session Max Limited** to get a
maximum; Keycloak's default is 60 days.

**To end a person's long login:** in the Keycloak admin console, open
**Users**, then the person, then the **Consents** tab, and revoke the
client (`meho-cli` or `meho-mcp`). The next renewal fails, and the CLI
asks the person to run `meho login` again. With the Admin REST API:
`DELETE /admin/realms/<realm>/users/<user-id>/consents/<client-id>`.
The **Offline Access** tab of each client lists all long logins of
that client.

If the realm does not offer `offline_access` to `meho-cli` (an older
realm), `meho login` logs in without it. It prints one line: the login
ends after a short time without use, and how to fix the realm. MEHO's
built-in login setup gets the scope at the next upgrade. On your own
Keycloak, assign it as described in Step 2, in the admin console. Do
not run `meho admin keycloak bootstrap-clients` on a realm that already
works: it also rewrites the other sign-in clients and their mappers.

### How long an MCP login lasts

With `offline_access`, Keycloak issues an **offline** refresh token. The
login then lasts until it has not been used for the offline idle limit
above: 48 hours with the recommended setting. Using MEHO renews the
token, which starts the 48 hours again. If your realm turns on **Offline
Session Max Limited**, that maximum also applies.

Without `offline_access`, the refresh token ends at the realm's **SSO
Session Idle** limit, which is 30 minutes by default. After that, the next
MEHO call needs a new browser login.

Two more things must be true:

- The user has the realm role `offline_access`. Keycloak gives it to every
  user through the realm's default roles (`default-roles-<realm>`). Without
  it, the token request fails with "Offline tokens not allowed for the user
  or client".
- `meho admin keycloak bootstrap-clients` assigns `offline_access` and the
  three `mcp:*` scopes as optional scopes on `meho-mcp`. It also sets the 48-hour offline idle
  limit, on new and existing clients. To use a different limit, pass
  `--mcp-offline-session-idle=<seconds>`. Each run sets the value again,
  so a value changed by hand goes back to 48 hours unless you pass the
  flag.
- The same command also gives `meho-cli` the scope `offline_access` and a
  48-hour offline idle limit. Pass `--cli-offline-access=<seconds>` for
  another limit, or `--cli-offline-access=0` to leave `meho-cli` without
  a long login. The `=0` run also removes a long login an earlier run
  gave `meho-cli`; logins that ask for `offline_access` are then refused.

## The web console session

The web console keeps its own session on the MEHO server. It ends after
**1 hour without use**, and after **10 hours** at most. The console
renews its login at Keycloak in the background. If Keycloak has ended
its own session earlier, the renewal fails and the console session ends
too. So set the realm's SSO session to the same values:

| Realm setting | Value | Where |
|---|---|---|
| SSO Session Idle (`ssoSessionIdleTimeout`) | **`3600`** seconds (1 hour) | **Realm settings → Sessions** |
| SSO Session Max (`ssoSessionMaxLifespan`) | **`36000`** seconds (10 hours) | **Realm settings → Sessions** |

Keycloak's defaults are 30 minutes and 10 hours. With them, the console
session ends after 30 minutes without use.

When the session ends, the console shows "Your session ended. Please
sign in again." and a **Sign in** button. After the login, the person is
back on the same page.

The two MEHO settings are `UI_SESSION_SLIDING_EXTENSION_SECONDS`
(default `3600`) and `UI_SESSION_ABSOLUTE_LIFETIME_SECONDS` (default
`36000`). If you change them, change the realm values to match.

**Approving in the console needs a recent sign-in.** The console session
may be hours old, but an approval needs the sign-in behind it to be at
most 15 minutes old (`FRESH_SIGN_IN_MAX_AGE_SECONDS`) and to have asked
for the password (the `acr` claim). A `destructive` operation needs a
sign-in of its own, at most 5 minutes old. When the sign-in is too old,
the request shows **Sign in to approve** in place of **Approve**. That
link starts a console login with `prompt=login` and `max_age=0`, so
Keycloak asks for the password (and the code, or a passkey) also in a
browser it remembers. The person then opens the request again and
approves. Deny needs no new sign-in. For this check the `meho-web` client
needs the `basic` and `acr` default scopes: the realm file and
`meho admin keycloak bootstrap-clients` set them.

**Why the console has no long login.** The console does not ask for
`offline_access`:

- The console session lives on the MEHO server, not in the browser.
- A long login would still work after the person logs out in the
  browser.
- When the login server remembers the browser, a new login is quick.

**On an existing realm, set the two values once.** The Keycloak that
ships with the MEHO chart reads the realm file only when it creates the
realm. Its setup job adds clients and groups, but it does not change
realm settings. So on a running realm, do one of these:

- In the Keycloak admin console, open **Realm settings → Sessions** and
  set **SSO Session Idle** to 1 hour and **SSO Session Max** to 10
  hours.
- Or use the MEHO Keycloak connector (`keycloak.realm.update`). It asks
  for an approval. Put the two values in a file and run:

  ```bash
  cat > realm-sessions.json <<'JSON'
  {"ssoSessionIdleTimeout": 3600, "ssoSessionMaxLifespan": 36000}
  JSON
  meho keycloak realm update --target <your-keycloak-target> -f realm-sessions.json
  ```

## Approve-only principal

By default, clearing the approval queue needs the **operator** role. For
separation of duties you can instead grant a dedicated approver the
**`approver`** capability — an optional claim, orthogonal to the tenant
role. A principal provisioned as `read_only` role **plus**
`approver=true` may approve and reject parked requests but is refused
`call_operation`: "approve, but never operate." (See
[Approvals and break-glass](../guides/approvals-and-break-glass.md#a-third-path-a-segregated-approver).)

The claim is fail-closed — absent or malformed means *not* an approver.
To make a person an approver, put them in the **`MEHO-Approvers`**
group ([Step 5](#step-5-create-the-five-groups)), and in no other of the
five groups. That group carries `tenant_role` = `read_only` and
`approver` = `true`, and the `approver` mapper from
[Step 3](#step-3-add-the-protocol-mappers) copies it into the
token. Its **Token Claim Name** must match the backplane's
`JWT_APPROVER_CLAIM_NAME` (default `approver`). The person keeps a low
tenant role (`read_only`), so the capability adds approval power
without adding dispatch power.

## Remember the browser, and passkeys

Two realm settings make a login rare for a person:

- **Remember the browser for 30 days.** The login page shows a
  **Remember me** box. When the person ticks it, Keycloak keeps the
  login in that browser for 30 days. A new `meho login`, a Claude Code
  login or a console login in that browser then asks no password and no
  code.
- **Passkeys.** A passkey is a login key kept on the device, for example
  Touch ID, Windows Hello or a security key. A login with a passkey asks
  no password and no one-time code. It is faster, and it does not work
  on a fake login page.

The Keycloak that ships with the MEHO chart has both settings in new
installs. On your own Keycloak, or on a built-in realm that already runs,
set them once (see [A realm that already runs](#a-realm-that-already-runs)).

| Where in the Keycloak admin console | Setting | Value |
|---|---|---|
| Realm settings → **Login** | Remember me | On |
| Realm settings → **Sessions** | SSO Session Idle Remember Me | 30 days |
| Realm settings → **Sessions** | SSO Session Max Remember Me | 30 days |
| Realm settings → **Login** → Login screen customization | Enable Passkeys | On |
| Authentication → Policies → **Webauthn Passwordless Policy** | User verification requirement | Required (the device asks for a PIN or a fingerprint) |
| Authentication → Policies → **Webauthn Passwordless Policy** | Discoverable credential | Required |

Keycloak 26.7 replaced the old setting **Require discoverable credential**
(Yes or No) with **Discoverable credential**. Leave the old one at
**Not specified**.

### What stays strict

- **Approvals still ask every time, on every surface.** The fresh
  sign-in for an approval
  (see [The approval client](#the-approval-client-meho-cli-approve))
  sends `prompt=login` and `max_age=0`. Keycloak then asks again, also in
  a remembered browser: password and code, or a passkey. MEHO also checks
  the sign-in time in the token. A token from a remembered browser keeps
  the time of the first sign-in, so a browser cookie alone cannot approve
  on the command line, through REST, or in the console.
- **Approvals in the console need the same recent sign-in.** The console
  checks the sign-in behind the console session: at most 15 minutes old,
  5 minutes for a `destructive` operation, and it must have asked for the
  password. When it is older, the request shows **Sign in to approve**,
  and that login asks for the password also in a remembered browser (see
  [The web console session](#the-web-console-session)). Deny needs no new
  sign-in.
- **The console keeps its own limit.** MEHO ends a console session after
  `UI_SESSION_ABSOLUTE_LIFETIME_SECONDS` (10 hours by default). The longer
  Keycloak login does not make it longer. After that limit the console
  sends the person to the login page. In a remembered browser, that login
  asks nothing, and the new session can read and deny, but not approve
  until the person signs in again with the password.

### The risk, and how to end a login

Whoever uses the unlocked computer has the person's login for up to 30
days. So lock the computer when you leave it. To end a remembered login:

- **The person** logs out in that browser: **Log out** in the MEHO
  console, or **Sign out** on the Keycloak account page.
- **An admin** ends all sessions of the person: in the Keycloak admin
  console, open the person under **Users**, tab **Sessions**, and sign
  out each session. Or through MEHO with `keycloak.user.logout`
  (`meho keycloak user logout --username <name>`; it waits for approval).

The next login then asks for the password and the code again.

### Add a passkey

A person adds a passkey on the Keycloak account page
(`<keycloak-url>/realms/meho/account`), under **Account security →
Signing in**. On the next login, the person
clicks **Sign in with Passkey**, or picks the passkey that the browser
offers in the user name field. When a passkey was used, Keycloak skips the
one-time code.

A passkey also works for the fresh sign-in of an approval: the password
page shows **Sign in with Passkey** too. If you set
`APPROVAL_ONE_TIME_CODE_ACR_VALUES`, MEHO records a passkey approval as a
normal fresh sign-in, because the code step did not run.

**Lost passkey.** The person logs in with password and code, as before.
Or an admin removes the passkey in the Keycloak admin console (Users → the
person → **Credentials** → delete the passkey), and the person adds a new
one.

### A realm that already runs

Keycloak reads the chart's realm file only at its first start. The
chart's realm finishing job cannot change realm settings either: its
account `meho-admin` may only manage clients and users. So a realm that
already runs gets these settings once, in one of two ways:

- **Through MEHO**, with `keycloak.realm.update` (it waits for
  approval). Put the values in a file, for example `realm-login.json`:

    ```json
    {
      "rememberMe": true,
      "ssoSessionIdleTimeoutRememberMe": 2592000,
      "ssoSessionMaxLifespanRememberMe": 2592000,
      "webAuthnPolicyPasswordlessPasskeysEnabled": true,
      "webAuthnPolicyPasswordlessUserVerificationRequirement": "required",
      "webAuthnPolicyPasswordlessResidentKey": "required"
    }
    ```

    Then run `meho keycloak realm update --target <keycloak-target>
    --realm meho -f realm-login.json`. MEHO reads the realm, changes only
    these fields, and writes it back.

- **By hand**, in the Keycloak admin console, with the table above.

**A realm made before Keycloak 26.4.** Keycloak 26.4 added a step to the
browser flow of new realms: **Condition - credential**. It skips the
one-time code after a passkey login. An upgrade does not add it to an older
realm. The built-in Keycloak of the chart started with a newer version, so
its realm has the step. Without the step, a passkey replaces the password,
but a person with a one-time code still types the code.

MEHO adds the step with one operation. A second person must approve it,
because it changes how every person of the realm logs in:

```bash
meho operation call keycloak-admin-26.x keycloak.authflow.ensure_passkey_code_skip \
  --target <keycloak-target>
```

- **The step is there** (a realm made on 26.4 or newer): MEHO changes
  nothing. The result says `changed: false`. So the call is safe to run
  twice.
- **The realm uses the built-in `browser` flow without the step:** MEHO
  copies `browser` as `browser with passkeys`, adds **Condition -
  credential** to the copy (the settings in step 3 below), and makes the
  copy the realm's browser flow. Then it reads the flow and the realm back.
  `browser_flow` in the result is the flow the realm now uses.
- **A copy `browser with passkeys` is there from a run that stopped
  half-way:** MEHO finishes it, when its other steps match `browser`,
  settings included. Otherwise it stops, changes nothing more, and names
  the difference.
  Delete or rename that copy, then run again.
- **The realm uses its own flow**, or the flow has no conditional one-time
  code sub-flow: MEHO stops and changes nothing. Do the steps below by
  hand.

The Keycloak server must run 26.4 or newer.

**The way back:** make `browser` the realm's browser flow again:

```bash
meho operation call keycloak-admin-26.x keycloak.realm.update \
  --target <keycloak-target> --params '{"representation": {"browserFlow": "browser"}}'
```

The copy then has no effect, and you can delete it.

To do it by hand in the Keycloak admin console:

1. **Check.** Open **Authentication → Flows** and the flow that is bound
   as the browser flow (usually `browser`). In the sub-flow whose name
   starts with **Browser - Conditional**, look for **Condition -
   credential**. When it is there, you are done.
2. **Copy the flow.** Keycloak does not let you add a step to its built-in
   `browser` flow. So open `browser`, choose **Duplicate**, and give the
   copy a name, for example `browser with passkeys`. When the realm already
   uses its own copy of the flow, change that copy and skip this step.
3. **Add the step.** In the copy's **Browser - Conditional ...** sub-flow,
   add the condition **Condition - credential**. Set it to **Required**
   and move it right after **Condition - user configured**. Open its
   settings (the **Settings** icon) and set:
    - **Credentials**: `webauthn-passwordless`
    - **Included**: off

    With **Included** off, the condition is true only when no passkey was
    used. So the one-time code runs after a password login, and not after
    a passkey login.

4. **Use the copy.** In the copy's menu, choose **Bind flow** and pick
   **Browser Flow**.
5. **Test.** Log in with a passkey: Keycloak asks no one-time code. Log in
   with the password: Keycloak still asks for the code.

By hand, the way back is the same: bind `browser` as the **Browser Flow**
again.

## Wire it into Helm

Tell the chart which client the CLI should use (already shown in
[the install trail, Step 6](index.md#step-6-write-your-values-file)):

```yaml
config:
  keycloakCliClientId: meho-cli
  # The default; set it only when your approval client has another ID.
  keycloakCliApproveClientId: meho-cli-approve
```

The backplane serves both at `/api/v1/auth-config`, which is how
`meho login` and `meho approvals approve` discover them. `meho login --client-id <id>` overrides it
per invocation.

## Verify

```bash
# 1. The realm's OIDC discovery document is reachable and names your issuer.
curl -fsS "https://keycloak.example.com/realms/<realm>/.well-known/openid-configuration" | jq .issuer

# 2. The backplane advertises the CLI client.
curl -fsS https://meho.example.com/api/v1/auth-config | jq .
# → {"keycloak_issuer": "…", "audience": "meho-backplane", "cli_client_id": "meho-cli",
#    "cli_approve_client_id": "meho-cli-approve"}

# 3. Device-code login completes end to end.
meho login https://meho.example.com

# 4. The stored token is accepted end to end.
meho status
```

To check the **claim shape** — `aud` contains both `meho-backplane` and
`https://<backplane-host>/mcp`, and `sub`, `tenant_id`, `tenant_role`,
`groups` are all present — you need the raw token, and the CLI has no
verb that prints it. `meho login` stores it in the OS keyring
(Keychain, Secret Service, Wincred), falling back to a 0600-mode file
at `$XDG_CONFIG_HOME/meho/credentials.json` on headless hosts. Force
that file backend with `MEHO_KEYRING_DISABLE=1` when you need to read
the token yourself:

```bash
MEHO_KEYRING_DISABLE=1 meho login https://meho.example.com

# The file holds one entry per backplane; a fresh host has exactly one.
# JWT payloads are unpadded base64url, so translate the alphabet and
# re-pad before decoding.
jq -r '.entries[].access_token' \
  "${XDG_CONFIG_HOME:-$HOME/.config}/meho/credentials.json" \
  | cut -d. -f2 | tr '_-' '/+' \
  | awk '{ n = length % 4; if (n) $0 = $0 substr("===", 1, 4 - n); print }' \
  | base64 -d | jq .
```

When all four pass, return to
[the install trail, Step 5](index.md#step-5-decide-ingress-tls-and-trust).

## If login fails

The first-login failures are finite and well-mapped. In the order they
usually appear:

| Symptom | Cause | Fix |
|---|---|---|
| `unauthorized_client` at login start | The client is confidential, or the device grant is off | Step 2 — public client, device grant **On**. |
| `meho login` fails at once with `invalid_request` and "Missing parameter: code_challenge_method" | The login client (`meho-cli`) requires a PKCE method, and the CLI is older than 0.44.0. Those versions send no PKCE at the device login, and Keycloak checks PKCE there too. A realm made with MEHO 0.40.0, 0.41.0 or 0.42.0 (the built-in Keycloak, or `meho admin keycloak bootstrap-clients`) set `S256` on it. | Update the CLI to 0.44.0 or later. Or clear the PKCE method on the login client: in the admin console (Advanced → Proof Key for Code Exchange, set it to empty), or with the operation `keycloak.client.update` and the representation `{"attributes": {"pkce.code.challenge.method": ""}}` (an empty value removes the attribute). Step 2. |
| Token issued, every call 401s with `invalid_audience` / `missing_tenant_claim` / `missing_tenant_role_claim` | Missing protocol mappers, or the person is in none of the five groups | Step 3 — add all the mappers; Step 5 — put the person in one group. Then log out and back in. |
| Everyone gets the same role, whatever their group | A fixed-value (Hardcoded claim) `tenant_role` mapper is still on the client | Delete it, and add the User Attribute mapper of Step 3 in the admin console. On an existing realm, do not run `meho admin keycloak bootstrap-clients` for this: it rewrites all the mappers and clients. |
| Token issued, every call 401s with `invalid_token` or `missing_sub`; the decoded token has no `sub` | The `basic` scope is not a default scope on the client | Step 4 — assign it, re-login (existing tokens stay broken). |
| `meho approvals approve` fails with `fresh_sign_in_required` right after you signed in again | The access token has no `auth_time` or no `acr` (the `basic` or `acr` scope is missing), or it was not made for the approval client (the chart's `config.keycloakCliApproveClientId` does not name the client), or the client does not use its own login flow | Step 4 — assign `basic` and `acr` to `meho-cli-approve`; check the client ID and its login flow in Step 2. Or approve in the web interface. |
| The console shows **Sign in to approve** right after you signed in with your password | The access token of `meho-web` has no `auth_time` or no `acr` (the `basic` or `acr` scope is missing on `meho-web`), or `FRESH_SIGN_IN_MAX_AGE_SECONDS` is very short | Step 4 — assign `basic` and `acr` to `meho-web` as default scopes, then sign in again. |
| The browser shows "Client not found" or "Invalid parameter: redirect_uri" during `meho approvals approve` | `meho-cli-approve` is missing, or `http://127.0.0.1:*` is not one of its redirect URIs | Step 2 — create it, or add the redirect URI with `keycloak.client.update`. Use `meho admin keycloak bootstrap-clients` only on a new realm. |
| `meho login` times out before you can approve | An ambient wrapper timeout (CI step, IDE task) shorter than the approval wait | Run `meho login` in a real terminal, or raise the wrapper timeout. |
| MCP login fails with `invalid_scope` ("Invalid scopes: …") and the Keycloak login page never appears | The client asked for a scope that `meho-mcp` does not offer: `offline_access`, `mcp:read`, `mcp:execute` or `mcp:admin` | Create the scope if it is missing (for example with `keycloak.client_scope.create`), then assign it as an **optional** scope on `meho-mcp` (see the MCP client table above). On a new realm, `meho admin keycloak bootstrap-clients` does both. |
| `x509: certificate signed by unknown authority` | Workstation OS trust store missing your CA | Step 1 / [TLS and ingress](tls-ingress.md#your-workstation-os-trust-store). |

A fuller, client-by-client troubleshooting page ships with the
[Connect clients](../clients/index.md) section. Until then, the
in-repo
[auth onramp recipe](https://github.com/evoila/meho/blob/main/deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp)
carries the deepest version of this matrix, including advanced
realm shapes (per-audience capability claims, metadata-document
clients).
