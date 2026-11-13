# Move the login setup to another install

Use this page when you move MEHO to a new install, for example to
another install form or to a new machine. It moves the login setup
that lives in Keycloak:

- the identity providers (for example Entra ID),
- the directory connection (Active Directory or LDAP),
- the groups, with their attributes,
- who is in which group.

[The settings file](../guides/settings-file.md) does not carry any of
this. You move it with Keycloak's own partial export and partial
import, and do a few steps by hand. MEHO has no command for it.

!!! warning "What Keycloak's partial export leaves out"

    - **Secrets.** Keycloak replaces every secret with `*` characters:
      the client secret of an identity provider and the bind password
      of a directory. You type them again on the new install.
    - **People.** The export has no users, so it has no group members.
    - **The directory connection.** The export file has it, but the
      partial import does not read it. You add it again by hand.

    Keycloak itself says the admin console export is not a backup, and
    not meant to move a whole realm. Here you use it only for the
    groups and the identity providers. You add the rest by hand.

## Before you start

- **The old install still runs.** You read from it in step 1 and
  step 6.
- **A Keycloak admin login** on both installs. See
  [The console admin](keycloak-realm.md#the-console-admin), or
  [Keycloak break-glass](keycloak-break-glass.md) when nobody can log
  in.
- **The same tenant values in the new install.** Give the new install
  the same Helm values as the old one for
  `coreKeycloak.firstTenantId`, `coreKeycloak.tenantName` and
  `coreKeycloak.tenants`. Then the new install makes the same role
  groups, with the same tenant ids. The tenant ids must also match
  the ids in your settings file.
- **The company login values.** When the old install sets up its
  company login with Helm values (`coreKeycloak.companyLogin`: Entra
  ID, authentik, your own Keycloak), copy these values to the new
  install. Make the Secret with the client secret on the new install
  too. The new install then sets up this login by itself. When the
  new install has another address, add its redirect URI in your login
  system (see the recipe in [Company login](login/index.md)).
- **A Keycloak target in MEHO** on the new install, for step 6. See
  [Add a tenant](../guides/tenant-bootstrap.md#before-you-start).

## 1. Export from the old install

In the Keycloak admin console of the **old** install, open realm
`meho`:

1. Click **Realm settings**.
2. Open the **Action** menu (top right) and pick **Partial export**.
3. Turn on **Include groups and roles**. Leave **Include clients**
   off: the new install makes its own clients.
4. Click **Export**. Keycloak saves the file `realm-export.json`.

The file has no secrets, but it shows how your login is built. Keep it
in a safe place, and delete it when you are done.

## 2. Import into the new install

In the Keycloak admin console of the **new** install, open realm
`meho`:

1. Click **Realm settings**.
2. Open the **Action** menu and pick **Partial import**.
3. Pick the file `realm-export.json`.
4. Pick only **Groups** and **Identity providers**. Do not pick
   clients or roles: the new install made its own, and they must stay
   as they are.
5. For a resource that already exists, pick **Skip**. Then the groups and
   the login the new install made stay as they are. Keycloak adds only
   what is missing.
6. Click **Import**.

Check in **Groups** that every group of the old install is there,
with its attributes.

## 3. Add the directory connection again

Only when your people come from Active Directory or LDAP.

Follow the recipe [Active Directory / LDAP](login/ad-ldap.md) on the
new install:

- step 2, "Connect the directory",
- step 3, "Bring the directory groups in".

Use the same values as on the old install. You can read them in the
old Keycloak admin console under **User federation**, or in the export
file. The bind password is not in the file: type it in step 4.

## 4. Type the secrets again

The export has `*` characters in place of each secret. Type each one
again on the new install, before anyone logs in:

- **Each identity provider you imported** in step 2: in **Identity
  providers**, open it and type the **Client Secret** again. An
  identity provider that the Helm values set up needs nothing here.
- **The directory connection** from step 3: in **User federation**,
  open it and type the **Bind credentials** again. Click **Test
  authentication**.

## 5. Check the logins

Log in once with a person from each login system: a person from the
directory, a person from each identity provider. A person who comes
from the directory or from an identity provider gets their group from
there. Check that each one gets the right tenant and role.

## 6. Add the other group members

Only the people who do not get their group from a directory or an
identity provider. For example, people you made by hand in Keycloak.

1. On the **old** install, list the members of each group:

    ```bash
    meho keycloak group list --target <keycloak-target> --json
    meho keycloak group member list --target <keycloak-target> --id <group-id>
    ```

2. On the **new** install, each person must exist first. Make them as
   in [Add a tenant](../guides/tenant-bootstrap.md).
3. On the **new** install, add each person to their group:

    ```bash
    meho keycloak group member add --target <keycloak-target> \
      --username <username> --group-name <group-name>
    ```

Each add waits until a person approves it. A group decides a person's
tenant and role, so MEHO never adds a member without approval. The box
at the top of [Add a tenant](../guides/tenant-bootstrap.md) says who
may approve.

Put each person in exactly one role group (see
[the rules](login/index.md#the-rules)).

## What else moves, and how

- **MEHO's own settings** (policies, conventions, targets): with
  [the settings file](../guides/settings-file.md).
- **Passwords of targets and keys**: with the backup of your password
  safe. See [Back up, restore and update](backup-restore-update.md).
