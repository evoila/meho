## Note
A `secret.move` whose source or sink is a `keycloak:` ref is now a change of rights. The requester never approves it, and another admin (a tenant admin or a platform admin) must. Its approval card names, for each `keycloak:` side, the Keycloak target, its host and the reference of its admin login. The approved run works only on that target, host and admin login, and MEHO refuses it before any Keycloak login when one of them changed. A move that waits for approval from before the upgrade has no such card, and MEHO refuses it when it runs: nothing is read or written.

## Action
Ask another admin to approve such moves. Send a move with a `keycloak:` side that waits for approval again after the upgrade. No schema action.
