<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Kubernetes op surface onboarding — operator recipe

> Operator-facing recipe for the G3.2 `k8s-1.x` op surface — the
> `meho k8s …` verb tree, the agent meta-tool path, and the migration
> off the consumer's `kubectl-vcf.sh` wrapper. With the G3.10-T4
> [#948](https://github.com/evoila/meho/issues/948) credential-broker
> wiring landed, `meho operation call k8s.<op> target=…` reads the
> kubeconfig out of Vault under the operator's identity and executes a
> real read against the cluster — the rubric **State 2** wiring per
> [`docs/codebase/connector-release-readiness.md`](../codebase/connector-release-readiness.md).
> The op handlers live in
> [`backend/src/meho_backplane/connectors/kubernetes/`](../../backend/src/meho_backplane/connectors/kubernetes/);
> the engineering-facing companion is
> [`docs/codebase/connectors-kubernetes.md`](../codebase/connectors-kubernetes.md);
> the deploy prerequisite is the Vault-policy runbook
> [`connector-vault-policy.md`](./connector-vault-policy.md).
> This doc is the cookbook every RDC operator reads when retiring
> `kubectl-vcf.sh` in favour of `meho k8s …`.

## What "State 2" means here

Per the
[connector release-readiness rubric](../codebase/connector-release-readiness.md):

- **State 1** (where this connector was before G3.10): dispatch +
  catalog only — ops indexed and searchable, but the default
  kubeconfig loader raised `NotImplementedError`, so a real
  `operation call` against a Vault-backed target failed and only the
  injected-loader test path worked.
- **State 2** (what G3.10-T4 ships): the default loader performs the
  live operator-context Vault read for the **`shared_service_account`**
  auth model. `operation call k8s.<op> target=…` executes end to end
  against a real cluster. The kubeconfig YAML lives in Vault at the
  target's `secret_ref` under the field name `kubeconfig`; the loader
  forwards the operator's validated Keycloak JWT to Vault's JWT/OIDC
  auth method, reads the YAML, parses it via
  [`parse_kubeconfig_yaml`](../../backend/src/meho_backplane/connectors/kubernetes/kubeconfig.py),
  and feeds the resulting dict to
  `kubernetes_asyncio.config.new_client_from_config_dict`.
- **State 3** (not yet): every advertised auth model wired; full catalog
  in production rotation. `per_user` (operator-by-operator user
  impersonation via the K8s `User-Impersonation` headers) and
  `impersonation` are not yet wired and a target tagged with either
  raises a clear boundary error.

## What this surface is

The `k8s-1.x` connector is a **typed** connector: hand-coded handlers
against the [`kubernetes_asyncio`](https://github.com/tomplus/kubernetes_asyncio)
SDK (per locked decision #8), registered into the G0.6
`endpoint_descriptor` table at backplane startup. It dispatches under
the `(product="k8s", version="1.x", impl_id="k8s")` registry triple —
the connector id `k8s-1.x`. The `impl_id == product` single-impl shape
mirrors the Vault sibling; the library name `kubernetes_asyncio` lives
in the package layout + `pyproject.toml`, not the registry triple. A
future EKS-specific transport would land as a sibling row under
`("k8s", "1.x", "<other-impl>")` and the dispatcher picks via
`target.preferred_impl_id`.

The v0.2 op surface (Initiative
[#320](https://github.com/evoila/meho/issues/320)) is the **read**
working set the consumer's `kubectl-vcf.sh` exercises daily — write
ops (apply / delete / scale / exec / port-forward) stay in the
wrapper until v0.2.next ships policy + approval flow:

| Group | Ops | Class |
| --- | --- | --- |
| cluster | `k8s.about`, `k8s.ls` | read-only discovery |
| inventory | `k8s.namespace.list`, `k8s.node.list` | read-only |
| workload | `k8s.pod.list`, `k8s.pod.info`, `k8s.deployment.list`, `k8s.deployment.info` | read-only |
| network | `k8s.service.list`, `k8s.ingress.list` | read-only |
| config | `k8s.configmap.list` (keys-only), `k8s.configmap.info` (full data) | read-only |
| events | `k8s.event.list` | read-only |
| logs | `k8s.logs` | read-only |

Every op dispatches through the same `POST /api/v1/operations/call`
route the agent surface uses — auth, policy, audit, broadcast, and
JSONFlux all run as documented in [CLAUDE.md](../../CLAUDE.md) §6. The
CLI verb tree is operator ergonomics over that one route; it is **not**
a separate data path and is **not** mirrored on the MCP surface
(CLAUDE.md postulate 5).

## Prerequisites

- **A reachable Kubernetes API server.** Any cluster: `rke2-meho`,
  `rke2-infra`, Tanzu Supervisor, future EKS / GKE / AKS. The connector
  is API-server-shape-agnostic; the
  [k8s API spec](https://kubernetes.io/docs/reference/kubernetes-api/)
  is the contract.
- **A kubeconfig with read RBAC** for the resources the operator wants
  to query: `get` / `list` / `watch` on namespaces, nodes, pods,
  deployments, services, ingresses, configmaps, events; `get` on
  `pods/log`. A read-only cluster role bound to the kubeconfig's
  service account is the minimum-viable RBAC; the `view` ClusterRole
  ships with every cluster and covers everything except
  `pods/log` (add a `Role` for that in each namespace the operator
  reads logs from, or extend the cluster role).
- **A registered K8s target.** The CLI verbs take `--target <slug>`
  (e.g. `--target rke2-meho`); the slug resolves server-side to a row
  in the `targets` table. The target carries `product="kubernetes"`,
  `host` (the API server FQDN), `port` (default 6443), `secret_ref`
  (the Vault path holding the kubeconfig), and
  `auth_model="shared_service_account"`.
- **The kubeconfig stored in Vault** at the path the target's
  `secret_ref` points to, under field name `kubeconfig`. See "Target +
  auth model" below for the rotation contract.
- **An operator session.** `meho login <backplane-url>` writes the
  session token the CLI reuses across every verb. `meho k8s …` needs
  `operator` role minimum (same gate as every dispatch verb).
- **The federation chain is green** if the kubeconfig lives in Vault.
  `meho vault sys health --target rdc-vault` is the fastest end-to-end
  smoke; fix the chain via
  [`vault-provisioning.md`](./vault-provisioning.md) before troubleshooting
  K8s-side errors.

## Target + auth model

The shipped connector's auth model is **`shared_service_account`** —
the kubeconfig's bundled service-account cred is used regardless of
operator. Per-operator impersonation (via the `User-Impersonation`
headers the K8s API supports) is feasible but requires per-operator
RBAC configuration and is **out of scope** for v0.2.

What this means for the kubeconfig in Vault:

- The kubeconfig YAML lives in Vault at the path the target's
  `secret_ref` points to (e.g. `secret/data/<tenant>/k8s/<cluster>-kubeconfig`)
  under field name `kubeconfig`. The path is the **logical KV-v2 path
  relative to the mount root** — no `secret/` mount prefix and no
  `/data/` segment. It is exactly the string you store as the target's
  `secret_ref` and the string the loader passes to hvac's
  `read_secret_version(path=…, mount_point="secret")`, which inserts
  the `/data/` itself
  ([`connectors/kubernetes/kubeconfig.py`](../../backend/src/meho_backplane/connectors/kubernetes/kubeconfig.py)).
- The backplane reads it lazily on first op invocation per target via
  `load_kubeconfig_from_vault(target, operator)` (G3.10-T4 [#948](https://github.com/evoila/meho/issues/948));
  the operator's validated Keycloak JWT is forwarded to Vault's
  JWT/OIDC auth method via
  [`vault_client_for_operator`](../../backend/src/meho_backplane/auth/vault.py)
  so the read happens **under the operator's Vault Identity entity** —
  per-operator RBAC + per-operator audit. The resulting
  `kubernetes_asyncio.client.ApiClient` is cached per
  `target.secret_ref` and reused across ops (an in-flight per-operator
  cache key swap lands when `per_user` ships).
- Rotation: re-write the kubeconfig in Vault, then restart the
  backplane (the cache lives in-process; future Initiative may add a
  cache-invalidation op). Until then, restart is the rotation path.

### The deploy prerequisite (Vault policy + Keycloak → Vault identity)

The `meho-mcp` Vault role's templated ACL policy must grant read on
the target's `secret_ref`, and the operator's Keycloak JWT `sub` must
match a Vault Identity entity alias on the corresponding identity
provider so the operator's read is attributed to *their* entity rather
than rejected. Without this in place the live read returns Vault 403
(`VaultRoleDeniedError`). The cross-repo deploy runbook
[`connector-vault-policy.md`](./connector-vault-policy.md) documents
the exact policy + identity wiring; the same prerequisite gates the
`vmware-rest-9.0` State 2 path.

### No kubeconfig content leaves the backplane

The State-2 wiring is built so the kubeconfig YAML is ephemeral
in-memory state:

- The loader
  ([`connectors/kubernetes/kubeconfig.py`](../../backend/src/meho_backplane/connectors/kubernetes/kubeconfig.py))
  logs only the target name, host, `secret_ref` path, and the
  requested *field name* (`kubeconfig`) — never any kubeconfig
  content (no server URL, no client certificate, no bearer token).
- The connector's `ApiClient`-build log event carries the target name
  and host only — same discipline.
- The parsed kubeconfig dict is consumed by
  `kubernetes_asyncio.config.new_client_from_config_dict` to build the
  `ApiClient` and then dropped; it never enters the
  `OperationResult`, the audit row payload, or the broadcast event.

This is asserted by the recorded-fixture E2E
([`tests/test_connectors_k8s_credread.py`](../../backend/tests/test_connectors_k8s_credread.py)):
canary server URL + bearer token + client-cert strings are seeded into
the (faked) Vault read and asserted absent from the result, every
captured log event, and the broadcast payload. The live k3d + Vault
E2E ([`tests/integration/test_connectors_k8s_live_vault.py`](../../backend/tests/integration/test_connectors_k8s_live_vault.py))
re-runs the same no-leak assertions against a real k3s + real Vault
chain (env-gated via `MEHO_RUN_LIVE_K3D_VAULT=1`).

## Step-by-step onboarding

### 1. Stage the kubeconfig in Vault

```console
$ vault kv put secret/<tenant>/k8s/<cluster>-kubeconfig \
    kubeconfig=@/path/to/kubeconfig.yaml
```

Use a Vault path under the tenant's namespace (the federation chain's
`meho-mcp` policy already grants read on `secret/<tenant>/*`). The
field name **must** be `kubeconfig` — the connector reads exactly
this key.

### 2. Register the target

```console
$ meho targets create \
    --name rke2-meho \
    --product kubernetes \
    --host k8s.rke2-meho.example.com \
    --port 6443 \
    --secret-ref secret/data/<tenant>/k8s/rke2-meho-kubeconfig \
    --auth-model shared_service_account
```

The `--secret-ref` value matches the path in step 1 with the KV-v2
`/data/` infix (Vault's KV-v2 API path shape). `--port` defaults to
6443 — the standard K8s API server port — and can be omitted.

### 3. Verify

```console
$ meho targets probe rke2-meho
$ meho k8s about --target rke2-meho
```

- `probe` does a kubeconfig-free TLS GET against `/readyz` (falling
  back to `/healthz` for legacy clusters); 200 / 401 both count as ok
  because auth surfaces at op time.
- `about` exercises the full path (Vault → kubeconfig → ApiClient →
  `/version` → `OperationResult`) and is the end-to-end smoke.

A green `about` proves: Vault federation works, the kubeconfig has
the right cluster URL + a valid SA cred, the cluster is reachable,
the dispatcher resolves the descriptor row, and the audit row writes
synchronously. Every K8s op uses the same path; if `about` is green
the rest of the surface is unblocked.

### 4. Sample ops

```console
$ meho k8s namespace list --target rke2-meho
$ meho k8s node list --target rke2-meho
$ meho k8s pod list --target rke2-meho --namespace argocd
$ meho k8s deployment info --target rke2-meho --namespace argocd argocd-server
$ meho k8s logs --target rke2-meho --namespace argocd argocd-server --tail 200
```

## The CLI verb surface

Every verb pre-bakes `connector_id="k8s-1.x"` so operators never type
the connector id. All verbs accept `--target <slug>` (required),
`--json` (emit the full `OperationResult` envelope for `jq`), and
`--backplane <url>` (override the URL from the last `meho login`).
Exit codes mirror `meho operation call`.

### Discovery — `meho k8s about / ls`

```console
$ meho k8s about --target rke2-meho
$ meho k8s ls --target rke2-meho                 # cluster root
$ meho k8s ls --target rke2-meho /argocd         # kind->count summary
$ meho k8s ls --target rke2-meho /argocd/pods    # forwards to k8s.pod.list
```

| Verb | op_id | Notes |
| --- | --- | --- |
| `about` | `k8s.about` | Cluster product / version / platform. No params. |
| `ls [path]` | `k8s.ls` | Three shapes by path: `/` → namespaces + cluster-scoped kinds; `/<ns>` → kind→count summary; `/<ns>/<kind>` → forwards to `k8s.<kind>.list`. |

### Inventory — `meho k8s namespace / node …`

```console
$ meho k8s namespace list --target rke2-meho
$ meho k8s node list --target rke2-meho
```

| Verb | op_id | Result |
| --- | --- | --- |
| `namespace list` | `k8s.namespace.list` | `{rows: [{name, status, age_seconds, labels}], total}` |
| `node list` | `k8s.node.list` | `{rows: [{name, status, roles, version, kernel, os, internal_ip, taints, age_seconds, labels}], total}` |

### Workload — `meho k8s pod / deployment …`

```console
$ meho k8s pod list --target rke2-meho --namespace argocd
$ meho k8s pod list --target rke2-meho --all-namespaces --label-selector app=argocd-server
$ meho k8s pod list --target rke2-meho --namespace kube-system --field-selector status.phase=Running --limit 50
$ meho k8s pod info --target rke2-meho --namespace argocd argocd-server-7c4d8f6b6-abcde
$ meho k8s deployment list --target rke2-meho --namespace argocd
$ meho k8s deployment info --target rke2-meho --namespace argocd argocd-server
```

| Verb | op_id | Notes |
| --- | --- | --- |
| `pod list` | `k8s.pod.list` | `--namespace XOR --all-namespaces`. `--label-selector` / `--field-selector` / `--limit` / `--continue-token` forward the standard k8s knobs. |
| `pod info <name>` | `k8s.pod.info` | Exact name or unique prefix. Ambiguous prefixes return a structured error listing candidates. |
| `deployment list` | `k8s.deployment.list` | Same selector shape as pod list. |
| `deployment info <name>` | `k8s.deployment.info` | Exact name or unique prefix. |

### Network — `meho k8s service / ingress …`

```console
$ meho k8s service list --target rke2-meho --namespace argocd
$ meho k8s ingress list --target rke2-meho --namespace argocd
```

| Verb | op_id | Result |
| --- | --- | --- |
| `service list` | `k8s.service.list` | `{rows: [{name, namespace, type, cluster_ip, external_ips, ports, selector}], total}` |
| `ingress list` | `k8s.ingress.list` | `{rows: [{name, namespace, class, hosts, tls_hosts, rules: [{host, paths: [{path, path_type, service, port}]}]}], total}` |

### Config — `meho k8s configmap …`

```console
$ meho k8s configmap list --target rke2-meho --namespace argocd     # keys only
$ meho k8s configmap info --target rke2-meho --namespace argocd argocd-cm   # full data
```

| Verb | op_id | Notes |
| --- | --- | --- |
| `configmap list` | `k8s.configmap.list` | **Key names only**, never values. The keys-only shape protects against bulk-broadcasting config data to the activity stream. |
| `configmap info <name>` | `k8s.configmap.info` | Full data: `{name, namespace, data, binary_data, metadata}`. Audited as `op_class=read`; G6.3 may upgrade sensitively-named configmaps to `sensitive-read`. |

### Observability — `meho k8s event / logs …`

```console
$ meho k8s event list --target rke2-meho --namespace argocd --field-selector type=Warning
$ meho k8s logs --target rke2-meho --namespace argocd --tail 500 argocd-server
$ meho k8s logs --target rke2-meho --namespace argocd --container argocd-server --since 15m argocd-server
$ meho k8s logs --target rke2-meho --namespace argocd --previous argocd-server  # after a restart
```

| Verb | op_id | Notes |
| --- | --- | --- |
| `event list` | `k8s.event.list` | `--field-selector` (e.g. `type=Warning`) + `--limit` (default 100, capped at 500). Rows sorted most-recent-first. |
| `logs <pod>` | `k8s.logs` | Non-streaming; capped at 1 MiB serialised. `--tail` defaults to 100, capped at 5000. `--container` required for multi-container pods (auto-selected when single-container). `--since` accepts duration strings (`5m`, `1h`, `24h`, `7d`). `--previous` fetches logs from the previous container instance. |

`k8s.logs` truncates oversize payloads **line-boundary from the front**
(most-recent lines kept) and sets `truncated=true` with
`truncated_byte_count`. For live tailing, operators continue using
`kubectl-vcf.sh -f` until v0.2.next ships the streaming transport
(MCP's `tools/call` envelope has no streaming shape as of 2025-06-18).

## The agent meta-tool path

Agents never see `meho k8s …` — those are operator-only CLI
ergonomics. Per [CLAUDE.md](../../CLAUDE.md) postulate 5, an agent
reaches every K8s op through the narrow-waist meta-tools:

```text
search_connectors(query="kubernetes")             → finds k8s-1.x
list_operation_groups(connector_id="k8s-1.x")     → cluster / inventory / workload / network / config / events / logs
search_operations(connector_id="k8s-1.x",
                  query="list pods in a namespace",
                  group="workload")
call_operation(connector_id="k8s-1.x",
               operation_id="k8s.pod.list",
               target={"name": "rke2-meho"},
               params={"namespace": "argocd"})
```

The agent's flow is always: pick connector → list operation groups →
search operations (optionally scoped to a group) → `call_operation`.
The CLI verb table above and the `call_operation` params are 1:1 —
`meho k8s pod list --target rke2-meho --namespace argocd` and the
`call_operation` call above dispatch the identical route, audit row,
and broadcast event. Each op's `llm_instructions` payload (registered
at `register_typed_operation()` time) is what `search_operations`
surfaces to rank and guide the agent; it is reviewable per op group
in
[`backend/src/meho_backplane/connectors/kubernetes/`](../../backend/src/meho_backplane/connectors/kubernetes/)
(`ops.py` (about + logs), `ops_core.py` (ls / namespace / node),
`ops_workload.py`, `ops_network.py`, `ops_config.py`, `ops_events.py`,
`ops_logs.py`).

## JSONFlux handle behaviour for set-shaped ops

The set-shaped K8s ops — `k8s.pod.list`, `k8s.deployment.list`,
`k8s.service.list`, `k8s.ingress.list`, `k8s.configmap.list`,
`k8s.event.list`, plus `k8s.namespace.list` / `k8s.node.list` against
busy clusters — return `{"rows": [...], "total": N}` from the handler.
Per v0.1-spec §4 / CLAUDE.md postulate 6, a set larger than the
JSONFlux threshold (~50 rows / 4 KB) must come back as a sample +
result handle, never the raw list.

The wrapping is the **dispatcher's** job, not the handler's: the
handler returns `{"rows": [...], "total": N}` verbatim and `dispatch`
passes it through the configured `Reducer` before audit/broadcast.

**v0.2 ships only `PassThroughReducer`, so the v0.2 default is
pass-through** — every list op returns the full inline row list with
no handle, regardless of row count. The threshold-aware reducer (and
the `result_query` / `result_aggregate` / `result_describe` /
`result_export` meta-tools that drill into a handle) ship in a follow-on
Initiative; swapping it in touches one `set_default_reducer` call, not
the K8s handlers. Operationally: in v0.2 expect the full row list
inline; when the real reducer lands, large `meho k8s pod list` /
`deployment list` / `event list` results return a handle and you drill
in with the `meho operation` result verbs exactly as for any other
connector's set-shaped op.

For now, operators reading a busy namespace can use the server-side
`--limit` + `--continue-token` knobs on `pod list` / `deployment list`
to page the read at the API server's boundary rather than relying on
JSONFlux. The `k8s.ls /<namespace>` summary uses `limit=1` +
`remaining_item_count` server-side so it stays cheap regardless of
namespace size.

## Audit and broadcast classification

Every K8s op is `op_class=read` — the v0.2 surface is read-only. The
audit row carries the canonical params and result status; the
broadcast event publishes per-tenant for read transparency.

Two ops deserve operator attention:

| op_id | Broadcast class | Why |
| --- | --- | --- |
| `k8s.configmap.list` | `read` (full detail) | Returns key names only — no values are broadcast. |
| `k8s.configmap.info` | `read` (full detail) | **Returns full data.** Values land in the audit row's response payload and the broadcast event. If the configmap holds something cred-ish (CI tokens by mistake, kubeconfig bundles, etc.) it surfaces on the per-tenant feed. G6.3 may upgrade sensitively-named configmaps (e.g. `*-secret-config`) to `op_class=sensitive-read` with the same aggregate-only redaction `vault.kv.read` gets; v0.2 ships the conservative no-special-redaction default. |

For secrets, operators read through Vault (the canonical secret
store), not via the K8s API. `k8s.secret.*` ops are explicitly **out
of scope** for v0.2.

## Migrating off `kubectl-vcf.sh`

The consumer's [`scripts/kubectl-vcf.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/kubectl-vcf.sh)
wraps `kubectl --kubeconfig <target>.yaml <verb>` with per-target
kubeconfig resolution. The `meho k8s` verbs replace it for the
read-only workflows; write workflows stay in the wrapper.

| Wrapper invocation | `meho k8s …` replacement | Notes |
| --- | --- | --- |
| `kubectl-vcf.sh rke2-meho version` | `meho k8s about --target rke2-meho` | `about` returns the full version detail + the product slug heuristic. |
| `kubectl-vcf.sh rke2-meho get namespaces` | `meho k8s namespace list --target rke2-meho` | |
| `kubectl-vcf.sh rke2-meho get nodes` | `meho k8s node list --target rke2-meho` | |
| `kubectl-vcf.sh rke2-meho -n argocd get pods` | `meho k8s pod list --target rke2-meho --namespace argocd` | |
| `kubectl-vcf.sh rke2-meho get pods --all-namespaces` | `meho k8s pod list --target rke2-meho --all-namespaces` | |
| `kubectl-vcf.sh rke2-meho -n argocd describe pod <name>` | `meho k8s pod info --target rke2-meho --namespace argocd <name>` | `info` returns a flat dict instead of the multi-section text shape; `--json \| jq` lets the operator script against it. |
| `kubectl-vcf.sh rke2-meho -n argocd get deployments` | `meho k8s deployment list --target rke2-meho --namespace argocd` | |
| `kubectl-vcf.sh rke2-meho -n argocd describe deployment <name>` | `meho k8s deployment info --target rke2-meho --namespace argocd <name>` | |
| `kubectl-vcf.sh rke2-meho -n argocd get svc` | `meho k8s service list --target rke2-meho --namespace argocd` | |
| `kubectl-vcf.sh rke2-meho -n argocd get ingress` | `meho k8s ingress list --target rke2-meho --namespace argocd` | |
| `kubectl-vcf.sh rke2-meho -n argocd get cm` | `meho k8s configmap list --target rke2-meho --namespace argocd` | **Key names only.** Use `configmap info` for values. |
| `kubectl-vcf.sh rke2-meho -n argocd get cm <name> -o yaml` | `meho k8s configmap info --target rke2-meho --namespace argocd <name>` | Returns JSON; pipe through `--json \| jq -y` if YAML is preferred. |
| `kubectl-vcf.sh rke2-meho -n argocd get events --field-selector type=Warning` | `meho k8s event list --target rke2-meho --namespace argocd --field-selector type=Warning` | |
| `kubectl-vcf.sh rke2-meho -n argocd logs <pod> --tail=200` | `meho k8s logs --target rke2-meho --namespace argocd <pod> --tail 200` | Non-streaming. For live tailing, keep using `kubectl-vcf.sh logs -f`. |
| `kubectl-vcf.sh rke2-meho -n argocd logs <pod> --since=15m --previous` | `meho k8s logs --target rke2-meho --namespace argocd --since 15m --previous <pod>` | |

What `kubectl-vcf.sh` did that `meho k8s` deliberately does **not** do
(out of scope for v0.2 — keep the wrapper for these until a future
Initiative lands them):

- **Write ops** — `apply`, `create`, `patch`, `delete`, `scale`,
  `cordon`, `drain`, `exec`, `port-forward`, `cp`. v0.2 is read-only;
  write ops are v0.2.next pending policy + approval workflow.
- **Streaming** — `kubectl logs -f`, `kubectl exec -it`, watches. The
  MCP `tools/call` envelope has no streaming shape as of 2025-06-18.
- **Helm** — `helm-vcf.sh` is its own wrapper retiring under a future
  `HelmConnector` Initiative.
- **CRDs** — ArgoCD `Application`, cert-manager `Certificate`, etc.
  Stay on `kubectl-vcf.sh` for v0.2; file as G3.x follow-ups if real
  demand surfaces.
- **Multi-cluster federation** — "show me argocd pods across
  rke2-meho AND rke2-infra" is single-target-per-op in v0.2;
  aggregation is a higher-level concern.

Migration discipline: run the `meho k8s` form alongside the wrapper
for an overlap window, diff the outputs, then retire the wrapper call
site. The MEHO path adds the full audit row + broadcast event the
bash pattern never had — that audit coverage is the point of
migrating.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `no backplane URL configured` (exit 2) | Never logged in / no `--backplane`. | `meho login <url>` or pass `--backplane <url>`. |
| `auth_expired` / stored token rejected | Keycloak token expired; refresh failed. | `meho login <url>` again. |
| `status=error … unknown_op` on any `meho k8s` verb | Connector registration didn't run, or the natural-key triple drifted. | Verify the backplane started cleanly; check the connector registered (`meho connector list` shows `k8s-1.x`). If the connector is listed but ops are unknown, the typed-op registrar likely failed during lifespan — check backplane logs for `register_kubernetes_typed_operations`. |
| `status=denied` | `read_only` role, or a tenant policy denied the dispatch. | Use an `operator`-role token. |
| `probe` fails / `about` fails with TLS error | Kubeconfig's `cluster.server` URL is unreachable, wrong cert, or stale. | Test `kubectl --kubeconfig <kubeconfig.yaml> version` against the same file outside MEHO; fix the kubeconfig and re-stage in Vault. |
| `about` returns `status=error` with `unauthorized` / `forbidden` | Kubeconfig's service-account cred is expired, deleted, or lacks RBAC. | Rotate the SA token in the cluster, re-export the kubeconfig, re-stage in Vault, restart the backplane (in-process ApiClient cache). |
| `pod info <name>` returns `connector_error` with multiple candidates | Ambiguous prefix. | Pass the full pod name from a prior `pod list`. |
| `logs <pod>` returns `connector_error` with `MultiContainerAmbiguityError` | Pod has multiple containers and `--container` was omitted. | Re-issue with `--container <name>`; the error message lists the candidate container names. |
| `logs <pod>` returns `truncated=true` | Result hit the 1 MiB serialised cap. | Reduce `--tail` or scope `--since`; the cap is line-boundary from the front (most-recent kept). |
| `configmap info` returns sensitive data on the broadcast feed | v0.2 audits at `op_class=read` with full detail. | Avoid using configmaps for secrets (use Vault); G6.3 will optionally upgrade sensitively-named configmaps to `sensitive-read` (aggregate-only on the feed). |
| Probe ok but every op times out | Kubeconfig's `cluster.server` URL works for `/readyz` but not for the operator-facing endpoints (e.g. through a partial network ACL). | Reach the API server directly from the backplane host and re-test; not a MEHO bug. |

## References

- Initiative: [#320 G3.2 `k8s-1.x` typed op surface](https://github.com/evoila/meho/issues/320); Goal [#214](https://github.com/evoila/meho/issues/214) (G3 connector parity).
- Tasks that shipped this surface: [#321](https://github.com/evoila/meho/issues/321) (T1 skeleton + `k8s.about`), [#322](https://github.com/evoila/meho/issues/322) (T2 inventory), [#323](https://github.com/evoila/meho/issues/323) (T3 workload), [#324](https://github.com/evoila/meho/issues/324) (T4 network/config/events), [#325](https://github.com/evoila/meho/issues/325) (T5 logs), [#326](https://github.com/evoila/meho/issues/326) (T6 CLI verbs + k3d E2E + this doc).
- Engineering companion: [`docs/codebase/connectors-kubernetes.md`](../codebase/connectors-kubernetes.md), [`docs/codebase/kubernetes-connector.md`](../codebase/kubernetes-connector.md).
- Locked decision: [#8 in `docs/planning/v0.2-decisions.md`](../planning/v0.2-decisions.md) — `kubernetes_asyncio` library, single-impl `(k8s, 1.x, k8s)` triple.
- Federation-chain setup (kubeconfig-in-Vault prerequisite): [`vault-provisioning.md`](./vault-provisioning.md).
- Broadcast feed onboarding: [`broadcast-onboarding.md`](./broadcast-onboarding.md). Audit query: [`audit-query.md`](./audit-query.md).
- Op handlers: [`backend/src/meho_backplane/connectors/kubernetes/`](../../backend/src/meho_backplane/connectors/kubernetes/). CLI verbs: [`cli/internal/cmd/k8s/`](../../cli/internal/cmd/k8s/).
- E2E acceptance harness: [`backend/tests/integration/test_connectors_k8s_e2e.py`](../../backend/tests/integration/test_connectors_k8s_e2e.py) (meta-tool flow) + [`backend/tests/integration/test_connectors_k8s_k3d.py`](../../backend/tests/integration/test_connectors_k8s_k3d.py) (handler-level).
- Consumer wrapper retiring (partial — write ops stay): [`scripts/kubectl-vcf.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/kubectl-vcf.sh).
- `kubernetes_asyncio`: <https://github.com/tomplus/kubernetes_asyncio>.
- Kubernetes API spec: <https://kubernetes.io/docs/reference/kubernetes-api/>.
- k3d (CI test cluster): <https://k3d.io/>.
