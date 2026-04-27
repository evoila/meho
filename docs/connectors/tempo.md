# Tempo

> Last verified: v2.0

Tempo is the distributed tracing backend that gives MEHO visibility into request flows across microservices. MEHO's Tempo connector translates natural-language questions about traces into precise API calls -- operators can find slow traces, error traces, and service dependencies without writing TraceQL or navigating trace waterfalls manually.

All 4 observability connectors (Prometheus, Loki, Tempo, Alertmanager) share the same `ObservabilityHTTPConnector` base, meaning identical auth setup and consistent behavior across your monitoring stack.

## Authentication

All observability connectors use the shared `ObservabilityHTTPConnector` authentication model:

| Method | Credential Fields | Notes |
|--------|------------------|-------|
| None | -- | Direct access to internal Tempo (e.g., in-cluster) |
| Basic Auth | `username`, `password` | HTTP Basic Auth via reverse proxy (nginx, Apache) |
| Bearer Token | `token` | OAuth2 proxy, service mesh, or API gateway |

**Setup:**

1. **No auth (default):** Point MEHO at your Tempo URL (e.g., `http://tempo:3200`). Common for in-cluster access.
2. **Basic auth:** Used when Tempo is behind a reverse proxy requiring HTTP basic credentials.
3. **Bearer token:** Used when Tempo is behind an OAuth2 proxy or service mesh.

**Multi-tenancy:** For multi-tenant Tempo deployments (e.g., Grafana Cloud Tempo), configure `org_id` on the connector. MEHO will include the `X-Scope-OrgID` header on all requests automatically.

Optional: Set `skip_tls_verification: true` if Tempo uses a self-signed certificate.

## Operations

MEHO registers 10 pre-defined operations for Tempo, organized into four categories:

### Traces (5 operations)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `search_traces` | READ | Search traces by service, operation, duration, status, and tags. Returns compact one-liner per trace: trace_id, root service, root operation, duration, span count, error count. |
| `get_trace` | READ | Retrieve full trace as a flat span table: timestamp, service, operation, duration, status, span_id, parent_span_id. Top 50 spans by duration. |
| `get_span_details` | READ | Deep-dive into a single span: all custom tags, db.statement, exception stacktrace, and resource attributes. |
| `get_slow_traces` | READ | Find traces exceeding a duration threshold (default >1s). Shortcut for search_traces pre-filtered by duration. |
| `get_error_traces` | READ | Find traces with errors. Shortcut for search_traces pre-filtered to status=error. |

### Service Graph (2 operations)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `get_service_graph` | READ | Retrieve service dependency graph: nodes (service, span count, error rate, avg duration) and edges (source to target, call rate, error rate, p50/p95 latency). Requires Tempo metrics-generator. |
| `get_trace_metrics` | READ | Get trace-derived metrics per service: span count, error count, avg duration, p95 duration. |

### Discovery (2 operations)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_tags` | READ | Discover available trace tags (span attributes): service.name, http.method, http.status_code, db.system, etc. |
| `list_tag_values` | READ | Get all values for a specific trace tag -- e.g., list all services, HTTP methods, or database systems represented in traces. |

### Query (1 operation)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `query_traceql` | **WRITE** | Execute a raw TraceQL query. **Requires operator approval.** Common patterns: `{span.http.status_code >= 500}`, `{resource.service.name = "api"} && {span.db.system = "redis"}`. |

!!! warning "TraceQL Escape Hatch"
    The `query_traceql` operation requires WRITE trust level because it executes arbitrary queries against your trace data. MEHO will present the query to the operator for approval. For standard trace investigation, the 9 pre-defined READ operations cover most diagnostic scenarios.

## Example Queries

Ask MEHO questions like:

