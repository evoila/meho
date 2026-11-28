## Note
A Vault operation that puts a name or a mount into a Vault path (for example `vault.auth.userpass.read`, `vault.auth.approle.delete`, `vault.identity.group.delete`, `vault.sys.policy.write`) now accepts only a plain value: a name has no `/`, is not `.` or `..`, and has no `?`, `#`, `%`, backslash or control character; a mount is such parts split by `/`. A call with another value is refused as `invalid_params` before it waits for approval. `vault.auth.userpass.update_password` now has the risk level `dangerous`, the same as `vault.auth.userpass.write`, so only a tenant admin may approve it. See https://github.com/evoila/meho/blob/main/docs/codebase/connectors-vault.md.

## Action
Check scripts and runbooks that call these operations with a name or mount holding one of these characters, and change them to the plain name. Make sure a tenant admin can approve the password changes of userpass logins.
