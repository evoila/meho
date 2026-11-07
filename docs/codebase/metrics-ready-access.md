# `/metrics`, `/ready` and the API docs pages

## Overview

The backplane serves these pages without the JWT auth chain:

- `GET /metrics` (`meho_backplane/main.py`) — the Prometheus exposition
  (process/GC collectors + `http_requests_total`).
- `GET /ready` (`meho_backplane/health.py`) — the readiness verdict plus a
  `features` block that enumerates the gated features and, indirectly, the
  effective four-eyes / feature-gate posture of the deploy.
- `GET /openapi.json`, `/docs`, `/redoc` (`meho_backplane/main.py`) — the
  API docs pages FastAPI serves.

What is true now:

- `/metrics` and the `features` block of `/ready` answer only a caller that
  sends the metrics token. A chart install always has one: the chart makes
  it (see "The chart" below).
- The API docs pages are off unless `API_DOCS_ENABLED` is set.
- `/ready` runs its probes at most once per short window, so a caller who
  needs no login cannot make the probes run on every call.

## The guard

`meho_backplane/metrics_access.py` has two pieces:

- `has_metrics_access(request) -> bool` — `True` when the token is unset, or
  when the request sends `Authorization: Bearer <token>` with the matching
  token. It never logs and never raises.
- `verify_metrics_access` — a FastAPI dependency built on that check. It
  raises `401` when the check fails. Only `/metrics` declares it.

Behaviour:

- **No token.** When `Settings.metrics_auth_token` (`METRICS_AUTH_TOKEN`) is
  empty, both endpoints are open and `/ready` returns the full body. That is
  the state of a bare image run, and of a chart install with
  `metricsAuth.enabled: false`.
- **`/metrics` with the token set.** It requires
  `Authorization: Bearer <token>`. A missing / malformed / mismatched header is
  refused `401 metrics_auth_required` with a `WWW-Authenticate: Bearer`
  challenge, **before** any registry content is rendered.
- **`/ready` with the token set (#3594).** It never answers 401. The body
  depends on the caller:

  | Caller | Status code | Body |
  |---|---|---|
  | Matching bearer token | 200 ready / 503 not ready | `status`, `checks`, `sandbox_mode`, `features` |
  | No header, wrong token, or another scheme | 200 ready / 503 not ready | `status`, `checks`, `sandbox_mode` (no `features`) |

  The readiness verdict is not secret, and the kubelet already relies on it.
  The `features` block shows the deploy's feature and approval settings, so
  only a caller with the token gets it.
- The compare is constant-time (`secrets.compare_digest` over UTF-8 bytes) and
  the presented value is never logged.

## Why `/ready` is split instead of blocked

A kubelet readiness probe cannot send a header whose value comes from a
Secret. In core Kubernetes, `httpGet.httpHeaders[].value` is a plain string
only. If `/ready` answered 401 without the bearer, turning on the token would
fail the probe, remove the Pod from the Service, and leave the ingress with no
ready backend. The only fix would be to copy the token as plain text into the
probe config, and keep that copy in sync by hand on every token change.

So `/ready` serves the verdict to every caller and leaves out only the
`features` block. The readiness probe keeps pointing at `/ready` with no
header. A wrong or old token also gets the verdict (not a 401), so a probe that
still carries an old header from earlier guidance keeps working after a token
change.

## Why bearer, not source-CIDR

The interim event mitigation was an ingress-level CIDR block. The durable
in-app fix is a **bearer token**, not a peer-IP CIDR check: behind the shared
ingress the app sees the ingress controller's IP, not the real client's, so a
peer-IP allowlist cannot distinguish a booth visitor from the scraper without
trusting a spoofable `X-Forwarded-For`. A bearer token is proxy-independent and
is the standard Prometheus scrape auth (`bearer_token` / ServiceMonitor
`bearerTokenSecret`).

## `/healthz` is never guarded

`GET /healthz` (liveness) is a pure process-up signal (always 200,
`{"status": "ok"}`, no posture) and is deliberately left unguarded, so a
bearer-less liveness path always exists even with the guard on. A deployment
that enables the guard must hand the token to the Prometheus scraper. The
liveness and readiness probes need no change and no header.

## The chart

`deploy/charts/meho` makes the token by default (`metricsAuth.enabled: true`):

- `templates/metrics-auth-secret.yaml` renders the Secret
  `<release>-metrics-auth` (key `token`, 32 alphanumeric characters). On a
  cluster the template looks the existing Secret up first, so a `helm
  upgrade` keeps the value. `helm template` has no cluster to look in, so
  every render makes a new token; the Deployment carries a checksum of the
  Secret, so the pod rolls with it.
- `templates/deployment.yaml` sets `METRICS_AUTH_TOKEN` from that Secret by
  `secretKeyRef`, never as plain text.
- `templates/servicemonitor.yaml` (when `serviceMonitor.enabled`) sends the
  token with `authorization.credentials`, the Prometheus Operator field that
  replaced the deprecated `bearerTokenSecret`. The operator reads the Secret
  from the release's namespace.
- The readiness probe is unchanged: `/ready` gives the verdict without a
  header.
- `metricsAuth.existingSecret` names an operator-owned Secret instead;
  `metricsAuth.enabled: false` sets no token.

## `/ready` runs the probes at most once per window

`GET /ready` reads `readiness_snapshot(max_age_s=DEFAULT_READINESS_TTL_S)`
(2 s), the same cache the web console uses. Inside the window every call,
with or without the token, gets the cached verdict and checks; the first
call after the window runs the probes once, under a single-flight lock. The
probes do live network calls (Keycloak, Vault, the database), so this bounds
what a burst of calls can cost. `sandbox_mode` and `features` are added per
call. The kubelet probe (every 5 s by default) still sees a fresh sweep on
each hit. The cache stores an empty string for a check with no detail; the
wire shape of `/ready` keeps `null` there. `clear_probes()` (test-only) also
drops the cache.

## The API docs pages

`/openapi.json`, `/docs` and `/redoc` are off unless `API_DOCS_ENABLED` is a
true value (`main.api_docs_enabled`). The env var is read directly at import
time, like `MEHO_ENABLE_RBAC_TEST_ROUTE`, because the app is built before the
chassis settings may be pinned. `app.openapi()` keeps working either way:
the CLI's committed `cli/api/openapi.json` snapshot and the tests that read
the schema use it in-process. In the chart, set the env var with `extraEnv`.

## Tests

- `backend/tests/test_metrics_access.py` — `/metrics` 401s without the
  token (missing / wrong / non-bearer) and admit with the correct bearer,
  `/ready` verdict without `features` for no / wrong / non-bearer headers
  (503 not ready and 200 ready), `/ready` full body with the correct bearer,
  and `/healthz` always 200.
- `backend/tests/test_ready_cached_verdict.py` — a burst of `/ready` calls
  runs the probes once, header-less callers share the cached verdict when
  the token is set, the probes run again after the window, and clearing the
  registry drops the cache.
- `backend/tests/test_api_docs_pages.py` — the three docs pages answer 404
  by default, the switch parses true and false values, and `app.openapi()`
  still builds.
- `backend/tests/test_chart_metrics_auth.py` — the chart renders the Secret,
  the `secretKeyRef` env and the ServiceMonitor `authorization` block by
  default, uses an operator-named Secret instead when given one, and renders
  none of them with `metricsAuth.enabled: false`.
