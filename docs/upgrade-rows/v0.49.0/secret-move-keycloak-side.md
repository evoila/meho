## Note
A `secret.move` whose source or sink is a `keycloak:` ref is now a change of rights. It always waits, also for an admin in the web console. Only an admin (a tenant admin or a platform admin) approves it, with a click in the web console, and the admin may be the person who asked. Its approval card names, for each `keycloak:` side, the Keycloak target, how MEHO connects to it (host, port, http or https, TLS check) and the reference of its admin login. The approved run works only on that target, with that connection and admin login, and MEHO refuses it before any Keycloak login when one of them changed. A move that waits for approval from before the upgrade has no such card, and MEHO refuses it when it runs: nothing is read or written.

## Action
Approve such moves in the web console. Send a move with a `keycloak:` side that waits for approval again after the upgrade. No schema action.
