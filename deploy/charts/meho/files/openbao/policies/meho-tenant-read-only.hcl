# meho-tenant-read-only (tenant_role read_only): read and list the secrets of
# the login's own tenant (docs/cross-repo/connector-vault-tenant-policy.md
# §2.1). The tenant comes from the login's metadata, which the `meho-mcp`
# role copies from the token's tenant_id claim. The setup Job replaces
# JWT_ACCESSOR with the accessor of the `jwt` login.
path "secret/data/tenants/{{identity.entity.aliases.JWT_ACCESSOR.metadata.tenant_id}}/*" {
  capabilities = ["read"]
}
path "secret/metadata/tenants/{{identity.entity.aliases.JWT_ACCESSOR.metadata.tenant_id}}/*" {
  capabilities = ["read", "list"]
}
