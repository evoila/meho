# meho-snapshot: the backup token, stored in the Secret
# <release>-core-vault-snapshot (#3960). Taking a raft snapshot needs sudo.
path "sys/storage/raft/snapshot" {
  capabilities = ["read", "sudo"]
}
