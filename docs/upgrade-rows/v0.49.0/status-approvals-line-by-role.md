## Note
`meho status` now prints the approval rule for your own role in the current tenant on its `Approvals` line. It no longer says "a second person must approve" for everyone.

## Action
If a script reads the `Approvals` text of `meho status`, change it to read the new line, or read `effective_posture` from `meho status --json` instead. The JSON output does not change. See https://evoila.github.io/meho/latest/guides/approvals-and-break-glass/ for the rule.
