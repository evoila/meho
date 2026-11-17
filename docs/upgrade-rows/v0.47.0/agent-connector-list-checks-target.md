## Note
A hosted agent with a `connectors` list may use only targets of those connectors. MEHO checks the connector that the target picks, and refuses a call or preview on a target of another connector with `connector_not_allowed`. A standing grant of a service login covers only targets of the grant's own connector; a call on a target of another connector waits for a person's approval.

## Action
If a hosted agent calls targets of a connector that is not in its `connectors` list, add that connector to the agent's list. If a service login's standing grant should cover targets of another connector, make a grant for that connector.
