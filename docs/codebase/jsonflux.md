# JSONFlux result preservation

When a registered typed operation's explicit response schema has a top-level
array plus one recognised Boolean outcome field (`handshake`, `reachable`,
`success`, `ok`, or `healthy`) or two or more outcome fields (`handshake`, `reachable`, `reason`,
`success`, `ok`, `healthy`, `status`, `state`, `resultStatus`, or
`executionStatus`), it must declare every such outcome field in
`llm_instructions.result_scalars.keys`. The registry conformance sweep enforces
that rule. A lone `status` is not enough to qualify: it can be an opaque vendor
state rather than a complete outcome.

An operation may declare `result_document: {"always": true}` when other
operations load its whole result by handle (the migration inventory reads):
the reducer then stores the payload as a document record whatever its size.
See "Document records" in `result-spill.md`.

An operation may declare `result_objects` for a bounded top-level identity
projection. `result_object_truncations` names an inline field whose value was
bounded. Every object the hint names is also stored whole with the spilled
rows (the same place as an omitted scalar), so a `result_query` read with
`path="/<object key>"` returns the complete value. When a field was cut,
`result_object_note` rides inline next to the truncations and names that read,
or says the object could not be kept when the store dropped it. Objects the
hint does not name are not persisted.