- "Find traces for the checkout service with errors in the last hour"
- "Show me the slowest traces to the payment gateway"
- "Look up trace ID abc123def456"
- "What's the service dependency graph look like?"
- "Which service has the highest error rate in the last 30 minutes?"
- "Show me the details of the slowest span in this trace"
- "Find traces where the database call took longer than 2 seconds"
- "What services are involved in requests to the /api/checkout endpoint?"
- "Are there any traces with HTTP 500 errors from the API gateway?"
- "What trace tags are available for filtering?"

MEHO uses a progressive disclosure approach: first `search_traces` to find relevant traces, then `get_trace` to inspect the span breakdown, then `get_span_details` to deep-dive into a specific span's attributes and stack traces.

## Topology

Tempo is a **query-only connector** -- it does not discover topology entities. Traces describe the behavior of services and infrastructure that other connectors (Kubernetes, Prometheus) already track as topology entities.

The connection between Tempo traces and infrastructure happens through resource attributes: `service.name` in traces maps to Kubernetes deployments/services, and `net.host.name` maps to pods and nodes.

## Cross-System Observability

Tempo completes the observability picture when combined with Prometheus and Loki:

- **Tempo + Prometheus:** When RED metrics show elevated latency for a service, find the actual slow traces in Tempo to identify which specific operations or downstream calls are causing the delay. The `get_service_graph` operation shows dependency relationships that explain cascading latency.
- **Tempo + Loki:** Trace a request from end to end: find the error trace in Tempo, then correlate with log entries in Loki using the timestamp and service name. If the trace shows a database error, check Loki for the corresponding stack trace or error details.
- **Tempo + Alertmanager:** When an alert fires for high error rate, use Tempo to find the error traces from that time period and identify which service in the call chain is the root cause.

The canonical cross-system investigation flow: Prometheus detects the anomaly (metric spike) -> Tempo reveals the request flow (which services, where the latency is) -> Loki provides the details (stack traces, error messages).

## Troubleshooting

### Trace ID Not Found

**Symptom:** `get_trace` returns no data for a known trace ID.
**Cause:** The trace may have been ingested after the retention period, or the trace ID format is incorrect. Tempo trace IDs are hex strings.
**Fix:** Verify the trace ID format (should be a hex string, e.g., `0af7651916cd43dd8448eb211c80319c`). Check your Tempo retention period -- traces older than retention are automatically deleted.

### Service Graph Returns Empty

**Symptom:** `get_service_graph` returns no data.
**Cause:** The service graph endpoint requires `metrics-generator` to be enabled in Tempo's configuration. Without it, the endpoint returns 404 (MEHO handles this gracefully as empty result).
**Fix:** Enable `metrics-generator` in your Tempo configuration. The service graph is derived from trace data and requires this component to compute service relationships.

### Search Returns Too Many or Too Few Traces

**Symptom:** `search_traces` returns unexpected number of results.
**Cause:** Default limit is 20 traces. For broad searches (no service filter), this may not surface the relevant traces.
**Fix:** Use specific filters: `service_name`, `min_duration`, `status='error'`, or `tags` to narrow results. Increase `limit` if needed, but keep in mind that very large result sets increase LLM context usage.

### TraceQL Syntax Errors

**Symptom:** `query_traceql` returns errors about invalid syntax.
**Cause:** TraceQL has a specific syntax different from PromQL or LogQL. Common mistakes: using `=` instead of `=` for string matches, forgetting curly braces around span selectors.
**Fix:** Use pre-defined operations when possible. For TraceQL, follow the pattern: `{resource.service.name = "my-service" && span.http.status_code >= 500}`. Use `list_tags` to discover valid tag names.

### Slow Trace Search Performance

**Symptom:** `search_traces` or `get_slow_traces` takes a long time to respond.
**Cause:** Tempo search performance depends on the backend storage and the time range being searched.
**Fix:** Narrow the time range (use `1h` instead of `24h`). Filter by service name to reduce the search space. Consider using the `status='error'` filter if you're specifically looking for failures.

---

*Connector type: Observability (ObservabilityHTTPConnector)*
*Operations: 10 (9 READ, 1 WRITE)*
*Topology entities: None (query-only)*
