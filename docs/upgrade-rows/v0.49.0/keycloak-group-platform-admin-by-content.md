## Note
MEHO decides from what a Keycloak group gives whether a change of the group can give platform admin rights, not from the group's name. In MEHO's own login realm, `keycloak.group.member.add` and `keycloak.group.update_attributes` need a platform admin as approver when the group or a parent group has the attribute `platform_admin=true`, when its path is under `/MEHO-Platform-Admins/`, when it gives the realm role in `JWT_PLATFORM_ADMIN_ROLE_NAME` (when set), or when the new attributes set `platform_admin`. When MEHO cannot read the group, a platform admin must approve too. Right before an approved change runs, MEHO reads the group again with the same rule.

## Action
Some of these approvals now need a platform admin, where a tenant admin was enough. Make sure a platform admin can approve them. No schema action.
