## Note
A new install makes one Admin login, which is also the platform admin, and no `approver` login. New realms and new extra tenants get three role groups: Readers, Operators and Admins. The Linux install has no `--approver-user` option. When install.sh runs again on a host that has an `approver_user` entry in its install record, it ignores the entry, writes the install record and the Helm values file again without it, and leaves the login in Keycloak as it is. The Helm value `coreKeycloak.firstLogin.approverUsername` is still accepted and is ignored. Existing logins and groups stay.

## Action
**Nothing to do.** A script that passes `--approver-user` to install.sh must drop the option.
