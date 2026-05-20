<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Reverse-proxy contract (TLS-terminating Ingress)

Operator runbook for the handshake between MEHO's backplane and the
cluster's TLS-terminating reverse proxy (typically an Ingress
controller — `nginx-ingress`, `traefik`, or equivalent). Without this
contract held on both sides, FastAPI's trailing-slash 307 redirects
emit a `Location: http://...` header that downgrades the client's
second hop and creates a MITM window — surfaced by the 2026-05-20 RDC
in-lab dogfood as **Signal #3** and fixed in Issue
[#730](https://github.com/evoila/meho/issues/730).

## Why this exists

The backplane runs the FastAPI app behind uvicorn. When the cluster's
Ingress terminates TLS and forwards the request to the backplane Pod
over plain HTTP, uvicorn sees a `scheme: http` ASGI scope by default.
FastAPI's `redirect_slashes` (enabled by default; the v0.2 backplane
relies on it for canonical trailing-slash semantics) reflects that
scheme into the `Location` header:

```
$ curl -k -L -w "final=%{url_effective}\n" \
    https://meho.evba.lab/api/v1/connectors/k8s-1.x/operations
HTTP/1.1 307 Temporary Redirect
Location: http://meho.evba.lab/api/v1/connectors/k8s-1.x/operations/
                ^^^^ HTTPS→HTTP downgrade
{"detail":"Method Not Allowed"}
final=http://meho.evba.lab/api/v1/connectors/k8s-1.x/operations/
```

`curl -k -L` shrugs through the downgrade; a malicious on-path
attacker can observe the second hop in plaintext. The fix is to make
uvicorn honour `X-Forwarded-Proto: https` from the Ingress so the 307
reflects `https://`, but only when the headers come from a connecting
peer the backplane trusts as an upstream proxy — naively trusting
every peer would let any in-cluster Pod lie about the original
scheme.

## Backplane side (what `evoila/meho` ships)

The backplane Pod's container CMD includes the uvicorn
`--proxy-headers` flag:

```dockerfile
CMD ["uvicorn", "meho_backplane.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
```

`--proxy-headers` installs uvicorn's `ProxyHeadersMiddleware` at the
ASGI server layer (before FastAPI sees the request). The middleware
parses `X-Forwarded-Proto` and `X-Forwarded-For` and updates the ASGI
scope's `scheme` + `client` fields **only when the immediate TCP peer
is in the trusted list**. The trusted list comes from the
`FORWARDED_ALLOW_IPS` env var (uvicorn reads it natively; equivalent
to the `--forwarded-allow-ips` CLI flag).

The Helm chart wires that env var via
[`deploy/charts/meho/values.yaml`](../../deploy/charts/meho/values.yaml):

```yaml
config:
  # uvicorn's secure default — fails-closed in-cluster because the
  # Ingress controller's pod IP is never the loopback. Operators MUST
  # override for production.
  forwardedAllowIps: "127.0.0.1"
```

The chart's [`templates/configmap.yaml`](../../deploy/charts/meho/templates/configmap.yaml)
renders the value into `FORWARDED_ALLOW_IPS`, which the Deployment's
container picks up via `envFrom: configMapRef`.

## Operator side (what the cluster MUST provide)

### 1. The Ingress MUST forward the original scheme

The TLS-terminating Ingress in front of the backplane MUST set, at
minimum:

| Header | Value | Why |
| --- | --- | --- |
| `X-Forwarded-Proto` | `https` (when the client connected over TLS) | The backplane reflects this into the 307 `Location`. Wrong value → wrong redirect. |
| `X-Forwarded-Host` | The hostname the client used (e.g. `meho.evba.lab`) | Logging + future use; not load-bearing for the redirect fix but the standard contract. |
| `X-Forwarded-For` | The chain of client IPs ending in the immediate upstream | Audit + RFC 7239 hygiene. The backplane records this; not load-bearing for the redirect fix. |

Most ingress controllers set these by default once TLS is configured:

- **`nginx-ingress`** — sets all three automatically when
  `nginx.ingress.kubernetes.io/ssl-redirect: "true"` (the default on
  TLS-enabled Ingresses). No extra annotations needed.
- **`traefik`** — sets all three automatically as part of its default
  `entryPoint` configuration; verify with `traefik.toml` /
  `IngressRoute` middleware list.
