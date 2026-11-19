## Note
A hosted agent with a `connectors` list now keeps to the list in more places. The list check runs before the wrong-connector check, so a refusal to such an agent names only the connectors in its list. Every result the agent stores keeps its list, and `result_query` reads only results stored under a list inside the agent's own. The fetch of an approved secret reveal is refused with `connector_not_allowed` when the request is for a connector, or a target of a connector, outside the list. An agent with a list does not get `query_topology`. An agent without a list works as before.

## Action
If a hosted agent with a `connectors` list needs `query_topology`, remove the list from its definition. If it reads a stored result that the person who started the run made, run the call again inside the agent run.
