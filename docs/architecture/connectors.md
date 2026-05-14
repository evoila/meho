# Connector architecture

> ## ⚠️ Architectural correction (2026-05-14)
>
> The content below describes the **v0.2 transitional shape** that shipped with G0.2 (#223). The final substrate is defined by [CLAUDE.md](../../CLAUDE.md) postulates + two new Initiatives:
>
> - **[#388 G0.6 Operation registry + resolver + dispatcher + JSONFlux substrate](https://github.com/evoila/meho/issues/388)** — `endpoint_descriptor` + `operation_group` tables; connector registry v2 keyed on `(product, version, impl_id)`; target↔connector resolver via fingerprint; dispatcher (lookup → validate → typed-handler OR generic-httpx-from-descriptor → JSONFlux → audit → broadcast); composite-operation recursion.
> - **[#389 G0.7 Spec ingestion pipeline](https://github.com/evoila/meho/issues/389)** — OpenAPI 3.0/3.1 parser + vi-json multi-spec merge + LLM-summarised operation groups + operator review queue.
>
> Connector kinds (two, both first-class, both versioned, multi-impl per product):
> - **Generic connectors** — operations auto-derived from a vendor OpenAPI spec by G0.7 into the `endpoint_descriptor` table (`source_kind='ingested'`). Examples: `vmware-rest-9.0`, `nsx-4.2`, `sddc-manager-9.0`, `harbor-2.x`, VCF mgmt plane, `hetzner-robot-2026-04`.
> - **Typed connectors** — operations hand-coded against a vendor SDK; register into the same `endpoint_descriptor` table via `register_typed_operation()` at connector init (`source_kind='typed'`). Examples: `vault-1.x`, `k8s-1.x` (kubernetes_asyncio per decision #8), `bind9-9.x`, `pfsense-2.7`, `holodeck-9.0`.
> - **Composite operations** are a third operation kind within a connector — hand-authored handlers that orchestrate other operations via the dispatcher's recursive `dispatch(...)` call (`source_kind='composite'`).
>
> **No per-op MCP tools.** The agent surface is ~17 meta-tools per CLAUDE.md. Vendor operations reach the agent via `search_operations(connector_id, query, group?)` + `call_operation(connector_id, op_id, target?, params)`. CLI alias verbs (`meho vmware vm list / cluster list / ...`) remain as operator-friendly conveniences that internally resolve to `call_operation` against the dispatcher.
>
> The `_op_map` pattern and `<product>.<resource>.<verb>` op-id namespace described below are **v0.2 transitional**. Operations now live in `endpoint_descriptor` rows; op_ids for ingested ops are `<METHOD>:<path>` (e.g. `GET:/api/vcenter/cluster`); op_ids for typed ops keep their dotted-namespace shape (`vault.kv.read`) but are stored as table rows, not in-code dicts. The shipped Vault + K8s connectors get refactored under [#390](https://github.com/evoila/meho/issues/390) + [#391](https://github.com/evoila/meho/issues/391).
>
> Read this header first; treat the body content as the historical baseline G0.6 + G0.7 evolve from.

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

**Op-id namespace** is `<product>.<resource>.<verb>` — e.g., `vsphere.vm.list`, `vault.kv.read`, `bind9.zone.create`.

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

> **Status:** G0.2-T1 (ABC + result models), G0.2-T2 (registry), G0.2-T3 (HTTP adapter), and G0.2-T4 (SSH adapter) have landed. G0.2-T5 (`VaultConnector` reference refactor) is still open. The tree is the target hierarchy each vendor Initiative under [G3 (#214)](https://github.com/evoila/meho/issues/214) will register against.

```text
Connector (ABC)                                      ← G0.2-T1 (shipped)
├── HttpConnector       — httpx.AsyncClient pool, tenacity retry, SSL_CERT_FILE   ← G0.2-T3 (shipped)
│   ├── VaultConnector  — refactor of auth/vault.py (G0.2-T5; reference impl)     ← G0.2-T5 (planned)
│   ├── VSphereConnector — vSphere REST API (G3.1)
│   ├── NSXConnector
│   ├── HarborConnector
│   └── ... (HTTP-API vendors)
└── SshConnector         — asyncssh, key+password, per-target connection pool     ← G0.2-T4 (shipped)
    ├── Bind9Connector
    ├── PfsenseConnector
    └── HolodeckConnector
```

**HTTP adapter** (T3, shipped): shared `httpx.AsyncClient` per target with retry on idempotent verbs only, `SSL_CERT_FILE` honored natively for the trust-bundle wiring from PR #212.

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

## Op dispatch flow (target shape, once G0.2-T2 / G0.3 land)

> **Status:** today's shipped substrate is the ABC + result models. The remaining pieces — connector registry ([G0.2-T2 / PR #295, open](https://github.com/evoila/meho/pull/295)), targets-as-data ([G0.3 / #224, open](https://github.com/evoila/meho/issues/224)), `_op_map` per connector, the `/api/v1/connectors/...` route surface, and the G6 broadcast hook — are not yet wired. The flow below is the **target shape** every Initiative under #214 / #220 / #217 will land against.

For a CLI call like `meho vsphere vm.list --target rdc-vcenter`:

1. CLI → `POST /api/v1/connectors/vsphere/vm.list` body `{target: "rdc-vcenter", params: {...}}`
2. FastAPI handler → `resolve_target(operator.tenant_id, "rdc-vcenter")` (G0.3) → `Target` row.
3. → `get_connector("vsphere")` → `VSphereConnector` class.
4. → `connector.execute(target, "vsphere.vm.list", params)`.
5. Connector looks up `op_id` in its per-product `_op_map`, runs the operation against the vendor.
6. Returns `OperationResult(status="ok", result=[...], duration_ms=42.3)`.
7. `AuditMiddleware` writes one row with `tenant_id`, `target_id`, `op_id`, `params_hash`.
8. G6 broadcast publishes an event (aggregate-only for `credential_read` op-class per decision #3 in [v0.2-decisions.md](../planning/v0.2-decisions.md)).
9. MCP path mirrors this: `tools/call name="vsphere.vm.list"` dispatches via the same `_op_map`.

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
6. **Operator side:** `meho targets create --product harbor --host harbor.evba.lab ...` (or `meho targets import` from a `targets.yaml` entry).
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
