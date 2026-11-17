## Note
An approved `secret.move` with a `vault-target:` side now checks that each Vault target is the one the approval showed. A request that waits for approval from before the upgrade does not show the target ids, so its approved run is refused and nothing is read or written.

## Action
Before the upgrade, approve or reject each waiting `secret.move` with a `vault-target:` side. After the upgrade, send a new request for each one that was refused.
