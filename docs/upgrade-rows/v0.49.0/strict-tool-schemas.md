## Note
On MEHO 0.48.0 and older, the sent input schemas of 11 tools hold `null` inside an `enum`, and a strict model gateway refuses the whole tool list. MEHO now sends the tool input schemas without `null` inside an `enum`. Output schemas are not changed. The server checks each call against the full schema, so MEHO accepts the same calls, and a call with `null` gives the same result as a call without the field. One client effect: a client that checks its own call against the sent schema before it sends it refuses `null` for these 16 fields itself.

## Action
None for most clients. A client that checks its own calls against the sent schema: leave such a field out instead of sending `null`. No schema action.
