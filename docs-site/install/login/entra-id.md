# Microsoft Entra ID

Use this when your people log in with Microsoft Entra ID. Keycloak
sends them to Entra. Entra checks who they are and sends back their
groups. Keycloak puts each person into the matching MEHO group.

You set this up with Helm values (`coreKeycloak.companyLogin`), not by
hand in the Keycloak admin console. The install sets it up again at
every `helm upgrade`. The page
[Company login with authentik](../company-login-authentik.md) explains
the same values in more detail.

**Before you start:**

- The realm is set up and the five MEHO groups exist. See
  [Company login](index.md).
- You can create an app registration in Entra.
- You know the address of your MEHO, for example
  `https://meho.example.com`. This page writes it as `<meho-host>`.

The Helm values use the alias `company` by default. The alias is part
of the redirect URI:

```text
https://<meho-host>/auth/realms/meho/broker/company/endpoint
```

## 1. Make the Entra groups

In Entra, make one security group per MEHO group, for example
`MEHO-Operators`. Put each person in **one** of them. Write down the
**Object ID** of each group: the token carries the ID, not the name.

## 2. Register Keycloak in Entra

In the Entra admin center: **App registrations → New registration**.

| Setting | Value |
|---|---|
| **Name** | `MEHO Keycloak` |
| **Supported account types** | Accounts in this organizational directory only |
| **Redirect URI** | Platform **Web**, `https://<meho-host>/auth/realms/meho/broker/company/endpoint` |

Then, on the new app:

1. **Certificates & secrets → New client secret.** Copy the value.
2. **Token configuration → Add groups claim.** Pick **Security
   groups**. For the **ID** token, keep **Group ID**. The token then
   carries `groups`: a list of group object IDs.
3. From **Overview**, copy the **Application (client) ID** and the
   **Directory (tenant) ID**.

If people are in many groups (more than 200), Entra leaves the
`groups` claim out. Then pick **Groups assigned to the application** in
step 2, and assign the MEHO groups to the app under **Enterprise
applications → MEHO Keycloak → Users and groups**.

## 3. Store the client secret

Put the Entra client secret into a Kubernetes Secret in MEHO's
namespace. The chart reads it by name and never prints it.

```bash
read -rs CLIENT_SECRET     # paste the Entra client secret; it is not shown
kubectl create secret generic meho-company-login -n meho \
  --from-literal=client_secret="$CLIENT_SECRET"
unset CLIENT_SECRET
```

## 4. Add the Helm values

Add this to your values file, next to your other `coreKeycloak`
values:

```yaml
coreKeycloak:
  companyLogin:
    enabled: true
    alias: company
    displayName: Company account
    discoveryUrl: https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration
    clientId: "<the Application (client) ID>"
    clientSecret:
      secretName: meho-company-login
      secretKey: client_secret
    scopes: openid profile email
    groupsClaim: groups
    groupMap:                         # Entra group Object ID -> MEHO group
      "<object-id-of-readers>": MEHO-Readers
      "<object-id-of-operators>": MEHO-Operators
      "<object-id-of-sa-operators>": MEHO-SA-Operators
      "<object-id-of-admins>": MEHO-Admins
      "<object-id-of-approvers>": MEHO-Approvers
    autoRedirect: false               # true later: go straight to Entra
```

- `groupMap`: the left side is the Entra group's **Object ID**, in
  quotes. The right side is the MEHO group. **Only the groups you list
  here give access.**
- `autoRedirect`: with `true`, Keycloak sends everyone straight to
  Entra and never shows its own password form. Start with `false`, so
  the first admin can still log in with a password. Switch to `true`
  once a person from Entra is in `MEHO-Admins`.

Then run `helm upgrade` as in
[Install or upgrade](../company-login-authentik.md#3-install-or-upgrade).
The install notes print the redirect URI. Check that it is the same as
the one in Entra. If you turn the company login on for an install that
already exists, read the note in that section first.

Keycloak checks the groups at every login. Remove a person from the
Entra group, and Keycloak removes them from the MEHO group at their
next login.

## 5. Keep Entra's checks

Entra's own checks stay in place: Conditional Access, MFA, blocked
sign-ins. They run when Keycloak sends the person to Entra. You do not
need a second step in Keycloak.

## Lab test

Use a test person from Entra who is not an admin.

- [ ] `meho login https://<meho-host>` works with the Entra account.
      `meho status` shows the right tenant and role.
- [ ] The web UI (`https://<meho-host>/ui/`) logs in with the same
      account and shows the same role.
- [ ] Claude (MCP) logs in through the browser and can call a MEHO
      tool that fits the role.
- [ ] Entra's MFA or Conditional Access ran during the login.
- [ ] Remove the person from the Entra group. The next login is
      refused with `401 missing_tenant_claim`.

Followed by a colleague who did not write this recipe: name, date,
result.
