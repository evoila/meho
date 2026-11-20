## Note
MEHO says nothing about a second admin on a change of rights, because an Admin now changes rights alone. The field `approvals.rights_change_admins` is not in the answers of `/api/v1/health` and `meho_status`, the answer of a call that waits for approval has no `approval_note`, and `meho doctor` has no "second admin" line.

## Action
A script or a monitor that reads `approvals.rights_change_admins` or `extras.approval_note` must stop reading it. No schema action.
