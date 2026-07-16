# Target-destination SSRF guard

## Overview

Operator-registered targets carry a `host` (and optionally `fqdn`) that
the HTTP connector transport dials with a Vault-resolved credential
attached. Without a destination screen, anything able to drive
`POST`/`PATCH /api/v1/targets` could point a target at loopback,
RFC 1918 space, `169.254.169.254` (cloud metadata), or the IPv6
analogues and have the backplane deliver a credential there — classic
server-side request forgery. The guard rejects non-public destinations
at **two layers** and is overridable only through an explicit,
operator-configured allowlist env var.

MEHO is an on-prem product: registering appliances on private space is
the normal case, not an edge case. The intended deployment posture is
"guard on, LAN ranges allowlisted" — a scoped opt-in, never a global
off-switch.

## Key types

- `meho_backplane/targets/ssrf_guard.py` — the shared guard module:
  - `TARGET_SSRF_ALLOWLIST_ENV` = `MEHO_TARGET_SSRF_ALLOWLIST`.
    Comma-separated CIDR ranges (`10.0.0.0/8`), bare IPs, and/or
    hostname literals (`vcenter.lab.internal`). Read per call (no
    process cache). A malformed CIDR raises a loud `ValueError` —
    silently dropping an entry would re-block a destination the
    operator explicitly opted in.
  - `TargetDestinationBlockedError(ValueError)` — raised on rejection.
    `ValueError` so pydantic validators surface it as a structured 422.
  - `assert_public_destination(host)` / `assert_public_destination_async(host)`
    — sync (schema-validator) and async (dispatch hot path, via
    `asyncio.to_thread`) entry points.
  - `_dialed_host(candidate)` — normalizes a non-IP-literal value to
    the host component httpx actually dials for `https://{candidate}`
    (refusing credentials/query/fragment and unparseable values).
  - `_resolve_addrs(host)` — the single DNS seam
    (`socket.getaddrinfo`), monkeypatched by tests.
- `meho_backplane/connectors/adapters/http.py` —
  `SsrfBlockedError(httpx.ConnectError)`, the connect-time rejection.

## Control flow

1. **Create/update** — `TargetCreate`/`TargetUpdate` field validators on
   `host` and `fqdn` call `assert_public_destination`. An IP literal is
   checked directly. Any other value is first normalized to the host
   httpx will actually dial (`_dialed_host`: the host component of
   `httpx.URL(f"https://{value}")`) — the transport composes its base
   URL as `https://{host}`, so URL structure inside the stored value is
   parsed *out* by httpx and the socket reaches the normalized host,
   never the raw string. Values embedding credentials, a query, or a
   fragment are refused outright (fail-closed, as are values httpx
   cannot parse); path- or port-bearing values are retained but reduced
   to their dialed host, which is then re-checked as an IP literal
   (`<ip>:<port>` normalizes back to a screenable literal), matched
   against allowlist hostname entries, and otherwise resolved, with
   every resolved address screened (any blocked, non-allowlisted
   candidate rejects). Blocked classes extend the ingest spec-fetch
   guard (`operations/ingest/openapi.py::_assert_fetchable_remote_url`):
   `not is_global` — which adds CGNAT `100.64.0.0/10` — plus the
   explicit `is_private` / `is_loopback` / `is_link_local` /
   `is_reserved` / `is_multicast` / `is_unspecified` union (kept
   alongside `is_global` because global-scope multicast reports
   `is_global=True` in both address families).
2. **Connect** — `HttpConnector._http_client` awaits
   `assert_public_destination_async(target.host)` on **every**
   acquisition, before the pool lookup, so a pooled client for a
   hostname whose DNS answer has since moved into private space is
   refused too (DNS-rebind window). Rejection is re-raised as
   `SsrfBlockedError`, an `httpx.ConnectError` subclass, so the
   dispatcher's existing `ConnectError` arm flattens it into the
   structured `connector_error` shape — no dispatcher changes. It is
   excluded from the transport retry policy (`_retryable`): the verdict
   is deterministic.

Design choices:

- **Screen what you dial.** Both layers screen the httpx-normalized
  dial host, so a value cannot screen as one destination and dial
  another. Path structure alone is *not* refused because the GitHub
  connector documents `owner/repo` / `api.github.com/repos/owner/repo`
  `host` shapes as an operator-facing contract (its `_base_url`
  override dials `api.github.com` regardless); the generic transport
  still screens the host such a value would dial.
- **Fail-open on unresolvable hostnames** at both layers. Split-horizon
  DNS is normal on-prem (the backplane may not share the target's
  resolver view at create time), and an unresolvable name cannot be
  dialed anyway; the moment it *does* resolve, the connect-time
  re-check screens the answer before any request is issued.
- **No topology oracle**: rejection messages never echo the resolved
  address — only the env-var remediation. Callers cannot use the create
  API as an internal-DNS probe.

## Dependencies

- stdlib `ipaddress` + `socket.getaddrinfo` only; no new packages.
- Test suite: `backend/tests/conftest.py::_default_target_ssrf_allowlist`
  (autouse) pins a permissive RFC 1918/loopback/ULA/link-local(v6)
  allowlist and stubs `_resolve_addrs` to "unresolvable" so the
  pre-guard fixture corpus (hundreds of `10.x` / `*.invalid` targets)
  keeps validating without real DNS traffic. `169.254.0.0/16` is
  deliberately not allowlisted suite-wide. Guard tests
  (`backend/tests/test_targets_ssrf_guard.py`) clear/re-pin both.

## Known issues

- The ingest spec-fetch guard (`_assert_fetchable_remote_url`) still
  lacks the `not is_global` posture this guard adopted, so CGNAT
  `100.64.0.0/10` passes it — separate sink, adjacent follow-up.
- The guard screens the transport's *intended* destination; it does not
  pin the subsequent httpx socket connect to the screened address
  (full DNS pinning would require a custom transport). The per-dispatch
  re-check narrows the TOCTOU window to a single dispatch.
- Non-HTTP transports (`SshConnector`, the kubeconfig-driven Kubernetes
  connector, GitHub App session client) are separate sinks outside this
  guard.

## Deployment surface

The allowlist env var is a first-class Helm chart value:
`config.targetSsrfAllowlist` in `deploy/charts/meho/values.yaml` renders
into `MEHO_TARGET_SSRF_ALLOWLIST` on the backplane ConfigMap
(`templates/configmap.yaml`), which the Deployment injects into the
container via `envFrom.configMapRef` — so a populated value reaches the
guard with no `extraEnv` escape hatch. It is schema-typed in
`values.schema.json` as a plain optional string: deliberately absent
from `config.required` and carrying no `minLength`, so the safe default
`""` validates on every install (and surfaces under `helm show values`).
That default renders `MEHO_TARGET_SSRF_ALLOWLIST: ""`, a genuine no-op
that keeps the guard fully on. See CHANGELOG v0.20.0 ("Deployment
impact — action likely required") and
`deploy/values-examples/values-rdc-example.yaml` for a populated
private-range example.

## References

- Task: evoila-bosnia/meho-internal#153 (parent backlog #101, goal #87)
- OWASP SSRF Prevention Cheat Sheet:
  <https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html>
- Sibling guard: `backend/src/meho_backplane/operations/ingest/openapi.py`
  (`_assert_fetchable_remote_url`) — different sink, same rejection
  classes.
