## Note
A write step of a multi-step operation now uses the override rows of its operation like a direct call, with the policy switches on or off. An override that raises the risk level or sets the approval flag now makes the step wait for a person the same way as a direct call. The approval of the whole run still covers a step that an override raised to `dangerous`; a step that an override sets to `destructive` or gives the approval flag keeps its own approval. An override that turns approval off does not change a step. No schema migration.

## Action
Before the upgrade, run `meho connector overrides` (as a platform admin, to see every tenant) and check each row that sets a higher risk level or requires approval. After the upgrade, the multi-step operations that run these operations as a step wait for approval at that step. Remove an override you no longer want with `meho connector remove-override`, and give an approver time for the steps that now wait.
