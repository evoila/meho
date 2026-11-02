# meho-admin: day-2 work by a person, through the `meho-admin` login role
# (members of the Keycloak group MEHO-Platform-Admins). Everything: more
# keyholders (`bao operator rotate-keys`), new cluster keys for the setup
# Job's login after a key rotation, policy changes. No root token is kept,
# so this login is the admin path.
path "*" {
  capabilities = ["create", "read", "update", "patch", "delete", "list", "sudo"]
}
