# Connector: kubernetes (k8s-1.x / `kubernetes_asyncio`)

## Overview

The `kubernetes` connector is the typed `Connector` subclass that
dispatches operator-facing Kubernetes operations under the
`(product="k8s", version="1.x", impl_id="k8s")` registry triple. The
single-impl `impl_id == product` shape mirrors the Vault sibling; the
library name `kubernetes_asyncio` lives in the package layout +
`pyproject.toml` dependency, not the registry's natural-key triple. It builds on the G0.6 operation registry: handlers register at
lifespan startup via `register_typed_operation()`, the dispatcher routes
calls via the descriptor table, and the operator surface is the
meta-tools (`search_operations` / `call_operation`) plus the
forthcoming CLI alias verbs (G3.2-T6).

The connector replaces the operator's daily `kubectl-vcf.sh` wrapper for
read workflows -- inventory listing, workload inspection, log fetching.
Write operations stay in the wrapper until v0.2.next ships policy +
approval flow.

Source: `backend/src/meho_backplane/connectors/kubernetes/`.

## Key types

- **`KubernetesConnector`** (`connector.py`) -- `Connector` subclass.
  Class attributes: `product="k8s"`, `version="1.x"`,
  `impl_id="k8s"`. Caches a per-target
  `kubernetes_asyncio.client.ApiClient` keyed on `secret_ref`; the
  kubeconfig loader is injectable for tests.
- **Op metadata** (`ops.py`) -- the `KubernetesOp` dataclass plus the
  `KUBERNETES_OPS` tuple the connector's `register_operations` walks
  at startup. The tuple merges `k8s.about` (T1's canary, refactored
  through G0.6), the T2 inventory ops (from `ops_core.py`), the T3
  workload ops (from `ops_workload.py`), and the T5 logs op (from
  `ops_logs.py`).
- **Core inventory helpers** (`ops_core.py`) -- pure mapping helpers
  (`namespace_row`, `node_row`, `taint_row`, `age_seconds`) plus the
  `CORE_OPS` registration rows for `k8s.ls` / `k8s.namespace.list` /
  `k8s.node.list`. Helpers stay pure-function so the unit suite can pin
  the wire shape against synthetic `V1Namespace` / `V1Node` model
  instances without booting an event loop.
