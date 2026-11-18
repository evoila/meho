## Note
The approval card of `linux.script.run` in the console shows each `env` name with its value. The MCP approval tools (`meho_approvals_get`, `meho_approvals_list`) show the `env` names only. A `linux.script.run` call with a secret in `env` (a name with a secret word such as `PASSWORD` or `TOKEN`, or a value that looks like a password, token or private key) is refused before it waits for approval: put secrets in `secret_env`.

## Action
A `linux.script.run` request with `env` that waits for approval at the upgrade shows only the `env` names. Reject it and send the call again, so the new card shows the values.
