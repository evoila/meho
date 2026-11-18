## Note
The approval request of 8 more Keycloak changes of rights now shows the object as it is now (`role_mapping.assign`, `role_mapping.client.assign`, `group.member.add`, `user.reset_password`, `user.enable` and the three login flow writes), and MEHO reads it again right before the approved run. When it changed, or MEHO cannot read it, the run is refused. A request of these 8 operations that already waited before the upgrade has no such state, so it is refused when approved. For `group.member.add` the run compares only the group id and the roles the group gives, not the member list, so a big group still runs. The switch `KEYCLOAK_RIGHTS_CHANGE_CARDS` now also covers these 8 writes: it decides only whether `preview_operation`, `meho_approvals_get` and `meho_approvals_list` show the new card keys. The run check is always on.

## Action
Send such a request again after the upgrade. No schema action.
