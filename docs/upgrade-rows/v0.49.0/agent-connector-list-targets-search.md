## Note
A hosted agent with a `connectors` list now sees only the targets of connectors in its list: `list_targets` shows only those, and the close target names in a `no_target` or `ambiguous_target` answer are only those. `list_operation_groups` and `search_operations` refuse a connector outside the list, like `call_operation`. Memory, the knowledge base and the activity feed are not limited by the list. An agent without a list works as before.

## Action
If such an agent needs a target or a connector that it does not see, add the connector to its `connectors` list.
