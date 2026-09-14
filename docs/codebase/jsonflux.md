# JSONFlux result preservation

When a registered typed operation's explicit response schema has a top-level
array plus two or more outcome fields (`handshake`, `reachable`, `reason`,
`success`, `ok`, `healthy`, `status`, `state`, `resultStatus`, or
`executionStatus`), it must declare every such outcome field in
`llm_instructions.result_scalars.keys`. The registry conformance sweep enforces
that rule. A lone `status` is not enough to qualify: it can be an opaque vendor
state rather than a complete outcome.

An operation may declare `result_objects` for a bounded top-level identity
projection. `result_object_truncations` names an inline field whose value was
bounded. The full value is retrievable through a JSONFlux handle only when the
projected object aliases the collection that was spilled (as TLS `leaf` aliases
`chain[0]`); arbitrary sibling objects are not implicitly persisted.
