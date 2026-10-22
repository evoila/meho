# Active Directory / LDAP

Use this when your people are in Active Directory or another LDAP
directory. Keycloak reads the people and their groups from the
directory. Passwords stay in the directory.

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
- You have a read-only bind account in the directory.
- Keycloak can reach the directory (LDAPS on port 636, or LDAP with
  StartTLS). If the directory uses an internal CA, Keycloak must trust
  it.

## 1. Pick the directory groups

You need one directory group per MEHO role. Pick one of two ways:

- **A. Same names (simplest).** Make groups named exactly like the MEHO
  groups: `MEHO-Readers`, `MEHO-Operators`, `MEHO-SA-Operators`,
  `MEHO-Admins`, `MEHO-Approvers`. Keycloak links each one to the MEHO
  group of the same name.
- **B. Your own names.** Use groups you already have or name by your
  rules, for example `SG-MEHO-Ops`. Keycloak puts each one under its
  MEHO group (`MEHO-Operators/SG-MEHO-Ops`), and the person gets the
  tenant and role of that MEHO group.

Put the groups in one place, for example
`OU=MEHO,OU=Groups,DC=example,DC=com`. Put each person in **one** of
them.

## 2. Connect the directory

In the Keycloak admin console, realm `meho`: **User federation → Add
LDAP providers** (or **Add new provider → LDAP**).

| Section | Setting | Value |
|---|---|---|
| General options | **Vendor** | `Active Directory` (or your LDAP vendor) |
| Connection and authentication | **Connection URL** | `ldaps://dc1.example.com:636` |
| | **Enable StartTLS** | On only with `ldap://` |
| | **Bind type** | `simple` |
| | **Bind DN** | the read-only bind account |
| | **Bind credentials** | its password |
| LDAP searching and updating | **Edit mode** | `READ_ONLY` |
| | **Users DN** | where your people are, for example `OU=People,DC=example,DC=com` |
| | **User LDAP filter** | optional, to limit who is read |
| | **Search scope** | `Subtree` if people sit in sub-OUs |
| Synchronization settings | **Import users** | On |
| | **Periodic changed users sync** | On, for example every 3600 seconds |
| Cache settings | **Cache policy** | `NO_CACHE` (or `MAX_LIFESPAN` with a few minutes) |

**Cache policy** decides when a removal from a directory group takes
effect. With the default cache, Keycloak keeps the old group of a
person until its cache entry ends, so a removed person keeps MEHO
access. With `NO_CACHE`, Keycloak reads the groups from the directory
at every login and token refresh.

Use **Test connection** and **Test authentication** before you save.
Then click **Save**. With **Edit mode** `READ_ONLY`, Keycloak never
writes to the directory, and people change their password in the
directory, not in Keycloak.

## 3. Bring the directory groups in

Open the provider → **Mappers** → **Add mapper**, with mapper type
`group-ldap-mapper`. These settings are the same for both ways:

| Setting | Value |
|---|---|
| **LDAP Groups DN** | the place from step 1, for example `OU=MEHO,OU=Groups,DC=example,DC=com` |
| **Group Name LDAP Attribute** | `cn` |
| **Group Object Classes** | `group` (AD) or `groupOfNames` |
| **Preserve Group Inheritance** | Off |
| **Membership LDAP Attribute** | `member` |
| **Membership Attribute Type** | `DN` |
| **Mode** | `READ_ONLY` |
| **User Groups Retrieve Strategy** | `LOAD_GROUPS_BY_MEMBER_ATTRIBUTE` (for AD nested groups: `LOAD_GROUPS_BY_MEMBER_ATTRIBUTE_RECURSIVELY`) |
| **Mapped Group Attributes** | empty |
| **Drop non-existing groups during sync** | **Off** |

**A. Same names:** one mapper.

| Setting | Value |
|---|---|
| **Name** | `meho-groups` |
| **LDAP Filter** | the filter below |
| **Groups Path** | `/` |

```text
(|(cn=MEHO-Readers)(cn=MEHO-Operators)(cn=MEHO-SA-Operators)(cn=MEHO-Admins)(cn=MEHO-Approvers))
```

**B. Your own names:** one mapper per MEHO group, each bringing in
exactly one directory group under its MEHO group.