- **Workload helpers** (`ops_workload.py`) -- pure row mappers
  (`pod_row`, `pod_info`, `deployment_row`, `deployment_info`,
  `container_status_row`, `pod_ready_string`) plus the prefix
  resolvers (`resolve_pod_name`, `resolve_deployment_name`) and the
  `WORKLOAD_OPS` registration rows for `k8s.pod.{list,info}` and
  `k8s.deployment.{list,info}` (G3.2-T3 #323). Two structured errors
  (`WorkloadNotFoundError`, `AmbiguousPrefixError`) drive the
  prefix-resolution UX: ambiguous matches surface the candidate list
  through the dispatcher's `connector_error` envelope so callers can
  render a "did-you-mean" hint.
- **Network helpers** (`ops_network.py`) -- pure mapping helpers
  (`service_row`, `service_port_row`, `ingress_row`, `ingress_rule_row`,
  `ingress_path_row`) plus the `NETWORK_OPS` registration rows for
  `k8s.service.list` / `k8s.ingress.list` (G3.2-T4 #324).
- **Config helpers** (`ops_config.py`) -- pure mapping helpers
  (`configmap_list_row`, `configmap_info`) plus the `CONFIG_OPS`
  registration rows for `k8s.configmap.list` / `k8s.configmap.info`
  (G3.2-T4 #324).
- **Event helpers** (`ops_events.py`) -- pure mapping helpers
  (`event_row`, `sort_event_rows_recent_first`) plus the `EVENT_OPS`
  registration row for `k8s.event.list` (G3.2-T4 #324). Split out of
  `ops_config.py` to fit under the 600-line code-quality cap; the
  separation also aligns with the operator mental model (events are
  observability, configmaps are configuration).
- **`KubernetesTargetLike`** (`kubeconfig.py`) -- runtime-checkable
  Protocol capturing the minimum target shape the connector reads:
  `name`, `host`, `port`, `secret_ref`. Replaced by the concrete
  `Target` model once G0.3 (#224) lands; the structural shape there
  satisfies the Protocol unchanged.
- **`load_kubeconfig_from_vault`** (`kubeconfig.py`) -- the default
  kubeconfig loader stub. Raises `NotImplementedError` until G0.3
  lands; tests and the integration suite inject a callable returning
  a pre-built kubeconfig dict.

## Shipped op surface

| op_id                  | safety | description                                                       |
| ---------------------- | ------ | ----------------------------------------------------------------- |
| `k8s.about`            | safe   | Product / version / platform via `VersionApi.get_code`.           |
| `k8s.ls`               | safe   | Synthetic walker: root / namespace / namespace+kind.              |
| `k8s.namespace.list`   | safe   | `CoreV1Api.list_namespace()` -- name / status / age.              |
| `k8s.node.list`        | safe   | `CoreV1Api.list_node()` -- status / roles / version.              |
| `k8s.pod.list`         | safe   | Pods with selectors + server-side pagination.                     |
| `k8s.pod.info`         | safe   | Full pod detail; exact name or unique prefix.                     |
| `k8s.deployment.list`  | safe   | Deployments with live replica counts + image + strategy.          |
| `k8s.deployment.info`  | safe   | Full deployment detail; exact name or unique prefix.              |
| `k8s.service.list`     | safe   | `CoreV1Api.list_namespaced_service()` -- type / cluster_ip / ports / selector. |
| `k8s.ingress.list`     | safe   | `NetworkingV1Api.list_namespaced_ingress()` -- class / hosts / TLS / rules. |
| `k8s.configmap.list`   | safe   | `CoreV1Api.list_namespaced_config_map()` -- **keys only, NO values**. |
| `k8s.configmap.info`   | safe   | `CoreV1Api.read_namespaced_config_map()` -- full data + binary_data. |
| `k8s.event.list`       | safe   | `CoreV1Api.list_namespaced_event()` -- pulls up to `MAX_EVENT_LIMIT` (500) rows, sorts client-side by `last_seen` desc, truncates to caller's `--limit`. Server has no `lastTimestamp` ordering guarantee. EventSeries `count` honoured. |
| `k8s.logs`             | safe   | `CoreV1Api.read_namespaced_pod_log()` non-streaming -- tail / container / since / previous + 1 MiB cap. |

T6 of Initiative #320 (CLI alias verbs + k3d acceptance) extends this
surface against the same `KubernetesOp` -> `KUBERNETES_OPS` ->
`register_operations` pattern.

### Workload-op pagination (`k8s.pod.list` / `k8s.deployment.list`)

The list handlers forward the standard k8s `label_selector` /
`field_selector` / `limit` / `_continue` filter knobs to the API
server so heavy-tenancy clusters can paginate server-side without
streaming every row through the connector. The operator passes
`limit=N` plus optional `continue_token=<cursor>` on each call; the
response carries `next_continue` whenever the server signals more
pages. Tokens are server-defined and expire after ~5-15 minutes; a
stale token returns 410 ResourceExpired, and the handler propagates
the API exception verbatim so the caller can restart without the
token.

`namespace` and `all_namespaces` are mutually exclusive in the
schema's `oneOf` clause -- exactly one must be supplied. The
`all_namespaces=true` path routes through
`list_pod_for_all_namespaces` / `list_deployment_for_all_namespaces`;
the per-namespace path uses the `_namespaced_` variants.

### Workload-op prefix resolution (`k8s.pod.info` / `k8s.deployment.info`)

Both `info` ops accept an exact name or a unique prefix within the
namespace. Resolution shape (`_resolve_from_items` in `ops_workload.py`):

1. Exact match wins -- `foo-bar` resolves to the literal `foo-bar`
   pod even when `foo-bar-x` also exists.
2. Otherwise the prefix matches collect into a candidate list.
3. Zero candidates -> `WorkloadNotFoundError`.
4. Multiple candidates -> `AmbiguousPrefixError` with the sorted
   candidate list on `.candidates`. The dispatcher's
   `connector_error` envelope carries the exception class name and
   the args list, so the agent / CLI can render a "did-you-mean"
   hint without parsing the error string.

The resolver pages via `list_namespaced_pod` /
`list_namespaced_deployment` without server-side pagination -- the
operator-facing namespaces are typically O(10..100) objects which
fits in one unpaginated response. Heavy-tenancy namespaces with
hundreds of workload objects are not the prefix-resolver's target
audience; the agent typically reaches them via the list path with
explicit pagination.

### ConfigMap privacy split

`k8s.configmap.list` and `k8s.configmap.info` deliberately split the
read surface: the list op returns `keys` (the union of `data` +
`binary_data` keys) but **never** the values, so a routine "what's
configured here?" scan does not bulk-broadcast configmap content
through the SSE / audit feed. Operators wanting values call
`k8s.configmap.info` per configmap; the audit row records the
targeted read so a post-incident query can answer "who read which
configmap when?". v0.2 classifies `info` as `op_class=read`; G6.3 may
upgrade specific configmap-name patterns (managed-by
`secret-translator`, names matching `*-secret-config`) to
`sensitive-read`.

## Control flow

### Connector init (lifespan)

1. The connector package's `__init__.py` calls
   `register_connector_v2(product="k8s", version="1.x",
   impl_id="k8s", cls=KubernetesConnector)` at import time. The v1
   entry under `"k8s"` is preserved for chassis-route compat.
2. The same module appends
   `register_kubernetes_typed_operations` to the typed-op registrar
   list via `register_typed_op_registrar`.
3. The FastAPI lifespan calls `_eager_import_connectors()` followed by
   `run_typed_op_registrars()`. The registrar walks `KUBERNETES_OPS`
   and routes each row through `register_typed_operation()`, which
   upserts the descriptor row in `endpoint_descriptor`.

The walk is **idempotent**: a second registrar run hits the body-hash
skip-re-embed branch and avoids re-encoding the descriptions, so pod
restarts on unchanged code stay cheap.

### Op dispatch (per request)

1. The operator's MCP / API call lands at
   `/api/v1/operations/call` with a `connector_id="k8s-1.x"` +
   `op_id="k8s.<verb>"` body.
2. The dispatcher resolves the descriptor row in `endpoint_descriptor`,
   runs JSON Schema validation against `parameter_schema`, and routes
   to the handler via `import_handler(descriptor.handler_ref)`. The
   bound-method form (`KubernetesConnector.k8s_namespace_list`) is
   rebound against the resolved connector instance.
3. The handler calls
   `kubernetes_asyncio.client.CoreV1Api(api_client).<list_xxx>()` and
   projects each model into the wire dict via the pure mapping
   helpers.
4. The dispatcher invokes `PassThroughReducer.reduce()` (v0.2's no-op
   reducer), writes the audit row, fires the broadcast event, and
   wraps the result in `OperationResult.status="ok"`.

### `k8s.ls` three-way dispatch

The `k8s.ls` handler is a thin path parser:

- `/` (or omitted) -- one `list_namespace()` call; returns sorted
  namespace names + the fixed `K8S_CLUSTER_KINDS` list.
- `/<namespace>` -- one `list_namespaced_<kind>(namespace=...,
  limit=1)` call per kind in `K8S_NAMESPACED_KIND_LISTERS`; the count
  is derived from `len(items) + remaining_item_count` so the operator
  pays one round-trip per probed kind, not one per row.
- `/<namespace>/<kind>` -- forwards through
  `KubernetesConnector.execute()` to `k8s.<kind>.list`. The shim is the
  same dispatcher path direct callers use; an unknown kind comes back
  as the structured `unknown_op` envelope and the forwarder surfaces it
  verbatim under `result`.

## Dependencies

- **`kubernetes_asyncio` >=32,<33** -- async fork of the official
  Python client. Targets the K8s 1.32 API surface (matches the k3s
  container image pinned for integration tests).
- **G0.2 chassis** -- `Connector` ABC plus the connector registry
  (v1 + v2 entries).
- **G0.6 substrate** -- `register_typed_operation()` for descriptor
  upserts; the dispatcher's lookup + handler-resolve + reducer path
  for op execution; `OperationResult` envelope.
- **Vault (G3.3, future)** -- the production kubeconfig loader resolves
  `target.secret_ref` to a kubeconfig dict via the operator-context
  Vault read path. The current stub raises `NotImplementedError`; tests
  and the integration suite inject a custom loader.

## JSONFlux handle pattern

Issue #322's "Handle threshold tested: against k3d populated with 50+
namespaces, sample of 20 + handle returned" acceptance criterion
assumed the shared `HandleStore` from G3.1-T4 (#304) would be in place.
#304 was **superseded** -- the Initiative-redraft note on the issue
spells this out -- and the substrate currently in the tree ships
`PassThroughReducer` as the only reducer. The reducer never populates
`OperationResult.handle`.

The handlers in this connector emit **raw row lists** (`{"rows": [...],
"total": N}`) -- the shape the future JSONFlux reducer will see when it
ships. The reducer, not the connector, owns the threshold check, the
row truncation, the spill to MinIO/S3/Valkey, and the `ResultHandle`
construction. Centralising the spill logic in one reducer keeps every
typed connector free of per-handler threshold code; per the substrate
split documented on `meho_backplane.operations.reducer`, set-shaped
reduction is the reducer's job, not the connector's.

The `total` key in the response envelope is the un-truncated row count
the reducer will read to decide whether to spill; today the value is
just `len(rows)` because nothing reduces.

## Known issues

- `_get_api_client` caches by `target.secret_ref`. Two tenants that
  legitimately register a target named `"rke2-meho"` in their own
  `targets.yaml` will not share the same `ApiClient` (different Vault
  paths, different secret_refs), but until G0.3 ships its `Target.id`
  the cache key is the operator's chosen opaque identifier. Swap to
  `target.id` when G0.3 finalises a row-PK shape.
- The kubeconfig loader is a stub
  (`load_kubeconfig_from_vault` raises `NotImplementedError`). T2+
  ships inject a custom loader at connector construction; the
  integration suite uses the testcontainers-emitted kubeconfig
  directly.
- `k8s.ls /<namespace>` queries a **fixed** list of kinds, not the
  full `kubectl api-resources --namespaced=true` enumeration. The
  trade-off is documented on `K8S_NAMESPACED_KIND_LISTERS`: full
  discovery would cost N round-trips per `ls` and risk RBAC-shaped
  403 spam on operator sessions. T3 / T4 expand the list as new
  per-kind `list` ops register.

## References

- Parent Initiative: [#320 G3.2](https://github.com/evoila/meho/issues/320)
  -- `k8s-1.x` typed connector (library: `kubernetes_asyncio`).
- Predecessor Tasks:
  - [#321 G3.2-T1](https://github.com/evoila/meho/issues/321) -- skeleton.
  - [#322 G3.2-T2](https://github.com/evoila/meho/issues/322) -- core
    inventory ops.
  - [#323 G3.2-T3](https://github.com/evoila/meho/issues/323) -- workload
    ops (`k8s.pod.{list,info}` / `k8s.deployment.{list,info}`).
  - [#325 G3.2-T5](https://github.com/evoila/meho/issues/325) -- `k8s.logs`.
- This Task: [#324 G3.2-T4](https://github.com/evoila/meho/issues/324)
  -- network + config + event ops (`k8s.service.list` /
  `k8s.ingress.list` / `k8s.configmap.list/info` / `k8s.event.list`).
- Substrate Initiative: [#388 G0.6](https://github.com/evoila/meho/issues/388)
  -- operation registry + dispatcher + JSONFlux substrate.
- `kubernetes_asyncio`: https://github.com/tomplus/kubernetes_asyncio
- Kubernetes API spec: https://kubernetes.io/docs/reference/kubernetes-api/
- Kubernetes Pod API:
  <https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/>
- Kubernetes Deployment API:
  <https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/deployment-v1/>
