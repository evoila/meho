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

### Session id issuance + capture (audit correlation)

Per the spec's *Session Management* section, a server MAY assign a session id at `initialize` by returning it in an `Mcp-Session-Id` **response header** on the `InitializeResult`; the client MUST then include the header on every subsequent HTTP POST to the MCP endpoint. The handshake is strictly **server-driven** — clients do not invent session ids, they only relay what the server gave them. MEHO runs **no** stateful session store in v0.2 — the id exists purely for **audit correlation** so per-session replay (`meho audit replay <session-id>`, G8.2) can reconstruct one agent's full operation trace.

**Issuance side (G0.15-T4 #1213).** On a successful `initialize` reply, [`_maybe_issue_initialize_session_id`](../../backend/src/meho_backplane/mcp/server.py) stamps a fresh `uuid4()` onto the response's `Mcp-Session-Id` header (and binds the same id into a structlog `mcp_session_id` contextvar so any post-issue log line carries the same correlation key). The issuance is gated on:

- Method is `initialize` and it's a *request*, not a notification.
- The dispatched response is HTTP 2xx **and** the JSON-RPC envelope has no `error` member — a failed initialize must not seed a session id.
- The client did not already send an `Mcp-Session-Id` header inbound (a resume / replay attempt where the client carries an id is accepted lenient; MEHO does not overwrite the client's correlation key).

Before G0.15-T4 #1213, MEHO captured the header end of the chain but never issued one. The visible symptom (`claude-rdc-hetzner-dc#753` finding 2) was every Claude Code MCP audit row landing with `agent_session_id: null` despite [`meho_status`](../../backend/src/meho_backplane/api/v1/health.py) / [`/ready.features.audit_replay.capture_mode`](../../backend/src/meho_backplane/api/v1/health.py) advertising `"always"` — both surfaces correctly reported the **capture** config; nothing populated the column because no client had a server-assigned session id to send back.

**Capture side.** On every `POST /mcp`, [`_bind_mcp_session_id`](../../backend/src/meho_backplane/mcp/server.py) binds an `mcp_session_id` structlog contextvar (G8.2-T2 #1010):

- Header present and a parseable UUID → bind it.
- Header present but malformed (non-UUID) → treated as absent (a malformed *client* header never 500s the call; a non-UUID can't go in a `uuid` column). The audit row's `agent_session_id` lands as NULL.
- Header absent/empty → contextvar stays unbound. The audit row's `agent_session_id` lands as NULL; the G8.2 replay route's session walk treats NULLs as "not part of any session," which is correct for a stateless-client call.

[`write_mcp_audit_row`](../../backend/src/meho_backplane/mcp/audit.py) reads the contextvar via [`_resolve_uuid_contextvar`](../../backend/src/meho_backplane/mcp/audit.py) and writes `audit_log.agent_session_id`. The structlog contextvar propagates down the async call chain inside one request, so the write picks up the binding without threading the value through every handler signature.

`MCP_REQUIRE_SESSION_ID=true` ([`Settings.mcp_require_session_id`](../../backend/src/meho_backplane/settings.py), default `false`) turns a missing/empty header into a JSON-RPC `-32600` Invalid Request **before** dispatch — no audit row is written for the rejected call. A present-but-malformed header is not a rejection in require-mode (the client did send an id, just an unparseable one); the audit row lands NULL and a structured `mcp_malformed_session_id` warning surfaces the misbehaving client without breaking client retry logic.

### `initialize.instructions` — session preamble

The spec-optional [`instructions`](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle) field on `InitializeResult` is MEHO's surface for the agent-facing session preamble. Two banded text sections stack into the field, in this order: **tenant conventions** (tenant-wide), then **runbook priming** (operator-personal, per-session). The assembler is [`assemble_preamble(tenant_id, operator_sub)`](../../backend/src/meho_backplane/conventions/preamble.py) at [`backend/src/meho_backplane/mcp/server.py:327`](../../backend/src/meho_backplane/mcp/server.py); the empty-string result is collapsed to `None` so a spec-conforming client sees the field omitted rather than emitted as a literal empty string.

Order rationale: tenant conventions are background context that frames every dispatch in this tenant; runbook priming is a per-session imperative the agent must consult before its next move. Conventions go first because they set the operating environment; priming goes second because it's the higher-precedence "what to do right now" layer. Two newlines separate the bands so they render as distinct paragraphs in the agent's context (see [`_combine_bands`](../../backend/src/meho_backplane/conventions/preamble.py)). The two bands have independent token budgets: a tenant with many conventions does not shrink the priming surface, and an operator with many in-progress runs does not shrink the conventions surface.

#### Tenant conventions band

Database-backed `kind='operational'` rows packed in `priority DESC, created_at ASC` order and wrapped in a positional `<<TENANT_CONVENTIONS ... END_TENANT_CONVENTIONS>>` guard with the `GUARD_PREFIX` reminder that the wrapped content is admin-authored tenant guidance, not system directives. Originated in G7.1-T4 ([#316](https://github.com/evoila/meho/issues/316)); the canonical references are [`docs/codebase/tenant_conventions.md`](../codebase/tenant_conventions.md) (control flow, packer arithmetic, OWASP LLM01 isolation) and [`docs/architecture/conventions-seed.md`](conventions-seed.md) (seed migrations + the consumer-side override template). This doc does not re-derive that material.

#### Runbook session priming

Added in G12.4 ([Initiative #1199](https://github.com/evoila/meho/issues/1199), Tasks [#1315](https://github.com/evoila/meho/issues/1315) / [#1316](https://github.com/evoila/meho/issues/1316)). Operators who attach to MCP with one or more `in_progress` runs in `runbook_runs` whose `assigned_to == operator.sub` see a per-run priming block appended after the conventions band. Generated by [`assemble_runbook_priming(operator_sub, tenant_id)`](../../backend/src/meho_backplane/runbooks/priming.py); appended in [`assemble_preamble_detailed`](../../backend/src/meho_backplane/conventions/preamble.py). The single example block:

```
<<RUNBOOK_PRIMING — CRITICAL>>
You are mid-runbook `cert-rotation-vcenter` v3 on step 2/7 (`revoke-old-cert`).
Follow only the current step. Do not look ahead. Do not improvise. Do not combine steps.
If the step looks wrong, call runbook_abort and escalate to a senior in chat.
Use runbook_next to advance once the current step's verify passes.

<<END_RUNBOOK_PRIMING>>
```

**Composition rules.** One block per in-progress run, capped at `MAX_PRIMING_BLOCKS = 5` ([`runbooks/priming.py`](../../backend/src/meho_backplane/runbooks/priming.py)). Operators with `>5` in-progress runs see one summary block (`"You have N in-progress runbook runs … call runbook_list_runs to see them and proceed one at a time."`) instead of per-run blocks — the per-block text would otherwise dominate the preamble. Each per-run block carries the run's `template_slug`, `template_version`, current `step_id`, and `n/total` position, all taken from the [`RunSummary`](../../backend/src/meho_backplane/runbooks/runs_schemas.py) row the run service returns; the delimiters (`BLOCK_START` / `BLOCK_END`) are hard-coded module constants emitted by the wrapper, so a slug that somehow contained the terminator string cannot escape the block (same positional-wrapper discipline the conventions band uses).

**Empty case is byte-identical.** An operator with no in-progress runs sees the conventions text alone, with no trailing separator and no priming guards — the [`_combine_bands`](../../backend/src/meho_backplane/conventions/preamble.py) helper short-circuits when `priming.text == ""` so the wire shape is unchanged from the pre-T2 (#1316) preamble. The test pin lives at [`backend/tests/test_conventions_preamble.py`](../../backend/tests/test_conventions_preamble.py).

**Regenerated per `initialize`, never cached.** The priming helper queries `runbook_runs` fresh on every handshake. Run state changes between MCP sessions (a senior reassigns a run, the operator advances or aborts one between sessions, a new run is started in the gap) and a cached priming text would lie about the operator's current obligations. The cost is one indexed query against `runbook_runs` per `initialize`; acceptable since `initialize` is once-per-session.

**Priming is a UX hint, not enforcement.** The load-bearing adherence mechanism is the **step opacity contract** owned by the runbook substrate ([G12.3](https://github.com/evoila/meho/issues/1198), see [`docs/architecture/runbooks.md`](runbooks.md) §The opacity contract): `runbook_next` returns the body of exactly one step, and there is no response shape on any surface — schema, function signature, service, transport — that could carry an adjacent or future step. The opacity contract is the floor; priming is how the agent should behave inside the floor. A future bug or regression in the priming text does not weaken opacity — an agent that ignores priming, or that never sees priming because the helper returned `""`, still cannot read step 3 while the run is on step 2, because the substrate has no code path that would surface it. The two layers are independent by design: never document priming as if it were the gate.

Response shapes per the spec's *Sending Messages to the Server* section:

| Input | Response |
|---|---|
| Request (has `id`) | HTTP 200 + JSON-RPC envelope |
| Notification (no `id`) | HTTP 202 Accepted with no body |
| Parse error | HTTP 200 + `error: { code: -32700, ... }` |
| Invalid request | HTTP 200 + `error: { code: -32600, ... }` |
| Unsupported `MCP-Protocol-Version` | **HTTP 400** + JSON-RPC error envelope (spec MUST) |
| Missing `Mcp-Session-Id` **and** `MCP_REQUIRE_SESSION_ID=true` | HTTP 200 + `error: { code: -32600, ... }` (no audit row written) |
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

Wire shape (returned via `tools/list`) drops `required_role` and `op_class` — clients shouldn't see server-side policy. It also strips any top-level `oneOf` / `allOf` / `anyOf` from `inputSchema`: the Anthropic Messages API rejects a top-level JSON-Schema combinator in a tool's `input_schema` and 400s the whole `tools` array (so one offender breaks every call in the session), hence the published copy must be combinator-free. The full schema — combinators retained — stays on `inputSchema` for server-side `jsonschema` validation, so the `-32602` rejections for bad argument shapes are unaffected. See `_wire_safe_input_schema` in [`mcp/registry.py`](../../backend/src/meho_backplane/mcp/registry.py). (#905)

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
operator_sub     ← from JWT (validated by /mcp auth chain)
tenant_id        ← operator.tenant_id
agent_session_id ← Mcp-Session-Id header (issued by server on initialize per G0.15-T4 #1213, echoed by client); NULL when the client didn't echo one back, and on chassis HTTP rows (G8.2-T2)
parent_audit_id  ← parent_audit_id contextvar (forward-compat; unbound in v0.2)
request_id       ← from RequestContextMiddleware (still runs)
method           ← "MCP"
path             ← "/mcp/tools/call/{tool_name}" or "/mcp/resources/read/{uri}"
status_code      ← 200 / 400 / 403 / 404 / 500 (derived from JSON-RPC outcome)
duration_ms      ← time.monotonic() bracket
payload          ← {op_id, params_hash, op_class}
```

`agent_session_id` and `parent_audit_id` are real `audit_log` columns (not payload keys), read from contextvars by [`_resolve_uuid_contextvar`](../../backend/src/meho_backplane/mcp/audit.py) — the same pattern the chassis uses for `target_id`. Chassis HTTP-side rows (written by `AuditMiddleware`) leave `agent_session_id` NULL by design: they are not part of an agent session.

Fail-closed: audit write failure → MCP call fails with JSON-RPC `INTERNAL_ERROR` (-32603). Compliance-critical.

`params_hash` is SHA256 of canonicalized (sorted-keys JSON) arguments — content-addressable, deterministic, doesn't leak the args.

## Target-reference shape convention

The agent surface has shapes for "name a target/node by name" that
diverge slightly across tools. The divergence is internally coherent
per tool's role; cross-tool, an agent carrying a target name across
the read and the write surfaces no longer needs to reshape it.

G0.13-T2 (#1132) is the additive convergence step: `call_operation`
now accepts a bare-string `target` alongside the existing dict shape,
matching `query_topology` / `query_audit`. The dict shape stays
supported -- agents pinned to it are unchanged -- and is the form
that opens the `fqdn` vhost-override door.

| Shape | Tool(s) | Why |
|---|---|---|
| `target: "<name>"` (bare string, **preferred forward**) | [`call_operation`](../../backend/src/meho_backplane/mcp/tools/operations.py) (since G0.13-T2 #1132), [`query_topology`](../../backend/src/meho_backplane/mcp/tools/topology.py) (kind=`dependents`/`dependencies`), [`query_audit`](../../backend/src/meho_backplane/mcp/tools/audit.py) | Either-shape acceptance reduces agent retries (the consumer's most-cited daily-driver sharp edge at v0.6.0). The handler normalises the bare string to `{name: <string>}` before dispatch, so downstream code sees one canonical form. |
| `target: {"name": "<name>"}` (object with `name` key) | [`call_operation`](../../backend/src/meho_backplane/mcp/tools/operations.py) | Original shape; still accepted unchanged. Opens the optional `fqdn` field for per-call vhost-override (`vcfa-rest-9.0`-style routing); the bare-string form does not, so callers needing the override stay on the dict. The dispatcher also reserves room here for future selector fields without a breaking schema change. |
| `from_name`/`to_name`: `"<name>"` (paired strings) | [`meho.topology.annotate`](../../backend/src/meho_backplane/mcp/tools/topology.py), [`meho.topology.unannotate`](../../backend/src/meho_backplane/mcp/tools/topology.py), [`query_topology`](../../backend/src/meho_backplane/mcp/tools/topology.py) (kind=`path`) | These tools name **two** nodes (a directed edge pair). The two flat fields mirror Python's `(from_, to)` keyword convention (with `from_name` because `from` is a reserved word) and let the JSON Schema layer require both atomically. A nested `{from: {name}, to: {name}}` object would be ceremony for no benefit. The future-`target`-unification work does *not* roll edge tools into a single `target` field; the directed-edge intent is signalled by the field names. |

[`list_targets`](../../backend/src/meho_backplane/mcp/tools/topology.py) returns rows that carry a bare `name`; that is the value the caller passes to `call_operation` (either as a bare string or wrapped as `{name: ...}`) or to `query_topology` (as a bare string).

### Forward convention for new tools

When a new tool needs to reference a target/node by name, pick the shape that matches the tool's *role*:

1. **Write/dispatch tools that act on one target** (anything like `call_operation`) — **accept the bare-string `target` as the primary shape and document the dict alias.** Either shape is fine; bare-string is preferred for cross-tool consistency. Reserve the dict for the case where forward-compat selector room (e.g. `fqdn`, future `alias_precedence`) is needed.
2. **Read tools that filter by one target name** (anything like `query_audit`, single-anchor closure queries) — **use the bare-string `target`.** No selector room is needed; keep the schema flat.
3. **Tools that operate on an edge (two endpoints)** — **use the `from_name`/`to_name` pair.** Match the existing `meho.topology.annotate` schema verbatim so an agent carrying a node-pair through the topology surface can hand the same arguments to the next tool without renaming.

A future breaking unification (a shared `TargetRef` / `TargetSelector` model that collapses the dict variant entirely) remains a v0.7+ window decision; that decision will cite this section. Until then: **do not introduce a fourth shape**. If you find yourself reaching for one, file an Initiative-level discussion rather than landing it.

## Adding an MCP tool

For a new vendor connector op:

1. **Implement the op** in your connector's `_op_map` (per [`connectors.md`](connectors.md)).
2. **Pick the target-reference shape** per the "Target-reference shape convention" section above — bare-string `target` for read tools (and as the preferred forward shape for write/dispatch tools too; the dict shape stays accepted on `call_operation` for forward-compat selector room), `from_name`/`to_name` pair for edge tools. The example below is a single-target read.
3. **Register an MCP tool** in `backend/src/meho_backplane/mcp/tools/<product>.py`:
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
4. **Lifespan eager-import** picks it up on restart.
5. **Verify** via `tools/list` then `tools/call` against a running backplane (use `@modelcontextprotocol/inspector`).

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
- [docs/architecture/runbooks.md](runbooks.md) — the runbook substrate, where the step opacity contract that priming is the UX surface for lives ([#1198](https://github.com/evoila/meho/issues/1198), [#1314](https://github.com/evoila/meho/issues/1314)).
- [docs/runbooks/authoring.md](../runbooks/authoring.md) — the authoring-side counterpart for runbook templates ([#1299](https://github.com/evoila/meho/issues/1299)).
- [docs/codebase/tenant_conventions.md](../codebase/tenant_conventions.md) — control flow and packer arithmetic of the conventions band; [docs/architecture/conventions-seed.md](conventions-seed.md) — seed migrations and consumer-side override template.
