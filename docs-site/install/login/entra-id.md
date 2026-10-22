# Microsoft Entra ID

Use this when your people log in with Microsoft Entra ID. Keycloak
sends them to Entra. Entra checks who they are and sends back their
groups. Keycloak puts each person into the matching MEHO group.

**Before you start:**

- The realm is set up and the five MEHO groups exist. See
  [Company login](index.md).
- You can log in to the Keycloak admin console as the console admin
  (a user of realm `master`, default name `console-admin`; name and
  temporary password in the Secret `<release>-core-console-admin`) at
  `https://<meho-host>/auth/admin/master/console/#/meho`, then pick
  realm `meho`. The console needs
  `coreKeycloak.ingress.exposeAdmin: true`. See
  [The console admin](../keycloak-realm.md#the-console-admin).
- You can create an app registration in Entra.
- You know the address of MEHO's Keycloak, for example
  `https://keycloak.example.com`.

Pick an alias for the identity provider now, for example `entra`. It
goes into the redirect URI:

```text
https://<keycloak>/realms/meho/broker/<alias>/endpoint
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
| **Redirect URI** | Platform **Web**, `https://<keycloak>/realms/meho/broker/<alias>/endpoint` |

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

## 3. Add Entra to Keycloak

In the Keycloak admin console, realm `meho`: **Identity providers →
OpenID Connect v1.0**.

| Setting | Value |
|---|---|
| **Alias** | the alias you picked, for example `entra` |
| **Display name** | `Company account` |
| **Discovery endpoint** | `https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration` |
| **Client authentication** | Client secret sent as post |
| **Client ID** | the Application (client) ID |
| **Client Secret** | the secret from step 2 |

Click **Add**. Then, on the provider:

- **Scopes**: `openid profile email`.
- **Sync mode**: `Force`.
- **Trust Email**: On.

Check that **Redirect URI** at the top of the page matches the one in
Entra.

## 4. Map each Entra group to a MEHO group

On the provider → **Mappers** → **Add mapper**. Make one mapper per
MEHO group:

| Setting | Value |
|---|---|
| **Name** | `MEHO-Operators` (the MEHO group) |
| **Sync mode override** | `Force` |
| **Mapper type** | `Advanced Claim to Group` |
| **Claims** | Key `groups`, Value the Entra group's Object ID |
| **Regex Claim Values** | Off |
| **Group** | `/MEHO-Operators` |

With **Force**, Keycloak checks the groups at every login. Remove a
person from the Entra group, and Keycloak removes them from the MEHO
group at their next login.

Never add a mapper that writes `tenant_id`, `tenant_role`, `approver`
or `platform_admin` on the person. The MEHO group gives those.

## 5. Keep Entra's checks

Entra's own checks stay in place: Conditional Access, MFA, blocked
sign-ins. They run when Keycloak sends the person to Entra. You do not
need a second step in Keycloak.

## 6. Optional: hide the Keycloak password form

By default the Keycloak login page shows a username and password form
and a **Company account** button. To send people straight to Entra:

1. **Authentication → Flows** → **browser** → **Action → Duplicate**.
2. In the copy, open the settings (gear) of **Identity Provider
   Redirector**. Set **Default Identity Provider** to your alias.
3. **Action → Bind flow** → **Browser flow**.

Do this only after a person from Entra is in `MEHO-Admins`. Local
Keycloak users, such as the first admin, then no longer see the
password form.

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
