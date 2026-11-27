## Note
With `CUSTOM_POLICY` on, the steps of a multi-step operation that a person approved as a whole change. A step whose operation has only the plain "always ask" mark no longer waits on its own: it goes ahead under the approval of the whole run. A step that a fixed rule covers (a credential call, a change of rights or of the policy, a call to an address the caller gives, a destructive step, a delete) or that runs a command as root still waits for its own approval. With the switch off, nothing changes. See https://github.com/evoila/meho/blob/main/docs/codebase/approvals.md.

## Action
None. `CUSTOM_POLICY` is off unless you turn it on. Before you turn it on, check which multi-step operations have steps with only the plain "always ask" mark: those steps will run under the approval of the whole run.
