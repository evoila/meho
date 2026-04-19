# MCP Client

> Last verified: v2.3

MEHO's MCP Client connector connects to external [Model Context Protocol](https://modelcontextprotocol.io/) servers, discovers their tools at runtime, and makes them available to the MEHO agent during investigations. Unlike static connectors with predefined operations, the MCP Client dynamically discovers what an external server offers and registers those tools as MEHO operations -- enabling the agent to use any MCP-compatible tool server as part of a cross-system investigation.

**Audience:** Operators adding external AI tool servers to MEHO.

## Authentication

| Transport | Config Fields | Notes |
|-----------|--------------|-------|
| Streamable HTTP | `server_url`, `transport_type: streamable_http` | Remote MCP server over HTTP |
| stdio | `command`, `args`, `env`, `transport_type: stdio` | Local subprocess MCP server |

**Optional credential:** `api_key` -- Bearer token for MCP server authentication (if the server requires it).

### Setup

1. **Identify the MCP server** you want to connect:
    - A code analysis server (e.g., filesystem tools)
    - A database query server (e.g., SQLite, PostgreSQL tools)
    - A custom tool server built with the MCP SDK
    - Any MCP-compatible server listed in the [MCP server registry](https://modelcontextprotocol.io/servers)

2. **Determine the transport type**:
    - **Streamable HTTP:** For remote servers accessible via URL (most common)
    - **stdio:** For local subprocess servers that communicate via stdin/stdout

3. **Get the server URL or command**:
    - For HTTP: The server's URL endpoint (e.g., `https://my-mcp-server.example.com/mcp`)
    - For stdio: The command to launch the server (e.g., `npx`, `uvx`, or a binary path)

4. **Add the MCP connector in MEHO UI**:
    - Set the connector name (this becomes the server identifier)
    - Set `transport_type` to `streamable_http` or `stdio`
    - For HTTP: Set `server_url` to the server endpoint
    - For stdio: Set `command` and optionally `args` and `env`
    - If the server requires authentication, set `api_key` in credentials

5. **MEHO discovers tools automatically** via the MCP `list_tools()` protocol. No manual configuration of operations is needed.

!!! tip "Server Name Prefixing"
    MEHO automatically sanitizes the connector name into a `server_name` identifier (lowercased, spaces to underscores, non-alphanumeric stripped). All discovered tools are prefixed as `mcp_{server_name}_{tool_name}` to prevent namespace collisions when multiple MCP servers are connected.

!!! warning "Tool Safety Levels"
    MEHO maps MCP tool annotations to its trust model. Tools with `readOnlyHint=true` map to READ, tools with `destructiveHint=true` map to DESTRUCTIVE, and all others default to READ (safe). If an MCP server provides tools that modify data, MEHO's approval modal will prompt for confirmation.

## Operations

MCP Client operations are **not predefined** -- they are discovered at runtime from the connected MCP server. When MEHO connects to a server:

1. MEHO calls `list_tools()` on the MCP server
2. Each tool is converted to a MEHO `OperationDefinition`
3. The operation ID is prefixed as `mcp_{server_name}_{tool_name}`
4. Parameters are extracted from the tool's JSON Schema input definition
5. The agent discovers these operations via `search_operations` like any other connector

**Example:** If you connect a server named "my database" that exposes tools `query` and `list_tables`, MEHO registers:

| Operation | Description |
|-----------|-------------|
| `mcp_my_database_query` | (from MCP server's tool description) |
| `mcp_my_database_list_tables` | (from MCP server's tool description) |

The agent can then use these tools naturally during investigations -- "query the database for recent error logs" will route to `mcp_my_database_query`.

### Tool Synchronization

MEHO detects when a server's tool set changes (using a SHA-256 hash of tool names and descriptions). When tools change, operations are re-synced automatically on the next connection.

## Example Queries

With MCP servers connected, ask MEHO questions like:

- "Query the database for all error events in the last hour" (database MCP server)
- "List the files in the /etc/nginx/ directory" (filesystem MCP server)
- "Search the codebase for references to the deprecated API endpoint" (code analysis MCP server)
- "What tables exist in the production database?" (database MCP server)
- "Run the health check tool on the staging environment" (custom tool server)

## Topology

The MCP Client connector does **not** discover topology entities. MCP tools are generic and do not follow MEHO's entity type conventions. If an MCP server returns data about infrastructure resources, those resources will appear in investigation results but will not be added to the topology graph.

## Troubleshooting

### Connection Failures

**Symptom:** Connector fails to connect with `ConnectionError` or `TimeoutError`
**Cause:** The MCP server is unreachable or not running
**Fix:** Verify the server URL is correct and the server is running. MEHO retries with exponential backoff (1s, 2s, 4s) before failing. For stdio servers, verify the command path is correct and the binary is installed.

### Tool Discovery Returns Empty

**Symptom:** Connector connects but no operations appear
**Cause:** The MCP server's `list_tools()` returns an empty tool list
**Fix:** Verify the MCP server has tools registered. Test with the MCP Inspector CLI: `npx @modelcontextprotocol/inspector@latest`. Some servers require initialization steps before tools are available.

### Transport Type Selection

**Symptom:** Unsure which transport to use
**Cause:** MCP supports multiple transports with different tradeoffs
**Fix:**
- **Streamable HTTP:** Use for remote servers, servers shared across multiple clients, and production deployments. Requires the server to expose an HTTP endpoint.
- **stdio:** Use for local development, servers that only run as subprocesses, and tools like `npx`-based servers. The server runs as a child process of MEHO.

### Authentication Errors

**Symptom:** Connection fails with 401 or 403 errors
**Cause:** The MCP server requires authentication that is not configured
**Fix:** Set the `api_key` credential in the connector configuration. The key is sent as a `Bearer` token in the `Authorization` header. Verify the key is valid with the MCP server administrator.
