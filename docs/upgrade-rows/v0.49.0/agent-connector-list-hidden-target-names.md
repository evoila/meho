## Note
A hosted agent with a `connectors` list now gets the answer "no target" (`no_target`) when it names a target of a connector outside its list, by name or by alias. Only the targets in the list count: when a name or an alias fits one target in the list and other targets outside it, the agent gets the target in the list. The "no target" answer has the same form as for a name that no target has. This holds for `call_operation`, `preview_operation`, the `target` filter of `meho_connector_list` and the docs tools. An agent without a list, a person, a service login and a runner login get the same answers as before.

## Action
None. If such an agent needs a target that it does not see, add the target's connector to its `connectors` list.
