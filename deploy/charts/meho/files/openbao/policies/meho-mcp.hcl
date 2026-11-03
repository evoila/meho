# meho-mcp: every login through the `meho-mcp` role gets this policy. Read on
# MEHO's own subtree, which holds the health check's test secret
# (docs/cross-repo/vault-provisioning.md §3). Tenant secrets come from the
# three per-tenant policies, given through the login's tenant_role group.
path "secret/data/meho/*" {
  capabilities = ["read"]
}
path "secret/metadata/meho/*" {
  capabilities = ["read"]
}
