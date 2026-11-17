## Note
**Action needed (service logins and run approvals).** No standing grant and no run approval covers `keycloak.role_mapping.assign` and `keycloak.group.member.add`. Each call waits for a person to approve it, also when an older grant names the operation. A new grant or run approval that names them is refused.

## Action
Plan for a person to approve each call. Remove grants for these two operations that no longer do anything.
