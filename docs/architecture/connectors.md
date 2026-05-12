# Connector architecture

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
```

Three required methods, all async. **Op-id namespace** is `<product>.<resource>.<verb>` — e.g., `vsphere.vm.list`, `vault.kv.read`, `bind9.zone.create`.

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

## Adapter shapes (planned: G0.2-T3 / T4)

> **Status:** None of the subclasses below exist yet. G0.2-T1 (ABC + result models) has landed; G0.2-T3 (HTTP adapter), G0.2-T4 (SSH adapter), and G0.2-T5 (`VaultConnector` reference refactor) are still open. The tree is the target hierarchy each vendor Initiative under [G3 (#214)](https://github.com/evoila/meho/issues/214) will register against once the adapters land.

```text
Connector (ABC)                                      ← G0.2-T1 (shipped)
├── HttpConnector       — httpx.AsyncClient pool, tenacity retry, SSL_CERT_FILE   ← G0.2-T3 (planned)
│   ├── VaultConnector  — refactor of auth/vault.py (G0.2-T5; reference impl)     ← G0.2-T5 (planned)
│   ├── VSphereConnector — vSphere REST API (G3.1)
│   ├── NSXConnector
│   ├── HarborConnector
│   └── ... (HTTP-API vendors)
└── SshConnector         — asyncssh, key+password, per-target connection pool     ← G0.2-T4 (planned)
    ├── Bind9Connector
    ├── PfsenseConnector
    └── HolodeckConnector
```

**HTTP adapter** (T3, planned): shared `httpx.AsyncClient` per target with retry on idempotent verbs only, `SSL_CERT_FILE` honored natively for the trust-bundle wiring from PR #212.

**SSH adapter** (T4, planned): asyncssh (async-native, no thread offload — beats paramiko for the event loop). Per-target connection cached; closed on lifespan teardown.

## Library choices

| Concern | Choice | Why |
|---|---|---|
| HTTP transport | `httpx` | Already in chassis, async, http2-capable, honors `SSL_CERT_FILE`. |
| Retry policy | `tenacity` (planned T3) | Exponential backoff, exception-type filtering. |
| SSH transport | `asyncssh` (planned T4) | Async-native (no `to_thread` offload). |
| Vault client | `hvac` (chassis-era) | Sync, wrapped in `asyncio.to_thread` already. |
| Vendor: vSphere | **vSphere REST API** | NOT pyvmomi (SOAP, sync). NOT govc subprocess. v0.2 returns 501 for REST gaps; v0.2.next adds a govc fallback if needed. |
| Vendor: Kubernetes | **Not yet decided** | `kubernetes_asyncio` is the lean; will be locked when the Kubernetes connector Initiative is filed under G3. |

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
