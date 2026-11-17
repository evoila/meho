## Note
A run approval never covers an operation that reads a credential (the `credential_read` class, for example `k8s.secret.read_to_ref`). Each call of such an operation waits for a person, the same as under a standing grant. This holds also for a run approval that a person approved before the upgrade: the entry stays on the list, and each of its calls waits. A new request that lists such an operation is refused. For a request made before the upgrade, `meho approvals runs show` and `meho approvals runs approve` name the entry under `entries_that_wait` ("each call waits for a person"; for `vault.kv.read`, "each call with reveal_secret waits for a person"). A read that returns no secret value runs at once and gets no note.

## Action
An automation run that reads credentials waits for a person on each such call. Approve each call in the console or the CLI, or leave the operation out of the run. No schema action.
