# Network Diagnostic Tools

> Added in v2.3 (Phase 96.1)

MEHO includes built-in network diagnostic tools that the investigation agent uses during connectivity troubleshooting. These tools work without any connector configuration -- the agent can probe endpoints directly from the MEHO server.

## Available Tools

| Tool | Purpose | Key Output |
|------|---------|------------|
| `dns_resolve` | Resolve hostnames to IP addresses and DNS records | A, AAAA, CNAME, MX, SRV, TXT, NS, SOA records |
| `tcp_probe` | Test raw TCP port connectivity | Connected/refused/timeout + latency |
| `http_probe` | Full HTTP/HTTPS endpoint check | Status code, latency, headers, redirects, body preview |
| `tls_check` | Inspect TLS certificates | Certificate chain, SANs, expiry date, issuer |

## How It Works

The agent automatically uses diagnostic tools when an operator reports connectivity issues. No manual tool invocation is needed -- the agent follows a decision tree:

1. **DNS first** -- Resolve the hostname to see where it points
2. **Probe the endpoint** -- HTTP probe for web endpoints, TCP probe for raw ports, TLS check for certificate issues
3. **Topology lookup** -- Match probe results to known infrastructure entities
4. **Connector investigation** -- Use the matched connector to investigate the backend

### Example Investigation

**Operator:** "The payment API at api.payments.internal is returning 502 errors"

**Agent automatically:**

1. Runs `dns_resolve("api.payments.internal")` -- gets IP `10.0.5.42`
2. Runs `http_probe("https://api.payments.internal")` -- confirms 502, notes 12s latency
3. Looks up `10.0.5.42` in topology -- finds it's a Kubernetes service in the `prod` cluster
4. Queries the K8s connector for pod status -- finds pods in CrashLoopBackOff
5. Reports findings with probe data + K8s investigation results

## Topology Integration

Each probe emits topology entities as persistent breadcrumbs:

| Tool | Entity Types Emitted |
|------|---------------------|
| `dns_resolve` | `ExternalURL`, `IPAddress` |
| `http_probe` | `ExternalURL` |
| `tls_check` | `TLSCertificate` |
| `tcp_probe` | `IPAddress` |

These entities are stored in the topology graph. Future investigations can find them via `lookup_topology` without re-probing, and they enable cross-system correlation (e.g., linking an external URL to the K8s service that serves it).

## Feature Flag

Network diagnostic tools are controlled by the `MEHO_FEATURE_NETWORK_DIAGNOSTICS` feature flag:

```bash
# Disable network diagnostic tools (default: true)
MEHO_FEATURE_NETWORK_DIAGNOSTICS=false
```

When disabled, the diagnostic tools are removed from the agent's tool set and the topology schema is not registered. The agent will not be able to probe endpoints but can still investigate via connectors.

## Dependencies

- `aiodns>=4.0.0` -- Async DNS resolution (A, AAAA, CNAME, MX, SRV, TXT, NS, SOA)
- `httpx` -- Already a project dependency, used for HTTP/HTTPS probes
- Standard library `asyncio` -- Used for TCP probes and TLS checks

## Common Investigation Patterns

| Symptom | Probe Sequence | Then |
|---------|---------------|------|
| "Website down" | `dns_resolve` -> `http_probe` | Check status code, look up in topology, investigate backend |
| "Can't connect to DB" | `dns_resolve` -> `tcp_probe` (port 5432/3306) | Check if port is open, find DB in topology |
| "Certificate expired" | `tls_check` | Report expiry, check certificate details |
| "Intermittent timeouts" | `dns_resolve` -> `http_probe` (note latency) | Compare latency across multiple probes |
| "Wrong endpoint" | `dns_resolve` -> check CNAME chain | Look for DNS misconfiguration |
| "Email not working" | `dns_resolve` (MX records) | Check MX records point to correct servers |
