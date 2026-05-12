# MCP server architecture

How MEHO speaks the Model Context Protocol alongside its HTTP API. The substrate landed in [G0.5 (#226)](https://github.com/evoila/meho/issues/226); every G3–G9 connector Initiative will register MCP tools against this server.

## Why MCP-in-v0.2

The locked decision (#7 in [v0.2-decisions.md](../planning/v0.2-decisions.md)) was to ship MCP alongside the CLI in v0.2 rather than defer to v0.2.next. Two consequences:

- Every G3–G9 verb gains a parallel MCP tool definition.
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
