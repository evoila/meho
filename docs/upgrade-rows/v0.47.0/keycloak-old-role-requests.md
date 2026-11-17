## Note
A request for `keycloak.role_mapping.assign` or `keycloak.role_mapping.client.assign` that already waited for approval before this version does not run, in every realm. Its card holds no list of what the role gives. The refusal says: "This request was made before MEHO read what a role gives. Send it again."

## Action
Send such a request again after the upgrade.
