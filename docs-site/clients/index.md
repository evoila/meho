# Connect clients

!!! note "Stub — content tracked by [evoila/meho#2672](https://github.com/evoila/meho/issues/2672)"

This section will cover connecting operators and agents to a running
backplane — **CLI first**, then the MCP client matrix:

- The `meho` CLI: static binary, `~/.config/meho/config.json`,
  device-code login (works on private networks).
- Claude Desktop / claude.ai Custom Connector (public-TLS path).
- Claude Code (`.mcp.json` + PKCE).
- The `mcp-remote` shim fallback.
- The "4-wall" troubleshooting matrix for auth walls.

Until it lands, the client-setup material lives in-repo:
[`docs/cross-repo/mcp-client-setup.md`](https://github.com/evoila/meho/blob/main/docs/cross-repo/mcp-client-setup.md).
