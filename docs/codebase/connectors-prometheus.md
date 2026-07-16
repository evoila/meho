# Prometheus connector

Read-only connector over the Prometheus HTTP API (`/api/v1`). One
connector serves the three PromQL-HTTP-compatible metrics backends an
estate runs: **Prometheus**, **Thanos Query**, and **Grafana
Mimir/Cortex**. Initiative #2228 / Task #2234.

## Overview

The metrics tier is the first place an operator looks when a feed goes
stale. `PrometheusConnector` brings that surface inside MEHO's dispatch →
policy-gate → audit boundary as a **read-only** connector: it can query
PromQL, discover series/labels, and inspect scrape-target / rule / alert
state, but it cannot write, delete, or reload anything.

Thanos Query and Mimir/Cortex expose the same `/api/v1` verbs, so one op
surface covers all three. Backend-specific differences are handled at the
connector level (Thanos Query has no scrape targets; Mimir mounts the API
under a `/prometheus` path prefix), not in the op catalog.

## Key types

- `PrometheusConnector` (`connectors/prometheus/connector.py`) — the
  `HttpConnector` subclass. Registry v2 triple
  `("prometheus", "2.x", "prometheus-api")` plus the
  `("prometheus", "", "")` wildcard fallback.
- `PROMETHEUS_OPS` (`connectors/prometheus/ops.py`) — the eight
  `PrometheusOp` descriptors registered at lifespan startup.
- `PrometheusReadOnlyError` — raised by the read-only gate before any
  upstream call.
- `PrometheusSecretLoader` — injectable KV-v2 secret loader (defaults to
  `load_vault_secret_data`), the seam tests use to exercise Bearer/Basic
  selection without a live Vault.

## Read-only by construction

Every dispatched request flows through `_api_get`, which calls
`_enforce_read_only(method, logical_path)` before touching the wire:

1. **GET-only** — any other method raises `PrometheusReadOnlyError`.
2. **`/api/v1/` path-allowlist** — a path outside `/api/v1/` (e.g.
   `/-/reload`) is rejected.
3. **`/api/v1/admin/` blocklist** — the Prometheus admin HTTP API
   (TSDB-delete, clean-tombstones, snapshot) is POST-only, so the GET
   gate already neutralises it; the path is blocklisted too so "admin is
   unreachable" holds by construction.
4. **traversal guard** — a `..` segment is rejected so a crafted path
   cannot climb out of the prefix.

The gate runs on the API-relative `logical_path` (the op's declared
path), *before* the per-target mount prefix is applied — so the allowlist
is checked against the caller's intent, not the wire path.

## Ops

| op_id | endpoint | group |
|---|---|---|
| `prometheus.query` | `GET /api/v1/query` | query |
| `prometheus.query_range` | `GET /api/v1/query_range` | query |
| `prometheus.series` | `GET /api/v1/series` | metadata |
| `prometheus.labels` | `GET /api/v1/labels` | metadata |
| `prometheus.targets` | `GET /api/v1/targets` | monitoring |
| `prometheus.rules` | `GET /api/v1/rules` | monitoring |
| `prometheus.alerts` | `GET /api/v1/alerts` | monitoring |
| `prometheus.get` | `GET <path under /api/v1/>` | passthrough |

All are `safety_level="safe"`, `requires_approval=False`, tagged
`read-only`. `prometheus.get` is the escape hatch for read endpoints the
curated ops do not cover (`/api/v1/status/tsdb`, `/api/v1/metadata`, …);
its `path` param is re-validated by the same gate, so the passthrough
cannot leave the read-only surface.

## Optional auth

In-cluster Prometheus is typically reached via port-forward and is
unauthenticated. `secret_ref = None` is therefore a first-class state:
`auth_headers` returns `{}` — **no credential load is attempted**. This
is net-new; every other connector's execute path fails closed on an unset
`secret_ref` (`_resolve_secret_ref` in `_shared/vault_creds.py`). The
only prior unauthenticated precedent was fingerprint/probe (argocd's
`_get_version_unauthenticated`), not execute.

