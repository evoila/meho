## Note
New optional setting `VAULT_OIDC_ROLE_TENANTS` (through the chart's `extraEnv`): a comma-separated list of the tenant ids that may use the shared Vault login role `VAULT_OIDC_ROLE`. When it is set, a tenant that is not in the list and has no entry in `VAULT_OIDC_ROLE_BY_TENANT` gets a clear refusal, and MEHO tries no Vault login. A wrong entry stops MEHO from starting.

## Action
No action needed. The default is empty, and then nothing changes. Leave it empty on the built-in Vault of the chart.
