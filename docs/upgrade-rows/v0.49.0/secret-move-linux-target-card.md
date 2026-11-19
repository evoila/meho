## Note
The approval card of a `secret.move` to a `linux:` place now names the Linux target, its host and its SSH port. The approved run writes only to that target, host and port. A move that waits for approval from before the upgrade has no such card, and MEHO refuses it when it runs: nothing is read or written. `preview_operation`, `meho_approvals_get` and `meho_approvals_list` show the target, host and port only when the new setting `SECRET_MOVE_LINUX_TARGET_CARD` is on (off by default).

## Action
Send such a waiting move again after the upgrade. No schema action.
