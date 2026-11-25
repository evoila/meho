## Note
The tool schemas that MEHO sends to MCP clients no longer have `null` inside an `enum`. A strict model gateway used to refuse the whole tool list because of it. Now it accepts the list. Only the sent schema changes. The server still checks every call against the full schema, so every call that works today still works. A client that sends `null` for such a field still gets the same result as one that leaves the field out. No schema migration.

## Action
No action needed.
