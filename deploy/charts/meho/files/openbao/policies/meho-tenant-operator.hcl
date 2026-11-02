# meho-tenant-operator (tenant_role operator): read_only's rights, plus
# writing new versions under the rotation path
# (docs/cross-repo/connector-vault-tenant-policy.md §2.2). The setup Job
# replaces JWT_ACCESSOR with the accessor of the `jwt` login.
path "secret/data/tenants/{{identity.entity.aliases.JWT_ACCESSOR.metadata.tenant_id}}/*" {
  capabilities = ["read"]
}
path "secret/metadata/tenants/{{identity.entity.aliases.JWT_ACCESSOR.metadata.tenant_id}}/*" {
  capabilities = ["read", "list"]
}
path "secret/data/tenants/{{identity.entity.aliases.JWT_ACCESSOR.metadata.tenant_id}}/rotation/*" {
  capabilities = ["create", "read", "update"]
}
# The break-glass path privileged/ is for tenant admins only. The wildcards
# above cover it too, so deny it here: the more specific path wins.
path "secret/data/tenants/{{identity.entity.aliases.JWT_ACCESSOR.metadata.tenant_id}}/privileged/*" {
  capabilities = ["deny"]
}
path "secret/metadata/tenants/{{identity.entity.aliases.JWT_ACCESSOR.metadata.tenant_id}}/privileged/*" {
  capabilities = ["deny"]
}
