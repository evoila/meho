# MCP Server

> Last verified: v2.3

MEHO exposes its investigation capabilities as an [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server, allowing external AI agents, IDEs, and CI/CD pipelines to trigger MEHO investigations, search the knowledge base, query the topology graph, and list connectors -- all through the standard MCP tool protocol.

**Audience:** Developers integrating MEHO into their IDE, CI/CD pipeline, or AI agent workflow.

## Connection

MEHO's MCP server is available via two transports:

| Transport | Endpoint | Use Case |
|-----------|----------|----------|
| Streamable HTTP | `https://your-meho-host/mcp` | Web clients, CI/CD, remote access |
| stdio | `python -m meho_app.api.mcp_server.server` | Claude Desktop, local IDE integration |

### Authentication

All MCP HTTP requests require a valid **Keycloak JWT token** in the `Authorization` header:

```
Authorization: Bearer <jwt-token>
```

The `MCPAuthMiddleware` validates the JWT against your Keycloak instance and injects the user context into every tool call. This ensures:

- All tool calls are scoped to the authenticated user's tenant
- Audit logs include the user identity
- RBAC rules apply to investigation results

For **stdio transport** (Claude Desktop), authentication is not required -- the server runs as a local subprocess with a default system context.

## Available Tools

MEHO exposes 4 curated **READ-only** tools. All tools are annotated with `readOnlyHint=true` -- no destructive operations are possible through the MCP server.

| Tool | Parameters | Description |
|------|-----------|-------------|
| `meho_investigate` | `query` (required), `connector_scope` (optional) | Trigger a full investigation session -- the agent uses its ReAct loop to investigate across connected infrastructure systems and returns findings |
| `meho_search_knowledge` | `query` (required), `limit` (optional, 1-50, default 10) | Search the knowledge base using hybrid search (BM25 + semantic) -- returns matching documents with text, relevance score, and source metadata |
| `meho_query_topology` | `entity_name` (required), `entity_type` (optional) | Look up an entity in the topology graph -- returns entity details, relationships, cross-connector correlations, and SAME_AS links |
| `meho_list_connectors` | *(none)* | List all active connectors for the authenticated user's tenant -- returns connector ID, name, type, and status |

### Tool Details

**`meho_investigate`** is the primary tool. It creates a new chat session, sends the query to MEHO's specialist agent, and returns the investigation results including the session ID, status, summary, and findings. Use `connector_scope` to limit which connectors the agent can query.

**`meho_search_knowledge`** is useful for quick lookups without triggering a full investigation. It queries MEHO's knowledge base (uploaded documents, connector operation documentation) and returns ranked results.

**`meho_query_topology`** enables entity-centric queries. Given an entity name (e.g., a pod name, VM name, or service name), it returns the entity's details and its relationships across connectors -- including SAME_AS correlations that link the same resource across different systems.

**`meho_list_connectors`** provides a lightweight healthcheck -- list what infrastructure systems are connected and available for investigation.

## Claude Desktop Integration

Add MEHO to Claude Desktop by editing `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "meho": {
      "command": "python",
      "args": ["-m", "meho_app.api.mcp_server.server"],
      "env": {
        "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/meho",
        "ANTHROPIC_API_KEY": "sk-ant-..."
      }
    }
  }
}
```

After restarting Claude Desktop, you can ask Claude questions like:

- "Use MEHO to investigate high latency on the payment-svc"
- "Search MEHO's knowledge base for Kubernetes troubleshooting guides"
- "Look up the payment-api pod in MEHO's topology"
- "What connectors are available in MEHO?"

## IDE Integration

For VS Code or other MCP-compatible editors, configure the MCP server using the Streamable HTTP transport:

```json
{
  "mcpServers": {
    "meho": {
      "type": "streamable-http",
      "url": "https://your-meho-host/mcp",
      "headers": {
        "Authorization": "Bearer <jwt-token>"
      }
    }
  }
}
```

This enables AI coding assistants to query MEHO's infrastructure intelligence during development -- for example, checking the topology when debugging a service or searching the knowledge base for deployment procedures.

## Security

- **READ-only tools:** All 4 tools are annotated with `readOnlyHint=true`. No WRITE or DESTRUCTIVE operations are exposed through the MCP server. The agent can investigate but cannot modify infrastructure.

- **Audit trail:** Every MCP tool call is audit-logged with the user identity, tool name, and parameters. Failed audit writes are logged but do not block the tool response (best-effort).

- **Keycloak JWT auth:** HTTP requests without a valid JWT are rejected with 401. The middleware validates tokens against your Keycloak JWKS endpoint.

- **Tenant isolation:** Tool calls are scoped to the authenticated user's tenant. An investigation triggered via MCP only accesses connectors in that tenant.

- **Feature flagged:** The MCP server is gated behind `MEHO_FEATURE_MCP_SERVER=true`. Set to `false` to disable the `/mcp` endpoint entirely.

## Troubleshooting

### Authentication Failures

**Symptom:** All requests return 401 `Authentication required`
**Cause:** Missing or invalid JWT token
**Fix:** Obtain a valid JWT from your Keycloak instance. For testing, use the Keycloak token endpoint:
```bash
curl -X POST https://your-keycloak/realms/meho/protocol/openid-connect/token \
  -d "grant_type=password&client_id=meho&username=admin&password=admin"
```

### Transport Selection

**Symptom:** Unsure whether to use HTTP or stdio
**Cause:** Different transports serve different use cases
**Fix:**
- **Streamable HTTP:** Use for remote access, CI/CD pipelines, and team-shared MEHO instances. Requires JWT auth.
- **stdio:** Use for local Claude Desktop integration where MEHO runs on the same machine. No auth needed but requires local database access.

### Tool Invocation Errors

**Symptom:** Tool calls return error responses
**Cause:** Internal service failures (database, agent, or knowledge store)
**Fix:** Check MEHO server logs for detailed error information. Common causes:
- Database connection issues (verify `DATABASE_URL`)
- Missing `ANTHROPIC_API_KEY` for `meho_investigate`
- Knowledge store not initialized (no embeddings provider configured)

### MCP Endpoint Not Available

**Symptom:** `/mcp` returns 404
**Cause:** MCP server feature flag is disabled
**Fix:** Set `MEHO_FEATURE_MCP_SERVER=true` in your `.env` file and restart MEHO.
