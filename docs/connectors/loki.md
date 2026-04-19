# Loki

> Last verified: v2.0

Loki is the log aggregation engine behind MEHO's log investigation capabilities. MEHO's Loki connector translates natural-language questions about logs into structured LogQL queries -- operators can search, filter, and analyze log data without learning LogQL syntax or knowing which labels are available.

All 4 observability connectors (Prometheus, Loki, Tempo, Alertmanager) share the same `ObservabilityHTTPConnector` base, meaning identical auth setup and consistent behavior across your monitoring stack.

## Authentication

All observability connectors use the shared `ObservabilityHTTPConnector` authentication model:

| Method | Credential Fields | Notes |
|--------|------------------|-------|
| None | -- | Direct access to internal Loki (e.g., in-cluster) |
| Basic Auth | `username`, `password` | HTTP Basic Auth via reverse proxy (nginx, Apache) |
| Bearer Token | `token` | OAuth2 proxy, service mesh, or API gateway |

**Setup:**

1. **No auth (default):** Point MEHO at your Loki URL (e.g., `http://loki:3100`). Common for in-cluster access.
2. **Basic auth:** Used when Loki is behind a reverse proxy requiring HTTP basic credentials.
3. **Bearer token:** Used when Loki is behind an OAuth2 proxy or service mesh.

**Multi-tenancy:** For multi-tenant Loki deployments (e.g., Grafana Cloud Loki), the `org_id` is configured at the connector level. MEHO will include the `X-Scope-OrgID` header on all requests automatically.

Optional: Set `skip_tls_verification: true` if Loki uses a self-signed certificate.

## Operations

MEHO registers 8 pre-defined operations for Loki, organized into three categories:

### Log Search (5 operations)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `search_logs` | READ | Search logs with label filters (namespace, pod, service, container), severity, and text filter. Returns summary stats plus structured log lines (timestamp, severity, source, message). |
| `get_error_logs` | READ | Retrieve error and warning logs. Shortcut for search_logs pre-filtered to error/warn/fatal severity levels. |
| `get_log_context` | READ | Retrieve log lines surrounding a specific timestamp for incident context. Returns lines before and after the target timestamp. |
| `get_log_volume` | READ | Query log volume statistics over time. Returns counts/rates bucketed by time interval -- detects log spikes, outage windows, and volume trends. |
| `get_log_patterns` | READ | Detect repeating log patterns with occurrence counts. Groups similar log lines by structural pattern -- useful for identifying systemic errors vs one-off events. |

### Discovery (2 operations)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_labels` | READ | Discover available log labels in Loki (namespace, pod, container, service_name, level, job, etc.) |
| `list_label_values` | READ | Get all values for a specific log label -- e.g., list all namespaces, pods, or services available in Loki |

### Query (1 operation)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `query_logql` | **WRITE** | Execute an arbitrary LogQL query (log queries or metric queries like `count_over_time`, `rate`). **Requires operator approval.** |

!!! warning "LogQL Escape Hatch"
    The `query_logql` operation requires WRITE trust level because it executes arbitrary queries against your log data. MEHO will present the query to the operator for approval. For standard log investigation, the 7 pre-defined READ operations cover the vast majority of diagnostic scenarios.

## Example Queries

Ask MEHO questions like:

- "Show me error logs from the payment service in the last 15 minutes"
- "What log lines contain 'timeout' across all services?"
- "Are there any OOMKilled errors in the production namespace?"
- "Show me the log context around when the crash happened at 10:30 AM"
- "What's the log volume trend for the checkout service over the last 24 hours?"
- "Are there repeating error patterns in the API gateway logs?"
- "What labels are available in Loki for filtering?"
- "List all namespaces that have logs in Loki"
- "Show me warning and error logs from pod nginx-abc123 in the last hour"
- "Has there been a spike in error logs recently?"

MEHO automatically builds the LogQL label selectors and filters from your natural-language question. For complex queries (e.g., multi-stage pipelines, metric aggregations), MEHO may propose a LogQL query via `query_logql` for your approval.

## Topology

Loki is a **query-only connector** -- it does not discover topology entities. Loki stores log data about infrastructure components that other connectors (Kubernetes, Prometheus) already track as topology entities.

The connection between Loki logs and infrastructure happens through shared labels: the `namespace`, `pod`, and `node` labels in Loki match the Kubernetes entities discovered by the K8s connector, enabling cross-system correlation.

## Cross-System Observability

Loki is most powerful when combined with the other observability connectors:

- **Loki + Prometheus:** When Prometheus shows a metric spike (e.g., elevated error rate from `get_red_metrics`), use Loki to find the corresponding error logs in the same time window. The shared `namespace` and `service` labels make correlation seamless.
- **Loki + Tempo:** Error logs often contain trace IDs. MEHO can extract a trace ID from a Loki log line and retrieve the full distributed trace from Tempo to understand the request flow that triggered the error.
- **Loki + Kubernetes:** When `get_error_logs` surfaces pod crashes, cross-reference with Kubernetes pod status, events, and restart counts to determine if it's a recurring issue or an isolated incident.

In a single MEHO conversation, you can ask "Why are we seeing 500 errors in the checkout service?" and MEHO will check Loki for error logs, correlate with Prometheus request metrics, and if a trace ID is found, pull the full trace from Tempo.

## Troubleshooting

### Large Time Range Queries Return Slowly or Timeout

**Symptom:** Operations like `search_logs` with `time_range='7d'` are very slow or timeout.
**Cause:** Loki is optimized for recent data. Large time ranges scan massive amounts of log data.
**Fix:** Narrow the time range to the relevant window (e.g., `1h`, `6h`). Use `get_log_volume` first to identify the interesting time period, then search within that window.

### No Logs Found for a Known Service

**Symptom:** `search_logs(service='my-service')` returns empty results.
**Cause:** The label name for your service may differ. Loki labels are configured by the log shipper (Promtail, Grafana Agent, etc.) and may use `service_name`, `app`, or `job` instead of `service`.
**Fix:** Use `list_labels` to discover available labels, then `list_label_values` to find the correct label and value for your service.

### Label Cardinality Issues

**Symptom:** Queries with many label filters return unexpected results or errors.
**Cause:** High-cardinality labels (e.g., unique request IDs) can cause performance issues in Loki.
**Fix:** Filter by low-cardinality labels first (namespace, service) and use `text_filter` for high-cardinality matching. Avoid using high-cardinality labels as primary filters.

### Log Pattern Detection Misses

**Symptom:** `get_log_patterns` returns fewer patterns than expected.
**Cause:** Pattern detection works best on structured/semi-structured logs. Highly variable log formats may not cluster well.
**Fix:** Narrow the scope with namespace and service filters to improve pattern quality. Consider using `search_logs` with a text filter for specific error messages instead.

### Natural Language to LogQL Mismatch

**Symptom:** MEHO searches for the wrong labels or filters.
**Cause:** Ambiguous phrasing -- "service errors" could mean different things depending on label structure.
**Fix:** Be specific about the dimension you want to filter: "error logs from namespace payments, pod checkout-abc" rather than just "checkout errors". If unsure about available labels, ask "What labels are available in Loki?" first.

---

*Connector type: Observability (ObservabilityHTTPConnector)*
*Operations: 8 (7 READ, 1 WRITE)*
*Topology entities: None (query-only)*
