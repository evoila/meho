// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// The login client of this AI tool (#6210).
//
// MEHO can give each AI tool kind a login client of its own. With the
// MCP_CLIENT_PER_TOOL_KIND switch on, GET /api/v1/auth-config lists them:
//
//   "mcp_clients": {"claude-code": "meho-mcp-code", "claude-desktop": "meho-mcp-desktop"}
//
// resolveToolClient() reads that list once at start and picks the client:
//
// 1. MEHO_MCP_CLIENT_ID is set: that client, and no Meho-Tool header. MEHO
//    is not asked. This is the same as before.
// 2. MEHO lists this tool's kind: that client, and the header
//    "Meho-Tool: <kind>". mcp-remote names its saved login file after the
//    URL and the headers, not after the client id. So only the header gives
//    each tool kind a saved login of its own; the client id alone does not.
// 3. Otherwise (the switch is off, an older MEHO, an error, or no answer
//    within 5 seconds): the shared meho-mcp client and no header, as before.
//
// The Claude Desktop launcher imports this module. The Claude Code shim runs
// it as a script:
//
//   node tool-client.mjs <kind> <mcp-url> [<client id from MEHO_MCP_CLIENT_ID>]
//
// The script prints two lines: the header (an empty line when there is
// none), then the JSON for mcp-remote's --static-oauth-client-info.

import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";

export const TOOL_HEADER = "Meho-Tool";
export const DEFAULT_CLIENT_ID = "meho-mcp";
const TIMEOUT_MS = 5000;
// A Keycloak client id from MEHO goes into a JSON argument and a log line.
// Accept only plain names.
const CLIENT_ID = /^[A-Za-z0-9._:-]{1,255}$/;

// The auth-config URL of the MEHO behind an MCP endpoint: the /mcp at the
// end of the path is replaced with /api/v1/auth-config.
export function authConfigUrl(mcpUrl) {
  const url = new URL(mcpUrl);
  const path = url.pathname.replace(/\/+$/, "");
  const base = path.endsWith("/mcp") ? path.slice(0, -"/mcp".length) : "";
  url.pathname = `${base}/api/v1/auth-config`;
  url.search = "";
  url.hash = "";
  return url.href;
}

// The mcp_clients list MEHO sends, or {} when it sends none.
export async function fetchToolClients(mcpUrl, { fetchImpl = fetch, timeoutMs = TIMEOUT_MS } = {}) {
  const response = await fetchImpl(authConfigUrl(mcpUrl), {
    headers: { accept: "application/json" },
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  const list = (await response.json())?.mcp_clients;
  return list && typeof list === "object" && !Array.isArray(list) ? list : {};
}

// Pick the login client and the Meho-Tool header (or null) for this tool.
// Never throws: on any error it keeps the shared client.
export async function resolveToolClient({
  kind,
  mcpUrl,
  override = "",
  fetchImpl,
  timeoutMs,
  say = () => {},
}) {
  const chosen = (override ?? "").trim();
  if (chosen) {
    return { clientId: chosen, header: null };
  }
  let listed;
  try {
    listed = (await fetchToolClients(mcpUrl, { fetchImpl, timeoutMs }))[kind];
  } catch (err) {
    say(
      `could not read the login client list from MEHO (${err.message}); ` +
        `using the shared client ${DEFAULT_CLIENT_ID}`,
    );
    return { clientId: DEFAULT_CLIENT_ID, header: null };
  }
  if (typeof listed === "string" && CLIENT_ID.test(listed)) {
    return { clientId: listed, header: `${TOOL_HEADER}: ${kind}` };
  }
  return { clientId: DEFAULT_CLIENT_ID, header: null };
}

function isMain() {
  try {
    return realpathSync(process.argv[1] ?? "") === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
}

if (isMain()) {
  const [kind, mcpUrl, override] = process.argv.slice(2);
  const { clientId, header } = await resolveToolClient({
    kind,
    mcpUrl,
    override,
    say: (text) => process.stderr.write(`meho plugin: ${text}\n`),
  });
  process.stdout.write(`${header ?? ""}\n${JSON.stringify({ client_id: clientId })}\n`);
}
