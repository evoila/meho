## Note
In MEHO's own login realm, a platform admin now approves every Keycloak write, and every `secret.move` that reads from or writes to that realm. The one exception is a write that sets only keys from a short list that cannot touch sign-in or rights: `displayName` of `keycloak.realm.update`, `name` and `description` of `keycloak.client.update`, and `firstName` and `lastName` of `keycloak.user.update`. When MEHO cannot read which realm a write changes, it counts as the login realm. The approval card says why. Right before an approved write runs, MEHO checks it again. In other realms, the approvers stay the same.

## Action
Many Keycloak approvals in the login realm now need a platform admin, where a tenant admin was enough. Make sure a platform admin can approve them. A Keycloak write or a `secret.move` with a Keycloak side that was approved before the update and has not run yet is refused when its run starts, in every realm: send it again. No schema action.
