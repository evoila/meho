## Note
With the switch `CUSTOM_POLICY` on, an operation that is marked "always ask for approval" (`requires_approval`) with no other reason to wait counts as `dangerous` when it is `safe` or `caution`. 50 built-in operations move from `caution` to `dangerous`. Each call still waits as before, but only an Admin approves another person's request for it, and `APPROVAL_ALLOW_SELF_APPROVAL` never covers `dangerous`: an install with one Operator and no Admin can no longer approve its own requests for these operations. Inside a multi-step run that a person approved, a step whose operation had only this mark no longer waits for its own approval; it runs under the approval of the run. With the switch off, nothing changes. See https://github.com/evoila/meho/blob/main/docs/codebase/approvals.md.

## Action
Before you turn `CUSTOM_POLICY` on, make sure each tenant has at least one Admin who can approve `dangerous` requests.
