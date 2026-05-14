# MCP server architecture

> ## ⚠️ Architectural correction (2026-05-14)
>
> The content below was written under the **v0.2 transitional shape** (every G3-G9 verb gets a parallel MCP tool). That model was wrong against [CLAUDE.md](../../CLAUDE.md) postulate 5. The corrected shape:
>
> - **The agent surface is ~17 meta-tools** registered by G0.5 (#226 updated): `search_connectors`, `list_connectors`, `list_operation_groups`, `search_operations`, `call_operation`, `search_knowledge`, `add_to_knowledge`, `search_memory`, `add_to_memory`, `broadcast_recent`, `broadcast_announce`, `broadcast_watch`, `list_targets`, `query_topology`, `query_audit`, `result_query`, `result_aggregate`, `result_export`, `result_describe`.
> - **No per-vendor MCP tools.** Vendor operations (e.g. vCenter's 3,000+ paths, K8s's 13 typed ops) reach the agent through `call_operation(connector_id, op_id, target?, params)`, backed by the G0.6 dispatcher — see [operations-substrate.md](operations-substrate.md) for the canonical reference (tables, registry v2, dispatcher pipeline, composite recursion, JSONFlux reducer, meta-tools).
> - **Admin operations** (override management, replay, annotation) use the `meho.*` admin namespace (`meho.broadcast.overrides.set`, `meho.audit.replay`, `meho.topology.annotate`, `meho.memory.promote`), tenant_admin role required, visible in `tools/list` only with admin scope.
> - **The `_op_map` pattern** described later in this doc is v0.2 transitional. Operations live in G0.6's `endpoint_descriptor` table; typed connectors register via `register_typed_operation()` (driven by `register_typed_op_registrar()` + the lifespan-run registrar list).
>
> Read [CLAUDE.md](../../CLAUDE.md) for the canonical surface contract and [operations-substrate.md](operations-substrate.md) for the dispatcher + registry + tables. Treat the body content below as the historical baseline that G0.5 amendment + G0.6 + G0.7 evolved from.

---

How MEHO speaks the Model Context Protocol alongside its HTTP API. The substrate landed in [G0.5 (#226)](https://github.com/evoila/meho/issues/226).

## Why MCP-in-v0.2

The locked decision (#7 in [v0.2-decisions.md](../planning/v0.2-decisions.md)) was to ship MCP alongside the CLI in v0.2 rather than defer to v0.2.next. Two consequences:

- Operators + agents get a unified working surface; the CLI + MCP dispatch through the same backplane path.
- External pilots (the first being a customer beyond `rdc-internal`) get day-1 agent-native access via Claude Desktop, Cline, Continue, or any MCP-spec-conformant client.

## Spec target

**[MCP 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18)**. Pinned in [`mcp/schemas.py`](../../backend/src/meho_backplane/mcp/schemas.py) as `PROTOCOL_VERSION`. Future revisions will need an ADR + a coordinated upgrade.

## Transport

**Streamable HTTP**, not stdio. MEHO is a hosted server, not a local subprocess. The `/mcp` route accepts JSON-RPC 2.0 POST requests with a single envelope per body (batch arrays are unsupported — MCP Streamable HTTP transport mandates single envelopes).

Response shapes per the spec's *Sending Messages to the Server* section:

| Input | Response |
|---|---|
| Request (has `id`) | HTTP 200 + JSON-RPC envelope |
| Notification (no `id`) | HTTP 202 Accepted with no body |
| Parse error | HTTP 200 + `error: { code: -32700, ... }` |
| Invalid request | HTTP 200 + `error: { code: -32600, ... }` |
| Unsupported `MCP-Protocol-Version` | **HTTP 400** + JSON-RPC error envelope (spec MUST) |
| Missing/invalid Bearer token | HTTP 401 + `WWW-Authenticate: Bearer resource_metadata="..."` |
| Insufficient scope/role | HTTP 403 |
| GET on `/mcp` | HTTP 405 (FastAPI default; SSE-on-GET unimplemented in v0.2) |

## OAuth 2.1 resource-server pattern

MEHO acts as an **OAuth 2.1 resource server** per [RFC 9728 (Protected Resource Metadata)](https://datatracker.ietf.org/doc/html/rfc9728) + [RFC 8707 (Resource Indicators)](https://www.rfc-editor.org/rfc/rfc8707.html). The flow:

1. Client sends request without token → server returns **401** + `WWW-Authenticate: Bearer resource_metadata="<backplane>/.well-known/oauth-protected-resource"`.
2. Client fetches PR metadata at the URL in step 1.
3. Client follows OAuth 2.1 (PKCE + `resource` parameter) against the discovered Keycloak authorization server.
4. Client retries with `Authorization: Bearer <token>`. Server validates `aud` matches `MCP_RESOURCE_URI`.

**PR metadata document** at [`/.well-known/oauth-protected-resource`](../../backend/src/meho_backplane/api/well_known.py) returns:

```json
{
  "resource": "https://meho.evba.lab/mcp",
  "authorization_servers": ["https://keycloak.evba.lab/realms/evba"],
  "scopes_supported": ["mcp:read", "mcp:execute"],
  "bearer_methods_supported": ["header"]
}
```

**Critical:** the MCP audience is **distinct** from the chassis HTTP API audience. An HTTP-API JWT (audience = `KEYCLOAK_AUDIENCE`) is rejected at `/mcp` with 401. Clients call MEHO at MCP-shaped or HTTP-shaped endpoints with the appropriate token, not interchangeably.

## Tool registry + Resource registry

Two parallel registries in [`mcp/registry.py`](../../backend/src/meho_backplane/mcp/registry.py):

### `ToolDefinition` + handler

```python
class ToolDefinition(BaseModel):
    name: str                # dotted: "meho.status", "vault.kv.read"
    description: str         # agent-facing — LOAD-BEARING for UX
    inputSchema: dict        # JSON Schema 2020-12
    outputSchema: dict | None
    required_role: TenantRole  # MEHO-internal; dropped from wire shape
    op_class: str              # "read" | "write" | "credential_read" | "audit_query"
```

Wire shape (returned via `tools/list`) drops `required_role` and `op_class` — clients shouldn't see server-side policy.

### `ResourceTemplateDefinition` + handler

```python
class ResourceTemplateDefinition(BaseModel):
    uriTemplate: str           # RFC 6570: "meho://tenant/{tenant_id}/info"
    name: str
    description: str
    mimeType: str = "application/json"
    required_role: TenantRole
```

**Spec correctness note:** v0.2 uses `resources/templates/list`, not `resources/list`. Per the 2025-06-18 spec, concrete-URI resources go through `resources/list`; templated resources go through `resources/templates/list`. Every MEHO resource carries a template, so v0.2's `resources/list` response is always empty.

### Registration discipline

[`mcp/registry.py`](../../backend/src/meho_backplane/mcp/registry.py) enforces three reject-at-boot rules for resources:

- Duplicate placeholder names within one template (e.g., `meho://tenant/{id}/{id}`).
- Exact-duplicate `uriTemplate` strings.
- Same-shape collision (e.g., `meho://kb/{slug}` and `meho://kb/{id}` would silently shadow).

Surface failures at boot, never at request time.

## RBAC filtering

Every registered tool/resource carries a `required_role` (one of `read_only` / `operator` / `tenant_admin`). The list methods filter the registry against the calling operator's `tenant_role`; tools/resources above the operator's role rank don't appear in the response. Role rank declared as a pinned tuple in [`mcp/registry.py`](../../backend/src/meho_backplane/mcp/registry.py) — `read_only < operator < tenant_admin`.

Helper `role_at_least(actual, required)` is the single source for the role ordering, used by both list-time filtering and call-time re-checks.

## Tool description quality

Per AI-engineering best-practices: **the description IS the agent's prompt for when to use the tool.** Imprecise descriptions get tools called incorrectly.

Good: "Returns the operator's identity (sub, tenant) plus the MEHO backplane's dependency status (Vault reachable, DB migrated). Use at session start to verify the MCP session can reach all subsystems. No arguments required."

Bad: "Status check tool."

Every G3–G9 tool registration MUST pass a description review before merge.

## Audit integration

Per [G0.5-T5 (#250, PR #300)](https://github.com/evoila/meho/pull/300): MCP handlers write their own audit rows per `tools/call` and `resources/read`, **not** via the chassis `AuditMiddleware`. The middleware path-excludes `/mcp` requests (see `_AUDIT_SKIP_PATH_PREFIXES` in [`audit.py`](../../backend/src/meho_backplane/audit.py)) because the JSON-RPC envelope carries multiple potential ops — one audit row per HTTP request would be wrong granularity for G8's audit queries.

The per-operation writer is [`mcp/audit.py::write_mcp_audit_row`](../../backend/src/meho_backplane/mcp/audit.py), called from inside [`mcp/handlers.py`](../../backend/src/meho_backplane/mcp/handlers.py) for both `tools/call` and `resources/read`. MCP audit row shape:

```text
operator_sub  ← from JWT (validated by /mcp auth chain)
tenant_id     ← operator.tenant_id
request_id    ← from RequestContextMiddleware (still runs)
method        ← "MCP"
path          ← "/mcp/tools/call/{tool_name}" or "/mcp/resources/read/{uri}"
status_code   ← 200 / 400 / 403 / 404 / 500 (derived from JSON-RPC outcome)
duration_ms   ← time.monotonic() bracket
payload       ← {op_id, params_hash, op_class}
```

Fail-closed: audit write failure → MCP call fails with JSON-RPC `INTERNAL_ERROR` (-32603). Compliance-critical.

`params_hash` is SHA256 of canonicalized (sorted-keys JSON) arguments — content-addressable, deterministic, doesn't leak the args.

## Adding an MCP tool

For a new vendor connector op:

1. **Implement the op** in your connector's `_op_map` (per [`connectors.md`](connectors.md)).
2. **Register an MCP tool** in `backend/src/meho_backplane/mcp/tools/<product>.py`:
   ```python
   register_mcp_tool(
       ToolDefinition(
           name="vsphere.vm.list",
           description="List VMs visible to the operator's vSphere session, ...",
           inputSchema={
               "type": "object",
               "properties": {
                   "target": {"type": "string", "description": "Target name or alias."},
                   "folder": {"type": "string", "description": "Optional folder filter."},
               },
               "required": ["target"],
               "additionalProperties": False,
           },
           required_role=TenantRole.OPERATOR,
           op_class="read",
       ),
       handler=_vsphere_vm_list_handler,
   )
   ```
3. **Lifespan eager-import** picks it up on restart.
4. **Verify** via `tools/list` then `tools/call` against a running backplane (use `@modelcontextprotocol/inspector`).

## Adding an MCP resource

```python
register_mcp_resource(
    ResourceTemplateDefinition(
        uriTemplate="meho://kb/{slug}",
        name="Knowledge-base entry",
        description="One kb entry by slug. Tenant-scoped to the operator's tenant.",
        mimeType="text/markdown",
        required_role=TenantRole.OPERATOR,
    ),
    handler=_kb_entry_handler,
)
```

Handler receives `(operator, bound_params)` where `bound_params = {"slug": "..."}`. Must validate `operator.tenant_id` matches whatever tenant scope the resource is supposed to honor — cross-tenant reads return 403.

## Manual runbook (pre-release verification)

The automated proof lives in [`backend/tests/integration/test_mcp_inspector.py`](../../backend/tests/integration/test_mcp_inspector.py) — direct JSON-RPC against the dispatch chain, deterministic in CI. Before any release, run the same flow against the running backplane with a real off-machine MCP client; that's what catches spec drift between the wire test and a third-party implementation. Two recommended paths:

### MCP Inspector CLI (deterministic, scriptable)

`@modelcontextprotocol/inspector` ships a non-interactive CLI mode that emits JSON to stdout — runnable from any shell, no UI. Use this for repeatable per-release smoke and for debugging spec interop issues.

```bash
# Mint a token against the realm with `resource=https://<backplane>/mcp` so
# the issued `aud` claim matches MCP_RESOURCE_URI. The exact flow depends on
# how the operator registered the MCP client in Keycloak — see
# `docs/cross-repo/mcp-client-setup.md` for the recipe.
TOKEN=$(meho login --print-token)  # or the device-code flow against Keycloak

# Sanity: list tools.
npx @modelcontextprotocol/inspector --cli \
  https://meho.example.com/mcp \
  --transport http \
  --method tools/list \
  --header "Authorization: Bearer $TOKEN"

# Call meho.status — exercises the full chain.
npx @modelcontextprotocol/inspector --cli \
  https://meho.example.com/mcp \
  --transport http \
  --method tools/call \
  --tool-name meho.status \
  --header "Authorization: Bearer $TOKEN"
```

Expected output: `tools/list` shows `meho.status`; `tools/call meho.status` returns the operator-identity bundle from the chassis `/api/v1/health`. A 401 with `WWW-Authenticate: Bearer resource_metadata=...` means the token's `aud` doesn't match `MCP_RESOURCE_URI` — re-check the Keycloak client's `resource` parameter.

### Claude.ai Custom Connector (the dogfooding path)

[Anthropic's Custom Connectors flow](https://modelcontextprotocol.io/docs/develop/connect-remote-servers) is the production-shaped path: Claude.ai (web) → Settings → Connectors → Add custom connector → paste `https://<backplane>/mcp` → complete OAuth in the popup → the MEHO tools appear in the conversation toolbar.

Note that the local-`claude_desktop_config.json` shape documented in the Claude Desktop quickstart is for *stdio* MCP servers spawned as subprocesses; remote HTTPS MCP servers like MEHO route through the Custom Connector UI instead. Operators on Claude Desktop can still verify connectivity locally by hitting Claude.ai in a browser against the same Claude account; the connector persists across both surfaces.

Verification checklist after the connector is wired:

- The connector card shows MEHO's tools (`meho.status` at minimum in v0.2; product tools as G3-G9 land).
- A chat that prompts "check that MEHO is reachable" should result in Claude calling `meho.status` and surfacing the bundle.
- The audit_log table on the backplane should grow by one row per `tools/call` and `resources/read` — verify with a quick `SELECT method, path, operator_sub, status_code FROM audit_log ORDER BY occurred_at DESC LIMIT 10`.

If Claude renders a connector error rather than the tools, the OAuth handshake failed — the most common cause is the realm's MCP client missing the `resource_metadata` parameter; the second is `BACKPLANE_URL` resolving to a host the Custom Connector backend can't reach (firewall / private DNS). See [`docs/cross-repo/mcp-client-setup.md`](../cross-repo/mcp-client-setup.md) for the Keycloak-side wiring.

## What's intentionally out of scope

- **Stdio transport** — MEHO is hosted.
- **Dynamic Client Registration (RFC 7591)** — operators register MCP clients manually in Keycloak. v0.2.next polish.
- **Resource subscriptions** (`resources/subscribe`) — v0.2 advertises `subscribe: false`.
- **`listChanged` notifications** — v0.2 advertises `listChanged: false` on both tools and resources.
- **MCP prompts / sampling / roots** — defined in the spec but not load-bearing for v0.2's operator surface.
- **Server-initiated progress notifications + cancellation** — all v0.2 tools are short-lived.
- **Inter-server delegation** (MEHO as MCP client to a downstream MCP server) — out of scope; MEHO is the leaf server.

## References

- [MCP 2025-06-18 spec](https://modelcontextprotocol.io/specification/2025-06-18) (the binding contract).
- [MCP 2025-06-18 Authorization](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization) — OAuth 2.1 resource-server pattern.
- [MCP 2025-06-18 Lifecycle](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle) — initialize, notifications/initialized.
- [MCP 2025-06-18 Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) — tool definition + invocation.
- [MCP 2025-06-18 Resources](https://modelcontextprotocol.io/specification/2025-06-18/server/resources) — resource definition + templates/list vs list.
- [RFC 9728 (Protected Resource Metadata)](https://datatracker.ietf.org/doc/html/rfc9728), [RFC 8707 (Resource Indicators)](https://www.rfc-editor.org/rfc/rfc8707.html), [OAuth 2.1 draft](https://datatracker.ietf.org/doc/html/draft-ietf-oauth-v2-1-13).
- [docs/planning/v0.2-decisions.md](../planning/v0.2-decisions.md) — decision #7 (ship MCP in v0.2).
- [docs/architecture/connectors.md](connectors.md) — the parallel connector op registry every MCP tool wraps.
