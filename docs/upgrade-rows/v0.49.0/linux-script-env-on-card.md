## Note
The approval card of `linux.script.run` in the console shows each `env` name with its value. Reads by an agent or a token show the names only: the MCP approval tools (`meho_approvals_get`, `meho_approvals_list`), `preview_operation`, and the REST approval reads of a `linux.script.run` request with an agent, service or runner login. The script gets each `env` value byte for byte as the card shows it, also a value that ends in line breaks. A `linux.script.run` call with a secret in `env` (a name with a secret word such as `PASSWORD` or `TOKEN`, or a value that looks like a password, token or private key) is refused before it waits for approval: put secrets in `secret_env`.

## Action
A `linux.script.run` request with `env` that waits for approval at the upgrade shows only the `env` names. Reject it and send the call again, so the new card shows the values.
