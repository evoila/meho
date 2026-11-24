## Note
A hosted agent with a `connectors` list now keeps to the list in more places. The list check runs before the wrong-connector check, so a refusal to such an agent names only the connectors in its list. Every result the agent stores keeps its list, and `result_query` reads only results stored under a list inside the agent's own. The fetch of an approved secret reveal is refused with `connector_not_allowed` when the request is for a connector, or a target of a connector, outside the list. With the new setting `AGENT_TOPOLOGY_OFF_WITH_CONNECTOR_LIST=true` (off by default), an agent with a list does not get `query_topology`. An agent without a list works as before.

## Action
None for `query_topology`: the setting is off by default, so an agent with a list keeps it. If it reads a stored result that the person who started the run made, run the call again inside the agent run.
