# meho-configurator: the setup Job's own login after the first install
# (role `vault-init` on the `kubernetes-sa` login, bound to the Job's
# service account). Exactly what a later run changes. It can write
# policies, so treat this login as an admin login.
path "sys/mounts" {
  capabilities = ["read"]
}
path "sys/mounts/secret" {
  capabilities = ["create", "read", "update", "sudo"]
}
path "sys/auth" {
  capabilities = ["read"]
}
path "sys/auth/jwt" {
  capabilities = ["create", "read", "update", "sudo"]
}
path "sys/auth/kubernetes-sa" {
  capabilities = ["create", "read", "update", "sudo"]
}
path "sys/policies/acl/*" {
  capabilities = ["create", "read", "update", "list"]
}
path "auth/jwt/*" {
  capabilities = ["create", "read", "update", "list"]
}
path "auth/kubernetes-sa/*" {
  capabilities = ["create", "read", "update", "list"]
}
path "identity/group/name/*" {
  capabilities = ["create", "read", "update"]
}
path "identity/group-alias" {
  capabilities = ["create", "update"]
}
path "identity/group-alias/id/*" {
  capabilities = ["create", "read", "update"]
}
path "auth/token/roles/meho-periodic" {
  capabilities = ["create", "read", "update"]
}
path "auth/token/create/meho-periodic" {
  capabilities = ["create", "update"]
}
path "secret/data/meho/test/*" {
  capabilities = ["create", "read", "update"]
}
