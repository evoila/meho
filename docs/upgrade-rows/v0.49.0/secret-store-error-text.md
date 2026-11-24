## Note
When the secret store (Vault or OpenBao) refuses or fails a login, a read or a write, the answer holds a fixed MEHO sentence, for example "The secret store refused the request.", and not the store's own text or address. The error codes and the fields stay the same. The server log events `vault_login_failed` and `vault_request_failed` hold the store's text, with the address and secret values replaced by a mark.

## Action
None for most installs. A script or an agent prompt that reads the store's text from `error` or `exception_message` reads `error_code` instead. An admin finds the store's text in the server log.
