## Note
A call that reads, writes or makes a credential (the classes `credential_read`, `credential_write` and `credential_mint`, for example `vault.kv.put` or `vault.token.create`) waits for a person on every call. No standing grant, no agent grant and no run approval covers it, also one made before the upgrade: such a grant stays stored, but each call waits. An agent's call of such an operation waits also at level `safe`. Grant create, run approval create and agent grant create (an exact op id with `auto-execute`) refuse such an operation.

## Action
Find the grants that name such an operation with `meho service-principals grants list`, `meho agent grant list` and `meho approvals runs list`, and remove them or leave them. An automation run that writes or makes credentials waits for a person on each such call: approve each call in the console or the CLI. No schema action.
