## Note
A new approval request of a hand-written operation stores the class `read` or `write` (the class of its run), not `other`, so an audit filter on the class finds it under the new class. A run approval does not cover an operation that reads, makes or sends a secret: a person approves each call, and a run approval request that lists one is refused.

## Action
If you filter approval requests or audit rows by class, add `read` and `write` to the filter for hand-written operations. Leave secret operations out of run approval requests.
