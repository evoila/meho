# meho-scheduler: the scheduler's own token, stored in the Secret
# <release>-core-vault-scheduler (docs/cross-repo/vault-provisioning.md §6).
path "secret/data/agents/+/credentials" {
  capabilities = ["create", "read", "update"]
}
path "secret/metadata/agents/+/credentials" {
  capabilities = ["read"]
}
# The dead-token check reads lookup-self after a refused write.
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
