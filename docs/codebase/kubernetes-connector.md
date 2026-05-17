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
  `kubernetes_asyncio.client.ApiClient` per target keyed on
  `target.secret_ref` (the Vault path holding the kubeconfig); the
  cache key is intentionally the secret_ref, not `target.name`, so two
  tenants holding same-named targets get distinct ApiClients.

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
  -- Protocol the connector reads against. Only four attributes:
  `name`, `host`, `port`, `secret_ref`. The G0.3 `Target` model
  satisfies it structurally once G0.3 lands.

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
5. Handler returns `dict[str, Any]`. The dispatcher's
   `PassThroughReducer` lands that dict verbatim into
   `OperationResult.result`. The audit subsystem writes one
   `audit_log` row with `params_hash` derived from the input params
   only -- log contents (or any other returned payload) are never
   written to the audit row.

### `k8s.logs` -- per-call flow

1. Dispatcher validates `params` against `K8S_LOGS_PARAMETER_SCHEMA`
   (pod_name + namespace required; tail capped at 5000; since matches
   `^\s*\d+\s*[smhd]\s*$`).
2. `KubernetesConnector.logs(target, params)` delegates to
   `ops_logs.k8s_logs(connector, target, params)`.
3. `k8s_logs` resolves the `ApiClient` for the target via
   `connector._get_api_client(target)` (cached on secret_ref) and
   builds a `client.CoreV1Api(api_client)`.
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

## Dependencies

* `kubernetes_asyncio>=32,<33` (target K8s 1.32 API; async fork of the
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
