## Note
On MEHO 0.48.0 and older, the sent input schemas of 11 tools, and the sent output schema of `call_operation`, hold `null` inside an `enum`, and a strict model gateway refuses the whole tool list. MEHO now sends the tool input and output schemas without `null` inside an `enum`. The server checks each call against the full schema, so MEHO accepts the same calls, and a call with `null` gives the same result as a call without the field. One client effect: a client that checks its own call against the sent schema before it sends it refuses `null` for these 16 fields itself. An output schema still accepts `null` where MEHO answers `null` (for example `delivery`), so a client that checks the answer against it still passes.

## Action
None for most clients. A client that checks its own calls against the sent schema: leave such a field out instead of sending `null`. No schema action.
