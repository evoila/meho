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
anyone who can reach the ingress — at a customer-facing event (meho-internal#320)
that is production activity readable at the public booth. #3499 adds an
**opt-in** guard so a deployment can require auth on both endpoints, with
**behaviour unchanged by default**.

## The guard

`meho_backplane/metrics_access.py::verify_metrics_access` is a FastAPI
dependency both routes declare (`dependencies=[Depends(verify_metrics_access)]`).

- **Default-open.** When `Settings.metrics_auth_token` (`METRICS_AUTH_TOKEN`) is
  empty — the default — the dependency returns immediately. The in-cluster
  Prometheus scraper (the chart's `ServiceMonitor`) and the kubelet readiness
  probe keep working with no config change.
- **Opt-in bearer.** When the token is set, both endpoints require
  `Authorization: Bearer <token>`; a missing / malformed / mismatched header is
  refused `401 metrics_auth_required` with a `WWW-Authenticate: Bearer`
  challenge, **before** any registry content or readiness verdict is rendered.
  The compare is constant-time (`secrets.compare_digest` over UTF-8 bytes) and
  the presented value is never logged.

## Why bearer, not source-CIDR

The interim event mitigation was an ingress-level CIDR block. The durable
in-app fix is a **bearer token**, not a peer-IP CIDR check: behind the shared
ingress the app sees the ingress controller's IP, not the real client's, so a
peer-IP allowlist cannot distinguish a booth visitor from the scraper without
trusting a spoofable `X-Forwarded-For`. A bearer token is proxy-independent and
is the standard Prometheus scrape auth (`bearer_token` / ServiceMonitor
`bearerTokenSecret`); a kubelet probe carries it via `httpHeaders`.

## `/healthz` is never guarded

`GET /healthz` (liveness) is a pure process-up signal (always 200,
`{"status": "ok"}`, no posture) and is deliberately left unguarded, so a
bearer-less liveness path always exists even with the guard on. A deployment
that enables the guard must then hand the token to the scraper and carry it on
the **readiness** probe's `httpHeaders`; liveness needs no change.

## Tests

`backend/tests/test_metrics_access.py` — default-open (`/metrics`, `/ready`
reachable, no 401), guard-on 401s (missing / wrong / non-bearer), guard-on
admit with the correct bearer, and `/healthz` always 200.
