## Note
New setting `MCP_STRICT_TOOL_SCHEMAS` (chart: `config.mcpStrictToolSchemas`), off by default. When on, the tool schemas that MEHO sends to MCP clients have no `null` inside an `enum`, so a strict model gateway accepts the tool list. The server still checks every call against the full schema. No schema migration.

## Action
No action needed. Turn it on only if a strict model gateway refuses the MEHO tool list.
