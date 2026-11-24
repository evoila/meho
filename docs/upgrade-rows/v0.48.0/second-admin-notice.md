## Note
MEHO now says when a change of rights cannot be approved because the tenant has only one admin: the answer of the call says "This change needs a second admin to approve it. This tenant has only one admin." with a link to the guide. `meho doctor` shows a new line "second admin", a warning when the tenant has one admin. `/api/v1/health` and `meho_status` report the number in `approvals.rights_change_admins`. See https://evoila.github.io/meho/latest/guides/approvals-and-break-glass/#changes-of-rights-need-another-admin.

## Action
Run `meho doctor`. When the line "second admin" warns, add a second admin in Keycloak (in `MEHO-Admins` or `MEHO-Platform-Admins`). No schema action.
