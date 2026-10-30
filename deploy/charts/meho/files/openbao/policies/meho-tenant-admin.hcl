# meho-tenant-admin (tenant_role tenant_admin): everything on the secrets of
# the login's own tenant, including the privileged path that only this
# policy names (docs/cross-repo/connector-vault-tenant-policy.md §2.3). The
# setup Job replaces JWT_ACCESSOR with the accessor of the `jwt` login.
path "secret/data/tenants/{{identity.entity.aliases.JWT_ACCESSOR.metadata.tenant_id}}/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
path "secret/metadata/tenants/{{identity.entity.aliases.JWT_ACCESSOR.metadata.tenant_id}}/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
path "secret/data/tenants/{{identity.entity.aliases.JWT_ACCESSOR.metadata.tenant_id}}/privileged/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
# The version soft-delete (vault.kv.delete) posts to secret/delete/<path>,
# which OpenBao checks on that path, not on data/.
path "secret/delete/tenants/{{identity.entity.aliases.JWT_ACCESSOR.metadata.tenant_id}}/*" {
  capabilities = ["update"]
}
