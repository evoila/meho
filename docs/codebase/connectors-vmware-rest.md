# Connector: vmware-rest (vSphere 8.5+ / 9.0)

## Overview

The `vmware-rest` connector is the hand-rolled `HttpConnector` subclass
that dispatches ingested vCenter REST operations under the
`(product="vmware", version="9.0", impl_id="vmware-rest")` registry
triple. It pairs with the G0.7 ingestion pipeline's auto-shim (which
makes ~1,275 + ~2,195 `endpoint_descriptor` rows resolvable but not
dispatchable) to deliver real session-authenticated calls against
vSphere 8.5+ / ESXi 8.5+ targets, plus 15 hand-authored composites
that orchestrate cross-spec workflows: 7 read composites
(G3.1-T5 / `#508` shipped 5; `#2080` added `host.network_uplinks`;
`#2135` added `host.vsan_health`) and 8 write composites
(G3.1-T6 / `#509`). The
write composites cover every state-mutating operator workflow named
in [#214](https://github.com/evoila/meho/issues/214) as required for
govc-wrapper retirement.

Source: `backend/src/meho_backplane/connectors/vmware_rest/`.

## Key types

- **`VmwareRestConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="vmware"`, `version="9.0"`,
  `impl_id="vmware-rest"`, `supported_version_range=">=8.5,<10.0"`,
  `priority=1`.
- **Read composites** (`composites/_read.py`) — seven module-level
  `async def` handlers (`cluster_drs_recommendations_composite`,
  `event_tail_composite`, `performance_summary_composite`,
  `datastore_usage_composite`, `network_portgroup_audit_composite`,
  `host_network_uplinks_composite`, `host_vsan_health_composite`).
  Since `#2253` each accepts `(operator, target, params, connector)`
  and issues its 1-3 sub-ops **directly on the resolved connector
  session** — `connector._get_json` / `connector._post_json` mounted
  through `connector.mount_op_path` (`_read_sub_op`) — with **no**
  `dispatch_child`, **no** ingested `endpoint_descriptor` lookup, and
  **no** L2 pre-flight, so the read composites work on a fresh boot
  with zero vCenter-catalog ingest (the `composite_l2_missing` defect
  class, consumer signal 20, is gone for reads). The `connector` kwarg
  is the substrate `#2251` added to the composite handler contract; the
  dispatcher forwards the instance it already resolved for the
  composite's target. The direct path drops two of `dispatch_child`'s
  four `#508` guarantees (bounded recursion, per-sub-op param
  validation) and relocates the other two (audit-tree linkage collapses
  to the top-level composite audit row; per-sub-op policy/broadcast is
  the top-level op's). The two dropped guarantees are acceptable for
  **reads**; the relocated per-sub-op policy gate is load-bearing for
  **writes**, so the write composites (`#2256`) re-apply it per governed
  write sub-call through the reusable
  `operations.composite.enforce_subop_policy` seam (`#2254`) — see the
  write-composites bullet. Registered with `safety_level="safe"` +
  `requires_approval=False` — read-only overrides of
  `register_composite_operation()`'s `dangerous` / `True` defaults.
  `host_network_uplinks_composite` (`#2080`) lists hosts via
  `GET:/vcenter/host`, then per host reads `config.network.pnic` +
  `config.network.proxySwitch` through the vi-json PropertyCollector
  `RetrievePropertiesEx` method — the pnic link-state / uplink mapping
  the plain REST surface cannot reproduce (drives physical
  switch-port-occupancy reasoning); the per-host property read is
  best-effort (a failed read nulls the network detail with a
  `read_note` rather than sinking the composite).
  `host_vsan_health_composite` (`#2135`) queries the vSAN
  health-service vmomi method `VsanQueryVcClusterHealthSummary` on the
  `vsan-cluster-health-system` singleton, scoped to the target
  cluster's MoRef — the `govc vsan.health.*` equivalent, returning the
  cluster-wide `overall_health` colour plus the health-test `groups`
  list. It is likewise best-effort (a failed health-service read nulls
  `groups` / `overall_health` with a `read_note`).
- **Write composites** (`composites/_write.py`) — eight module-level
  `async def` handlers (`vm_create_composite`, `vm_clone_composite`,
  `vm_snapshot_revert_composite`, `vm_migrate_composite`,
  `vm_power_bulk_composite`, `host_evacuate_composite`,
  `host_detach_from_vds_composite`, `cluster_patch_composite`). Since
  `#2256` each accepts `(operator, target, params, connector)` and
  issues its raw-REST sub-ops **directly on the resolved connector
  session** — `connector._get_json` (`_read_sub_op`) for the resolution
  reads, `connector._post_json` (`_write_sub_op`) for the mutating
  writes, both mounted through `connector.mount_op_path` — with **no**
  ingested `endpoint_descriptor` lookup and **no** L2 pre-flight, so the
  write composites also work on a fresh boot with zero catalog ingest.
  Each orchestrates 2-N sub-ops with documented status enums on the
  response envelope (`{"status": "created" | "rolled_back" | …}`).
  Registered with T4's defaults `safety_level="dangerous"` +
  `requires_approval=True` so the **top-level** composite pops the
  approval queue on every dispatch — that top-level gate stays the single
  primary approval decision.

  **Preserving write governance on the direct path.** Because a direct
  session call bypasses the dispatcher, each mutating sub-call first
  routes through `operations.composite.enforce_subop_policy` (`#2254`)
  before `_post_json` fires: the seam re-runs the same `policy_gate`
  against an in-memory descriptor carrying the sub-op's declared
  governance (`safety_level="dangerous"`, `requires_approval=False`) and
  returns an `awaiting_approval` / `denied` `OperationResult` when the
  gate does not clear. The handler returns that verbatim (the dispatcher
  passes a handler-returned `OperationResult` straight through), so an
  internal write **queues** or is **denied** instead of executing
  un-gated — property 3 of `#508`'s four guarantees preserved. The
  sub-op posture is `requires_approval=False` on purpose: flooring it to
  `True` would double-gate the approval-resume path (the resume re-runs
  the handler with the top-level gate satisfied, but the seam is not
  resume-aware). A human/service operator whose composite was already
  approved therefore auto-executes each write; an agent without a grant
  is denied a `dangerous` sub-op (governance not lowered); an agent with
  a per-`(principal, op, target)` `needs_approval` grant queues it.
  `host_evacuate_composite` is the first production composite that
  dispatches another composite (`vmware.composite.vm.migrate`) via
  `dispatch_child` — that composite→composite recursion is a
  registrar-guaranteed `source_kind="composite"` row (never an ingested
  primitive), so it stays on the `dispatch_child` path per `#2248`; the
  recursion-depth contextvar (default cap 8) handles the depth-1 nesting
  cleanly, and the resolved `vm.migrate` runs its own relocate write on
  the direct session under the same governance seam.
- **`register_vmware_composite_operations`** (`composites/_register.py`)
  — async registrar function called from `run_typed_op_registrars` at
  lifespan startup. Iterates a single `_COMPOSITES` tuple of 15
  `_CompositeSpec` rows (7 read + 8 write); each row carries its
  own `safety_level` + `requires_approval` so the policy posture is
  implied by the spec, not by global defaults. Idempotent on re-run
  via the body-hash skip path.
- **Typed ops** (`typed_ops.py`, `#2257`) — the first vmware
  `source_kind="typed"` op, `vmware.host.usage`. Unlike a composite, a
  typed op is a **bound method** on `VmwareRestConnector`
  (`host_usage(self, operator, target, params)`) that the dispatcher
  binds to the resolved connector instance and calls directly — no
  `dispatch_child`, no ingested-descriptor sub-ops, no L2 pre-flight. It
  therefore works on a **fresh boot with zero catalog ingest** — the same
  direct-session property the 7 read composites (`#2253`) and, since
  `#2256`, the 8 write composites now share. The only `dispatch_child`
  leg left on the whole vmware surface is the `host.evacuate` →
  `vm.migrate` composite→composite recursion (a registrar-guaranteed
  `source_kind="composite"` row, not an ingested primitive, `#2248`). The metadata
  lives in a frozen `VmwareTypedOp` dataclass (mirroring
  `argocd/ops.py::ArgoCdOp`); the implementation
  (`host_usage_impl(connector, operator, target, params)`) lists hosts
  via `GET /vcenter/host` then, per host, reads `summary.quickStats`
  (CPU/memory load, MHz/MB), `summary.hardware` (capacity totals:
  `cpu_mhz` per core, core/package/thread counts, `memory_size_bytes`),
  and `runtime.inMaintenanceMode` through a direct PropertyCollector
  `RetrievePropertiesEx` on the connector session. Both calls are routed
  through `mount_op_path` so they land on `/api` (modern) or `/rest`
  (legacy/vcsim). The per-host property read is best-effort (a failed
  read nulls the detail with a `read_note`, mirroring
  `host_network_uplinks_composite`); the host listing is load-bearing.
  This op is why the plain REST host summary (liveness only) is not
  enough — `overallCpuUsage` / `overallMemoryUsage` live on the WS-API
  `HostSystem`, not the REST resource. It establishes the vmware typed-op
  pattern future per-host/per-VM typed reads reuse.
- **`register_vmware_typed_operations`** (`typed_ops.py`) — async
  registrar wrapper queued onto `run_typed_op_registrars` (via
  `register_typed_op_registrar` in the package `__init__`, alongside the
  composite registrar). Walks `VMWARE_TYPED_OPS`, resolves each op's
  `handler_attr` to the connector bound method, looks the group's
  curated `when_to_use` blurb up in `VMWARE_TYPED_WHEN_TO_USE_BY_GROUP`
  (required for a grouped typed op), and upserts each row into
  `endpoint_descriptor` with `source_kind="typed"` via
  `register_typed_operation`.
- **`VsphereTargetLike`** (`session.py`) — runtime-checkable Protocol
  capturing the minimum target shape the connector reads: `name`,
  `host`, `port`, `secret_ref`, `auth_model`. Replaced by the concrete
  `Target` model once G0.3 (#224) lands; the model satisfies the
  Protocol structurally without code edits here.
- **`VsphereSessionLoader`** (`session.py`) — async callable type
  resolving a `(target, operator)` pair to
  `{"username": ..., "password": ...}`. The `operator` parameter
  (threaded down the HTTP auth surface by G3.9-T1) carries the full
  frozen `Operator` so the live loader (G3.9-T3) can read the
  service-account secret under the operator's identity via
  `vault_client_for_operator(operator)`. Injectable on connector
  construction (`VmwareRestConnector(session_loader=…)`) so unit tests,
  integration tests, and pre-G0.3 production deploys override the
  default Vault loader.
- **`load_session_credentials_from_vault`** (`session.py`) — default
  loader, stubbed `NotImplementedError` until G3.9-T3 lands the
  operator-context Vault read path. Accepts `(target, operator)` but
  ignores `operator` while stubbed. Mirrors the
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
   list. The package `__init__` then queues
   `register_typed_op_registrar(register_vmware_typed_operations)`
   (`#2257`) for the typed-op rows.
4. The registry's v2 table now resolves `(vmware, 9.0, vmware-rest)`
   to `VmwareRestConnector`. The G0.7 auto-shim's idempotency check
   (in `ensure_connector_class_registered`, once #408's pipeline lands
   in main) no-ops on subsequent ingests against the same triple.
5. Lifespan calls `run_typed_op_registrars()`, which iterates every
   queued registrar and upserts: the 15 `vmware.composite.*` rows with
   `source_kind="composite"` (7 reads with `safety_level="safe"` +
   `requires_approval=False`; 8 writes with `safety_level="dangerous"`
   + `requires_approval=True`), plus the `vmware.host.usage` row with
   `source_kind="typed"` (`safety_level="safe"` + `requires_approval=False`).
   The typed row resolves and dispatches with **zero catalog ingest** —
   it depends on no ingested descriptor.

### Per-target session

1. First call to `auth_headers(target, operator)` against a target whose
   name isn't in `self._session_tokens`:
   a. Acquires `self._session_lock` (asyncio.Lock).
   b. Calls `self._session_loader(target, operator)` for the
      service-account credentials (the operator is threaded so the live
      loader can read Vault under the operator's identity; the stub
      ignores it).
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

The 15 composites (7 reads + 8 writes) land as `source_kind="composite"`
rows in `endpoint_descriptor`. At dispatch time:

1. Dispatcher resolves `(vmware-rest-9.0, vmware.composite.<verb>)`
   to the row, sees `source_kind="composite"`, builds a
   `DispatchChild` callable via
   `get_dispatch_child(dispatch=dispatch, parent_operator=...,
   parent_target=..., parent_audit_id=..., parent_op_id=...)`.
2. Handler is resolved via `import_handler(descriptor.handler_ref)`
   to one of the module-level functions in `composites/_read.py` or
   `composites/_write.py`. The dispatcher also resolves the connector
   instance for the composite's target (`#2251`).
3. Dispatcher invokes the handler with the keyword args it declares:
   `connector` (the resolved instance — every read and write handler)
   and/or `dispatch_child` (only `host_evacuate_composite`, for its
   composite→composite recursion).
4. Handler issues its sub-ops **directly on the connector session**
   (`connector._get_json` / `connector._post_json` mounted via
   `connector.mount_op_path`), no `endpoint_descriptor` lookup. Write
   sub-calls pass through `enforce_subop_policy` first (see the
   write-composites bullet above). The lone exception is
   `host.evacuate`'s `await dispatch_child(op_id="vmware.composite.vm.migrate", …)`
   recursion, which re-enters the dispatcher's `source_kind="composite"`
   branch, inherits `parent_audit_id` via the contextvar, and increments
   `composite_depth_var` (bounded at `Settings.composite_max_depth=8`).
5. Handler aggregates the sub-op responses into a single dict and
   returns; the dispatcher wraps it as an `OperationResult` with
   `status="ok"` and `result=<aggregated dict>`. A write handler may
   instead return an `awaiting_approval` / `denied` `OperationResult`
   (from `enforce_subop_policy`); the dispatcher passes that through
   verbatim.

Direct-session sub-calls drop the per-sub-op audit rows (the top-level
composite row is the audit anchor) and the per-sub-op parameter-schema
validation; the per-sub-op policy gate is relocated onto
`enforce_subop_policy` for writes. Only the `host.evacuate` →
`vm.migrate` recursion still rides `dispatch_child` and keeps its
audit-tree linkage + bounded-recursion guard.

### Recursive composite dispatch (`host.evacuate` → `vm.migrate`)

`host_evacuate_composite` is the first production composite that
calls another composite via `dispatch_child`. Two-level nesting:

```text
host.evacuate                                            # depth 0 (top-level dispatch)
  ├─ GET:/vcenter/vm                                     # depth 0 (direct session read)
  └─ vmware.composite.vm.migrate (× N)                  # depth 1 (dispatch_child of a composite)
       ├─ GET:/vcenter/cluster/{c}/drs/recommendations  # depth 1 (direct session read)
       └─ POST:/vcenter/vm/{vm}?action=relocate         # depth 1 (direct session write, gated)
  └─ PATCH:/vcenter/host/{host}/maintenance?action=enter # depth 0 (direct session write, gated)
```

`composite_depth_var` (default cap 8) handles the one nesting level
naturally. Post-`#2256` the **audit** tree is two-level, not three:
one `host.evacuate` parent row and N `vm.migrate` child rows (the
`dispatch_child` recursion). The raw-REST leaves run directly on the
session and write **no** audit row of their own — the top-level
composite's row is the audit anchor. `composite_depth_var` reads inside
those direct sub-calls ramp 0 (host.evacuate's own reads/writes) → 1
(the vm.migrate frame's reads/writes). The substrate guard's coverage in
`tests/test_operations_composite.py` proves the depth-cap behaviour
holds; this connector's recursive composite is the first production
caller.

### L1/L2 dispatch contract + pre-flight (G0.14-T10 / #1151) — retired-in-place

> **Historical, superseded by the two-world migration (Goal `#2247`).**
> The section below describes the pre-`#2253`/`#2256` model where every
> composite fanned out to **ingested** L2 rows via `dispatch_child` and
> guarded a missing catalog with a pre-flight. All 15 composites now issue
> their raw-REST sub-ops **directly on the connector session** (reads
> `#2253`, writes `#2256`), so they no longer touch ingested rows and no
> longer call `preflight_l2_dependencies`. The `composites/_preflight.py`
> module and the `CompositeL2Dependency*` exceptions are kept intact but
> unused pending their removal in `#2259`; the `_SUB_OPS_*` tuples in
> `_read.py` / `_write.py` are retained as the canonical sub-op-path
> manifest the ingest-reconcile acceptance guard checks. The
> platform-wide registration-time invariant
> (`operations.composite_invariant`, `#2262`) is the replacement safety
> net: a code-shipped op that declares a sub-op resolving to an
> `ingested` row fails the boot closed.

The 15 composites are the **L1** surface: hand-authored aggregators
each connector ships as `source_kind='composite'` descriptors. Every
composite's body fans out to **L2** raw-REST primitives
(`GET:/vcenter/datastore`, `POST:/vcenter/vm/{vm}/power?action=start`,
etc.). Pre-migration these went through `dispatch_child(...)` against the
~3,470 ingested descriptor rows derived from `vcenter.yaml` +
`vi-json.yaml`.

The L2 surface is **not** registered by default. Operators bring it in
by running `meho connector ingest --catalog vmware/9.0`, which posts
the spec sources from
`backend/src/meho_backplane/operations/ingest/catalog.yaml` through
the ingest pipeline. Until that ingest runs, the L2 descriptor rows do
not exist and any composite that tried to dispatch into them would
crash mid-call with the dispatcher's generic `unknown_op` error — the
coupling the direct-session migration removed.

Each composite handler runs an explicit pre-flight check
(`preflight_l2_dependencies` in `composites/_preflight.py`) before any
`dispatch_child(...)` call. The pre-flight walks the composite's
declared sub-op-ids against `endpoint_descriptor`; if any are missing,
it raises `CompositeL2DependencyMissing`. The dispatcher catches that
exception specifically (ahead of the generic exception branch) and
surfaces it as a structured `composite_l2_missing` error per the
`docs/codebase/error-message-shape.md` convention (G0.14-T11 #1141).
The response shape is:

```json
{
  "status": "error",
  "op_id": "vmware.composite.datastore.usage",
  "error": "composite_l2_missing: composite '...' depends on L2 sub-ops not registered ...",
  "extras": {
    "error_code": "composite_l2_missing",
    "missing_op_ids": ["GET:/vcenter/datastore", ...],
    "catalog_command": "meho connector ingest --catalog vmware/9.0"
  }
}
```

The first call against a stale catalog pays the DB walk; subsequent
calls hit a per-process cache and short-circuit. A negative result
(missing or disabled L2) is **not** cached -- the operator's expected
next action is to remediate and retry, and we want the retry to see
fresh state from the database.

### Disabled vs absent L2 (`composite_l2_disabled`, #1601)

`lookup_descriptor` hard-filters `is_enabled = TRUE`, so a sub-op whose
descriptor row **exists but is disabled** (`is_enabled = false`)
resolves to `None` exactly like one that was never ingested. On a
default `vmware-rest-9.0` deploy the ~3,470 L2 ops land
ingested-but-disabled, so collapsing both into `composite_l2_missing`
would tell the operator to re-run `meho connector ingest` -- which has
already happened. The pre-flight therefore classifies each
non-dispatchable sub-op with the `is_enabled`-agnostic
`descriptor_exists_any_state` probe (used **only** to classify -- a
disabled op stays non-dispatchable, it is never transient-enabled at
dispatch):

- **present but disabled** -> `CompositeL2DependencyDisabled` ->
  structured `composite_l2_disabled`:

  ```json
  {
    "status": "error",
    "op_id": "vmware.composite.datastore.usage",
    "error": "composite_l2_disabled: ... present in this connector's catalog but disabled ... 'meho connector edit-op vmware-rest-9.0 <op_id> --enable' ...",
    "extras": {
      "error_code": "composite_l2_disabled",
      "disabled_op_ids": ["GET:/vcenter/datastore", ...],
      "connector_id": "vmware-rest-9.0"
    }
  }
  ```

  The remediation names a **real** verb: per-op
  `meho connector edit-op <connector_id> <op_id> --enable`. Connector-level
  `meho connector enable <connector_id>` is named only as the caveated
  alternative -- it does **not** re-enable spec-ingested ops, which land
  `group_id = NULL` and the enable cascade filters on `group_id` (see
  `ingest/_internals.py` / `ingest/_upsert.py`), so per-op `edit-op
  --enable` is the deterministic path. The original report proposed a
  group-level enable verb; no such verb exists, so the remediation never
  references one.

- **truly absent** (no row in any state) -> unchanged
  `composite_l2_missing` + the catalog-ingest remediation above.

When a single walk turns up both states, **disabled takes precedence**
-- only one exception can surface, and the re-enable remediation is the
one a default ingested-but-disabled deploy needs.

Composite-to-composite sub-ops (`vmware.composite.*`, today only
`host.evacuate` -> `vm.migrate`) are deliberately skipped by the
pre-flight: their registration is guaranteed by the same lifespan
registrar that brings their parent composite in, so validating them
would create a startup-order false positive without catching any real
gap.

Three options were considered for the L2-dependency strategy
(per #1151's *Desired state*); Option B (lazy pre-resolve on first
call) was chosen as it (a) closes signal 20's actual gap (a
remediation-bearing error message) without (b) blocking on
T9 (#1150)'s server-side catalog-driven ingest landing first, (c)
disrupting the boot order, or (d) inverting the catalog ingest
posture (an explicit operator action by design since v0.5.1).
See `composites/_preflight.py`'s module docstring for the full
trade-off matrix vs. Options A (eager-at-registration) and C
(ship-L1+L2-as-unit).

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

### Read-composite best-effort enrichment (`datastore.usage`, #1908)

Read composites distinguish **load-bearing** sub-ops from **optional
enrichment** sub-ops. A load-bearing failure routes through
`_require_ok` and raises `CompositeSubOpError`, which the dispatcher
wraps into a `connector_error` envelope -- the whole composite fails.
An optional enrichment failure must **not** sink the composite: the
leg degrades and the rows the core use case needs are still returned.

`datastore.usage` is the canonical example. Its per-datastore layout is
`GET:/vcenter/datastore` (listing, load-bearing) → per row
`GET:/vcenter/datastore/{datastore}` (capacity/free/type, load-bearing)
→ `GET:/vcenter/vm?filter.datastores=...` (VM placement, **best-effort**).
The "which datastores are filling up?" use case is satisfied by the
capacity/free/type read, which has already succeeded by the time the VM
lookup runs. So when the VM lookup errors -- e.g. an 8.0 vCenter that
400s the `filter.datastores` query the 9.0 spec emits -- the row is
still returned with `capacity`/`free_space`/`type` intact,
`vm_count`/`vm_names` set to `null`, and an `enrichment_note` string
recording the failing sub-op, its status, and the underlying error.
The response schema marks `vm_count`/`vm_names` as nullable and adds the
optional `enrichment_note` key (present only on a skipped row).

`capacity`/`free_space` are read from the per-datastore detail payload
with a **list-row fallback** (#2078): some vCenter builds (observed on an
8.0.3 vCenter against the 9.0 spec) return a detail `Datastore.Info` that
populates `free_space` but omits `capacity`, while the
`GET:/vcenter/datastore` listing row carries both. The row-builder takes
the detail value when present and falls back to `entry.get(...)` on the
already-fetched listing row otherwise, so `capacity`/`free_space` are
`null` only when neither source carries them — the composite no longer
discards a capacity it already had, which is what made `%`-full
uncomputable off the composite alone.

### Bubbling a sub-op's structured error (#1908)

`CompositeSubOpError` folds the failed sub-op's most diagnostic line
into its message via `_describe_sub_op_failure`, rather than stopping at
the terse `error` summary (`connector_error: HTTPStatusError`). The
helper prefers the structured `http_status` + `upstream_message` the
dispatcher's 403/422/auth builders extract; for every other status
(400/404/5xx, routed through the generic `connector_error` builder) it
falls back to `extras["exception_message"]` -- the stringified
`httpx.HTTPStatusError`, which already carries the status code **and**
the offending URL. The same helper feeds `datastore.usage`'s
`enrichment_note`. Net effect: the 400 + URL that previously only showed
on a manual sub-op replay now ride the composite's error envelope (or
the per-row note). The `returned status='<status>'` clause is preserved
so existing string-matching consumers keep working.

### Park-time approval previews (#1608)

All 8 write composites ship `requires_approval=True`, so a human/agent
dispatch parks as a durable `ApprovalRequest` row. Pre-#1608 that row's
`proposed_effect` was the identifier-only default `{op_id, connector_id,
target_id}` — and since the dispatch `params` are deliberately never
serialised onto a reviewer-facing surface (#1503), the four-eyes
approver could not tell a one-VM power cycle from a 1000-VM outage.

`composites/_write_preview.py` registers one preview builder per write
composite on the generic per-op hook (`register_preview_builder`,
`operations/_preview.py`, #1437). The builder result lands under
`proposed_effect["preview"]`, wrapped with the op's sensitivity
`op_class` (see [`approvals.md`](approvals.md)). Two depths:

| Composite | Preview | Depth |
| --- | --- | --- |
| `vm.power.bulk` | `{action, filter, resolved, total_resolved}` | live read (`GET:/vcenter/vm`) |
| `host.evacuate` | `{host, tolerate_partial_failure, resolved, total_resolved}` | live read (`GET:/vcenter/vm`) |
| `host.detach_from_vds` | `{host, dvs, fallback_network, resolved, total_resolved}` | live read (`GET:/vcenter/vm`) |
| `cluster.patch` | `{cluster, patch_method, resolved, total_resolved}` | live read (`GET:/vcenter/cluster/{cluster}/host`) |
| `vm.create` | creation-spec echo (name, guest_os, sizing, networks, power-on) | param echo, no I/O |
| `vm.clone` | clone-coordinates echo | param echo, no I/O |
| `vm.snapshot.revert` | `{vm, snapshot_name}` echo | param echo, no I/O |
| `vm.migrate` | `{vm, cluster, target_host, target_host_source}` | param echo, no I/O |

The live-read previews resolve the same entity set the approved
dispatch would act on, through the **same shared helpers** the handlers
use at dispatch time (`_write._resolve_vm_list` /
`_write._resolve_cluster_hosts`) — one resolution code path, two call
sites. The `resolved` list is capped at 20 entries
(`_PREVIEW_RESOLVED_CAP`), identity-only per row (`vm`/`host`, `name`,
`power_state`); `total_resolved` always carries the uncapped count. The
four param-echo composites name their full blast radius in params, so
no read can change what the preview says; `vm.migrate` deliberately
does **not** pre-resolve a DRS recommendation (point-in-time output
would mislead the reviewer — the preview says
`target_host_source="drs_at_execution"` instead).

At park time the composite handler never runs, but the shared
`_write._resolve_vm_list` / `_write._resolve_cluster_hosts` helpers are
now direct-session (`#2256`): the live-read builders call them with the
connector instance the dispatcher resolved into the `PreviewContext`
(`ctx.connector_instance`), so the one listing `GET` runs straight on the
session. Because it is a direct read, the three properties the old
park-time `dispatch_child` adapter enforced hold intrinsically — no
policy-gate re-entry (a direct call cannot re-enter the dispatcher), no
unparented audit rows (a direct read writes none), reads-only (the
helpers only ever issue the listing `GET`). This also fixes the
fresh-boot gap the pre-`#2256` preview had: the retired adapter resolved
the sub-op against an **ingested** descriptor row, so on a
zero-catalog-ingest deploy the live-read preview always degraded to
`preview_unavailable`; the direct-session read now resolves the entity
set on a fresh boot. This mirrors how the k8s.apply dry-run (#1437), the
argocd snapshot reads (#1452), and the vault capability probe (#1504) run
their preview I/O — connector-level, un-dispatched.

Everything is fail-soft — the park always proceeds — but a decline and
a failure degrade differently (#1628): a builder that *declines*
(malformed params, or no resolved connector instance) parks with the
identifier-only default, while a builder that *raises* (vCenter
unreachable, the listing read errors on this deploy) parks with the
identifier fields **plus** `preview_unavailable: true` and a
`preview_error` reason naming the failed read. The marker rides through
every reviewer surface that renders `proposed_effect` verbatim (REST
`GET /api/v1/approvals`, `meho.approvals.list` / `.get`, `meho
approvals show`), so "blast-radius unknown" is distinguishable from a
genuinely small action. The 7 read composites register no builder —
they never park.

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
- **Reactive 401 recovery at the dispatch path (#2067)** — vSphere's
  ~5-minute idle timeout means a long-idle cached session may see a 401 on
  the next dispatch. The connector caches one token per `(tenant_id,
  target.id)` with no TTL, and `_request_json`/`_post_json` do not retry 401
  themselves. Recovery instead lives at the generic-ingested dispatch arm:
  on an auth-class status it calls the connector's public
  `invalidate_session(target)` hook (which pops the cached token + login
  path under `_session_lock`, keyed on `target_cache_key`) and re-dispatches
  the op once, so the next transport call misses the cache and re-runs
  `_establish_and_cache_session` — re-authenticating and re-running the
  modern→legacy `/api/session` 404 fallback. A second 401 (re-login also
  failed) surfaces as `connector_auth_failed`. **Proactive** token TTL
  (re-mint before the doomed call) remains v0.2.next polish.
- **`vi-json.yaml` ingestion live** — T3 (#503) shipped the ingestion
  pipeline (depends on T2 / #501's `$ref` resolver). The same
  connector dispatches the ~2,195 vi-json ops alongside the ~1,275
  vCenter REST ops; both share the `vmware-api-session-id` session
  header per `docs/vcenter-9.0/MANIFEST.md`. Two of the read
  composites (`event.tail`, `performance.summary`) call vi-json
  sub-ops; the other three call vCenter REST sub-ops only.
- **All 15 composites shipped** — T5 (#508) ships 5 read, #2080 adds a 6th read,
  #2135 adds a 7th read (`host.vsan_health`); T6 (#509) ships the 8 write
  composites. The "All hand-authored composites land as endpoint_descriptor rows with
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
- **`network.portgroup.audit` op_id reconciliation (#1602)** — the
  read composite originally dispatched
  `GET:/vcenter/network/distributed-switch` and
  `GET:/vcenter/network/distributed-portgroup` (both **singular**),
  neither of which resolves against the canonical `vmware/9.0` ingest.
  The vSphere Automation REST distributed-switch resource is **plural**
  (`GET:/vcenter/network/distributed-switches`, a preview feature), and
  there is **no** dedicated distributed-portgroup list resource at all —
  distributed portgroups are enumerated via the generic
  `GET:/vcenter/network` resource filtered to the
  `DISTRIBUTED_PORTGROUP` type. Both corrected sub-ops live in the REST
  Automation `vcenter.yaml` (this is **not** a VI-JSON MoRef family
  swap). The generic `Network` summary carries no parent-DVS field, so
  the per-portgroup `dvs`/`dvs_name` enrichment is best-effort and
  `filter_dvs` scopes only the DVS-name index, not the portgroup set.
  A build-time guard
  (`tests/acceptance/test_portgroup_audit_op_id_reconcile.py`) parses
  the pinned `vcenter.yaml` and asserts every audit sub-op_id is emitted
  by the ingest, so a future drift goes red in CI.
- **`host.detach_from_vds` write composite carries the same singular
  `distributed-portgroup` defect (out of scope for #1602)** —
  `_write.py`'s `_OP_LIST_PORTGROUPS =
  "GET:/vcenter/network/distributed-portgroup"` has the identical
  unresolvable spelling. #1602 is scoped to the read
  `network.portgroup.audit` composite only; the write-side fix plus a
  reconcile guard over the 8 write composites' `_SUB_OPS_*` against the
  real pinned spec (the existing
  `test_connectors_vmware_rest_composites_l2_ingest_reconcile.py`
  synthesises its fixture *from* the constants, so it cannot catch a
  wrong key) is a follow-up under the #1529 cleanup.

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
