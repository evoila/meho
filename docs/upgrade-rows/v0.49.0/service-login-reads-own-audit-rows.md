## Note
An agent, runner, add-on or other service login now reads only the audit rows of its own calls, whatever role it has: in `POST /api/v1/audit/query` and its filters (also `has_trace`), the class counts, `show`, the trace read, `by-work-ref`, `who-touched`, the MCP tool `query_audit`, the operation `audit.loose_rows` and every session replay. A row or session of another login answers like one that does not exist. The `shape="tree"` replay of `query_audit` checks from the stored audit rows that the session holds a row of the caller's own calls; it does not read the `Mcp-Session-Id` header for this check. People keep every row of their tenant.

## Action
A service login that read the audit rows of other logins now gets fewer rows. To look into the calls of others, a person runs the query.
