## Note
With `CUSTOM_POLICY` on, a raise of an operation's risk level or approval with a token (`meho connector edit-op`, the REST API, the MCP tool `meho_connector_edit_op`), and the removal of such a change (`meho connector remove-override`), waits for an Admin's click in the web console; the command prints the approval request id. An "approval off" from the command line, also after a fresh `meho login`, no longer runs at once: it waits for a platform admin's click in the web console. With the switch off nothing changes.

## Action
With `CUSTOM_POLICY` on, scripts that raise risk levels or remove overrides with a token now get `202` and must wait for the approval; tell your Admins to approve these requests in the web console. No schema action.
