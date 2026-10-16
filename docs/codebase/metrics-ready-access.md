# `/metrics` and `/ready` access guard

## Overview

The backplane serves two operational exposition endpoints that are **not**
behind the JWT auth chain:

- `GET /metrics` (`meho_backplane/main.py`) — the Prometheus exposition
  (process/GC collectors + `http_requests_total`).
- `GET /ready` (`meho_backplane/health.py`) — the readiness verdict plus a
  `features` block that enumerates the four gated features and, indirectly, the
  effective four-eyes / feature-gate posture of the deploy.

On a shared ingress, unauthenticated, both leak production operational state to
anyone who can reach the ingress — at a customer-facing event
that is production activity readable at the public booth. #3499 adds an
**opt-in** guard so a deployment can require auth, with **behaviour unchanged
by default**.

## The guard

`meho_backplane/metrics_access.py` has two pieces:

- `has_metrics_access(request) -> bool` — `True` when the token is unset, or
  when the request sends `Authorization: Bearer <token>` with the matching
  token. It never logs and never raises.
- `verify_metrics_access` — a FastAPI dependency built on that check. It
  raises `401` when the check fails. Only `/metrics` declares it.

Behaviour:

- **Default-open.** When `Settings.metrics_auth_token` (`METRICS_AUTH_TOKEN`) is
  empty — the default — both endpoints are open and `/ready` returns the full
  body. Nothing changes for the scraper or the kubelet readiness probe.
- **`/metrics` with the token set.** It requires
  `Authorization: Bearer <token>`. A missing / malformed / mismatched header is
  refused `401 metrics_auth_required` with a `WWW-Authenticate: Bearer`
  challenge, **before** any registry content is rendered.
- **`/ready` with the token set (#3594).** It never answers 401. The body
  depends on the caller:

  | Caller | Status code | Body |
  |---|---|---|
  | Matching bearer token | 200 ready / 503 not ready | `status`, `checks`, `features` |
  | No header, wrong token, or another scheme | 200 ready / 503 not ready | `status`, `checks` (no `features`) |

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

## Tests

`backend/tests/test_metrics_access.py` — default-open (`/metrics` reachable,
`/ready` full body, no 401), `/metrics` guard-on 401s (missing / wrong /
non-bearer) and admit with the correct bearer, `/ready` guard-on verdict
without `features` for no / wrong / non-bearer headers (503 not ready and 200
ready), `/ready` full body with the correct bearer, and `/healthz` always
200.
