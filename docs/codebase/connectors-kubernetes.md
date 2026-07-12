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
meta-tools (`search_operations` / `call_operation`) plus the CLI
alias verbs that ship with G3.2-T6 (`meho k8s <verb> --target <name>
…`), implemented in [`cli/internal/cmd/k8s/`](../../cli/internal/cmd/k8s/)
and documented in
[`docs/cross-repo/kubernetes-onboarding.md`](../cross-repo/kubernetes-onboarding.md).

The connector replaces the operator's daily `kubectl-vcf.sh` wrapper for
read workflows -- inventory listing, workload inspection, log fetching --
and, as of G3.14-T1 (#1403), for the single-call **write** surface
(scale / rollout-restart / namespace-create / annotate / label / cordon /
apply / delete / secret-create / job-create). Every write op is
`requires_approval=True`, so once #1401's human-queue routing is live an
operator's write dispatch is parked for approval rather than auto-run or
hard-denied. `k8s.exec` (G3.14-T2) and `k8s.drain` stay in the wrapper.

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
  kubeconfig loader. Since #2397 it reads through the shared
  **credential-backend seam** rather than opening Vault directly: it
  calls `load_vault_secret_data(target, operator, mount=mount)`
  (`_shared/vault_creds.py`), which runs the fail-closed precondition
  guards, splits the `secret_ref` scheme via `split_credential_ref`
  (schemeless / `vault:` → the operator-context Vault KV-v2 read,
  `gsm:` → GCP Secret Manager, …), and returns the raw secret-field
  dict; the loader then extracts the `kubeconfig` field and parses the
  YAML into the dict shape
  `kubernetes_asyncio.config.new_client_from_config_dict` accepts.
  Routing through the seam is what lets a
  `gsm:<project>/<secret>#kubeconfig` ref authenticate on a
  `CREDENTIAL_BACKEND=gsm` / no-Vault deployment (the last-mile gap
  #2227 left for the k8s connector); it also inherits the Vault-kind
  KV-v2 API-path-shape guard, so a `secret/data/…`-shaped ref fails
  actionably instead of 404ing. For the default Vault backend the read
  still happens **under the operator's Vault Identity entity** —
  per-operator RBAC + per-operator audit. Tests still inject a custom
  loader for unit-scope determinism; the `(target, operator)` signature
  is shared by the default and every injected loader. Fail-closed
  guards (in the seam): empty `operator.raw_jwt` (the system-call
  carve-out) and unset `target.secret_ref` both raise
  `VaultCredentialsReadError` before any store is touched. This is the
  rubric **State 2** wiring (`shared_service_account` only). Decision:
  [`docs/architecture/connector-auth.md`](../architecture/connector-auth.md).

## Probe ↔ dispatch convergence on the route operator (G0.16-T4 #1306)

The `Connector.fingerprint(target, operator=None)` ABC signature
gained the optional `operator` parameter in G0.16-T4. The four
fingerprint surfaces that authenticate via Vault (this connector,
`vmware-rest-9.0`, `sddc-rest-9.0`, `nsx-rest-4.2`) now read the
per-target credentials under the **same identity** the dispatch path
uses:

- **REST probe route** (`POST /api/v1/targets/{name}/probe`) — the
  `require_operator` dependency lifts the chassis-validated operator
  and the handler forwards it via
  `cls().fingerprint(target, operator=operator)`.
- **UI re-probe route** (`POST /ui/connectors/{name}/probe`) —
  `resolve_operator_or_403` lifts an operator gated on
  `TENANT_ADMIN`, and the handler forwards it identically.
- **Dispatch path** (`POST /api/v1/operations/call`) — unchanged; the
  dispatcher has always threaded the operator into the connector's
  HTTP auth surface.

The fallback `operator=None` synthesises a system operator (whose
non-empty placeholder JWT fails closed at the live Vault round-trip)
for callers that have no real operator in scope (the readiness probe
worker, the K8s topology refresh service). This preserves the
locked Option A decision's system-call carve-out — *system-initiated
calls cannot perform an operator-context Vault read*.

Pre-#1306 the probe routes hard-coded the system operator
synthesis inside each connector's `fingerprint()`. Vault's JWT/OIDC
auth method rejected the placeholder JWT as `malformed jwt: must
have three parts` (compact-JWS format requires three dot-separated
parts; the placeholder
`"system:connector-probe-placeholder-jwt"` has zero), which surfaced
on every probe of the four affected connectors in the v0.8.0 dogfood
cycle (`claude-rdc-hetzner-dc#771` Finding 4). The fix is the
single-source-of-truth shape — probe + dispatch both flow the same
real operator through the same loader.

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
| `k8s.service.list`     | safe   | `CoreV1Api.list_namespaced_service()` / `list_service_for_all_namespaces()` -- type / cluster_ip / ports / selector + `label_selector`. |
| `k8s.ingress.list`     | safe   | `NetworkingV1Api.list_namespaced_ingress()` / `list_ingress_for_all_namespaces()` -- class / hosts / TLS / rules + `label_selector`. |
| `k8s.configmap.list`   | safe   | `CoreV1Api.list_namespaced_config_map()` / `list_config_map_for_all_namespaces()` -- **keys only, NO values** + `label_selector`. |
| `k8s.configmap.info`   | safe   | `CoreV1Api.read_namespaced_config_map()` -- full data + binary_data. |
| `k8s.event.list`       | safe   | `CoreV1Api.list_namespaced_event()` / `list_event_for_all_namespaces()` -- pulls up to `MAX_EVENT_LIMIT` (500) rows, sorts client-side by `last_seen` desc, truncates to caller's `--limit`. Server has no `lastTimestamp` ordering guarantee. EventSeries `count` honoured. Forwards `label_selector` + `field_selector`. |
| `k8s.logs`             | safe   | `CoreV1Api.read_namespaced_pod_log()` non-streaming -- tail / container / since / previous + 1 MiB cap. |
| `k8s.exec`             | **dangerous** (`requires_approval=True`) | `CoreV1Api.connect_get_namespaced_pod_exec()` over the `WsApiClient` websocket transport -- bounded argv command-and-capture: stdout / stderr demuxed from the `v4.channel.k8s.io` channels + exit code parsed from the channel-3 status frame, per-stream 1 MiB cap, bounded timeout. Interactive `-it` deferred. |

### `k8s.exec` -- websocket command-and-capture (`ops_exec.py`)

`k8s.exec` is the one op that cannot ride the cached read `ApiClient`.
Pod exec is an HTTP `Upgrade` to a websocket carrying the multiplexed
`v4.channel.k8s.io` sub-protocol, which `kubernetes_asyncio` only speaks
through its `kubernetes_asyncio.stream.WsApiClient` subclass. The
connector therefore keeps a **parallel** per-target cache
(`KubernetesConnector._ws_api_clients`, keyed on `secret_ref` exactly
like `_api_clients`), built lazily from the same operator-identity
kubeconfig via `_get_ws_api_client` and closed alongside the read
clients in `aclose` (no leaked sockets).

The library's `WsApiClient.request` with `_preload_content=True` (the
default) concatenates stdout + stderr into one blob *and discards the
status frame* -- so the exit code is lost and the two streams can no
longer be told apart. To meet the op's contract the handler passes
`_preload_content=False`, which returns the raw aiohttp websocket
context manager; it then `async with`-es the socket, demuxes the leading
channel byte itself (1 = stdout, 2 = stderr, 3 = error/status), and
parses the exit code from the channel-3 frame via
`WsApiClient.parse_error_data` (`status == "Success"` -> 0; otherwise the
code in `details.causes[0].message`).

A bounded `timeout_seconds` (default 30, capped 300) wraps the drain in
`asyncio.wait_for`. On expiry the socket is closed explicitly and partial
output is returned with `timed_out=true` and `exit_code=null`. The
partial-output guarantee is why the demux writes into a caller-owned
`_ExecCapture` accumulator rather than returning a tuple: a `wait_for`
cancellation discards a coroutine's return value but not the bytes
already written to the shared accumulator.

Each stream is capped at 1 MiB independently and front-truncated when
oversize (`*_truncated_byte_count` recorded), reusing the never-log
posture of `k8s.logs` -- the audit row hashes only the request params,
never the captured bytes. Pod / container resolution reuses
`resolve_pod_and_container` from `ops_logs`.

Interactive exec (`kubectl exec -it` -- a live PTY/shell) is
**deliberately out of scope**: the dispatcher returns a single
`OperationResult` with no incremental stdin/stdout envelope, the same
deferral `k8s.logs -f` took. `stdin` and `tty` are pinned `False` on the
wire and no code path flips them; both land once the MCP `tools/call`
envelope grows a streaming shape.

G3.2-T6 (CLI alias verbs + k3d E2E acceptance + operator-facing
onboarding doc) layers operator ergonomics on top of this surface
without adding new ops. The CLI verbs in
[`cli/internal/cmd/k8s/`](../../cli/internal/cmd/k8s/) pre-bake
`connector_id="k8s-1.x"` on the existing
`POST /api/v1/operations/call` route; the E2E harness in
[`backend/tests/integration/test_connectors_k8s_e2e.py`](../../backend/tests/integration/test_connectors_k8s_e2e.py)
proves dispatcher -> handler -> k3s round-trips through the
`search_operations` + `call_operation` meta-tools for every registered
op; the onboarding recipe in
[`docs/cross-repo/kubernetes-onboarding.md`](../cross-repo/kubernetes-onboarding.md)
is the operator cookbook for migrating off `kubectl-vcf.sh`.

### Write op surface (G3.14-T1, #1403)

The single-call write ops live in `ops_write.py` (caution-class
handlers + the annotate/label kind dispatch table),
`ops_write_dangerous.py` (dangerous-class handlers + the delete dispatch
table), and `ops_write_meta.py` (the JSON-Schema parameter shapes +
`KubernetesOp` registration rows for **all** write ops, split out so each
handler file stays under the 600-line code-quality cap and the
operator-facing surface reads in one place). Handlers are bound-method
shims on `KubernetesConnector` forwarding to module-level functions --
the same pattern `ops_workload.py` / `ops_logs.py` use. Every op is
`safety_level` caution|dangerous and `requires_approval=True`.

| op_id                  | safety    | k8s call                                                          |
| ---------------------- | --------- | ---------------------------------------------------------------- |
| `k8s.scale`            | caution   | `AppsV1Api.patch_namespaced_deployment_scale` -- before/after replicas in result. |
| `k8s.rollout.restart`  | caution   | Stamp `kubectl.kubernetes.io/restartedAt` on the pod template (kubectl parity). |
| `k8s.namespace.create` | caution   | `CoreV1Api.create_namespace` -- create-or-ignore-409 (idempotent). |
| `k8s.annotate`         | caution   | Strategic-merge patch of `metadata.annotations` over a kind table (deployment/pod/service/namespace/node); null value removes a key. |
| `k8s.label`            | caution   | Same as annotate over `metadata.labels`; relabeling a Service-selected workload can re-route traffic. |
| `k8s.cordon`           | caution   | `CoreV1Api.patch_node(spec.unschedulable)`; `uncordon=true` reverses. Eviction-free. |
| `k8s.apply`            | dangerous | Server-side apply over `DynamicClient.server_side_apply` (`field_manager="meho"`), GVK resolved per manifest doc from discovery. `dry_run="server"` -> API `?dryRun=All` (mutates nothing, returns the would-be object). |
| `k8s.delete`           | dangerous | Kind dispatch **scoped to pod/job/replicaset in v1** (namespace/PVC/PV rejected); explicit `propagation_policy` / `grace_period_seconds`. |
| `k8s.secret.create`    | dangerous | `CoreV1Api.create_namespaced_secret`; values written but never echoed (response is key-names only). |
| `k8s.job.create`       | dangerous | `BatchV1Api.create_namespaced_job` from a spec body; response is identity only. |

**Secret redaction reuses the shipped `credential_write` op-class
(#1401), not a new mechanism.** `k8s.secret.create` was already in
`_CREDENTIAL_WRITE_OPS` (`broadcast/events.py`); G3.14-T1 adds
`k8s.job.create` (a Job pod-template's inline `env` can carry secret
material in `params`). `classify_op` returns `credential_write`, so
`redact_payload` collapses the broadcast event to aggregate-only
`{op_class, result_status}` -- the secret never reaches the SSE stream or
a Slack mirror. The handlers also return value-free summaries, so the
`OperationResult` itself carries no secret.

**`k8s.apply` dry-run preview.** The op supports `dry_run="server"` (maps
to the API's `?dryRun=All`) so the would-be object is returned without
mutating -- the diff-preview an agent or reviewer runs before the real
apply. Note: the dispatcher / approval-queue substrate has **no per-op
`proposed_effect` builder hook**, so the dry-run result is not
auto-populated into the approval row at queue time today; the dry-run is
expressible + returned by the op, and queue-time auto-population is a
follow-up that needs a dispatcher hook.

### Shared list-op request shape (`ops_listparams.py`)

Every namespaced list op on this connector
(`k8s.pod.list`, `k8s.deployment.list`, `k8s.event.list`,
`k8s.service.list`, `k8s.ingress.list`, `k8s.configmap.list`) shares
the same input-parameter shape via the building blocks in
[`ops_listparams.py`](../../backend/src/meho_backplane/connectors/kubernetes/ops_listparams.py):

- `namespace` XOR `all_namespaces` -- the `oneOf` clause
  (`NAMESPACE_XOR_ALL_NAMESPACES`) enforces exactly-one. The
  `all_namespaces=true` path routes through the
  `list_X_for_all_namespaces` variant; the per-namespace path uses
  the `_namespaced_` variant.
- `label_selector` -- forwarded server-side; same K8s selector syntax
  for every op.

Pod / deployment list additionally use the full base via
`LIST_BASE_PROPERTIES`, which adds `field_selector` + `limit` +
`continue_token`. The operator passes `limit=N` plus optional
`continue_token=<cursor>` on each call; the response carries
`next_continue` whenever the server signals more pages. Tokens are
server-defined and expire after ~5-15 minutes; a stale token returns
410 ResourceExpired, and the handler propagates the API exception
verbatim so the caller can restart without the token.

The event / service / ingress / configmap list ops deliberately omit
some knobs of the full base:

- `k8s.event.list` keeps `field_selector` + `limit` but omits
  `continue_token` -- the handler's client-side recency-sort +
  truncation contract supersedes server-side paging; the omission is
  documented in `K8S_EVENT_LIST_PAGINATION_HINT`.
- `k8s.service.list`, `k8s.ingress.list`, `k8s.configmap.list` omit
  `field_selector` + `limit` + `continue_token` -- these resources
  are typically O(10) per namespace and the
  [G0.17-T1](https://github.com/evoila/meho/issues/1330) sweep
  deferred the paging widening as a mechanical follow-up.

See [`docs/codebase/api-shape-conventions.md`](api-shape-conventions.md)
§10 for the convention this shared shape anchors (intra-connector
list-op request-shape parity); the rule applies across connectors
once siblings adopt the pattern.

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
   entry under `"k8s"` is preserved so `get_connector("k8s")`-keyed
   callers (`/api/v1/targets/{name}/probe`) keep resolving the class
   without the dispatcher's v2 resolver path.
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

### Resolver tie-break (wildcard vs. versioned)

K8s is the shipped case for the resolver's
**`versioned_over_wildcard`** demotion step (G0.14-T2 #1143): the
package registers under both `("k8s", "", "")` (v1 wildcard, for
`get_connector` callers) and `("k8s", "1.x", "k8s")` (v2 versioned,
for `connector_id="k8s-1.x"`-keyed dispatch). When a Target is
created with `product="k8s"` and no fingerprint version (the common
first-use case — the operator runs `POST /api/v1/targets` before
`POST /api/v1/targets/{name}/probe`), both registry entries match
the `(product=k8s, version=None)` filter step. The
`KubernetesConnector` class doesn't advertise a
`supported_version_range`, so both entries score
`(_SPECIFICITY_UNBOUNDED, 0.0)` on the specificity ladder.

The resolver's step 1 catches this: when ≥1 entry carries a
non-empty `(version, impl_id)` slot, the wildcard `(product, "", "")`
is demoted before the rest of the ladder runs. The versioned entry
wins; the operator never sees the
`AmbiguousConnectorResolution` bare-500 (signal 9 in
`claude-rdc-hetzner-dc#697`). The rule generalizes to any future
connector that uses the same double-registration shape — only
wildcards lose to a co-registered versioned sibling, never the
reverse. When the wildcard is the *only* candidate (a connector
that registered v1-only), it still wins; the demotion step is
conditional on a versioned entry being present.

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
4. The dispatcher invokes the default
   [`JsonFluxReducer.reduce()`](../architecture/jsonflux.md) (G0.6.1,
   #750) — large row lists materialize into a `ResultHandle`, small
   ones pass through — writes the audit row, fires the broadcast event,
   and wraps the result in `OperationResult.status="ok"`.

### Topology discovery (`discover_topology`)

G0.14-T12 (#1201) lands the first
[`Connector.discover_topology`](../../backend/src/meho_backplane/connectors/base.py)
override against a shipped connector. The populator emits the minimum
the v0.6.0 release-body amendment promised: cluster + namespaces +
nodes, with `belongs-to` edges from each namespace and each cluster
node to the target. The
[refresh service](../../backend/src/meho_backplane/topology/refresh.py)
calls the override on demand
(`POST /api/v1/topology/refresh/{target_name}`) and on the per-tenant
scheduled cadence, diffs the snapshot against `graph_node` +
`graph_edge` rows for `(tenant_id, target_id)`, and applies inserts /
updates / soft-deletes in one transaction.

**What lands on the graph (v0.7-shape)**

| `NodeHint.kind` | Count             | Properties carried                              |
| --------------- | ----------------- | ----------------------------------------------- |
| `target`        | exactly 1         | `git_version` / `major` / `minor` / `platform` from `VersionApi.get_code()` (same payload `k8s.about` returns) |
| `namespace`     | N (≥ 4 on k3s)    | Mirrors [`namespace_row`](../../backend/src/meho_backplane/connectors/kubernetes/ops_core.py): `status` / `age_seconds` / `labels` |
| `node`          | M (≥ 1)           | Mirrors [`node_row`](../../backend/src/meho_backplane/connectors/kubernetes/ops_core.py): `status` / `roles` / `version` (kubelet) / `kernel` / `os` / `internal_ip` / `taints` / `age_seconds` / `labels` |

`EdgeHint` rows: one `belongs-to` from every namespace to the target,
one `belongs-to` from every cluster node to the target. `cluster` is
**not** in the v0.2 [`NodeKind` enum](../../backend/src/meho_backplane/connectors/schemas.py)
(the enum is closed per the module docstring); the cluster manifests
as a `target`-kinded node so the refresh service's natural-key
contract holds without enum changes (which are a G9.2 concern).

**Explicit out-of-scope (sibling Tasks)**

- **Pods** — `CoreV1Api.list_pod_for_all_namespaces()` or N
  `list_namespaced_pod(namespace)` calls. A 100-namespace cluster
  would mean 100 list calls per refresh tick; the v0.7.x deploy
  hasn't surfaced refresh-cost data yet.
- **Services** — same scaling concern as pods.
- **Ingresses** — same.
- **Deployments** — same.
- **Volumes** (`PersistentVolume` cluster-scope + `PersistentVolumeClaim`
  namespaced) — same.

When the cost picture and operator demand justify them, file sibling
Tasks against [Initiative #1139](https://github.com/evoila/meho/issues/1139)
or a future G9.4 Initiative.

**Operator threading**

The [`Connector.discover_topology(self, target)`](../../backend/src/meho_backplane/connectors/base.py)
ABC signature stays unchanged at v0.7 (out of scope for T12). The K8s
override extends the signature with a keyword-only `operator: Operator
| None = None` parameter; the refresh service introspects the bound
method via [`inspect.signature`](https://docs.python.org/3/library/inspect.html#inspect.signature)
and forwards the per-tenant system operator the scheduler synthesises
([`_system_operator`](../../backend/src/meho_backplane/topology/scheduler.py))
when the keyword is declared. Connectors whose override doesn't
declare `operator` (the inherited no-op default, plus any future
override that doesn't need credentials) are invoked verbatim. The
forwarded operator flows through
[`_get_api_client(target, operator)`](../../backend/src/meho_backplane/connectors/kubernetes/connector.py)
so the operator-context Vault → kubeconfig chain reads under the
synthesised identity (the same chain `k8s.about` and every other
operator-aware op already use).

**Where the helpers live**

[`backend/src/meho_backplane/connectors/kubernetes/_topology.py`](../../backend/src/meho_backplane/connectors/kubernetes/_topology.py)
exposes pure functions (`build_target_node_hint`,
`namespace_node_hint`, `node_node_hint`,
`namespace_to_target_edge`, `node_to_target_edge`,
`build_topology_hints`) that re-use `namespace_row` / `node_row` so
the populator and the inventory ops share their wire shape. The
unit suite at
[`backend/tests/test_connectors_k8s_topology.py`](../../backend/tests/test_connectors_k8s_topology.py)
drives synthetic `V1Namespace` / `V1Node` / `VersionInfo` fixtures;
the k3s testcontainer slice in
[`backend/tests/integration/test_connectors_k8s_k3d.py`](../../backend/tests/integration/test_connectors_k8s_k3d.py)
covers the live API round-trip plus the
`(kind, name)` idempotency property the refresh service depends on.

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
- **Credential-backend seam** -- the production kubeconfig loader
  resolves `target.secret_ref` to a kubeconfig dict via the shared
  `load_vault_secret_data` seam (`_shared/vault_creds.py`), which
  dispatches on the ref scheme to the Vault KV-v2 operator-context read
  (default) or GCP Secret Manager (`gsm:`, #2397). Tests and the
  integration suite inject a custom loader.

## JSONFlux handle pattern

Issue #322's "Handle threshold tested: against k3d populated with 50+
namespaces, sample of 20 + handle returned" acceptance criterion
assumed the shared `HandleStore` from G3.1-T4 (#304) would be in place.
#304 was **superseded** -- the Initiative-redraft note on the issue
spells this out -- and the substrate's default reducer is now the
threshold-aware [`JsonFluxReducer`](../architecture/jsonflux.md)
(G0.6.1, #750), which materializes large row lists into a
`ResultHandle` (in-memory DuckDB) and passes small ones through.

The handlers in this connector emit **raw row lists** (`{"rows": [...],
"total": N}`) -- the shape the JSONFlux reducer sees. The reducer, not
the connector, owns the threshold check, the row truncation, the
materialization, and the `ResultHandle` construction. Centralising that
logic in one reducer keeps every typed connector free of per-handler
threshold code; per the substrate split documented on
`meho_backplane.operations.reducer`, set-shaped reduction is the
reducer's job, not the connector's.

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

- Topology populator: [#1201 G0.14-T12](https://github.com/evoila/meho/issues/1201)
  -- `KubernetesConnector.discover_topology` (cluster + namespaces +
  nodes); closes the v0.6.0 release-body amendment promise on
  `claude-rdc-hetzner-dc#697` signal 13.
- Parent Initiative: [#320 G3.2](https://github.com/evoila/meho/issues/320)
  -- `k8s-1.x` typed connector (library: `kubernetes_asyncio`).
- Predecessor Tasks:
  - [#321 G3.2-T1](https://github.com/evoila/meho/issues/321) -- skeleton.
  - [#322 G3.2-T2](https://github.com/evoila/meho/issues/322) -- core
    inventory ops.
  - [#323 G3.2-T3](https://github.com/evoila/meho/issues/323) -- workload
    ops (`k8s.pod.{list,info}` / `k8s.deployment.{list,info}`).
  - [#325 G3.2-T5](https://github.com/evoila/meho/issues/325) -- `k8s.logs`.
  - [#324 G3.2-T4](https://github.com/evoila/meho/issues/324) -- network +
    config + event ops (`k8s.service.list` / `k8s.ingress.list` /
    `k8s.configmap.list/info` / `k8s.event.list`).
- This Task: [#1404 G3.14-T2](https://github.com/evoila/meho/issues/1404)
  -- `k8s.exec` bounded command-and-capture over the `WsApiClient`
  websocket transport (interactive `-it` deferred). Parent Initiative:
  [#1398 G3.14](https://github.com/evoila/meho/issues/1398) -- kubernetes
  write/exec op surface.
- Substrate Initiative: [#388 G0.6](https://github.com/evoila/meho/issues/388)
  -- operation registry + dispatcher + JSONFlux substrate.
- `kubernetes_asyncio`: https://github.com/tomplus/kubernetes_asyncio
- Kubernetes API spec: https://kubernetes.io/docs/reference/kubernetes-api/
- Kubernetes Pod API:
  <https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/>
- Kubernetes Deployment API:
  <https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/deployment-v1/>
