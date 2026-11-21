## Note
The guide for your own Vault (`coreVault.enabled: false`) gives one policy recipe: the per-tenant policy, so each tenant reads only `secret/tenants/<tenant id>/`. The `meho-mcp` login role in the guide is bound to the audience and to the addresses MEHO uses (`token_bound_cidrs`), and has `token_ttl=5m`. Only MEHO needs to reach the Vault login path. The built-in OpenBao is not changed.

## Action
If you bring your own Vault, apply the per-tenant policy from [the Vault provisioning guide](https://github.com/evoila/meho/blob/main/docs/cross-repo/vault-provisioning.md) and set `token_bound_cidrs` on the `meho-mcp` role to the addresses MEHO uses to reach Vault. When MEHO reaches Vault through a proxy or address translation, bind the address Vault sees. Then check that `GET /api/v1/health` still shows `vault.read_ok: true`.
