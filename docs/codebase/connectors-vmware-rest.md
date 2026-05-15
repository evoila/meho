# Connector: vmware-rest (vSphere 8.5+ / 9.0)

## Overview

The `vmware-rest` connector is the hand-rolled `HttpConnector` subclass
that dispatches ingested vCenter REST operations under the
`(product="vmware", version="9.0", impl_id="vmware-rest")` registry
triple. It pairs with the G0.7 ingestion pipeline's auto-shim (which
makes ~1,275 + ~2,195 `endpoint_descriptor` rows resolvable but not
dispatchable) to deliver real session-authenticated calls against
vSphere 8.5+ / ESXi 8.5+ targets, plus 13 hand-authored composites
that orchestrate cross-spec workflows: 5 read composites (G3.1-T5 /
#508) and 8 write composites (G3.1-T6 / #509). The write composites
cover every state-mutating operator workflow named in [#214](https://github.com/evoila/meho/issues/214)
as required for govc-wrapper retirement.

Source: `backend/src/meho_backplane/connectors/vmware_rest/`.

## Key types

- **`VmwareRestConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="vmware"`, `version="9.0"`,
  `impl_id="vmware-rest"`, `supported_version_range=">=8.5,<10.0"`,
  `priority=1`.
- **Read composites** (`composites/_read.py`) — five module-level
  `async def` handlers (`cluster_drs_recommendations_composite`,
  `event_tail_composite`, `performance_summary_composite`,
  `datastore_usage_composite`, `network_portgroup_audit_composite`).
  Each accepts `(operator, target, params, dispatch_child)` per the
  `DispatchChild` Protocol and orchestrates 1-3 sub-op dispatches
  back into the same `vmware-rest-9.0` connector. Registered with
  `safety_level="safe"` + `requires_approval=False` — read-only
  overrides of `register_composite_operation()`'s `dangerous` / `True`
  defaults.
- **Write composites** (`composites/_write.py`) — eight module-level
  `async def` handlers (`vm_create_composite`, `vm_clone_composite`,
  `vm_snapshot_revert_composite`, `vm_migrate_composite`,
  `vm_power_bulk_composite`, `host_evacuate_composite`,
  `host_detach_from_vds_composite`, `cluster_patch_composite`). Same
  `DispatchChild`-Protocol contract; each orchestrates 2-N sub-ops
  with documented status enums on the response envelope
  (`{"status": "created" | "rolled_back" | …}`). Registered with
  T4's defaults `safety_level="dangerous"` + `requires_approval=True`
  so the policy gate pops the approval queue on every dispatch.
  `host_evacuate_composite` is the first production composite that
  dispatches another composite (`vmware.composite.vm.migrate`) via
  `dispatch_child` — the recursion-depth contextvar (default cap 8)
  handles the depth-2 nesting cleanly.
- **`register_vmware_composite_operations`** (`composites/_register.py`)
  — async registrar function called from `run_typed_op_registrars` at
  lifespan startup. Iterates a single `_COMPOSITES` tuple of 13
  `_CompositeSpec` rows (5 read + 8 write); each row carries its
  own `safety_level` + `requires_approval` so the policy posture is
  implied by the spec, not by global defaults. Idempotent on re-run
  via the body-hash skip path.
- **`VsphereTargetLike`** (`session.py`) — runtime-checkable Protocol
  capturing the minimum target shape the connector reads: `name`,
  `host`, `port`, `secret_ref`, `auth_model`. Replaced by the concrete
  `Target` model once G0.3 (#224) lands; the model satisfies the
  Protocol structurally without code edits here.
- **`VsphereSessionLoader`** (`session.py`) — async callable type
  resolving a target to `{"username": ..., "password": ...}`.
  Injectable on connector construction (`VmwareRestConnector(session_loader=…)`)
  so unit tests, integration tests, and pre-G0.3 production deploys
  override the default Vault loader.
- **`load_session_credentials_from_vault`** (`session.py`) — default
  loader, stubbed `NotImplementedError` until G0.3 lands the
  operator-context Vault read path. Mirrors the
  `load_kubeconfig_from_vault` pattern in `connectors/kubernetes/`.

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` in
   `meho_backplane/connectors/registry.py`, which walks every
   `connectors/<product>/` subpackage.
2. Importing `meho_backplane.connectors.vmware_rest` triggers the
   module-level `register_connector_v2(product="vmware", version="9.0",
   impl_id="vmware-rest", cls=VmwareRestConnector)` call.
3. The same import triggers the side-effect import of
   `meho_backplane.connectors.vmware_rest.composites`, whose
   `__init__` calls
   `register_typed_op_registrar(register_vmware_composite_operations)`
   to queue the composite-row upsert onto the lifespan's registrar
   list.
4. The registry's v2 table now resolves `(vmware, 9.0, vmware-rest)`
   to `VmwareRestConnector`. The G0.7 auto-shim's idempotency check
   (in `ensure_connector_class_registered`, once #408's pipeline lands
   in main) no-ops on subsequent ingests against the same triple.
5. Lifespan calls `run_typed_op_registrars()`, which iterates every
   queued registrar -- including the composite one -- and upserts the
   13 `vmware.composite.*` rows into `endpoint_descriptor` with
   `source_kind="composite"` (5 reads with `safety_level="safe"` +
   `requires_approval=False`; 8 writes with `safety_level="dangerous"`
   + `requires_approval=True`).

### Per-target session

1. First call to `auth_headers(target, raw_jwt)` against a target whose
   name isn't in `self._session_tokens`:
   a. Acquires `self._session_lock` (asyncio.Lock).
   b. Calls `self._session_loader(target)` for the service-account
      credentials.
   c. POSTs `/api/session` with HTTP basic auth (creds["username"],
      creds["password"]).
   d. Parses the response body: a JSON-quoted string (vSphere 7.0+
      modern shape) or `{"value": "<token>"}` (pre-7.0 legacy shape;
      kept for vcsim cross-version compatibility).
   e. Caches the token under `target.name`.
2. Subsequent calls take the fast path: lock acquisition + cache hit +
   return.
3. The dispatcher's call path
   (`HttpConnector._request_json` / `_post_json`) reads
   `auth_headers()`, gets `{"vmware-api-session-id": "<token>"}`, sends
   it on every dispatched op against this target.

### fingerprint() / probe()

`fingerprint(target)` issues `GET /api/about` (auth headers injected
lazily via the cached session token); the response payload populates
the canonical `FingerprintResult`:

- `vendor="vmware"`
- `product` — via `product_from_line_id(payload.product_line_id)`
  (`vpx` → `vcenter`; `embeddedEsx`/`esx` → `esxi`; fall-through for
  unknown values; `""`/`None` → `"unknown"`)
- `version`, `build`, `edition` — straight from the payload
- `extras` — `uuid`, `full_name`, `product_line_id`, `api_type`,
  `os_type`

`probe(target)` delegates to `fingerprint()` and folds the boolean
reachable flag into a `ProbeResult`. Failure modes (TCP `ConnectError`,
TLS error, 401 from `/api/session`, 5xx from `/api/about`) surface as
`reachable=False` with the exception class + message in
`extras["error"]` / `ProbeResult.reason`.

### aclose()

1. Snapshot the cached session tokens, clear the dict.
2. For each `(target_name, token)` pair: issue `DELETE /api/session`
   with the `vmware-api-session-id` header. A failure (5xx, transport
   error, 401 from an expired session) is logged via structlog
   `vsphere_session_revoke_failed` / `vsphere_session_revoke_non_2xx`
   but doesn't block shutdown — Kubernetes' 30 s
   `terminationGracePeriod` would otherwise be at risk.
3. Delegate to `super().aclose()` to close the per-target httpx
   clients.

### execute()

Legacy shim — synthesises a system-tenant `Operator` and calls
`meho_backplane.operations.dispatch(...)` against the
`connector_id="vmware-rest-9.0"` encoding. Post-G0.6 callers
(`/api/v1/operations/call`, MCP `call_operation`, CLI verbs from #511)
construct a real `Operator` and call `dispatch()` directly; they don't
reach this method.

### Composite dispatch

The 13 composites (5 reads + 8 writes) land as `source_kind="composite"`
rows in `endpoint_descriptor`. At dispatch time:

1. Dispatcher resolves `(vmware-rest-9.0, vmware.composite.<verb>)`
   to the row, sees `source_kind="composite"`, builds a
   `DispatchChild` callable via
   `get_dispatch_child(dispatch=dispatch, parent_operator=...,
   parent_target=..., parent_audit_id=..., parent_op_id=...)`.
2. Handler is resolved via `import_handler(descriptor.handler_ref)`
   to one of the module-level functions in `composites/_read.py` or
   `composites/_write.py`.
3. Dispatcher invokes
   `handler(operator=..., target=..., params=..., dispatch_child=...)`.
4. Handler issues N `await dispatch_child(connector_id="vmware-rest-9.0",
   op_id=..., params=...)` calls. Each child dispatch:
   - Inherits `parent_audit_id` via the contextvar so the child's
     audit row's `parent_audit_id` column is set automatically.
   - Increments `composite_depth_var` (bounded at
     `Settings.composite_max_depth=8`; over-depth raises
     `CompositeRecursionLimitExceeded` pre-recursion).
   - Re-enters the dispatcher's same code path -- the child sub-op
     hits the `source_kind="ingested"` branch which routes through
     `VmwareRestConnector.execute()` for the actual HTTP call.
5. Handler aggregates the sub-op responses into a single dict and
   returns; the dispatcher wraps it as an `OperationResult` with
   `status="ok"` and `result=<aggregated dict>`.

The composite handlers go through `dispatch_child` rather than
calling `_request_json` directly so the audit-tree linkage,
bounded-recursion guard, policy gate, broadcast publish, and
parameter-schema validation all run on every sub-call.

### Recursive composite dispatch (`host.evacuate` → `vm.migrate`)

`host_evacuate_composite` is the first production composite that
calls another composite via `dispatch_child`. Two-level nesting:

```
host.evacuate                           # depth 0 (top-level dispatch)
  └─ vmware.composite.vm.migrate (× N) # depth 1 (dispatch_child of a composite)
       ├─ GET:/vcenter/cluster/{c}/drs/recommendations  # depth 2 (typed sub-op)
       └─ POST:/vcenter/vm/{vm}                         # depth 2 (typed sub-op)
  └─ PATCH:/vcenter/host/{host}/maintenance             # depth 1 (typed sub-op)
```

`composite_depth_var` (default cap 8) handles this naturally. The
audit log shows a 3-level tree per `host.evacuate` dispatch: one
parent row, N `vm.migrate` child rows, each with two grandchild rows
(DRS lookup + relocate). The substrate guard's coverage in
`tests/test_operations_composite.py` proves the depth-cap behaviour
holds; this connector's recursive composite is the first production
caller.

### Write-composite partial-failure conventions

Write composites return a structured `{"status": ...}` envelope so
callers can branch on `status` without parsing free-form prose. The
status alphabets per composite (from each handler's `response_schema`
enum) are:

| Composite | Status values |
| --- | --- |
| `vm.create` | `created`, `rolled_back` |
| `vm.clone` | `completed`, `pending`, `timeout` |
| `vm.snapshot.revert` | `reverted`, `ambiguous`, `not_found` |
| `vm.migrate` | `migrated`, `no_recommendation` |
| `vm.power.bulk` | (per-VM `results` + aggregate `summary` + `aborted_on_failure`) |
| `host.evacuate` | `evacuated`, `partial`, `aborted` |
| `host.detach_from_vds` | `detached`, `incomplete` |
| `cluster.patch` | `completed`, `stopped` |

`vm.create` is the only composite that issues a compensating
mutation (`DELETE:/vcenter/vm/{vm}`) on partial failure. The other
write composites prefer "stop and report" semantics over silent
rollback -- the operator decides whether to manually finish or
revert.

## Dependencies

- `meho_backplane.connectors.adapters.http.HttpConnector` (G0.2-T3
  #242) — transport plumbing (retry, timeout, per-target pool,
  `_request_json` / `_post_json`).
- `meho_backplane.connectors.registry.register_connector_v2` (G0.6-T2
  #393).
- `meho_backplane.connectors.schemas` — `AuthModel`,
  `FingerprintResult`, `OperationResult`, `ProbeResult`.
- `meho_backplane.operations.dispatch` (G0.6-T5 #396) — invoked by
  `execute()`'s legacy shim.
- `httpx` (transitively via `HttpConnector`).
- `structlog` for structured log events.
- Test-only: `respx` for HTTP mocking in unit tests, `testcontainers`
  for the vcsim-backed integration test.

## Known issues / gaps

- **Default loader stubbed** — `load_session_credentials_from_vault`
  raises `NotImplementedError` until G0.3 (#224) lands. Production
  deploys must inject a custom `session_loader` at connector
  construction. Same pattern as `KubernetesConnector(kubeconfig_loader=…)`.
- **`auth_model` enum gating** — only `shared_service_account` (and
  `None` for pre-G0.3 targets) is accepted. `per_user` and
  `impersonation` raise `NotImplementedError`; both are deferred to
  v0.2.next.
- **No proactive 401 retry** — vSphere's ~5-minute idle timeout means
  a long-idle connection may see a 401 on the next dispatch. The
  caller-side retry logic in `_request_json` does not retry 401 by
  policy; an explicit refresh loop is v0.2.next polish.
- **`vi-json.yaml` ingestion live** — T3 (#503) shipped the ingestion
  pipeline (depends on T2 / #501's `$ref` resolver). The same
  connector dispatches the ~2,195 vi-json ops alongside the ~1,275
  vCenter REST ops; both share the `vmware-api-session-id` session
  header per `docs/vcenter-9.0/MANIFEST.md`. Two of the read
  composites (`event.tail`, `performance.summary`) call vi-json
  sub-ops; the other three call vCenter REST sub-ops only.
- **All 13 composites shipped** — T5 (#508) ships the 5 read
  composites; T6 (#509) ships the 8 write composites. The "All ~13
  hand-authored composites land as endpoint_descriptor rows with
  source_kind='composite'" Definition-of-done line in [#227](https://github.com/evoila/meho/issues/227)
  is fully ticked.
- **`vm.clone` task polling is wall-clock bounded** — the composite
  blocks up to `timeout_seconds` (default 600s) before returning
  `status='timeout'`. The vSphere task may still complete in the
  background; callers should poll `GET:/cis/tasks/{task}` if
  long-running deploys are normal. An async-task-tracking substrate
  is v0.2.next.
- **Per-VM rollback for `vm.power.bulk` is by design absent** — bulk
  power operations are intentionally non-transactional. Partial-
  failure tolerance is the documented contract; a transactional
  bulk-power would require vSphere-side support that doesn't exist.
- **`cluster.patch` sequential, not concurrent** — concurrent host
  patches would overwhelm DRS by forcing every VM in the cluster to
  vMotion at once. The composite serialises hosts and lets DRS
  rebalance between iterations.

## References

- Parent Initiative: [#227 G3.1 vmware-rest-9.0](https://github.com/evoila/meho/issues/227)
- Parent Task: [#498 G3.1-T1 VmwareRestConnector](https://github.com/evoila/meho/issues/498)
- Composite-helper Task: [#504 G3.1-T4 register_composite_operation()](https://github.com/evoila/meho/issues/504)
- Read-composite Task: [#508 G3.1-T5 vmware-rest read composites](https://github.com/evoila/meho/issues/508)
- Write-composite Task: [#509 G3.1-T6 vmware-rest write composites](https://github.com/evoila/meho/issues/509)
- Composite recursion substrate: [#398 G0.6-T7 composite recursion infrastructure](https://github.com/evoila/meho/issues/398)
- G0.7 canary that ingested the rows this connector dispatches:
  [#408 G0.7-T8 vSphere canary](https://github.com/evoila/meho/issues/408)
  (closed via PR #493 on 2026-05-15).
- vSphere REST session contract:
  [vSphere Automation API security schema](https://developer.broadcom.com/xapis/vsphere-automation-api/latest/api-security-schema/).
- vcsim simulator: <https://github.com/vmware/govmomi/tree/main/vcsim>.
- Closest in-repo precedents:
  - Package layout + v2 registration pattern:
    `backend/src/meho_backplane/connectors/vault/__init__.py`.
  - Injectable-loader Protocol pattern:
    `backend/src/meho_backplane/connectors/kubernetes/kubeconfig.py`.
  - `auth_headers` + `_request_json` HTTP plumbing:
    `backend/src/meho_backplane/connectors/adapters/http.py`.
