# MEHO Introspection MCP Server

Model Context Protocol (MCP) server that enables LLMs (like Claude in Cursor) to introspect MEHO's execution behavior.

## Features

- **Session Listing**: Find recent chat sessions
- **Full Transcripts**: Access complete execution traces with LLM prompts, operation calls, SQL queries
- **Focused Analysis**: Get explanations focused on errors, performance, or decisions
- **Cross-Session Search**: Find patterns across multiple sessions

## Quick Start (Automatic with dev-env.sh)

The MCP server is automatically installed when you run local development:

```bash
./scripts/dev-env.sh local
```

This:
1. Installs the `meho_mcp_server` package
2. Starts the MEHO backend at `http://localhost:8000`
3. Configures Cursor via `.cursor/mcp.json`

**After running `dev-env.sh local` for the first time, restart Cursor to enable MCP tools.**

## Manual Installation

```bash
# From the MEHO.X repository root
pip install ./meho_mcp_server

# Or install dependencies directly
pip install mcp httpx
```

## Configuration

Environment variables (set automatically by dev-env.sh):

```bash
export MEHO_API_URL=http://localhost:8000  # MEHO API base URL
export MEHO_AUTH_TOKEN=your-token          # Optional: Authentication token
```

Cursor configuration is in `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "meho-introspection": {
      "command": "python",
      "args": ["-m", "meho_mcp_server.server"],
      "env": {
        "MEHO_API_URL": "http://localhost:8000"
      }
    }
  }
}
```

## Usage with Cursor

After installation and Cursor restart, use the tools in conversations with Claude:

```
Claude: Let me check what happened in the last MEHO session...
[Uses meho_get_transcript with session_id="latest"]

I can see the session made 3 LLM calls and 2 HTTP requests.
The vSphere API call failed with a 401 Unauthorized error...
```

## Available Tools

| Tool | Description |
|------|-------------|
| `meho_list_sessions` | List recent chat sessions |
| `meho_get_transcript` | Get full execution transcript |
| `meho_get_summary` | Get session summary statistics |
| `meho_get_llm_calls` | Get all LLM calls with prompts/responses |
| `meho_get_sql_queries` | Get all SQL queries with results |
| `meho_get_operation_calls` | Get all operation calls with bodies |
| `meho_get_event_details` | Get single event details |
| `meho_search_events` | Search across sessions |
| `meho_explain_session` | Get human-readable explanation |

## Common Debugging Workflows

### "Why did MEHO give that answer?"
1. `meho_get_transcript session_id="latest"`
2. Look at the LLM calls to see reasoning
3. Check tool calls to see what data was retrieved

### "The API call failed"
1. `meho_get_operation_calls session_id="latest" status_filter="error"`
2. Examine request/response to identify the issue

### "What's using so many tokens?"
1. `meho_get_summary session_id="latest"`
2. Check token usage in summary
3. `meho_get_llm_calls` to see which call used most tokens

## Running the Server

```bash
# As a module
python -m meho_mcp_server.server

# Or using the CLI entry point
meho-mcp-server
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest meho_mcp_server/tests/
```

## Part of TASK-186

This MCP server is part of TASK-186: Deep Observability & Introspection System.
