# An approved call reads secrets with the approver's login (decision)

**Status:** draft, for the owner to confirm

## The decision

When a person approves a waiting call, MEHO runs the call with the
approver's login. So the secret store sees the approver, not the person who
asked.

MEHO keeps this. Running the call as the person who asked would need MEHO to
keep long logins of people while a call waits. MEHO keeps no such login.

## What the audit row says

The audit row of every call has the field `secret_read_as`. It holds the id of
the login that the secret store accepted. For an approved call it is the
approver's id. The person who asked stays on the approval request row.

The field is off in audit answers by default. The setting
`AUDIT_SECRET_READ_AS` turns it on.