| Name | LDAP Filter | Groups Path |
|---|---|---|
| `meho-readers` | `(cn=SG-MEHO-Read)` | `/MEHO-Readers` |
| `meho-operators` | `(cn=SG-MEHO-Ops)` | `/MEHO-Operators` |
| `meho-sa-operators` | `(cn=SG-MEHO-SAOps)` | `/MEHO-SA-Operators` |
| `meho-admins` | `(cn=SG-MEHO-Admin)` | `/MEHO-Admins` |
| `meho-approvers` | `(cn=SG-MEHO-Approve)` | `/MEHO-Approvers` |

Save, then click **Sync LDAP groups to Keycloak** on each mapper.

What you see after the sync, in **Groups**:

- **A:** each `MEHO-*` group once, at the top level, still with its
  attributes. The sync reports the five as *updated*, not *imported*.
- **B:** each directory group as the only child of its MEHO group, for
  example `MEHO-Operators` → `SG-MEHO-Ops`. Nothing new at the top level.

Keep **Mapped Group Attributes** empty, so the sync never writes the
group attributes. Keep **Drop non-existing groups during sync** off, so
the sync never removes a group the directory does not know, such as
`MEHO-Platform-Admins`. The **LDAP Filter** keeps all other directory
groups out of Keycloak.

!!! warning "Way B: tell MEHO your group names"

    MEHO refuses a person in two role groups (`401
    multiple_role_groups`). It finds them by name in the token's
    `groups` claim, and with way B that claim holds your names (for
    example `SG-MEHO-Ops`), not the MEHO names. Set the backplane
    setting `JWT_ROLE_GROUPS` to the five MEHO names **and** your five
    names. Without it, a person in two of your groups gets one of the
    two roles, and you cannot tell which.

    The chart has no named value for this setting yet. Set it with
    `extraEnv` in your values file, comma-separated, no spaces:

    ```yaml
    extraEnv:
      - name: JWT_ROLE_GROUPS
        value: "MEHO-Readers,MEHO-Operators,MEHO-SA-Operators,MEHO-Admins,MEHO-Approvers,SG-MEHO-Read,SG-MEHO-Ops,SG-MEHO-SAOps,SG-MEHO-Admin,SG-MEHO-Approve"
    ```

    Way A needs nothing here: the default is the five MEHO names.

## 4. Never map role attributes

The provider comes with a few attribute mappers (username, email, first
name, last name). Leave them. Never add a `user-attribute-ldap-mapper`
whose **User Model Attribute** is `tenant_id`, `tenant_role`,
`approver` or `platform_admin`. A value on the person wins over the
group, so a directory field would decide the role. The group decides
it.

## 5. Add a second login step

With LDAP, Keycloak checks the password itself. Entra's phone check
(or any other check in your company login) does not run here. So add a
second step in Keycloak: a code from an authenticator app.

1. **Authentication → Flows** → click the **browser** flow → **Action →
   Duplicate**. Name it `browser-otp`.
2. In the copy, find the sub-flow `browser-otp forms`. Inside it is a
   conditional sub-flow with the **OTP Form** step (named
   `... Conditional OTP` or, in newer 26.x versions,
   `... Conditional 2FA`).
3. Set that conditional sub-flow to **Required**, and its **OTP Form**
   step to **Required**. If it also has a **Condition - user
   configured** step, set that step to **Disabled**.
4. **Action → Bind flow** → **Browser flow** → **Save**.

Check **Authentication → Required actions**: **Configure OTP** must be
on (it is by default). At their first login, each person scans a QR
code with an authenticator app (for example Microsoft Authenticator or
FreeOTP). After that, every login asks for the code.

The same flow runs for `meho login`, the web UI and Claude (MCP): all
three log in through the browser.

## Lab test

Use a test person from the directory who is not an admin.

- [ ] `meho login https://<meho-host>` works with the directory
      account. `meho status` shows the right tenant and role.
- [ ] The web UI (`https://<meho-host>/ui/`) logs in with the same
      account and shows the same role.
- [ ] Claude (MCP) logs in through the browser and can call a MEHO
      tool that fits the role.
- [ ] The first login asks to set up the authenticator app. Every
      later login asks for the code.
- [ ] **Groups** shows each `MEHO-*` group once, at the top level,
      still with its attributes (way B: your groups under them).
- [ ] Remove the person from the directory group. The next login, and
      the next token refresh, are refused with
      `401 missing_tenant_claim`.
- [ ] Way B: a person in two of your groups is refused with
      `401 multiple_role_groups`.

An automated test covers the Keycloak part of both ways against
OpenLDAP: `backend/tests/integration/test_keycloak_ldap_groups_e2e.py`.
It does not replace the lab test with your directory.

Followed by a colleague who did not write this recipe: name, date,
result.
