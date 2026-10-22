# Local users

Use this when your people are not in a directory yet, or when the team
is small. You create each person in Keycloak by hand.

**Before you start:** MEHO's built-in Keycloak made the realm `meho`
at install, so the five MEHO groups exist. See
[Company login](index.md). Log in to the admin console of realm `meho`
as the Keycloak admin (`keycloak-admin`, temporary password in the
Secret `<release>-core-keycloak-console`) at
`https://<meho-host>/auth/admin/meho/console/`. The console needs
`coreKeycloak.ingress.exposeAdmin: true`. See
[The Keycloak admin](../keycloak-realm.md#the-keycloak-admin).

## Create a person

In the Keycloak admin console, pick the realm `meho`.

1. Go to **Users → Add user**.
2. Fill in **Username**, **Email**, **First name** and **Last name**.
   The realm's user profile asks for all of them.
3. Click **Join groups** and pick **one** of the five MEHO groups. Only
   one.
4. Click **Create**.

## Give a first password

1. Open the person → **Credentials** → **Set password**.
2. Type a password. Leave **Temporary** on.
3. Click **Save**, then send the password to the person on a safe
   channel.

With **Temporary** on, Keycloak asks the person for a new password at
their first login.

Do not touch the **Attributes** tab. `tenant_id`, `tenant_role` and
`approver` stay empty on the person. The group gives them.

## Change a role or remove a person

- **New role:** open the person → **Groups** → **Leave** the old group,
  then **Join Group** for the new one. The change applies at the next
  login.
- **No more access:** remove the person from their MEHO group, or turn
  off **Enabled** on the person. The next login is refused.

## Lab test

Use a test person who is not an admin.

- [ ] `meho login https://<meho-host>` works with the Keycloak account.
      `meho status` shows the right tenant and role.
- [ ] The web UI (`https://<meho-host>/ui/`) logs in with the same
      account and shows the same role.
- [ ] Claude (MCP) logs in through the browser and can call a MEHO
      tool that fits the role.
- [ ] Keycloak asked for a new password at the first login.
- [ ] Remove the person from the group. The next login is refused with
      `401 missing_tenant_claim`.

Followed by a colleague who did not write this recipe: name, date,
result.
