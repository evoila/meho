## Note
A tenant with an entry in `VAULT_OIDC_ROLE_BY_TENANT` now always gets the tenant folder check, also when `VAULT_KV_TENANT_SCOPE_PREFIX` is empty. MEHO then reads and writes only Vault paths under `secret/tenants/<tenant id>/` for that tenant. A tenant without an entry is unchanged.

## Action
Keep the secrets of each tenant that has an entry in `VAULT_OIDC_ROLE_BY_TENANT` under `secret/tenants/<tenant id>/`. List the targets of that tenant with `meho targets list` and move each target whose `secret_ref` is outside that folder.
