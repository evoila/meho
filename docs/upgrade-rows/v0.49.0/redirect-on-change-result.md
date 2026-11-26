## Note
A change (PUT, POST, PATCH, DELETE) that an operation sends is sent at most twice: it follows only a 307 or 308 to the same path, and every other redirect after the change ends the call. A connector login follows a redirect on the same origin, as a read does; a login redirect to another origin ends the call with a plain error that names the HTTP status. Each trace entry keeps its path; for an operation that sends a secret, a path segment that holds a value of the call is replaced by `[redacted]`. The new switch `REDIRECT_ON_CHANGE_RESULT` (off by default) decides only the answer of such a redirect: off gives the generic `connector_error` answer, on says what the HTTP code means and marks a change uncertain. See https://github.com/evoila/meho/blob/main/docs/codebase/mcp.md.

## Action
None. Set `REDIRECT_ON_CHANGE_RESULT=true` to try the answer that says what the HTTP code means.
