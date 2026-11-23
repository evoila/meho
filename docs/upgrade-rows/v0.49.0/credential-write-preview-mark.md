## Note
The preview of a generic credential write shows each secret input as the fixed mark `***REDACTED***`, and its `preview_hash` changes with the hidden values. A destructive generic credential write that waits for approval at the upgrade, and that an older build made, reads as changed when it goes on.

## Action
The requester sends such a waiting credential write again after the upgrade. No schema action.
