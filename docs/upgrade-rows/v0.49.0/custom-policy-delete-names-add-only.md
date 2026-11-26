## Note
With the switch `CUSTOM_POLICY` on, the setting `SERVICE_GRANT_DELETE_SHAPED_PATTERNS` only adds names to the built-in delete names. It never removes one. An install whose setting replaced the built-in names gets the built-in names back, so more calls wait for approval: a call of an operation with a built-in delete name (for example a name that ends in `.delete`) waits, and a standing grant no longer covers it. With the switch off, the setting works as before. See https://github.com/evoila/meho/blob/main/docs/codebase/service-principal-grants.md.

## Action
If you turn on `CUSTOM_POLICY` and your `SERVICE_GRANT_DELETE_SHAPED_PATTERNS` lists other names, expect the calls with a built-in delete name to wait for approval again. Plan who approves them. No schema action.
