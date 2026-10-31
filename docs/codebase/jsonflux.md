# JSONFlux result preservation

When a registered typed operation's explicit response schema has a top-level
array plus one recognised Boolean outcome field (`handshake`, `reachable`,
`success`, `ok`, or `healthy`) or two or more outcome fields (`handshake`, `reachable`, `reason`,
`success`, `ok`, `healthy`, `status`, `state`, `resultStatus`, or
`executionStatus`), it must declare every such outcome field in
`llm_instructions.result_scalars.keys`. The registry conformance sweep enforces
that rule. A lone `status` is not enough to qualify: it can be an opaque vendor
state rather than a complete outcome.

An operation may declare `result_document: {"always": true}` when its result
must always be a document record: the reducer then stores the payload whole
whatever its size and returns the document summary. See "Document records"
in `result-spill.md`. An operation whose small result should come back whole
declares `result_handle` instead (below); `result_document` wins when an
operation declares both.

An operation may declare `result_objects` for a bounded top-level identity
projection. `result_object_truncations` names an inline field whose value was
bounded. Every object the hint names is also stored whole with the spilled
rows (the same place as an omitted scalar), so a `result_query` read with
`path="/<object key>"` returns the complete value. When a field was cut,
`result_object_note` rides inline next to the truncations and names that read,
or says the object could not be kept when the store dropped it.

Every other top-level object or list next to the rows (a "part", such as
`assumptions`, `totals` or `unknowns`) is kept by default, with no hint (#5139).
Each part is stored whole with the rows, so `path="/<part>"` returns it, and a
list part pages with `offset` / `limit`. Whole parts are also copied inline
while the inline summary stays under 4,096 bytes; a part is never cut. The
parts that do not fit are named in `parts_not_inline` (with
`parts_not_inline_count` when the names list is cut), and `parts_note` says how
to read them, or that they cannot be read because the store dropped them to
keep the rows. The limit is one budget for the whole summary, hints and notes
included. When the names and the note leave no room, the largest values move
out and are stored with the rows. A scalar (hinted or not) is counted under
`omitted_scalar_count` and read back with `path="/<field name>"`. An object or
list (a digest list such as `failed`, or the projection of a `result_objects`
object) is named in `parts_not_inline` like a part, and `path="/<name>"` returns
it whole. Only the reducer's own keys never move. Credential-class ops
keep the old rule: no part is copied or stored without their hint. The
`result_objects` hint only chooses the inline fields of the objects it names.

An operation may declare `result_handle: {"always": true}` (#5143) when other
operations take its result by its handle (an inventory read, whose result
`inventory.export` reads back). A result under the limits (50 rows,
4,096 bytes) then comes back **whole**, and the reducer also stores it as a
document record and returns a handle next to it. `result_query` with
`query={"path": ""}` returns the same answer. Over the limits nothing changes.
A credential-class operation gets no handle and nothing is stored. When the
store does not keep the record (no tenant, store down), the result still comes
back whole, with no handle. See `result-spill.md`.
