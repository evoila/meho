## Note
`meho login` makes a random session id and keeps it with the token. The CLI sends it in the `Meho-Session-Id` header on each call, and MEHO puts it on the audit rows of the call and on an approval request that waits. `meho approvals list` shows the `WORK-REF` and `SESSION` columns. Migration `0180` adds the nullable `approval_request.session_from_client` column; rows that exist stay empty.

## Action
None. A login made before the upgrade has no session id until the next `meho login`.
