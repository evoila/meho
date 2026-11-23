## Note
When a call waits for approval and its inputs hold a secret value, MEHO keeps the real inputs in the encrypted one-time store of the request, and the stored request shows a marker in place of each secret value. A secret value is a value under a secret name, or a value in a field that a connector marked as one that may hold a secret. The approved run sends the real values once. A re-run of a failed run needs the inputs sent again. Stored inputs run only when they match the approved hash. Without a valid `APPROVAL_HANDOFF_ENCRYPTION_KEY`, MEHO refuses such a call instead of letting it wait.

## Action
Check that `APPROVAL_HANDOFF_ENCRYPTION_KEY` is set to a valid key (the status page shows it). A request that waits at the upgrade still runs when approved. No schema action.
