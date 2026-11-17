## Note
New switch `MCP_PROFILE_CONNECTOR_LISTS` (off): an MCP tool profile from `MCP_CONNECTOR_PROFILES` can limit a session to named connectors, and a login token with the `mcp_profile` claim works only at `/mcp?profile=<name>`.

## Action
None. To use it, see https://github.com/evoila/meho/blob/main/docs/codebase/mcp.md and `meho admin keycloak bootstrap-clients --mcp-profile`.