- Bespoke proxies (HAProxy / Envoy / custom) — confirm the upstream
  block sends all three; the contract is identical to the standard
  reverse-proxy convention documented by uvicorn at
  [uvicorn.org/deployment/](https://www.uvicorn.org/deployment/).

### 2. The chart `forwardedAllowIps` MUST list the proxy's pod IPs

Override `config.forwardedAllowIps` in your `values-<env>.yaml`:

```yaml
config:
  # RKE2 default pod CIDR — every Ingress controller pod's IP falls
  # inside this CIDR, so the backplane trusts X-Forwarded-* only from
  # in-cluster proxies, never from a pod-to-pod direct request.
  forwardedAllowIps: "10.42.0.0/16"
```

Recommended values per ingress posture:

| Cluster shape | Recommended value | Rationale |
| --- | --- | --- |
| **Production / multi-tenant** | The pod CIDR of the ingress controller's namespace (e.g. `10.42.0.0/16` for RKE2 default) — or the specific pod IPs if pinned | Restricts trust to in-cluster proxies; pod-to-pod requests from other workloads cannot spoof the scheme |
| **Single-tenant lab / RDC dogfood** | The cluster's pod CIDR (`10.42.0.0/16` on RKE2; `10.244.0.0/16` on Kind/Calico) | Same trust restriction, broader scope acceptable when there is only one workload |
| **Ephemeral CI cluster (`pr-smoke`)** | `"*"` — disable the allow-list | The PR-smoke cluster is single-tenant by construction; every connecting peer is the cluster's own proxy. Never use this in production |
| **Default (no override)** | `"127.0.0.1"` (uvicorn's secure default) | Fails-closed: the Ingress controller's pod IP is never the loopback, so `X-Forwarded-*` is silently ignored and the bug persists. This is the intended behaviour — a misconfigured cluster fails visibly rather than silently downgrading |

### Anti-pattern: leaving the default in production

If the chart is installed without overriding `config.forwardedAllowIps`
on a TLS-terminated production deploy, the symptom is the **exact
bug** Issue #730 fixed — `curl -k -L` against any trailing-slash
route returns `Location: http://...`. The fix is to set the value
correctly, not to add `--proxy-headers=*` at the container level
(which would also work but defeats the trust restriction).

## Verification

After deploying with a non-default `forwardedAllowIps`:

```bash
# Replace meho.evba.lab with your Ingress host and the path with any
# route that 307-redirects (the prefix without trailing slash on a
# group route is the canonical case).
curl -k -L -w "final=%{url_effective}\n" \
    https://meho.evba.lab/api/v1/connectors/k8s-1.x/operations \
    -H "Authorization: Bearer ${MEHO_TOKEN}"
```

Expected: `final=https://meho.evba.lab/api/v1/connectors/k8s-1.x/operations/`.

Symptom of misconfiguration: `final=http://...`. Diagnose by
checking, in order:

1. **Ingress headers** — `kubectl logs -n ingress-nginx <pod>` (or
   the controller's equivalent) for any line that includes
   `X-Forwarded-Proto` on the request to the backplane. The value MUST
   be `https`.
2. **Backplane env var** — `kubectl exec -n <ns> <backplane-pod> --
   printenv FORWARDED_ALLOW_IPS`. The value MUST match the Ingress
   controller's pod CIDR (or `*` for ephemeral clusters).
3. **uvicorn CMD** — `kubectl exec -n <ns> <backplane-pod> -- ps -ef
   | grep uvicorn`. The CMD MUST include `--proxy-headers`.

If all three are correct and the bug persists, the immediate TCP peer
seen by the backplane is something other than the Ingress controller
— check for an intermediary (Service-mesh sidecar, eBPF rewriter,
NAT). The peer IP is what `FORWARDED_ALLOW_IPS` must match.

## Security note

`--proxy-headers` alone (with `FORWARDED_ALLOW_IPS=*`) is **not**
secure in a multi-tenant cluster. Any Pod on the cluster could send a
request directly to the backplane Service with
`X-Forwarded-Proto: https` and the backplane would believe it.
Restricting the trust list to the Ingress controller's pod IPs is the
load-bearing half of the fix; `--proxy-headers` is the enabling half.

## References

- [`backend/Dockerfile`](../../backend/Dockerfile) — CMD line with
  `--proxy-headers`.
- [`deploy/charts/meho/values.yaml`](../../deploy/charts/meho/values.yaml)
  `config.forwardedAllowIps` — the operator-facing knob.
- [`deploy/charts/meho/templates/configmap.yaml`](../../deploy/charts/meho/templates/configmap.yaml)
  `FORWARDED_ALLOW_IPS` — the env var the backplane consumes.
- [`backend/tests/test_proxy_headers.py`](../../backend/tests/test_proxy_headers.py)
  — regression test asserting the Location header reflects
  `X-Forwarded-Proto: https`.
- Initiative [#737](https://github.com/evoila/meho/issues/737) (v0.3.1
  dogfood hardening) → Task [#730](https://github.com/evoila/meho/issues/730)
  (this contract).
- [uvicorn — running behind a reverse proxy](https://www.uvicorn.org/deployment/).
- [Starlette `ProxyHeadersMiddleware` source](https://github.com/encode/starlette/blob/master/starlette/middleware/proxy_headers.py)
  — the underlying middleware uvicorn's `--proxy-headers` installs.
