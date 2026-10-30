# Kubernetes connector (`k8s-1.x`, library `kubernetes_asyncio`)

## Overview

The Kubernetes connector ships read-only typed operations against any
Kubernetes API server reachable from the backplane. It is identified by
the registry-v2 triple `(product="k8s", version="1.x", impl_id="k8s")`
(single-impl pattern, mirroring Vault -- the library name
`kubernetes_asyncio` lives in the package layout + `pyproject.toml`
dependency, not the registry triple) and is the v0.2 successor to the
operator's `kubectl-vcf.sh` daily-wrapper.

Operations register into the G0.6 `endpoint_descriptor` table via
`register_typed_operation()` at connector init -- not via in-code
`_op_map` dicts. Agents reach the ops through the G0.5 meta-tools
(`search_operations` + `call_operation`); the operator-facing CLI
(`meho k8s <op>`) is a thin convenience layer over
`POST /api/v1/operations/call`.

The connector is the reference shape for every typed connector that
follows (Vault is the other example today; bind9, pfSense, Holodeck
copy this pattern in G3.x).

## Key types

* `KubernetesConnector` (`backend/src/meho_backplane/connectors/kubernetes/connector.py`)
  -- the connector class. Inherits from `Connector` (the ABC in
  `meho_backplane.connectors.base`). Class-level attrs `product` /
  `version` / `impl_id` advertise the registry-v2 key. Caches one
  `kubernetes_asyncio.client.ApiClient` (REST) and, lazily, one
  `WsApiClient` (exec/websocket) per target, both keyed on the
  tenant-unique `(tenant_id, id)` tuple from the shared
  `target_cache_key` helper (`connectors/_shared/cache_key.py`). Keying
  on the target-row primary key — not `target.name` and not
  `target.secret_ref` — is a cross-tenant isolation requirement
  (security F04, #266): two targets in different tenants that share a
  name *or* a Vault `secret_ref` must never collapse onto one cached
  client, or the second tenant is served the first tenant's
  authenticated client without passing its own credential-store
  authorization. Both cache fast-paths also apply the
  `is_system_operator` guard (#1008): a system/operator-less caller
  (reachable through the operator-less `k8s.ls` forwarder) always falls
  through to the fail-closed credential loader and is never served a
  warm client a real operator primed. `invalidate_credentials(target)`
  is the duck-typed eviction hook (#2396) the dispatcher's
  establish-auth-failure arm calls and the seam that lets credential
  rotation / target mutation / revocation drop *both* cached client
  types for the target; a WCP Supervisor's normal mid-session token
  expiry is still handled in place by the cached `Configuration`'s
  refresh hook, not by eviction.

* `KubernetesOp` (`backend/src/meho_backplane/connectors/kubernetes/ops.py`)
  -- frozen dataclass describing one op's metadata. Each field mirrors
  the kwargs `register_typed_operation()` accepts (`op_id`, `summary`,
  `description`, `parameter_schema`, `response_schema`, `group_key`,
  `tags`, `safety_level`, `requires_approval`, `llm_instructions`),
  plus a `handler_attr` string naming the bound method on
  `KubernetesConnector` that implements the op.

* `KUBERNETES_OPS` (`backend/src/meho_backplane/connectors/kubernetes/ops.py`)
  -- the tuple of `KubernetesOp` entries the connector registers at
  lifespan startup. Currently carries:

  | op_id        | handler            | scope                  |
  |--------------|--------------------|------------------------|
  | `k8s.about`  | `about`            | cluster identity       |
  | `k8s.logs`   | `logs` (→ `ops_logs.k8s_logs`) | pod log fetch  |

  G3.2-T2 / T3 / T4 add the rest of the 13-op v0.2 read surface
  against this same tuple.

* `KubernetesTargetLike` (`backend/src/meho_backplane/connectors/kubernetes/kubeconfig.py`)
  -- Protocol the connector reads against: `name`, `host`, `port`,
  `secret_ref`, plus `id` / `tenant_id` (the `(tenant_id, id)` cache
  key components, #266). The G0.3 `Target` model carries all of them as
  UUIDs, so it satisfies the Protocol structurally.

* `ops_logs.py` (the `k8s.logs` handler)
  -- contains the module-level `k8s_logs(connector, target, params)`
  function, JSON Schemas (`K8S_LOGS_PARAMETER_SCHEMA`,
  `K8S_LOGS_RESPONSE_SCHEMA`), `LLM_INSTRUCTIONS` blob, and helpers
  (`parse_duration`, `resolve_pod_and_container`,
  `truncate_lines_to_byte_cap`, the `PodNotFoundError` /
  `MultiContainerAmbiguityError` exception types).

## Control flow

### Startup -- registration

1. `meho_backplane.connectors.kubernetes` package init:
   * Calls `register_connector("k8s", KubernetesConnector)` (v1
     registry, backward-compat) and `register_connector_v2(...)` with
     the canonical `(product, version, impl_id)` triple
     (`("k8s", "1.x", "k8s")`).
   * Registers `register_kubernetes_typed_operations` onto the
     lifespan-driven registrar list via `register_typed_op_registrar()`.
2. FastAPI lifespan calls `_eager_import_connectors()`, then
   `run_typed_op_registrars()`.
3. The K8s registrar calls
   `KubernetesConnector.register_operations()`, which walks
   `KUBERNETES_OPS` and feeds each entry to `register_typed_operation()`.
   The helper:
   * Composes an embedding text from summary / description / tags.
   * SHA-256-hashes the text into a `body_hash`.
   * Resolves `handler_ref` from the bound method's `__module__` +
     `__qualname__` (e.g.
     `meho_backplane.connectors.kubernetes.connector.KubernetesConnector.logs`).
   * UPSERTs the `endpoint_descriptor` row. On unchanged body it skips
     the ONNX embedding compute (body-hash skip-re-embed branch).

### Dispatch -- operator/agent call

1. Caller invokes `POST /api/v1/operations/call` with the canonical
   `connector_id="k8s-1.x"` and an `op_id="k8s.<verb>"`. The triple
   parses cleanly through
   `parse_connector_id("k8s-1.x") -> ("k8s", "1.x", "k8s")`.
2. The dispatcher resolves the `endpoint_descriptor` row by
   `(tenant_id, product, version, impl_id, op_id)`.
3. JSON Schema validator (`Draft202012Validator`) checks `params`
   against the descriptor's `parameter_schema`.
4. The dispatcher imports `handler_ref` (via
   `importlib.import_module` + chained `getattr`), binds the unbound
   method against the registered `KubernetesConnector` instance, and
   awaits it as `handler(target=target, params=params)`.
5. Handler returns `dict[str, Any]`. The dispatcher's default
   [`JsonFluxReducer`](../architecture/jsonflux.md) (G0.6.1, #750)
   lands a small dict verbatim into `OperationResult.result`, or
   materializes a large set-shaped one into a `ResultHandle`. The
   audit subsystem writes one `audit_log` row with `params_hash`
   derived from the input params only -- log contents (or any other
   returned payload) are never written to the audit row.

### `k8s.logs` -- per-call flow

1. Dispatcher validates `params` against `K8S_LOGS_PARAMETER_SCHEMA`
   (pod_name + namespace required; tail capped at 5000; since matches
   `^\s*\d+\s*[smhd]\s*$`; since_time / until_time are RFC 3339 times).
2. `KubernetesConnector.logs(target, params)` delegates to
   `ops_logs.k8s_logs(connector, target, params)`.
3. `k8s_logs` resolves the `ApiClient` for the target via
   `connector._get_api_client(target)` (cached on the tenant-unique
   `(tenant_id, id)` key) and builds a `client.CoreV1Api(api_client)`.
4. `resolve_pod_and_container()` lists pods in the namespace, picks
   the exact match if present, falls back to prefix match. Multi-
   container pods without `container` raise
   `MultiContainerAmbiguityError`; pod-not-found / ambiguous prefix
   raises `PodNotFoundError`. Both carry the candidate list in
   `args[1]` so the dispatcher's `connector_error` envelope can
   surface a "did-you-mean".
5. `parse_duration()` resolves `since` (e.g. `"5m"`) to
   `since_seconds=300`.
6. `tail` is defence-clamped to `MAX_TAIL_LINES=5000` even though the
   schema already enforces it.
7. `read_namespaced_pod_log(name, namespace, container, tail_lines,
   since_seconds, previous)` returns the body as a single `str`.
8. Split on `\n`, drop trailing empty line.
9. If `len(body.encode("utf-8")) > MAX_BODY_BYTES (=1 MiB)`,
   `truncate_lines_to_byte_cap()` drops leading lines at line
   boundaries until the kept body fits in 1 MiB. Result carries
   `truncated=True`, `line_count`, `byte_count`,
   `truncated_byte_count` for operator-facing "X KiB dropped" hints.

### `k8s.logs` -- reading an older part of a long log

Without `since_time`, `k8s.logs` returns the newest part of a log:
at most 5,000 lines with `tail`, and at most 1 MiB per call (the
oldest lines are dropped first). To read further back, use a time
window:

* `since_time` -- exact start time (RFC 3339). The handler reads
  forward from that time, oldest line first. It sends no `tailLines`
  unless the caller set `tail`. A call may set `since` or
  `since_time`, not both (`LogWindowError`).
* `until_time` -- exact end time (RFC 3339); needs `since_time`.
  Kubernetes has no end filter, so MEHO drops the lines after it.
* In this mode the handler asks for `timestamps=true`, so every
  returned line starts with its RFC 3339 nanosecond time.
* The window code lives in `ops_logs_window.py`. The request is
  built with `ApiClient.call_api` on the same `/log`
  path, because `kubernetes_asyncio`'s `read_namespaced_pod_log`
  does not accept `since_time`. The call uses
  `_preload_content=False` and reads the body as a stream, 64 KiB at
  a time (`collect_window`). The read stops as soon as the page is
  full or the window has ended, and the connection is closed. So a
  forward read from an old time never pulls the whole rest of the
  log into memory: at most one page plus the line being read.
  With `_preload_content=False` the SDK does not check the HTTP
  status, so `read_pod_log_window` raises `ApiException` itself.
* The API server passes `sinceTime` to the node in whole seconds.
  The handler reads the extra lines from the start of that second and
  throws them away as they arrive, however many there are. So paging
  keeps moving, also when more than a page is written in one second.
* When the window is bigger than 1 MiB, the oldest lines that fit are
  kept. Every kept line counts, so a page is never larger than 1 MiB.
  The answer has `truncated: true`, `truncated_end: "end"`,
  `line_count` and `next_since_time` (the time of the first line that
  did not fit).
* A page ends only at a time boundary. When the line that does not
  fit has the same timestamp as the last kept lines, those lines are
  dropped from the page again and come with the next page. So the
  lines at `next_since_time` are all still to come.
* Paging: call again with `since_time` set to `next_since_time`. The
  start is inclusive, so the next answer starts with exactly the
  lines that did not fit: no line is repeated and none is skipped.
  Repeat until `truncated` is false.
* One log line larger than 1 MiB can never be returned, on this page
  or a later one. The same holds for several lines with the same
  timestamp that are larger than 1 MiB together, because a page
  cannot end between them. The call fails with
  `LogLineTooLargeError`, a plain message that names the time. The
  caller continues behind it with a later `since_time`. Such a line
  before the window is skipped without being stored; one after
  `until_time` just ends the read.
* A long window still comes back behind a result handle, as any long
  `lines` list does.
* Limit: Kubernetes keeps only part of a log. The node rotates old log
  files away, and MEHO cannot read what Kubernetes no longer serves.

## Dependencies

* `kubernetes_asyncio>=36.1,<37` (async fork of the
  official Python client; no thread offload; loads kubeconfig from a
  dict).
* `httpx` -- only used by the kubeconfig-free `probe()` against
  `/readyz` / `/healthz`. The op dispatch path goes through
  `kubernetes_asyncio.client.ApiClient`, not httpx.
* `meho_backplane.operations.typed_register` -- the G0.6 registration
  helper.
* `meho_backplane.connectors.registry` -- v1 + v2 registries.
* `meho_backplane.connectors.schemas` -- `OperationResult`,
  `FingerprintResult`, `ProbeResult`.

## Known issues

* **Pod resolver fetches the full namespace pod list** even when the
  caller already has the exact pod name. G3.2-T3 (#323) ships the
  paginated server-side resolver against `read_namespaced_pod` +
  `field_selector`; `ops_logs.resolve_pod_and_container` switches
  to that path once T3 lands.
* **No log streaming.** v0.2 is non-streaming by design (MCP's
  `tools/call` envelope has no streaming shape in 2025-06-18; the
  connector's `OperationResult` is single-response). Operators
  following live logs continue using `kubectl-vcf.sh -f` until v0.2.next.
* **A burst within one second is read again on every page.** The API
  server sends `sinceTime` to the node in whole seconds, so every
  page starts at the start of the second of `next_since_time`. The
  lines before `next_since_time` are read and thrown away. With a very
  large burst in one second (many MiB), each page spends time reading
  that prefix again. No line is lost, but paging through such a
  second is slow.
* **1 MiB body cap is not configurable per-target.** The cap matches
  the operator's typical terminal scrollback and the MCP envelope's
  practical payload ceiling. A per-target override is v0.2.next.
* **Audit row carries no log content.** This is deliberate (log
  payloads would balloon audit_log rows); the audit row records only
  the request params + `params_hash`. Operators auditing "who pulled
  logs from argocd?" see the access pattern, not the content. If log-
  content audit becomes a requirement, it lands as a separate
  `audit_log_blob` table in a follow-on Goal.

## References

* G3.2 Initiative: [#320](https://github.com/evoila/meho/issues/320) --
  the parent typed-connector initiative.
* G3.2-T1: [#321](https://github.com/evoila/meho/issues/321) --
  connector skeleton (kubeconfig-from-Vault + fingerprint + probe).
* G3.2-T5: [#325](https://github.com/evoila/meho/issues/325) --
  `k8s.logs` op (this addition).
* G0.6: [#388](https://github.com/evoila/meho/issues/388) -- the
  `register_typed_operation()` substrate this depends on.
* G0.6-T-Refactor-K8s: [#391](https://github.com/evoila/meho/issues/391)
  -- migration of the skeleton from `_op_map` to the typed-op registry.
* `kubernetes_asyncio` -- https://github.com/tomplus/kubernetes_asyncio
* K8s `/log` API:
  https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.32/#read-log-pod-v1-core
