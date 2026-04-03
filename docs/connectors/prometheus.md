# Prometheus

> Last verified: v2.0

Prometheus is the metrics backbone of modern infrastructure monitoring. MEHO's Prometheus connector translates natural-language questions about CPU, memory, disk, network, and service health into precise PromQL queries -- so operators can diagnose performance issues without memorizing metric names or query syntax.

All 4 observability connectors (Prometheus, Loki, Tempo, Alertmanager) share the same `ObservabilityHTTPConnector` base, meaning identical auth setup and consistent behavior across your monitoring stack.

## Authentication

All observability connectors use the shared `ObservabilityHTTPConnector` authentication model:

| Method | Credential Fields | Notes |
|--------|------------------|-------|
| None | -- | Direct access to internal Prometheus (e.g., in-cluster) |
| Basic Auth | `username`, `password` | HTTP Basic Auth via reverse proxy (nginx, Apache) |
| Bearer Token | `token` | OAuth2 proxy, service mesh, or API gateway |

**Setup:**

1. **No auth (default):** Point MEHO at your Prometheus URL (e.g., `http://prometheus:9090`). Common for in-cluster access.
2. **Basic auth:** Used when Prometheus is behind a reverse proxy requiring HTTP basic credentials.
3. **Bearer token:** Used when Prometheus is behind an OAuth2 proxy or service mesh that validates bearer tokens.

Optional: Set `skip_tls_verification: true` if Prometheus uses a self-signed certificate.

## Operations

MEHO registers 14 pre-defined operations for Prometheus, organized into four categories:

### Infrastructure (8 operations)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `get_pod_cpu` | READ | Get CPU usage for all pods in a namespace (min/max/avg/current/p95/trend per pod, top 10) |
| `get_namespace_cpu` | READ | Get total CPU usage for a namespace as a single aggregate value |
| `get_node_cpu` | READ | Get CPU usage for all cluster nodes (per node, top 10) |
| `get_pod_memory` | READ | Get memory usage (working set) for all pods in a namespace (per pod in bytes, top 10) |
| `get_namespace_memory` | READ | Get total memory usage (working set) for a namespace |
| `get_node_memory` | READ | Get memory usage for all cluster nodes (per node in bytes, top 10) |
| `get_disk_usage` | READ | Get root filesystem disk usage for all cluster nodes (usage ratio 0-1, top 10) |
| `get_network_io` | READ | Get network receive and transmit rates for all nodes (bytes/sec rx and tx, top 10) |

### Service (1 operation)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `get_red_metrics` | READ | Get RED (Rate, Error rate, Duration) metrics for a service -- request rate, error rate, and latency percentiles (p50, p95, p99) |

### Discovery (4 operations)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_targets` | READ | List all Prometheus scrape targets with health status (up/down/unknown) and Kubernetes labels |
| `discover_metrics` | READ | Discover available metrics grouped by type (counter, gauge, histogram, summary), filterable by name pattern |
| `list_alerts` | READ | List all active alerts with name, state (firing/pending/inactive), labels, annotations, and value |
| `list_alert_rules` | READ | List all alert and recording rules with query, duration, labels, state, and health |

### Query (1 operation)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `query_promql` | **WRITE** | Execute an arbitrary PromQL query (instant or range). **Requires operator approval** -- use pre-defined operations for common queries instead. |

!!! warning "PromQL Escape Hatch"
    The `query_promql` operation requires WRITE trust level because it executes arbitrary queries. MEHO will present the query to the operator for approval before execution. For common use cases, the 13 pre-defined READ operations cover most diagnostic needs without requiring approval.

## Example Queries

Ask MEHO questions like:

- "What's the current CPU usage across all nodes?"
- "Show me the top memory consumers in the production namespace"
- "Are there any scrape targets that are down?"
- "What alerts are currently firing in Prometheus?"
- "Show me the request rate and error rate for the checkout service"
- "How has disk usage trended across nodes over the last 24 hours?"
- "What's the p95 latency for the API gateway service?"
- "List all available metrics related to HTTP requests"
- "Show me network I/O for the cluster nodes over the last 6 hours"
- "Are there any alert rules in an unhealthy state?"

MEHO translates these into the appropriate pre-defined operations, selecting the right parameters automatically. For complex or custom queries, MEHO may propose a PromQL query via `query_promql` (which requires your approval).

## Topology

Prometheus discovers **ScrapeTarget** entities representing monitored endpoints:

| Entity Type | Properties | Cross-System Links |
|-------------|------------|-------------------|
| ScrapeTarget | `job` (scrape job name), `instance` (host:port), `health` (up/down/unknown) | Links to K8s pods/nodes via optional `namespace`, `pod`, `node` labels from Kubernetes service discovery |

When Prometheus is configured with Kubernetes service discovery, scrape targets include K8s labels (`namespace`, `pod`, `node`), enabling MEHO to correlate metrics with Kubernetes entities discovered by the K8s connector. For example, a target showing `health: down` can be cross-referenced with the pod's status and restart count from Kubernetes.

## Cross-System Observability

Prometheus is most powerful when combined with the other observability connectors:

- **Prometheus + Loki:** Correlate metric spikes (e.g., high error rate from `get_red_metrics`) with log entries from the same time window (e.g., `search_logs` with matching namespace and time range)
- **Prometheus + Tempo:** When RED metrics show elevated latency, find the slowest traces in Tempo (`get_slow_traces`) for the same service to identify the bottleneck
- **Prometheus + Alertmanager:** Prometheus alert rules trigger alerts in Alertmanager -- use Prometheus to see the metric values and Alertmanager to manage the alert lifecycle (silence, acknowledge)

In a single MEHO conversation, you can ask "Why is the checkout service slow?" and MEHO will query Prometheus for RED metrics, Loki for error logs, and Tempo for slow traces -- building a complete picture without switching between dashboards.

## Troubleshooting

### Empty Results for Metric Queries

**Symptom:** Operations like `get_pod_cpu` return no data.
**Cause:** The requested time range exceeds Prometheus retention period, or the namespace/service name doesn't match any labels.
**Fix:** Check your Prometheus retention period (`--storage.tsdb.retention.time`). Use `discover_metrics` to verify metric names exist, and `list_targets` to confirm targets are being scraped.

### Scrape Targets Showing "Down"

**Symptom:** `list_targets` shows targets with `health: down`.
**Cause:** The target endpoint is unreachable, returning errors, or taking too long to respond.
**Fix:** Check the target pod/service status via the Kubernetes connector. Verify network policies allow Prometheus to reach the target. Check the scrape timeout configuration.

### RED Metrics Not Available

**Symptom:** `get_red_metrics` returns no data for a service.
**Cause:** The service doesn't expose `http_request_duration_seconds` histogram (the default metric). Service may use a custom metric name.
**Fix:** Use the `histogram_metric` parameter to specify the service's actual histogram metric name. Use `discover_metrics(search='duration')` to find available histogram metrics.

### Natural Language to PromQL Mismatch

**Symptom:** MEHO interprets a question differently than intended.
**Cause:** Ambiguous phrasing -- "usage" could mean CPU, memory, or disk.
**Fix:** Be specific: "CPU usage for pods in the payments namespace" rather than just "usage in payments". MEHO maps specific terms to specific operations.

---

*Connector type: Observability (ObservabilityHTTPConnector)*
*Operations: 14 (13 READ, 1 WRITE)*
*Topology entities: ScrapeTarget*