When `secret_ref` *is* set, the KV-v2 secret is read under the operator's
identity and the auth scheme is chosen by the stored fields:

- a `token` field → `Authorization: Bearer <token>`
- a `username` + `password` pair → `Authorization: Basic <b64>`

The field-shape discriminator mirrors the gh-rest connector's
App-vs-PAT selection; a secret carrying neither shape raises
`VaultCredentialsReadError`.

## Scheme and path prefix (per-target `extras`)

The base `HttpConnector._base_url` hardcodes `https`. A port-forwarded
Prometheus is plain `http` on `:9090`, and Mimir mounts the API under a
`/prometheus` prefix. Both are read from `target.extras` so no new Target
column is needed:

- `extras["scheme"]` ∈ `{http, https}` (default `https`).
- `extras["path_prefix"]` (default none; Mimir → `/prometheus`).

The prefix is applied to the **wire path** (`_wire_path`), not folded
into `base_url` — an absolute request path (`/api/v1/...`) would
otherwise replace a `base_url` that carried the prefix (httpx / RFC 3986
reference resolution).

## Fingerprint

`fingerprint()` reads `GET /api/v1/status/buildinfo` (the reachability +
version signal) and best-effort augments it with `/-/ready`, the
scrape-target count, the firing-alert count, and the rule-group count.
Each augmentation is `None` when unavailable (Thanos Query has no scrape
targets; Mimir exposes `/ready` rather than `/-/ready`).

### Why the flavour is operator-asserted

`fingerprint()` surfaces a `flavour` hint (`prometheus` | `thanos` |
`mimir`) in `extras["flavour"]` and as `FingerprintResult.edition`. It is
read from `target.extras["flavour"]` (default `prometheus`) — **not**
sniffed from the API response.

The reason is grounded in the upstream code: the buildinfo payload is
byte-identical across the three backends. Thanos and Mimir vendor
Prometheus's `PrometheusVersion` struct
(`version`/`revision`/`branch`/`buildUser`/`buildDate`/`goVersion`) and
its `serveBuildInfo` handler verbatim, so a Thanos or Mimir target is not
self-identifying from `/api/v1/status/buildinfo`. Rather than ship an
unreliable heuristic, the operator (who knows which of the three they
deployed) asserts the flavour. This is the substrate-minimalism call: a
dumb, honest hint over a smart-but-wrong sniffer.

## Dependencies

- `HttpConnector` (`connectors/adapters/http.py`) — pooled httpx client,
  retry, TLS trust, SSRF guard.
- `_shared/vault_creds.py` — `load_vault_secret_data` (raw KV-v2 field
  dict), `strip_credential_value`, `VaultCredentialsReadError`.
- `_shared/system_operator.py` — `synthesise_system_operator` for the
  operator-less probe/background-fingerprint path.
- `operations/typed_register.py` — `register_typed_operation` +
  `register_typed_op_registrar`.

## Known issues / notes

- The `flavour` hint is only as accurate as the operator's `extras`
  assertion; a mislabelled target reports the wrong flavour. This is
  accepted (see above) — auto-detection is not reliable.
- Reaching an in-cluster / port-forward target (`localhost`, ClusterIP,
  internal DNS) requires the operator to add the host to
  `MEHO_TARGET_SSRF_ALLOWLIST`; the shared SSRF guard otherwise blocks
  private destinations. This is a target-config concern, not connector
  behaviour.
- Write coverage is out of scope by construction (Initiative #2228). A
  future G3.x write-surface initiative would add any mutating ops behind
  the approval gate.

## References

- Prometheus HTTP API: <https://prometheus.io/docs/prometheus/latest/querying/api/>
- Thanos Query API compatibility: <https://thanos.io/tip/components/query.md/>
- Grafana Mimir HTTP API: <https://grafana.com/docs/mimir/latest/references/http-api/>
- Dual-registration pattern: `connectors/bind9/__init__.py`,
  `connectors/pfsense/__init__.py`.
- Unauthenticated precedent: argocd `_get_version_unauthenticated`.
