## Note
Only a person can remove a `deny` grant or a `needs-approval` grant of an agent. A call that removes such a grant with a service, runner or agent login now gets 403 `human_principal_required`, and the grant stays. This holds for REST, MCP and the console. Removing an `auto-execute` grant works with any login that has the `tenant_admin` role.

## Action
If a script removes `deny` or `needs-approval` grants, run it with a person's login. Scripts that only remove `auto-execute` grants need no change.
