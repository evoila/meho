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
- **Core-group object reads** (`ops_core_objects.py`, #3840) --
  `list_core_objects` / `get_core_object` send the `k8s.cr.*` GET to
  the core `/api/{version}` root when `group` is empty, and
  `list_items` copies the list kind onto items that lack one.
- **Access checks** (`ops_access.py`, #3840) -- `access_review_body`,
  `access_review_result`, `rules_review_result` plus the `ACCESS_OPS`
  rows for `k8s.auth.can_i` / `k8s.auth.rules`.
- **Admission webhooks** (`ops_admission.py`, #3840) --
  `admission_webhook_rows` plus the `ADMISSION_OPS` row for
  `k8s.admission_webhook.list`.
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

## vSphere Supervisor (WCP) SSO auth mode (#2905)

A **vSphere Supervisor** (the WCP "Supervisor cluster" fronting a
vSphere workload domain) is a real Kubernetes cluster people want to
observe, but it **issues no static/durable credential**: WCP owns the
cluster RBAC and even `administrator@vsphere.local` is not cluster-admin,
so the read-only-ServiceAccount + non-expiring-token recipe every normal
appliance/kubeadm cluster allows is unavailable. The only credential a
Supervisor hands out is the **short-lived vSphere-SSO token** from its
`POST /wcp/login` exchange (it expires with the SSO session). The static
`kubeconfig` path can't consume that durably — so the Supervisor was the
one k8s tier the connector couldn't register.

The WCP mode makes the connector hold the SSO credential and run the
login dance itself — the same shape `kubectl vsphere login` /
`kubelogin` implement — to **mint → cache → refresh** the Supervisor
token transparently.

### Credential-protocol discrimination (not `auth_model`)

`load_kubernetes_credential`
(`connectors/kubernetes/kubeconfig.py`) reads `target.secret_ref` **once**
through the same `load_vault_secret_data` seam the kubeconfig path uses,
then routes on the payload's **field shape** — the same pattern the
GitHub connector uses for its App-vs-PAT split, deliberately **not**
overloading the target's `auth_model` (that is the *identity* model —
`shared_service_account` etc. — not a credential protocol):

- a `kubeconfig` field → `KubeconfigCredential` (today's static path,
  checked **first** so an existing kubeconfig secret is never
  re-interpreted);
- else `username` + `password` fields → `WcpSsoCredential` (the WCP SSO
  path — the username is the fully-qualified SSO principal, e.g.
  `administrator@vsphere.local` or a scoped read-only namespace user, so
  no separate realm field is needed);
- neither → `VaultCredentialsReadError`.

A Supervisor target therefore differs from a normal k8s target **only**
in the shape of the credential its `secret_ref` holds. It still
registers as `product="k8s"`, still resolves through the single
`("k8s","1.x","k8s")` connector, and needs **no target-schema change** —
the fingerprint/resolver/target model are untouched (a whole sibling
connector class would be the wrong altitude for an auth-only difference).

### Mint → cache → refresh (`connectors/kubernetes/wcp.py`)

`build_wcp_api_configuration` performs the initial
`wcp_login` (`POST https://{host}:443/wcp/login`, HTTP Basic, **no body**
— which selects the top-level Supervisor context, not a
`guest_cluster_*` per-workload sub-session), takes the Supervisor CA from
the response's `kube_config`, and builds a
`kubernetes_asyncio.client.Configuration` whose bearer refreshes itself.
The **embedded `kube_config` is optional**: a Supervisor 9.x behind the
Foundation LB answers with `kube_config: null`, and `session_id` alone is
the bearer (what `kubectl vsphere login` uses). When it is absent, the
mapping is synthesised from the target — server `https://{host}:{port}`,
the target's pinned CA + `tls_server_name`, `session_id` as the token —
so the rest of the build path is identical (#3835). The target's
`verify_tls` / `tls_server_name` always win over anything an embedded
mapping carried.
The refresh rides `kubernetes_asyncio`'s async-capable
`refresh_api_key_hook`: `Configuration.get_api_key_with_prefix` awaits it
before reading `api_key["BearerToken"]` on **every** request, so the hook
re-mints once the cached token is inside `WCP_TOKEN_REFRESH_MARGIN_SECONDS`
of expiry (single-flight under a lock). Expiry is stamped from the
token's JWT `exp` claim (read unverified — it's our own token, used only
to schedule refresh) with a short `DEFAULT_WCP_TOKEN_TTL_SECONDS` fallback
for a non-JWT token. The cached `ApiClient` (keyed on `secret_ref` like
the static path) stays put across the token's lifetime — only its bearer
rotates — so both the read `ApiClient` and the exec `WsApiClient` get the
self-refreshing client with no per-op login round-trip.

### Internal-VIP redirect avoidance + TLS bootstrap

The `/wcp/login` response's `kube_config` points its `server` at the
Supervisor's **raw internal VIP**, which is unreachable when the
Supervisor is dialed over a NAT'd alias (a lab / operator-VPN norm). The
connector keeps that CA but **overrides the host to the
operator-reachable `target.host`** (the top-level Supervisor kube-API
context) and never follows the internal-VIP workload-session redirects.
Because it deliberately dials the alias rather than the cert's VIP SAN,
the cert is verified against the target's **`tls_server_name`** (falling
back to the dial host) so the Supervisor **CA chain stays verified with
hostname checking on** — a NAT-fronted Supervisor whose cert SANs the
internal VIP works with verification enabled when `tls_server_name` names
that SAN.

`tls_server_name` is threaded onto **both legs** — the `/wcp/login` POST
(httpx `extensions={"sni_hostname": …}`) and the kube-API leg
(`Configuration.tls_server_name`, which `kubernetes_asyncio` maps onto the
aiohttp `server_hostname`) — via one shared TLS-policy builder, so the
login exchange no longer fails hostname verification while the API leg
succeeds (#3832). A hostname/CA mismatch on the login leg now surfaces as
`WcpLoginError: TLS verification failed for '<host>' (server_name
'<name>')…` rather than a bare `ConnectError`.

TLS for the login POST itself follows the target's knobs exactly like the
shared HTTP transport: a `tls_ca_pin` (the Supervisor CA, staged
out-of-band) is verified against; otherwise `verify_tls` toggles
system-CA verification. A self-signed Supervisor therefore needs either
the CA pinned on the target (recommended) or `verify_tls=false` (lab) —
the same trust bootstrap `kubectl vsphere login` needs a thumbprint or
`--insecure-skip-tls-verify` for. No SSO credential or minted token is
ever logged (structlog events carry host / port / mode only).

### Operator staging

Register the Supervisor as an ordinary k8s target (`product: k8s`,
`host:` the reachable Supervisor address, `port:` the kube-API port,
default 6443) and stage its `secret_ref` with **`username` + `password`**
fields (vSphere SSO) instead of a `kubeconfig` field. Pin the Supervisor
CA via the target's `tls_ca_pin` (recommended) or set `verify_tls=false`
for a lab. When the Supervisor is reached through a NAT alias whose
address is **not** in the cert's SAN, set `tls_server_name` to the address
the cert SANs (the internal VIP) so both the login and API legs verify the
cert against it with hostname checking on. **Least privilege:** the SSO
super-admin works but the
recommended long-term credential is a scoped read-only vSphere SSO user
granted a read-only vSphere-Namespace role; the connector is
credential-agnostic and uses whatever SSO pair is staged. Sibling
issue #2902 (operator-CLI login-token longevity) is a different surface
(Keycloak OIDC) and shares no implementation with this WCP token dance.

## Passive-only kubeconfig schema (F03, #265)

A static kubeconfig is read from a per-target credential secret (Vault
KV-v2 / GSM). Target creation is tenant-admin gated, so the kubeconfig is
**tenant-controlled** — but a tenant's right to manage its own credential
must not translate into code execution or arbitrary file reads inside the
shared backplane process. `kubernetes_asyncio`'s kubeconfig loader
honours the full client-side feature set, three parts of which reach an
out-of-band sink the moment the document is loaded:

- an **`exec` credential provider** spawns a *subprocess* with the
  configured command/args (`KubeConfigLoader.load_from_exec_plugin`);
- a legacy **`auth-provider`** block (`gcp` / `oidc` / vendor plugins)
  performs a *network token fetch/refresh* (`load_gcp_token` /
  `_load_oid_token`);
- a **file-path reference** — cluster `certificate-authority`, user
  `client-certificate` / `client-key` / `tokenFile` — makes the loader
  *read that local path* off the backplane's own filesystem (`FileOrData`
  falls back to the `<key>` file whenever the inline `<key>-data` sibling
  is absent).

`connectors/kubernetes/kubeconfig_schema.py` (`enforce_passive_kubeconfig`)
enforces a minimal server-side schema **before** the parsed mapping can
reach that loader. It:

- **rejects** the three sink classes above with a specific, fail-closed
  `UnsupportedKubeconfigError` (a `ValueError` subclass, so it flows
  through the loaders' documented `ValueError` contract and the
  dispatcher's catch-all → `connector_error`). The message names only the
  offending section/field — never a credential value, endpoint, or
  certificate — so it is safe to log (preserves the no-leak canary
  discipline);
- **explicitly validates** the cluster `server` endpoint (must be an
  http(s) URL with a host), `proxy-url` (http/https/socks5 URL with a
  host), `insecure-skip-tls-verify` (boolean), `tls-server-name`, and the
  inline `certificate-authority-data`;
- **rebuilds a fresh mapping** from only the accepted fields — the
  untrusted input mapping is never returned or handed to the library.
  Only inline authentication survives: a bearer `token`, HTTP-basic
  `username`/`password`, and inline `client-certificate-data` /
  `client-key-data` / cluster `certificate-authority-data`.

Enforcement sits at the trust boundary — the Vault/GSM read — in **both**
production loaders (`load_kubernetes_credential`, the default, and the
legacy-injectable `load_kubeconfig_from_vault`), immediately after
`parse_kubeconfig_yaml`. `parse_kubeconfig_yaml` itself stays a pure
parse; the injectable `kubeconfig_loader=` / `credential_loader=` test
seams build dicts directly and are trusted (never a tenant secret).

The **WCP SSO path is unaffected**: a Supervisor secret carries
`username`/`password`, resolves to a `WcpSsoCredential`, and never
constructs a kubeconfig dict — so `enforce_passive_kubeconfig` is not on
that path.

No executable-provider facility is retained: `exec` is rejected
unconditionally, with no parameter or elevation re-enabling it. A
deployment that genuinely needs exec-plugin credentials would run that
behind a separate platform-admin-managed, isolated facility (out of scope
here). This mirrors the Kubernetes upstream warning that untrusted
kubeconfig can cause code execution or file exposure
([kubeconfig guidance](https://kubernetes.io/docs/concepts/configuration/organize-cluster-access-kubeconfig/)).

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
| `k8s.node.list`        | safe   | `CoreV1Api.list_node()` -- status / roles / version, plus `system_uuid` (the node's BIOS UUID), `os_image`, `architecture`, `cpu_capacity`, `memory_capacity` (`_node_identity.py`). |
| `k8s.pod.list`         | safe   | Pods with selectors + server-side pagination.                     |
| `k8s.pod.info`         | safe   | Full pod detail; exact name or unique prefix. Includes init containers, their statuses, and `image_pull_secrets` (names only). |
| `k8s.deployment.list`  | safe   | Deployments with live replica counts + image + strategy.          |
| `k8s.deployment.info`  | safe   | Full deployment detail; exact name or unique prefix. Includes `image_pull_secrets` (names only). |
| `k8s.job.list`         | safe   | `BatchV1Api.list_namespaced_job()` / `list_job_for_all_namespaces()` -- one row per Job with a one-word `state`, pod counts, `completions`, start / completion times. Same selectors + paging as `k8s.pod.list`. (`ops_job.py`) |
| `k8s.job.info`         | safe   | One Job by exact name or unique prefix: the row fields plus conditions (reason + message), `backoff_limit`, `active_deadline_seconds`, template containers, `pod_selector` and up to 50 of its pods, newest first. (`ops_job.py`) |
| `k8s.workload.list`    | safe   | `AppsV1Api` list for one `kind` (Deployment / StatefulSet / DaemonSet): desired `replicas` (DaemonSet: pods it should run), per-container `requests` / `limits` as API quantities, images, `volumes` with `claim_name`, StatefulSet `volume_claim_templates`, and `pod_selector`. `containers` holds the app containers and the sidecars (init containers with `restartPolicy: Always`); plain init containers are left out. Same selectors + paging as `k8s.deployment.list`. (`ops_workload_set.py`) |
| `k8s.service.list`     | safe   | `CoreV1Api.list_namespaced_service()` / `list_service_for_all_namespaces()` -- type / cluster_ip / ports / selector / lb_ingress (LoadBalancer VIP from `status.loadBalancer.ingress`, `[]` otherwise) + `label_selector`. |
| `k8s.ingress.list`     | safe   | `NetworkingV1Api.list_namespaced_ingress()` / `list_ingress_for_all_namespaces()` -- class / hosts / TLS / rules + `label_selector`. |
| `k8s.configmap.list`   | safe   | `CoreV1Api.list_namespaced_config_map()` / `list_config_map_for_all_namespaces()` -- **keys only, NO values** + `label_selector`. |
| `k8s.configmap.info`   | safe   | `CoreV1Api.read_namespaced_config_map()` -- full data + binary_data. |
| `k8s.event.list`       | safe   | `CoreV1Api.list_namespaced_event()` / `list_event_for_all_namespaces()` -- pulls up to `MAX_EVENT_LIMIT` (500) rows, sorts client-side by `last_seen` desc, truncates to caller's `--limit`. Server has no `lastTimestamp` ordering guarantee. EventSeries `count` honoured. Forwards `label_selector` + `field_selector`. |
| `k8s.storageclass.list` | safe  | `StorageV1Api.list_storage_class()` -- name / provisioner / `is_default` (GA `storageclass.kubernetes.io/is-default-class` annotation) / reclaim_policy / volume_binding_mode / allow_expansion. Cluster-scoped, no params. (`ops_storage.py`) |
| `k8s.persistentvolume.list` | safe | `CoreV1Api.list_persistent_volume()` -- name / phase / capacity / storage_class / claim_ref (`namespace/name`) / access_modes / reclaim_policy. Cluster-scoped, no params. (`ops_storage.py`) |
| `k8s.persistentvolumeclaim.list` | safe | `CoreV1Api.list_namespaced_persistent_volume_claim()` / `list_persistent_volume_claim_for_all_namespaces()` -- name / namespace / status / capacity (`status.capacity.storage`) / storage_class / volume_name / access_modes. `namespace` XOR `all_namespaces`. Backs `k8s.ls /<ns>/persistentvolumeclaims`. (`ops_storage.py`) |
| `k8s.crd.list`         | safe   | `ApiextensionsV1Api.list_custom_resource_definition()` -- group / kind / plural / scope / versions (`{name, served, storage}`). Discovery op for the generic CR reads. (`ops_customresource.py`) |
| `k8s.cr.list`          | safe   | Generic CR list over `CustomObjectsApi.list_namespaced_custom_object()` (with `namespace`) / `list_cluster_custom_object()` (without). Rows = metadata + bounded JSON `spec_excerpt` (cap `CR_SPEC_EXCERPT_MAX_BYTES` = 2048 B, `spec_truncated` flag). Requires `group`/`version`/`plural`. `group: ""` reads the core group under `/api/{version}` instead (`ops_core_objects.py`, #3840). (`ops_customresource.py`) |
| `k8s.cr.info`          | safe   | Generic single CR read over `CustomObjectsApi.get_namespaced_custom_object()` / `get_cluster_custom_object()`. Single-object projection (metadata + bounded `spec_excerpt`), not a rows envelope. Requires `group`/`version`/`plural`/`name`. `group: ""` reads one core-group object under `/api/{version}`. (`ops_customresource.py`) |
| `k8s.auth.can_i`       | safe   | `AuthorizationV1Api.create_self_subject_access_review()` -- may the target's kubeconfig identity do one verb on one resource (or one non-resource path)? `{allowed, denied, reason, evaluation_error}`. Same as `kubectl auth can-i`. (`ops_access.py`, #3840) |
| `k8s.auth.rules`       | safe   | `AuthorizationV1Api.create_self_subject_rules_review()` -- every rule that identity has in one namespace, one row per rule, plus `incomplete`. Same as `kubectl auth can-i --list`. (`ops_access.py`, #3840) |
| `k8s.admission_webhook.list` | safe | `AdmissionregistrationV1Api.list_validating_webhook_configuration()` + `list_mutating_webhook_configuration()` -- one row per webhook: rules, selectors, `failure_policy`, `side_effects`, `timeout_seconds`, service or URL. `caBundle` is left out. Optional `type` filter. (`ops_admission.py`, #3840) |
| `k8s.logs`             | safe   | `CoreV1Api.read_namespaced_pod_log()` non-streaming -- tail / container / since / previous + 1 MiB cap; since_time / until_time read an older time window, oldest line first. `container` also accepts an init container name. |
| `k8s.exec`             | **dangerous** (`requires_approval=True`) | `CoreV1Api.connect_get_namespaced_pod_exec()` over the `WsApiClient` websocket transport -- bounded argv command-and-capture: stdout / stderr demuxed from the `v4.channel.k8s.io` channels + exit code parsed from the channel-3 status frame, per-stream 1 MiB cap, bounded timeout. Interactive `-it` deferred. |
| `k8s.secret.list`     | safe   | `CoreV1Api.list_namespaced_secret()` / `list_secret_for_all_namespaces()` -- one row per Secret: name / namespace / type / `keys` (names only) / `key_count` / labels / `annotation_keys` (names only) / `owner_count` / created / resource_version. **Never a value.** Same `namespace` XOR `all_namespaces` + `label_selector` params as `k8s.configmap.list`. Needs `list` on `secrets`. (`ops_secret_list.py`) |
| `k8s.secret.info`     | safe   | `CoreV1Api.read_namespaced_secret()` -- the list row fields plus `immutable`, `owners` (kind + name) and `key_sizes` (bytes per key). **Never a value.** Needs `get` on `secrets`. (`ops_secret_list.py`) |
| `k8s.secret.read_to_ref` | **caution** (`requires_approval=True`) | `CoreV1Api.read_namespaced_secret()` -- reads one Secret's `data[data_key]` (default `value`), decodes it, and stages the value to a **tenant-scoped Vault `secret_ref`** via `vault.kv.put`; returns **only** the ref + provenance (SHA-256 / byte length), never the value. Classified `credential_read` (audit + broadcast aggregate-only). The governed VKS guest-kubeconfig read (`ops_secret_read.py`, #3496). |
| `k8s.secret.compare_to_ref` | safe | `CoreV1Api.read_namespaced_secret()` -- checks that one Secret key holds the same value as its Vault copy `{path, key, mount?}`. Answers only `same` and, per copy, `found` / `matches_first` / version; never a value or a hash. Classified `credential_read`. (`ops_secret_compare.py`) |

The multi-step read `k8s.migration.inventory` (group `migration`, `safe`)
collects the cluster's nodes and workloads in the common migration inventory
shape. It lives in `connectors/inventory/source_k8s.py`; see
`connectors-inventory.md`.

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
`KubernetesOp` registration rows for the write ops above, split out so each
handler file stays under the 600-line code-quality cap and the
operator-facing surface reads in one place). The two Secret change ops
(`k8s.secret.update` / `k8s.secret.delete`) keep their handlers, schemas
and rows together in `ops_secret_write.py`, with their previews in
`ops_secret_write_preview.py`. Handlers are bound-method
shims on `KubernetesConnector` forwarding to module-level functions --
the same pattern `ops_workload.py` / `ops_logs.py` use. Every op is
`safety_level` caution|dangerous (`k8s.secret.delete`: destructive) and
`requires_approval=True`.

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
| `k8s.secret.create`    | dangerous | `CoreV1Api.create_namespaced_secret`; values written but never echoed (response is key-names only). Values may be inline (`string_data` / `data`) or ref-sourced (`string_data_secret_ref` / `data_secret_ref`, resolved server-side through the credential-backend seam). Optional `labels` / `annotations` maps go on the Secret's metadata. |
| `k8s.job.create`       | dangerous | `BatchV1Api.create_namespaced_job` from a spec body; response is identity only. |
| `k8s.cronjob.run`      | caution   | `BatchV1Api.read_namespaced_cron_job`, then `BatchV1Api.create_namespaced_job`: one Job from the CronJob's job template, now (`ops_cronjob.py`). |
| `k8s.secret.update`    | dangerous | `CoreV1Api.patch_namespaced_secret` (mode `patch`, keeps other keys) or `read_namespaced_secret` + `replace_namespaced_secret` (mode `replace`, keeps only the given keys). Same value inputs as `k8s.secret.create`; response is key names only. `ops_secret_write.py`. |
| `k8s.secret.delete`    | **destructive** | `CoreV1Api.delete_namespaced_secret`. Preview first, then call with the `preview_hash`; a second person must approve; an agent cannot run it. `ops_secret_write.py`. |

**`k8s.cronjob.run` (`ops_cronjob.py`).** Starts a CronJob's job without
waiting for its schedule, the way `kubectl create job <name>
--from=cronjob/<cronjob>` does. The Job gets the annotation
`cronjob.kubernetes.io/instantiate: manual`, the job template's labels and
annotations, one owner reference to the CronJob (`controller: true`,
`blockOwnerDeletion` unset because that needs a right on
`cronjobs/finalizers`) and a copy of `spec.jobTemplate.spec`. Without
`job_name` the Job uses `generateName: <cronjob>-manual-` and the result
carries the name the API server chose. A missing CronJob raises
`WorkloadNotFoundError` before anything is created; a suspended CronJob
still runs. The params and the result hold only names, so the op is not
pinned `credential_write`. Its approval preview (`ops_write_preview.py`)
reads the CronJob and shows the schedule, `suspend`, `concurrencyPolicy`,
the names of its Jobs that still run and each container's name, image,
env var names and command size, never an env value or the command text.

**Secret redaction reuses the shipped `credential_write` op-class
(#1401), not a new mechanism.** `k8s.secret.create` was already in
`_CREDENTIAL_WRITE_OPS` (`broadcast/events.py`); G3.14-T1 adds
`k8s.job.create` (a Job pod-template's inline `env` can carry secret
material in `params`). `classify_op` returns `credential_write`, so
`redact_payload` collapses the broadcast event to aggregate-only
`{op_class, result_status}` -- the secret never reaches the SSE stream or
a Slack mirror. The handlers also return value-free summaries, so the
`OperationResult` itself carries no secret.

**`k8s.secret.create` ref-valued params (#2860).** Redaction keeps a
secret off audit/broadcast once it is *in* `params`, but an agent driving
the op still had to put the value there -- and an inline value transits
the caller's context (the model API) before MEHO ever sees it. To close
that leak vector, `k8s.secret.create` accepts ref-valued siblings that are
resolved **server-side, mid-dispatch**, so a Vault/GSM-resident value
materialises into the Secret without the value ever entering the caller's
context (the same pattern as keycloak's `password_secret_ref`, #2401):

- `string_data_secret_ref` / `data_secret_ref` -- a
  `{secret-key: "<scheme>:<vault-or-gsm-path>#<field>"}` map. Each ref is
  read under the operator's identity via
  [`load_vault_secret_data`](../../backend/src/meho_backplane/connectors/_shared/vault_creds.py)
  (the same credential-backend seam the kubeconfig loader rides -- no new
  connector dependency), and the resolved key/value is merged into
  `string_data` (plaintext) / `data` (base64) respectively.
- `string_data_secret_mount` / `data_secret_mount` -- the Vault KV-v2
  mount for the corresponding ref map (default `secret`; backends without
  a mount concept, e.g. GSM, ignore it).
- The `anyOf` widens so a call may supply inline maps, ref maps, or both
  (merged into one Secret; refs win a key collision). Multi-key Secrets
  (the ArgoCD spoke token + CA case) work natively -- one ref per key.
- The optional `#<field>` fragment on a ref names which field of the
  resolved payload to use (default: the Secret key). It is split off in
  the connector *before* the seam read -- a Vault KV-v2 path cannot carry
  a `#` (hvac would 404), so the caller owns fragment->field selection
  uniformly across the `vault:` and `gsm:` schemes. Backend-agnostic for
  free: the seam dispatches on scheme, so a `gsm:` ref resolves on a GSM
  deployment with no extra connector code.

The redaction above is unchanged and still applies: the op stays pinned in
`_CREDENTIAL_WRITE_OPS`, and only the *ref* (never a value) is ever in
`params`, so the audit row hashes a path and the broadcast stays
aggregate-only. A resolved payload missing the requested field raises
`KubernetesSecretRefError` (never a bare `KeyError`, never echoing a
value).

**`k8s.secret.create` labels and annotations (#3680).** Some tools find a
Secret by its label. ArgoCD, for example, reads repository credentials
from Secrets labelled `argocd.argoproj.io/secret-type: repository`. So
`k8s.secret.create` takes two optional string maps:

- `labels` -- set as the new Secret's `metadata.labels`.
- `annotations` -- set as the new Secret's `metadata.annotations`.

With them, one call (and one approval) creates such a Secret, also when
its value comes from a `*_secret_ref`. Before, this took a create plus a
`k8s.apply` to add the label. Labels and annotations are not secret: the
approval preview shows them in full. Any other unknown key is still
rejected (`additionalProperties: false`). `k8s.secret.update` keeps the
labels and annotations of an existing Secret as they are.

**`k8s.apply` dry-run preview.** The op supports `dry_run="server"` (maps
to the API's `?dryRun=All`) so the would-be object is returned without
mutating -- the diff-preview an agent or reviewer runs before the real
apply. When the op waits for approval, the `k8s.apply` preview builder in
`operations/_preview.py` runs this dry-run and stores the result on the
approval row (see `approvals.md`). Next to the dry-run summary it stores
the manifest itself (`preview.manifest`, one dict per document) and a
`preview.subject` line such as `Deployment/prod/web` (#3877). Before the
manifest is stored, `apply_manifest_echo` in `ops_write_preview.py` hides
Secret `data` / `stringData` values (key names stay), every `env` value
(variable names stay), every `httpHeaders` value of a probe or lifecycle
hook (header names stay), the text of every `command` / `args` list (only
its size in bytes stays, as `{"hidden": true, "bytes": N}`) and the
last-applied annotation. All of this is done at any depth: a Secret nested
in a custom resource and a pod template's copy of the annotation are hidden
too. Hiding a value in an `env` or `httpHeaders` entry does not stop the
walk: the entry's other fields are checked as well. Then the shared
params redaction of the generic echo runs over the documents. The size
limit comes last, after all redaction, because a redaction marker can be
longer than the value it replaces: the echo keeps at most 16 KB in total
as stored, counting every entry. A document that does not fit in full
keeps only its identity; when not even that fits, one closing entry
counts the documents left out.

**`k8s.apply` API-server rejections (#3876).** `DynamicClient.request`
calls the API with `_preload_content=False`, and kubernetes_asyncio only
raises `ApiException` for a non-2xx answer when `_preload_content` is
true. So the library hands back a rejection (an admission webhook 403, a
422 invalid object, a 409 field conflict) as a normal response with a
`Status` failure body. `_apply_one` therefore calls `server_side_apply`
with `serialize=False`, checks the HTTP status itself, and raises
`ApplyRejectedError` (a `ConnectorUpstreamError`) for any non-2xx answer,
on the real apply and on the dry-run. The result is `error` with
`extras.http_status` and the API server's `message` in
`extras.upstream_message`. The approval preview shows the rejection
(`preview_unavailable` with the message in `preview_error`) instead of a
clean-looking summary with no `resourceVersion` and no `uid`. The preview
message is redacted before it is stored, like `extras.upstream_message`,
so a denial that quotes a credential does not put it into the approval
row.
`ApplyNotPersistedError` is now only raised for a 2xx real apply with no
`resourceVersion`. `k8s.apply` is the only op that uses `DynamicClient`.
The other ops use the generated API classes, which raise `ApiException`
on a non-2xx answer (`k8s.exec` opens a websocket; a refused upgrade
raises from aiohttp). The discovery reads that
`DynamicClient` makes before the apply go through the same unchecked
path, but a denied discovery read still ends in an error (usually
`ResourceNotFoundError`), not a false success.

**`k8s.apply` API-server warnings (#3842).** The API server can accept
an apply and still send `Warning` response headers, for example when the
manifest uses a deprecated `apiVersion`. Each entry in `applied` has a
`warnings` list with the text of these headers for that document (an
empty list when there are none). This works on the real apply and on the
dry-run, so the approval preview shows the warnings too. The header form
is `299 - "text"`; several warnings can come as several header lines or
comma-separated in one line. A header in an unknown form is kept as it
is. Each text is redacted with the default policy before it is returned,
because an admission webhook can write any text into a warning. Other
write ops do not return warnings yet.

### Change or delete an existing Secret -- `k8s.secret.update` / `k8s.secret.delete`

`k8s.secret.create` only creates (a second create answers 409), and
`k8s.delete` accepts only pod / job / replicaset. So a credential change
(a new token or password in a Secret a workload already reads) and the
removal of a Secret had no governed path. `ops_secret_write.py` adds the
two ops in the `config` group, next to `k8s.secret.create`.

**`k8s.secret.update` (dangerous, `requires_approval=True`).**

- Takes `name`, `namespace`, `mode` and the same value inputs as
  `k8s.secret.create`: `string_data`, `data`, `string_data_secret_ref`,
  `data_secret_ref` and the two `*_secret_mount` fields. At least one value
  input is required. The ref maps are resolved through the same
  `_resolve_secret_refs` helper (its error message now names the calling
  op). The guidance tells the agent to prefer the `*_secret_ref` inputs, so
  the value never passes through the caller.
- It reads the Secret first. A missing Secret raises
  `KubernetesSecretNotFoundError` naming `k8s.secret.create`; an
  `immutable: true` Secret raises `KubernetesSecretImmutableError` ("delete
  it and create it again"). It never creates.
- `mode: patch` (default) sends only the given keys as a strategic merge
  patch (`patch_namespaced_secret` with a dict body), so every other key,
  the type, labels and annotations stay.
- `mode: replace` sends the read object back through
  `replace_namespaced_secret` with the new `data` / `stringData` maps. The
  type, labels, annotations and `resourceVersion` ride along, so the Secret
  ends with exactly the given keys, and a change by someone else between the
  read and the write makes the API server answer 409
  (`KubernetesSecretConflictError`, a `ConnectorUpstreamError`). Nothing is
  lost without notice.
- The op sends a secret, so the dispatcher withholds the message of a raised
  error from the result (#4854, `_vendor_text.py`). Each of the three error
  classes above also sets `remediation` to its own text (the Secret's name
  and what to do next, never a value). `result_connector_error` copies that
  attribute into `extras.remediation`, which the withholding keeps. So the
  caller, the audit row and the stored approval result still say "use
  `k8s.secret.create`", "delete it and create it again" or "nothing was
  written, run it again".
- Result: `{name, namespace, type, mode, data_keys, keys_set, keys_removed,
  updated}`. Key names only, never a value.
- The op is pinned in `_CREDENTIAL_WRITE_OPS` like `k8s.secret.create`.
  While it waits for approval its params live in the approval hand-off
  store, not on the approval row, and the broadcast is aggregate-only.
- Its preview (`_secret_update_preview`) shows `name`, `namespace`, `mode`,
  the key names that are set, the `*_secret_ref` paths and mounts, the key
  names the Secret has now and, in `replace` mode, the key names that will
  be removed. It reads the Secret for the current key names, never a value.
  Without a connector it shows the params-derived part only.

**`k8s.secret.delete` (destructive, `requires_approval=True`).**

- Takes `name` and `namespace`. Result: `{name, namespace, deleted}`.
- Destructive means: the caller runs `preview_operation`, then
  `call_operation` with the returned `preview_hash`; a person's call waits
  until a second person approves; an agent's call is refused (see
  `approvals.md`).
- Its preview (`_secret_delete_preview`) reads the Secret and fills the
  `blast_radius` block: `object` is the Secret (namespace, name, type and
  key names); `children` lists what uses the Secret in the same namespace:
  Pods, and the pod templates of Deployments, StatefulSets, DaemonSets and
  CronJobs. A pod spec uses a Secret through a volume (also a projected
  volume), `env` with `secretKeyRef`, `envFrom` with `secretRef`, or
  `imagePullSecrets`, in containers and init containers
  (`secret_uses_in_pod_spec`). `irreversibility` is `permanent`: the values
  are gone and Kubernetes keeps no copy.
- A missing Secret makes the preview fail with a clear message, so the
  destructive gate refuses to park the call (`blast_radius_required`) and
  nothing waits for approval.

Tests: `tests/test_connectors_k8s_secret_write.py`.

### Governed guest-cluster kubeconfig read -- `k8s.secret.read_to_ref` (#3496)

The `ops_secret_read.py` op closes the one gap the write surface left:
there was `k8s.secret.create` but **no data-returning Secret read**
(Secret `data` is clamped on broadcast). Extracting a credential from a
Secret therefore meant an out-of-band `kubectl get secret … -o
jsonpath`, escaping audit / policy / approval. The motivating case is
registering a **VKS guest cluster** as a second `product=k8s` target: the
shipped WCP SSO auth mode (#2905) reaches only the Supervisor, so a guest
cluster is registered from its Cluster-API-generated
`<cluster>-kubeconfig` Secret (the admin client-cert kubeconfig, stored
under the `value` data key) in the Supervisor namespace.

**No-transit shape (not the issue's `read_data` sketch).** The op does
**not** return the decoded kubeconfig to the caller. Borrowing the secret
broker's core invariant (`connectors/secret`, #1577), it reads the value
inside the backplane and writes it straight to a Vault `secret_ref`,
returning only `{secret_ref, field, registered_as, name, namespace,
data_key, value_sha256, length}`. So the kubeconfig never reaches the
`call_operation` result / the agent transcript at all -- not merely the
audit row. This is a deliberate hardening of #3496's `read_data` sketch
(which returned the value to the approval-gated caller); the round-trip
the issue asks for is served directly, because the op **is** the staging
step.

**Round-trip.** The destination path is derived with
`tenant_secret_ref(operator.tenant_id, register_as)`
(`connectors/vault/tenant_paths.py`) -- the same helper `POST
/api/v1/targets` uses to default an omitted `secret_ref` (#1723) -- and
the value is written under the `field` name the kubeconfig loader reads
(`kubeconfig` by default, see `load_kubeconfig_from_vault`). So the demo
flow is: `call_operation k8s.secret.read_to_ref … register_as=<guest>`
-> `meho targets create --name <guest> --product k8s --host <guest-VIP>
--port 6443` (secret_ref omitted; it defaults to the returned path) ->
`k8s.node.list` against the guest works.

**Governance (mirrors `sddc.credential.list`).** Three layers: (1)
`safety_level="caution"` + `requires_approval=True` (the policy gate
parks an unapproved dispatch); (2) the op-id is pinned in
`broadcast.events._CREDENTIAL_READ_OPS`, so `classify_op` returns
`credential_read` and audit + broadcast rows collapse to aggregate-only;
(3) the result is value-free **by construction** (only the ref + a
SHA-256 + byte length), so the connector-boundary `credential_read`
response scrub (#2467) and the defensive Tier-1 engine have no secret to
strip.

**Tenant scope, not duplicated.** The write rides the `vault.kv.put`
handler, which runs `enforce_tenant_scope`
(`connectors/vault/tenant_scope.py`) under the operator's own Vault
identity before any round-trip -- the same default-on guard the
`api/v1/targets.py` write-time `secret_ref` gate mirrors, enforced here
without re-implementing it (that gate raises `HTTPException` and is
FastAPI-coupled, so the connector-appropriate reuse is the shared guard
it wraps). The derived path is inside the operator's tenant subtree by
construction.

**Vault write check before approval (#3844).** The op is approval-gated
and its preview is hidden (it is a credential op), so on its own the
approver could not tell that Vault will refuse the write. The op
therefore registers a permission preflight
(`operations/_preview.py::_k8s_secret_read_to_ref_preflight`). At park
time it builds the same path the handler writes to
(`tenant_secret_ref(operator.tenant_id, register_as)` on the `secret`
mount) and runs the `vault.kv.put` capability check there
(`sys/capabilities-self`, capability names only, no secret value). It
uses `target=None`, like the handler's write, so the check uses the same
Vault role. If the identity lacks `create` + `update`, MEHO refuses the
park with `write_capability_denied` (#3542) and nobody approves a write
that would fail.

**Vault grant it needs (#3843).** The write path is fixed. It must stay
`tenants/<tenant_id>/<register_as>`: the tenant-scope guard refuses a
write outside the tenant subtree, and `targets create` without a
`secret_ref` reads this path. Of the per-tenant policies, only
`meho-tenant-admin` grants `create` + `update` there. The caller and the
approver both need it. The deploy steps are in
[`connector-vault-policy.md`](../cross-repo/connector-vault-policy.md)
§6.6.

**Supervisor case: no server rewrite (corpus-verified).** A VKS guest
kubeconfig's `clusters[].cluster.server` already points at the guest
cluster's own LoadBalancer API-server VIP and is used as-is, so **no**
server-address rewrite is performed -- the op stages the kubeconfig
verbatim (corpus:
`vsphere-supervisor-services-and-standalone-components.pdf`,
`VCFB1443LV-kubernetes-101-and-the-iaas-control-plane-on-vmware-cloud-fo.pdf`).

A missing `data_key`, or a value that is not valid base64 / UTF-8, raises
`KubernetesSecretDataError` naming the Secret + key (never the value);
the error surfaces through the dispatcher's `connector_error` envelope,
and nothing is written to Vault on failure.

### Check a Secret against its Vault copy -- `k8s.secret.compare_to_ref`

A Helm chart often reads a password from a Secret while the same password
is kept in Vault. After a change the operator must prove that both copies
match. `ops_secret_compare.py` answers that without showing the value.

- Params: `namespace`, `name`, `key` and the Vault copy
  `ref: {path, key, mount?}`.
- The Secret is read with the connector's normal Kubernetes access. Its
  value is base64-decoded, so it is compared with the plain text in Vault.
  A Vault value that is not a string (a number, `null`, a list) never
  matches the Secret.
- The Vault copy is read with the caller's own Vault login and tenant
  guard. Like `k8s.secret.read_to_ref`, it uses no Vault target
  (`target=None`), so the deployment's Vault and the default role apply.
  The read and the compare are the shared `read_kv_copy` /
  `compare_copies` of `vault.kv.compare` (`connectors/vault/kv_compare.py`).
- The two values are compared with `hmac.compare_digest` (constant time)
  and dropped.
- Answer: `{same, copies: [k8s copy, vault copy]}`. Each copy has
  `source`, its names, `key`, `found`, `matches_first`, `version` (the
  Secret's `resourceVersion` or the Vault version) and `reason`. Never a
  value or a hash.
- A copy that cannot be read answers `found: false` with `reason`
  `forbidden` (Kubernetes 403, Vault 403, or a Vault path outside the
  caller's tenant), `not_found` or `key_not_found`; the call does not
  fail. Other Kubernetes errors, a Vault login failure or an unreachable
  Vault fail the call.
- `safe`, no approval. The op id is on `_CREDENTIAL_READ_OPS`, so the
  flight recorder never stores the Secret body and the broadcast shows
  only the outcome.

### Shared list-op request shape (`ops_listparams.py`)

Every namespaced list op on this connector
(`k8s.pod.list`, `k8s.deployment.list`, `k8s.job.list`, `k8s.event.list`,
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

Pod / deployment / job list additionally use the full base via
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

### Workload-op prefix resolution (`k8s.pod.info` / `k8s.deployment.info` / `k8s.job.info`)

All three `info` ops accept an exact name or a unique prefix within the
namespace. Resolution shape (`_resolve_from_items` in `ops_workload.py`,
reused by `resolve_job_name` in `ops_job.py`):

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
`list_namespaced_deployment` / `list_namespaced_job` without server-side pagination -- the
operator-facing namespaces are typically O(10..100) objects which
fits in one unpaginated response. Heavy-tenancy namespaces with
hundreds of workload objects are not the prefix-resolver's target
audience; the agent typically reaches them via the list path with
explicit pagination.

### Container status detail in `k8s.pod.info` (#4828)

Each entry of `container_statuses` and `init_container_statuses` is
built by `container_status_row()` in `ops_workload.py`:

| field | source | meaning |
| --- | --- | --- |
| `name`, `image`, `ready`, `restart_count` | top-level `V1ContainerStatus` | `image` is the image the pod asked for. |
| `image_id` | `imageID` | Digest of the image the container really runs (`repo@sha256:...`). Empty until the image is pulled. Compare it with the pinned digest after an upgrade. |
| `state`, `last_state` | populated branch of the `V1ContainerState` union | One word: `running`, `waiting` or `terminated`; `None` when absent. |
| `reason`, `message` | `state.waiting` or `state.terminated` | Why the container waits (`ImagePullBackOff`, `ErrImagePull`, `CrashLoopBackOff`, `CreateContainerConfigError`) or stopped (`OOMKilled`, `Error`), with the error text. `None` while running. |
| `exit_code` | `state.terminated.exitCode` | Set only when the current state is `terminated`. |
| `last_reason`, `last_exit_code` | `last_state` | Why the container stopped the last time. For a `CrashLoopBackOff` container this is the real cause (e.g. `OOMKilled` / 137). |

The reason fields matter because `k8s.event.list` is the only other
place the reason shows up, and Kubernetes drops events after one
hour by default. The container status keeps the reason for as long as
the pod exists. The change is additive: `state` and `last_state` keep
their one-word shape.

### Environment variable sources in `k8s.pod.info` and `k8s.deployment.info` (#4890)

A variable that takes its value from a Secret or a ConfigMap has no
literal value in the pod definition. Before #4890 both ops showed it
as `{"name": ..., "value": null}`, the same as an empty variable. Now
each `containers[].env` and `init_containers[].env` entry built by
`_env_var_row()` in `ops_workload.py` keeps `name` and `value`, and
adds `value_from` when the entry has a source:

| `value_from` key | source in the pod spec | fields shown |
| --- | --- | --- |
| `secret_key_ref` | `valueFrom.secretKeyRef` | `name` (the Secret), `key`, `optional` |
| `config_map_key_ref` | `valueFrom.configMapKeyRef` | `name` (the ConfigMap), `key`, `optional` |
| `field_ref` | `valueFrom.fieldRef` | `field_path` (for example `status.podIP`) |
| `resource_field_ref` | `valueFrom.resourceFieldRef` | `container_name`, `resource` (for example `limits.memory`) |
| `file_key_ref` | `valueFrom.fileKeyRef` | `volume_name`, `path`, `key`, `optional` |

A literal entry has no `value_from` key, so its shape is unchanged.

Each container also gets `env_from` (`_env_from_row()`): one row per
`envFrom` source with `kind` (`secret_ref` or `config_map_ref`), the
object `name`, `optional` and `prefix`. `envFrom` loads every key of
the object as a variable, so the row names the object only.

MEHO does not read the Secret or the ConfigMap for this. The ops still
make one `list_namespaced_pod` / `list_namespaced_deployment` call and
copy the names, keys and field paths already written in the pod
definition. No secret value can appear in the result. The main use is
a container waiting with `CreateContainerConfigError`: the status row
says the container cannot start, and `value_from` / `env_from` say
which Secret and key it asks for.

### Image pull secrets in `k8s.pod.info` and `k8s.deployment.info` (#3686)

Both ops return `image_pull_secrets`: the names of the Secrets in the pod
spec's `imagePullSecrets` (for a Deployment, from its pod template), in
spec order. The list is empty when the spec names none. It answers "is
the image pulled with the expected pull secret?" without `kubectl get -o
yaml`. `_image_pull_secret_names()` in `ops_workload.py` copies the names
only. MEHO does not read the Secrets, so no secret content can appear in
the result.

### Init container logs in `k8s.logs` (#3686)

`k8s.logs` passes `include_init_containers=True` to
`resolve_pod_and_container()` in `ops_logs.py`. So an init container
name (for example a `migrate` step) is a valid `container` value, and
the `MultiContainerAmbiguityError` candidate list shows the app
containers first, then the init containers. Auto-pick still looks only
at the app containers: a pod with one app container and some init
containers still resolves to the app container. `k8s.exec` uses the same
resolver with the default (`False`), so its valid set stays the app
containers.

### Job reads (`ops_job.py`)

`k8s.job.list` and `k8s.job.info` answer "did the Job finish, and did it
work?" for migration, backup and one-off batch Jobs. Both are safe reads
in the `workload` group over `BatchV1Api`.

- `state` is one word from the Job's conditions, in this order: `failed`
  when the `Failed` condition is true, `complete` when `Complete` is
  true, `suspended` when `Suspended` is true, else `running`. `failed`
  wins so a Job that failed after a partial success reads as failed.
- `pods_active` / `pods_succeeded` / `pods_failed` come from `status`;
  `completions` from `spec` (`null` = any one success completes the
  Job). `start_time` and `completion_time` are UTC ISO 8601 strings;
  `completion_time` is set only when the Job succeeded.
- `k8s.job.info` adds `conditions` (`type` / `status` / `reason` /
  `message` / `last_transition_time` -- a failed Job says
  `BackoffLimitExceeded` or `DeadlineExceeded` here), `backoff_limit`,
  `active_deadline_seconds`, the template `containers` (name + image),
  `pod_selector` and `pods`.
- `pod_selector` is the Job's own `spec.selector` rendered as a selector
  string (`label_selector_string`). The API server sets that field on
  every stored Job; when it is missing the Job-name label
  `batch.kubernetes.io/job-name=<name>` is the fallback, so `pods` is
  never silently empty.
- `pods` are found with one `CoreV1Api.list_namespaced_pod` call using
  that selector, projected through `pod_row` (the `k8s.pod.list` row
  shape), ordered newest first and cut at `JOB_INFO_MAX_PODS` (50).
  `pods_truncated: true` says the list was cut; `k8s.pod.list` with
  `label_selector=<pod_selector>` returns all of them.
- The op declares `llm_instructions.result_scalars` so the state and
  the counts stay inline if the JSONFlux reducer ever spills the pod
  list to a handle.

Not covered: CronJob reads, and `k8s.ls /<namespace>` does not count
Jobs yet (`K8S_NAMESPACED_KIND_LISTERS` is unchanged).

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

### Storage + custom-resource read tier (#2830, `ops_storage.py` / `ops_customresource.py`)

Six safe reads that close the pre-flight sizing gap (the RDC Hetzner
consumer session that fell back to raw kubectl):

- **Storage** (`ops_storage.py`): `k8s.storageclass.list`,
  `k8s.persistentvolume.list`, `k8s.persistentvolumeclaim.list`. The two
  cluster-scoped list ops (StorageClass, PV) take no parameters (the
  `k8s.node.list` shape); PVC adopts the shared `namespace` XOR
  `all_namespaces` selector. `is_default` reads the GA
  `storageclass.kubernetes.io/is-default-class` annotation; PVC/PV
  capacity is the `storage` quantity plucked from `status.capacity` /
  `spec.capacity`; a PV's `claim_ref` renders as `namespace/name`.
- **Custom resources** (`ops_customresource.py`): `k8s.crd.list` is the
  discovery op (group / version / plural / scope / versions), and
  `k8s.cr.list` / `k8s.cr.info` read objects generically over
  `CustomObjectsApi` (namespaced call when a `namespace` is supplied,
  cluster call otherwise). CustomObjectsApi returns plain dicts, so the
  projection walks dict keys. Result size is bounded: the projection
  keeps metadata identifiers + `labels` and a JSON `spec_excerpt` capped
  at `CR_SPEC_EXCERPT_MAX_BYTES` (2048 B); over the cap, `spec_truncated`
  is `true` and the excerpt is a preview (not parseable JSON). The
  verbose `managedFields` metadata block is dropped.
- **Annotations, version and status on CR rows** (#3878): every
  `k8s.cr.list` row and the `k8s.cr.info` result also carry
  `resource_version`, `uid`, `annotations` and a `status_excerpt`. This
  lets a governed read confirm that an annotation-only change landed
  (for example a Cluster API pause annotation). `annotations` is an
  object column, so `result_query` cannot filter or sort on it. Return
  it with `select`, or read the raw rows by paging (`offset` /
  `limit`).
  - Each annotation value is cut at `CR_ANNOTATION_VALUE_MAX_BYTES`
    (256 B). `annotations_truncated` is `true` when any value was cut.
  - The `kubectl.kubernetes.io/last-applied-configuration` annotation
    is always left out. It is a copy of the whole last manifest, so it
    is large, and on a Secret it repeats every value.
  - On a `Secret`, every annotation value is the redacted placeholder
    (the same one `data` values get). The keys stay. Deploy tools can
    copy the whole manifest into an annotation, so no value is shown.
  - `status_excerpt` is the JSON `.status`, capped like `spec_excerpt`
    (2048 B), with its own `status_truncated` flag. It is `null` when
    the object has no `.status`.
- **Built-in kinds** (#3840): the same two ops read any kind no
  dedicated op covers. A built-in group with a name (for example
  `apiregistration.k8s.io` / `v1` / `apiservices`, or
  `admissionregistration.k8s.io` / `v1` /
  `validatingadmissionpolicies`) goes through `CustomObjectsApi` like a
  CRD. The core group has an empty name and a different root
  (`/api/v1`, not `/apis/...`), so `group: ""` is sent by
  `ops_core_objects.py` through the same `ApiClient.call_api` (same
  auth, TLS and wire span). The API server does not repeat `kind` /
  `apiVersion` on the items of a list of built-in objects, so
  `list_items` copies them from the list kind (`SecretList` ->
  `Secret`) before the row projection. That keeps the Secret
  redaction below working for list reads too.

### Access checks and admission webhooks (#3840, `ops_access.py` / `ops_admission.py`)

Three safe reads in the `access` group answer "why did this apply not
take effect?" without leaving MEHO:

- `k8s.auth.can_i` -- a `SelfSubjectAccessReview`. Takes `verb` and
  either a resource (`group`, `resource`, `subresource`, `name`,
  `namespace`, `version`) or a `non_resource_path`; the schema's
  `oneOf` allows exactly one of the two forms.
- `k8s.auth.rules` -- a `SelfSubjectRulesReview` for one `namespace`.
  `incomplete: true` means an authorizer could not list its rules, so
  confirm a single action with `k8s.auth.can_i`.
- `k8s.admission_webhook.list` -- one row per webhook of every
  validating and mutating webhook configuration.

Both reviews are HTTP `POST` calls, but the API server stores nothing,
so they are `safe`. They always check the identity in the target's
kubeconfig (the identity MEHO uses for that target), not the caller.

This tier also retires the `cluster_kinds` **dead-end advertisements**:
`k8s.ls /` had long listed `storageclasses` / `persistentvolumes` in its
root output (and the namespace walk counted `persistentvolumeclaims`)
with no op behind them. The op ids are exactly
`k8s.storageclass.list` / `k8s.persistentvolume.list` /
`k8s.persistentvolumeclaim.list` so the `k8s.ls` singular-normalisation
map (`_PLURAL_TO_SINGULAR_KIND`, pre-wired) forwards
`k8s.ls /<ns>/persistentvolumeclaims` to the new op instead of the
`unknown_op` envelope.

### Read-side Secret redaction (#3501, `redaction.py`)

Response redaction is otherwise pattern-based (the Tier-1 named-pattern
engine + optional Presidio), which keys on **labelled** secret shapes in
string leaves. A Kubernetes `Secret`'s `data` map is the opposite shape:
arbitrary, operator-chosen keys whose base64 values carry no in-leaf
label, so they would pass through unless a value happened to match a
named pattern.
[`connectors/kubernetes/redaction.py`](../../backend/src/meho_backplane/connectors/kubernetes/redaction.py)
closes that read-side gap with a pure, **structural** redactor
(`redact_kubernetes_payload`): it recognises any `kind: Secret` object
and replaces every `data` / `stringData` value with a fixed placeholder
carrying a `sha256:` digest — key names kept — regardless of whether the
value matches a pattern. It is the k8s sibling of the keycloak / rabbitmq
per-connector redactors (see [`redaction.md`](redaction.md)).

The redactor is wired into `custom_resource_row`
(`ops_customresource.py`), the projection both `k8s.cr.list` and
`k8s.cr.info` share, so a dynamic read pointed at core Secrets
(`group=""` / `version="v1"` / `plural="secrets"`) is scrubbed before
JSONFlux / audit / broadcast. For a list read, each item first gets
`kind: Secret` from its `SecretList` (see "Built-in kinds" above),
because the API server leaves `kind` off list items. The dedicated way
to look at core Secrets is `k8s.secret.list` / `k8s.secret.info` (next
section), which also run this redactor as a second net.

**Projection note.** `custom_resource_row` otherwise keeps only
`metadata` + bounded `.spec` / `.status` excerpts and drops every other
top-level field (a Secret's annotation keys show, but every annotation
value is replaced by the placeholder); a `Secret` has no `.spec`, so before #3501 a CR read of a Secret
returned no `data` at all (safe, but uninformative). The redactor lets
the projection surface a `data` / `string_data` **key inventory** (values
redacted, digests attached) for a `kind: Secret` object instead — safe
*and* useful. Non-Secret CR rows gain neither field. The op remains
`safe` / no-approval — the redaction, not a gate, is what makes a Secret
read leak-free.

**Carve-out.** `k8s.secret.read_to_ref` (#3496) is untouched by this
pass: it reads the value inside the backplane and stages it to a Vault
`secret_ref`, returning only the ref + a SHA-256 + byte length — no
`data` map and no `kind: Secret` object, so the structural walk finds
nothing to redact and the op's no-transit contract is preserved.

### List and describe Secrets by name only -- `k8s.secret.list` / `k8s.secret.info` (`ops_secret_list.py`)

Before a Secret change an operator needs to know which keys the Secret
has, and whether it carries labels, annotations or owners (a full
replace would drop them). These two safe reads answer that through MEHO,
so nobody needs `kubectl get secret` by hand.

- `k8s.secret.list` -- same params as `k8s.configmap.list`
  (`namespace` XOR `all_namespaces`, `label_selector`). One row per
  Secret: `name`, `namespace`, `type`, `keys`, `key_count`, `labels`,
  `annotation_keys`, `owner_count`, `created`, `resource_version`. A
  long list comes back as a link to the stored result (JSONFlux).
- `k8s.secret.info` -- params `name`, `namespace`. The row fields plus
  `immutable`, `owners` (`kind` + `name` of each owner reference) and
  `key_sizes` (bytes per key; the decoded size of a `data` value).

**Never a value.** The answer is built from names only: key names from
`data` / `stringData`, annotation names only (the
`kubectl.kubernetes.io/last-applied-configuration` annotation can hold
the whole Secret, values included), owner kind and name. No value is
copied out of the `V1Secret`. The answer then passes through
`redact_kubernetes_payload` as a second net. So no value reaches the
result, the audit row, the stored result or the broadcast feed.

**Kubernetes rights.** The target's service account needs `list` on
`secrets` for `k8s.secret.list` (cluster-wide for `all_namespaces`) and
`get` on `secrets` for `k8s.secret.info`. The API server sends the values
to MEHO on both calls; MEHO drops them before the answer is built.

Reading a value stays with `k8s.secret.read_to_ref` (copied to the
credential store, never shown).

Tests: `tests/test_connectors_k8s_secret_list.py`.

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

### Flight-recorder span per API request (`_wire_span.py`, #3841)

When the flight recorder captures a dispatch, every Kubernetes API HTTP
request now gets its own `vendor_call` span under the op's `typed` span.
Before, the trace showed only the `typed` span, so a "200 but nothing
happened" apply could not be explained from the trace.

- `instrument_api_client()` wraps `RESTClientObject.request` on each
  cached `ApiClient` once, when `_get_api_client` builds it. All typed
  `CoreV1Api` / `AppsV1Api` calls and the `DynamicClient` used by
  `k8s.apply` pass through that one method.
- The span holds: method, path (no query), the query with values kept
  only for safe switches (`dryRun`, `fieldManager`, `force`, selectors,
  ...; `continue` is masked), status, request and response headers
  through the header allowlist (`content-type`, `server`, `via`,
  `warning`, `audit-id` survive), bodies through the normal body
  redaction, and `response_object`: `kind`, `apiVersion`, `name`,
  `namespace`, `uid`, `resourceVersion`, `creationTimestamp`,
  `generation`, and a `managedFields` summary (manager, operation,
  apiVersion, time, subresource). Annotations, labels and `fieldsV1`
  are never copied into `response_object`.
- `response_object` is read from the response body **after** redaction,
  never from the raw response. So a field a body-path rule removed, or
  a credential shape the scrub replaced, shows the same marker in both
  places. When the body is not recorded (a secret-bearing op such as
  `k8s.secret.create`, a body over the 64 KiB cap, or a body that is
  not JSON) there is no `response_object`.
- A response body over the 64 KiB span cap is not recorded at all. The
  shared redactor cuts an oversized body at the cap, and a cut body can
  still parse as JSON (when only a trailing newline was cut), so Secret
  data in it would have been stored. The hook hands over no body; the
  span stores the omission marker plus a reason, and the trace is marked
  uncertain (operator-only). Request bodies are masked whole before
  the cap applies.
- Secret payloads are masked before capture. Every JSON request and
  response body first goes through the connector's structural Secret
  redactor (`redaction.py`, the same walk `k8s.cr.*` reads use). Every
  `data` / `stringData` value of a `kind: Secret` object, and every
  annotation value on it, is replaced by a placeholder plus a digest;
  key names stay. This covers a `k8s.apply` of a Secret
  manifest, a `k8s.cr.info` read of one, and the items of a
  `SecretList` (which carry no `kind` of their own). The normal body
  redaction then runs over the masked body.
- Non-2xx (`ApiException`) and transport errors are recorded
  (`status` = HTTP code or `error:<ExceptionClass>`) and re-raised
  unchanged. With `_preload_content=False` the wrapper reads the body
  first; `aiohttp` caches it, so the caller's later `json()` still
  works. A `watch` / `follow` stream is never read. When that read
  fails (a timeout, a connection dropped mid-body), the span is still
  recorded with the request, the HTTP status, and the response headers;
  only the body is missing. The caller's own read raises the same
  error.
- When capture is off the wrapper costs one contextvar read.
- Not covered: `k8s.exec` over `WsApiClient` (websocket transport).

The shared recorder is
`flight_recorder/sdk_http.py::record_sdk_vendor_call`; it reuses the
generic-connector redaction path so both span sources look the same.

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
