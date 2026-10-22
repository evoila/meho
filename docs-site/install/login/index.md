# Company login

People log in to MEHO with their company account. Their group decides
their tenant and their role.

MEHO always ships with a built-in Keycloak. Keycloak checks who the
person is. MEHO reads the tenant and the role from the login token.
The Helm chart makes the realm `meho` at install, with its clients,
groups and mappers (see [Keycloak realm setup](../keycloak-realm.md)
for what is in it). The recipes on these pages start from there: they
connect your company accounts to that realm.

## The groups

A person gets their tenant and role from **one** of these groups. Each
group carries the values as group attributes. A User Attribute mapper
on each login client copies them into the token.

| Group | Role in MEHO | What the person can do |
|---|---|---|
| `MEHO-Readers` | `read_only` | Read only. |
| `MEHO-Operators` | `operator` | Run actions. Risky changes wait for a second person. |
| `MEHO-SA-Operators` | `operator` | Same as Operators for now. Self-approval comes later ([#3325](https://github.com/evoila/meho/issues/3325)). |
| `MEHO-Admins` | `tenant_admin` | Manage the tenant. |
| `MEHO-Approvers` | `read_only` + `approver` | Approve other people's changes. Cannot run actions. |

`MEHO-Platform-Admins` is separate. It adds `platform_admin` to the
token and gives no tenant or role. Only the first admin needs it.

The full decision, with the reasons, is in the
[decision record](https://github.com/evoila/meho/blob/main/docs/decisions/login-role-groups.md).

## Pick a recipe

| Where your people are | Recipe |
|---|---|
| Nowhere yet, or a small team | [Local users](local-users.md): create them in Keycloak. |
| Active Directory or another LDAP directory | [AD / LDAP](ad-ldap.md): Keycloak reads the directory. |
| Microsoft Entra ID | [Entra ID](entra-id.md): Keycloak sends people to Entra. |
| authentik | [authentik](authentik.md): same pattern as Entra ID. |
| Your own Keycloak | [Your own Keycloak](customer-keycloak.md): it stays in front of MEHO's Keycloak. |

Okta or ADFS work like Entra ID: an OpenID Connect identity provider
plus one group mapper per MEHO group. We have not tested them. For
another Keycloak, see [Your own Keycloak](customer-keycloak.md).

## The rules

1. **One group per person.** A person is in exactly one of the five
   groups. In two groups, MEHO refuses the login with
   `401 multiple_role_groups`. In none, MEHO refuses it with
   `401 missing_tenant_claim`.
2. **Never set `tenant_id`, `tenant_role` or `approver` on a person.**
   A value on the person wins over the group. Keep the realm's
   **Unmanaged Attributes** at **Disabled**, **Admin can view** or
   **Admin can edit**. With **Enabled**, people can set their own role.
3. **A directory must not write these attribute names.** No LDAP
   attribute mapper and no identity provider mapper may write
   `tenant_id`, `tenant_role`, `approver` or `platform_admin` on a
   person. Map directory groups to the MEHO groups instead.
4. **A role change applies at the next login.** Move the person to
   another group. Tokens already issued keep the old role until they
   expire.
