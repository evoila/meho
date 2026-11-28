## Note
The delete of a hand-made topology node (`topology.delete_node`) now says that it is hard to undo (`hard_to_undo: deletes`): a node made again gets a new id, and the old node's edges do not come back. With the switch `CUSTOM_POLICY` on, it is one of the operations that newly wait for a person when a service login calls it under a standing grant. With the switch off, nothing changes. See https://github.com/evoila/meho/blob/main/docs/codebase/policy-classification.md.

## Action
Before you turn `CUSTOM_POLICY` on, check for standing grants of `topology.delete_node` with the query of the row on hard-to-undo operations. Plan for a person to approve those calls.
