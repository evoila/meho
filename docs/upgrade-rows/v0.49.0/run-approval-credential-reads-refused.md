## Note
A new run approval request that lists an operation that reads a credential (the `credential_read` class, for example `k8s.secret.read_to_ref`) is refused, with the reason and "Leave it out.". For a request made before the upgrade, `meho approvals runs show` and `meho approvals runs approve` name such an entry under `entries_that_wait`, and each of its calls waits for a person.

## Action
Leave operations that read a credential out of new run approval requests. Approve each such call in the console or the CLI. No schema action.
