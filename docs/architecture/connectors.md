# Connector architecture

> ## ⚠️ Architectural correction (2026-05-14)
>
> The content below describes the **v0.2 transitional shape** that shipped with G0.2 (#223). The substrate that supersedes it has landed:
>
> - **[operations-substrate.md](operations-substrate.md)** — canonical reference for the G0.6 substrate as-shipped: `endpoint_descriptor` + `operation_group` tables, the v2 connector registry, the dispatcher's eight-phase pipeline, composite recursion + audit-tree linkage, the JSONFlux reducer Protocol, and the three operation meta-tools. Read this first; the content below remains as historical baseline.
> - **[connector-resolution.md](connector-resolution.md)** — the resolver's tie-break ladder with three worked examples.
> - **[spec-ingestion.md](spec-ingestion.md)** — canonical reference for the G0.7 spec-ingestion pipeline as-shipped: OpenAPI 3.0/3.1 parser, `register_ingested_operations()` upsert + multi-spec merge + body-hash skip, two-pass LLM-summarised operation groups (`1 + ceil(N/50)` call budget), operator review-queue state machine (`staged → enabled → disabled`), the seven `meho connector ...` CLI verbs, `/api/v1/connectors*` REST routes, and the `meho.connector.*` admin MCP tools. Companion operator runbook: [`docs/cross-repo/connector-ingestion.md`](../cross-repo/connector-ingestion.md).
>
> Connector kinds (three, all first-class, all versioned, multi-impl per product):
> - **Generic (ingested) connectors** — operations auto-derived from a vendor OpenAPI spec by G0.7 into `endpoint_descriptor` rows (`source_kind='ingested'`). Examples planned: `vmware-rest-9.0`, `nsx-4.2`, `harbor-2.x`, `hetzner-robot-2026-04`.
> - **Typed connectors** — operations hand-coded against a vendor SDK; register into `endpoint_descriptor` via `register_typed_operation()` driven by `register_typed_op_registrar()` + the lifespan-run registrar list (`source_kind='typed'`). Shipped: `vault-1.x`, `k8s-1.x` (kubernetes_asyncio per decision #8). Planned: `bind9-9.x`, `pfsense-2.7`, `holodeck-9.0`.
> - **Composite operations** — hand-authored handlers that orchestrate other operations via the dispatcher's recursive sub-call (`source_kind='composite'`), with bounded recursion and audit-tree linkage.
>
> **No per-op MCP tools.** The agent surface is ~17 meta-tools per CLAUDE.md. Vendor operations reach the agent via `search_operations(connector_id, query, group?)` + `call_operation(connector_id, op_id, target?, params)`. CLI alias verbs (`meho vmware vm list / cluster list / ...`) remain as operator-friendly conveniences that internally resolve to `call_operation` against the dispatcher.
>
> The `_op_map` pattern and `<product>.<resource>.<verb>` op-id namespace described below are **v0.2 transitional**. Operations now live in `endpoint_descriptor` rows; op_ids for ingested ops are `<METHOD>:<path>` (e.g. `GET:/api/vcenter/cluster`); op_ids for typed ops keep their dotted-namespace shape (`vault.kv.read`) but are stored as table rows, not in-code dicts. The shipped Vault + K8s connectors were refactored under [#390](https://github.com/evoila/meho/issues/390) + [#391](https://github.com/evoila/meho/issues/391).
>
> Read [operations-substrate.md](operations-substrate.md) first; treat the body content below as the historical baseline G0.6 + G0.7 evolved from.

---

How MEHO talks to the systems it governs. The substrate landed in [G0.2 (#223)](https://github.com/evoila/meho/issues/223); every connector implementation under [G3 (#214)](https://github.com/evoila/meho/issues/214) inherits from this contract.

## Why a connector ABC

The v0.1 chassis had vendor-specific modules ([`auth/jwt.py`](../../backend/src/meho_backplane/auth/jwt.py) for Keycloak, [`auth/vault.py`](../../backend/src/meho_backplane/auth/vault.py) for Vault) with no shared shape. v0.2 generalises so the ~14 connectors planned for v0.2 don't ship as 14 one-offs:

- Same fingerprint contract across every vendor (consumer-needs.md §G3 L94–99).
- Same probe semantics → reusable in the readiness-probe registry.
- Same op-id namespace → uniform audit shape, MCP tool registration, broadcast classification.
- Same per-target identity-model field → operators pick `impersonation` / `shared_service_account` / `per_user` per target.

## The ABC

[`backend/src/meho_backplane/connectors/base.py`](../../backend/src/meho_backplane/connectors/base.py):

```python
class Connector(ABC):
    product: str  # subclass sets: "vsphere", "vault", "bind9", ...

    @abstractmethod
    async def fingerprint(self, target: Target) -> FingerprintResult: ...

    @abstractmethod
    async def probe(self, target: Target) -> ProbeResult: ...

    @abstractmethod
    async def execute(
        self, target: Target, op_id: str, params: dict[str, Any]
    ) -> OperationResult: ...

    # G9.1-T2 (#449) — topology discovery hooks. Non-abstract; defaults
    # return empty hints so every shipped v1 subclass remains compilable
    # without modification. Per-product overrides land in G3.x.
    async def discover_topology(self, target: Target) -> TopologyHints: ...

    async def list_candidates(
        self, seed_target: Target | None = None
    ) -> list[CandidateHint]: ...
```

Three required methods (`fingerprint` / `probe` / `execute`) plus two **non-abstract** topology hooks (G9.1-T2 #449). The topology hooks ship base-class defaults so every v1 subclass (VaultConnector #244, KubernetesConnector skeleton #321) keeps working without code change; per-product overrides land in the G3.x Initiatives.

**Op-id namespace** depends on source kind: typed ops use `<product>.<resource>.<verb>` (e.g., `vault.kv.read`, `bind9.zone.create`), while ingested ops use `<METHOD>:<path>` (e.g., `GET:/api/vcenter/cluster`). The `Connector.execute` docstring at [`base.py`](../../backend/src/meho_backplane/connectors/base.py) is the source-of-truth contract.

The G9.1-T3 refresh service calls `discover_topology(target)` on demand + on schedule and diffs the returned `TopologyHints` against `graph_node` + `graph_edge` rows for the same `(tenant_id, target_id)`. The G9.1-T6 `meho targets discover` verb calls `list_candidates(seed_target)` and surfaces candidates to the operator — auto-registration is intentionally out of scope.

## Result models

[`backend/src/meho_backplane/connectors/schemas.py`](../../backend/src/meho_backplane/connectors/schemas.py) — every model frozen, `extras` wrapped in `MappingProxyType` so in-place mutation also raises.

### `FingerprintResult` (consumer-needs L95 shape)

```python
class FingerprintResult(BaseModel):
    vendor: str
    product: str
    version: str | None
    build: str | None
    edition: str | None
    reachable: bool
    probed_at: datetime
    probe_method: str       # e.g. "GET /api/about" for vSphere REST
    extras: Mapping[str, Any]
```

Cached server-side keyed by `target.name`. Used by version-tagged doc/kb shelf lookup, operation-set selection, MCP capability advertisement.

### `ProbeResult`

```python
class ProbeResult(BaseModel):
    ok: bool
    reason: str | None
    latency_ms: float | None
    probed_at: datetime
```

Lightweight reachability + auth-challenge. Reused as a readiness probe in the [chassis registry](../../backend/src/meho_backplane/health.py).

### `OperationResult`

```python
class OperationResult(BaseModel):
    status: str            # "ok" | "error" | "denied"
    op_id: str
    result: dict | list | None
    error: str | None
    duration_ms: float
    extras: Mapping[str, Any]
```

Raw response in `result`. JSONFlux result-handle wrapping for large sets is an external concern (set-shaped ops > 20 rows return a handle reference; full set fetched via a follow-up op).

### `AuthModel` (per-target identity model per v0.1-spec L447–454)

```python
class AuthModel(StrEnum):
    IMPERSONATION = "impersonation"               # forward operator JWT (vSphere SSO etc.)
    SHARED_SERVICE_ACCOUNT = "shared_service_account"  # single SA per connector (Vault, bind9, NSX)
    PER_USER = "per_user"                         # per-operator cred in Vault (Harbor etc.)
```

Stored on every `Target` row (G0.3); read by the connector at `execute()` time.

### Topology hint models (G9.1-T2 #449)

The four models a connector populates when overriding `discover_topology` + `list_candidates`. All frozen end-to-end (matching the `FingerprintResult.extras` discipline — nested `properties` / `evidence` are wrapped in `MappingProxyType` so in-place mutation also raises).

```python
NodeKind = Literal[
    "target", "vm", "host", "network", "datastore", "namespace",
    "pod", "service", "ingress", "node", "principal",
    "vault-role", "vault-mount", "volume",
]  # auto-discoverable subset; curated kinds land in G9.2

EdgeKind = Literal["runs-on", "mounts", "routes-through", "belongs-to"]
# cross-system semantic edges ("authenticates-via", "depends-on", ...)
# need operator assertion and land in G9.2

class NodeHint(BaseModel):
    kind: NodeKind
    name: str
    properties: Mapping[str, Any]  # frozen via MappingProxyType

class EdgeHint(BaseModel):
    from_kind: NodeKind
    from_name: str
    to_kind: NodeKind
    to_name: str
    kind: EdgeKind
    properties: Mapping[str, Any]

class TopologyHints(BaseModel):
    nodes: tuple[NodeHint, ...]   # tuple so the frozen model is deeply immutable
    edges: tuple[EdgeHint, ...]
    discovered_at: datetime       # stamped at call time

class CandidateHint(BaseModel):
    name: str
    host: str
    port: int | None
    evidence: Mapping[str, Any]   # debugging payload — what made the connector think this candidate exists
    confidence: Literal["high", "medium", "low"]
```

`NodeKind` is the v0.2 auto-discoverable vocabulary; per-connector kinds (e.g. `"vault-role"`, `"vault-mount"`) are extensible per Initiative #363 via a migration. `EdgeKind` is intentionally narrow — the four high-confidence probe-derived edges. Probe-derived edges have to be high-confidence because a wrong edge in `topology dependents` output misleads the operator on the very op the verb is supposed to make safer.

## Adapter shapes (G0.2-T3 / T4)

> **Status:** G0.2-T1 (ABC + result models), G0.2-T2 (registry), G0.2-T3 (HTTP adapter), G0.2-T4 (SSH adapter), and G0.2-T5 (`VaultConnector` reference impl, #244) have landed. The `VaultConnector` was subsequently refactored under [G0.6-T-Refactor-Vault (#390)](https://github.com/evoila/meho/issues/390) to register via `register_connector_v2` + `register_typed_operation()` and to route `execute()` through the G0.6 dispatcher. The tree is the target hierarchy each vendor Initiative under [G3 (#214)](https://github.com/evoila/meho/issues/214) will register against.

```text
Connector (ABC)                                      ← G0.2-T1 (shipped)
├── HttpConnector       — httpx.AsyncClient pool, tenacity retry, SSL_CERT_FILE   ← G0.2-T3 (shipped)
│   ├── VaultConnector  — auth/vault.py wrapper (G0.2-T5; reference impl)         ← refactored under G0.6-T-Refactor-Vault #390
│   ├── VSphereConnector — vSphere REST API (G3.1)
│   ├── NSXConnector
│   ├── HarborConnector
│   └── ... (HTTP-API vendors)
└── SshConnector         — asyncssh, key+password, per-target connection pool     ← G0.2-T4 (shipped)
    ├── Bind9Connector
    ├── PfsenseConnector
    └── HolodeckConnector
```

**HTTP adapter** (T3, shipped): shared `httpx.AsyncClient` per target with retry on idempotent verbs only. TLS trust is per-target (#1774): a `verify_tls=True` target (the default) is built with **no** `verify=` argument so `SSL_CERT_FILE` is honored natively for the trust-bundle wiring from PR #212 — byte-identical to a connector with no TLS opt-out; a `verify_tls=False` target is built with a module-cached insecure `SSLContext` (`check_hostname` off, `CERT_NONE`) so it can reach a self-signed / internal-CA appliance. The opt-out is per-target, off by default, and loud (WARN at construction + audit row from the create/update path). The client pool key is the tenant-unique `(tenant_id, id)` (`target_cache_key`) plus the `verify_tls` dimension (`extra_cache_dimensions`) → `(tenant_id, id, verify_tls)`, so a PATCH that flips the flag is not served the stale client and the `(tenant_id, id)` tenant-isolation prefix (#1682/#1642) is unchanged.

**SSH adapter** (T4, shipped): asyncssh (async-native, no thread offload — beats paramiko for the event loop). Per-target connection cached in `SshConnector._connections`; `known_hosts=None` for v0.2 (host-key pinning deferred to v0.2.next). Auth reads `target.secret_ref`: `ssh_private_key` → key auth, `password` → password auth fallback. Closed on lifespan teardown via `aclose()`.

## Library choices

| Concern | Choice | Why |
|---|---|---|
| HTTP transport | `httpx` | Already in chassis, async, http2-capable, honors `SSL_CERT_FILE`. |
| Retry policy | `tenacity` | Exponential backoff, exception-type filtering. Shipped T3. |
| SSH transport | `asyncssh` | Async-native (no `to_thread` offload). Shipped T4. |
| Vault client | `hvac` (chassis-era) | Sync, wrapped in `asyncio.to_thread` already. |
| Vendor: vSphere | **vSphere REST API** | NOT pyvmomi (SOAP, sync). NOT govc subprocess. v0.2 returns 501 for REST gaps; v0.2.next adds a govc fallback if needed. |
| Vendor: Kubernetes | **`kubernetes_asyncio`** (locked [decision #8](../planning/v0.2-decisions.md)) | Async fork of the official Python client; mature; broad API coverage; loads kubeconfig from a dict — direct fit for the consumer's `kubeconfig`-in-Vault flow. Rejected: `kr8s` (smaller surface) and `kubectl` subprocess (per-op cost, harder to test). Skeleton landed in [G3.2-T1 (#321)](https://github.com/evoila/meho/issues/321) under [`backend/src/meho_backplane/connectors/kubernetes/`](../../backend/src/meho_backplane/connectors/kubernetes/). |

## The registry

Module-level dict in `connectors/registry.py` (G0.2-T2, PR #295 open). Mirrors the chassis [`register_probe`](../../backend/src/meho_backplane/health.py) pattern:

```python
register_connector("vsphere", VSphereConnector)
get_connector("vsphere")  # → VSphereConnector class
```

Eager-imported at app startup via the FastAPI `lifespan` so module-side `register_connector(...)` calls land before the first request. Duplicate registration raises `RuntimeError` — programmer bug, surfaces at boot, never at request time.

## Op dispatch flow (G0.6 substrate)

> **Status:** the G0.6 operation substrate (Initiative #388) is the canonical dispatch surface. The earlier v1 chassis route `POST /api/v1/connectors/{product}/{op_id}` from G0.2-T6 was deprecated and removed by G0.6-T11 (#412); see [operations-substrate.md](operations-substrate.md) for the full dispatcher path.

For an operator call like `meho operation call vmware-rest-9.0 vm.list --target rdc-vcenter` (CLI verb is the upcoming follow-up to G0.6-T8; the API route is live today):

1. CLI / MCP / direct HTTP → `POST /api/v1/operations/call` body `{connector_id: "vmware-rest-9.0", op_id: "vm.list", target: {"name": "rdc-vcenter"}, params: {...}}`.
2. FastAPI handler → `operations.dispatch(operator, connector_id, op_id, target, params)`.
3. → `resolve_target(operator.tenant_id, "rdc-vcenter")` (G0.3) → `Target` row.
4. → `resolve_connector(target)` against the v2 registry (`(product, version, impl_id)` keyed) → `VSphereConnector` class instance.
5. → `endpoint_descriptor` lookup by `(connector_id, op_id)`, `parameter_schema` validation, policy gate.
6. → Branch on `source_kind`: `ingested` builds the HTTP request from `method` + `path`, `typed` resolves `handler_ref` to a callable, `composite` runs the handler with a recursive `dispatch` callable.
7. → JSONFlux reducer wraps the response (pass-through default in v0.2).
8. Returns `OperationResult(status="ok", result=[...], duration_ms=42.3)`.
9. `AuditMiddleware` writes one row with `tenant_id`, `target_id`, `op_id`, `params_hash`, plus any `parent_audit_id` for composite-child dispatches.
10. G6 broadcast publishes an event (aggregate-only for `credential_read` op-class per decision #3 in [v0.2-decisions.md](../planning/v0.2-decisions.md)).
11. MCP path mirrors this: `tools/call name="call_operation"` dispatches through the same substrate. Vendor-specific identifiers (`vmware-rest-9.0`, `vm.list`) live in arguments, never in the MCP tool surface — see [CLAUDE.md](../../CLAUDE.md) postulate 5.

## Adding a new connector

Once G0.2 fully lands:

1. **Create the package** at `backend/src/meho_backplane/connectors/<product>/`.
2. **Implement the subclass** in `connector.py`:
   ```python
   class HarborConnector(HttpConnector):
       product = "harbor"

       async def fingerprint(self, target): ...
       async def probe(self, target): ...
       async def execute(self, target, op_id, params): ...

       _op_map = {
           "harbor.project.list": _harbor_project_list,
           "harbor.repository.list": _harbor_repository_list,
       }
   ```
3. **Register at module top** in `__init__.py`:
   ```python
   from meho_backplane.connectors.registry import register_connector
   from .connector import HarborConnector
   register_connector("harbor", HarborConnector)
   ```
4. **Register MCP tools** per operation in `mcp/tools/harbor.py` (the registry from G0.5):
   ```python
   register_mcp_tool(
       ToolDefinition(
           name="harbor.project.list",
           description="List Harbor projects accessible to the operator. ...",
           inputSchema={"type": "object", "properties": {...}},
           required_role=TenantRole.OPERATOR,
           op_class="read",
       ),
       handler=_harbor_project_list_mcp_handler,
   )
   ```
5. **Restart.** Lifespan eager-import picks up both the connector and the MCP tools.
6. **Operator side:** register the target by importing a `targets.yaml` entry — `meho targets import targets.yaml` (see the cross-repo onboarding docs for the descriptor shape).
7. **Verify:** `meho targets probe harbor-evba` → fingerprint. `meho harbor project.list` → ops.

### As an Initiative

For team workflow:

- File an Initiative under [#214](https://github.com/evoila/meho/issues/214) — `G3.x Initiative: <Product> connector`.
- ~6 Tasks per connector: probe + fingerprint, auth flow, 4–6 high-frequency operations, MCP tool registrations, vendor-simulator integration test (vcsim for vSphere; SSH container for SSH connectors).

## What's intentionally out of scope

- **Streaming / long-running ops** — v0.2 returns a single `OperationResult`; progress notifications + cancellation are v0.2.next.
- **Raw command passthrough** (`pyvmomi.call(...)`, `kubectl(args)`) — implementation-coupling + policy-tractability problems. Coverage gaps that auto-derivation + hand-written ops can't fill produce a structured `OperationResult(status="error", error="unsupported_op")` that becomes a backlog ticket.
- **Runbooks (multi-step composition)** — deferred to v0.2.next as a thin layer above `execute()`.
- **Custom JSON-Schema-shaped param overrides per operator** — v0.2 ships the param schemas the op_map declares; tenant-specific tweaks are out of scope.

## References

- v0.1-spec §"Versioned connectors + targets" L267–292 — fingerprint cache, target as instance.
- v0.1-spec §"Operations" L294–311 — three sources (auto-derived OpenAPI primary, hand-written secondary, runbooks tertiary).
- v0.1-spec §"Per-target identity model" L447–454 — the AuthModel enum origin.
- Consumer-needs.md §G3 L72–101 — contract requirements + connector tier priority.
- [docs/planning/v0.2-decisions.md](../planning/v0.2-decisions.md) — locked decisions (track count, library choices, sequencing).
- [docs/architecture/mcp.md](mcp.md) — the parallel MCP-tool registry every operation also registers against.
