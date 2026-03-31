# Alertmanager

> Last verified: v2.0

Alertmanager handles alert routing, grouping, silencing, and notification for Prometheus-based monitoring stacks. MEHO's Alertmanager connector lets operators investigate active alerts, manage silences, and check cluster health through natural conversation -- including **WRITE operations** for creating and expiring silences with full trust model enforcement.

All 4 observability connectors (Prometheus, Loki, Tempo, Alertmanager) share the same `ObservabilityHTTPConnector` base, meaning identical auth setup and consistent behavior across your monitoring stack.

## Authentication

All observability connectors use the shared `ObservabilityHTTPConnector` authentication model:

| Method | Credential Fields | Notes |
|--------|------------------|-------|
| None | -- | Direct access to internal Alertmanager (e.g., in-cluster) |
| Basic Auth | `username`, `password` | HTTP Basic Auth via reverse proxy (nginx, Apache) |
| Bearer Token | `token` | OAuth2 proxy, service mesh, or API gateway |

**Setup:**

1. **No auth (default):** Point MEHO at your Alertmanager URL (e.g., `http://alertmanager:9093`). Common for in-cluster access.
2. **Basic auth:** Used when Alertmanager is behind a reverse proxy requiring HTTP basic credentials.
3. **Bearer token:** Used when Alertmanager is behind an OAuth2 proxy or service mesh.

Optional: Set `skip_tls_verification: true` if Alertmanager uses a self-signed certificate.

## Operations

MEHO registers 9 pre-defined operations for Alertmanager, organized into three categories. **Alertmanager is the only observability connector with WRITE operations** -- silence management requires operator approval through MEHO's trust model.

### Alerts (3 operations)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_alerts` | READ | List all alerts with optional filters (state, severity, alertname, receiver). Returns alerts grouped by alertname with summary header: total, firing, silenced, inhibited counts. |
| `get_firing_alerts` | READ | Convenience shortcut: list only currently firing alerts (not silenced, not inhibited). Optional severity filter. |
| `get_alert_detail` | READ | Progressive disclosure for a single alert by fingerprint: full labels, all annotations (including runbook_url, dashboard_url), generatorURL, silenced_by, inhibited_by, startsAt. |

### Silences (4 operations)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_silences` | READ | List all silences with state summary (active/pending/expired counts) and compact table: ID, matchers, state, created_by, time remaining, comment. |
| `create_silence` | **WRITE** | Create a silence with explicit matchers and duration. Default duration 2h. Created_by auto-set to `MEHO (operator: username)`. **Requires operator approval.** |
| `silence_alert` | **WRITE** | Convenience: silence a specific alert by fingerprint. Auto-builds matchers from the alert's labels. Default duration 2h. **Requires operator approval.** |
| `expire_silence` | **WRITE** | Expire an active silence by ID. Re-enables notifications (safe direction). **Requires operator approval.** |

### Status (2 operations)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `get_cluster_status` | READ | Alertmanager cluster health: cluster name, peer count, per-peer details (name, address, state), HA readiness. |
| `list_receivers` | READ | List configured notification receivers (PagerDuty, Slack, email, webhook, etc.). Returns receiver names. |

!!! info "Trust Model for WRITE Operations"
    Alertmanager is the only observability connector with WRITE operations. When MEHO determines that creating or expiring a silence is appropriate, it presents the action to the operator for approval through the trust model UI. The operator sees exactly what matchers will be used, the duration, and the comment before approving.

    The `created_by` field is automatically set to `MEHO (operator: username)` so silences created through MEHO are traceable in audit logs.

## Example Queries

Ask MEHO questions like:

- "What alerts are currently firing?"
- "Show me all critical alerts for the production cluster"
- "Are there any silenced alerts I should know about?"
- "Give me details on the HighCPU alert that's firing"
- "Silence the disk-full alert for node-3 for 2 hours while I investigate"
- "How long has the HighMemoryUsage alert been firing?"
- "What notification receivers are configured?"
- "Is the Alertmanager cluster healthy?"
- "Show me all alerts with severity=warning"
- "Remove the silence on the NodeDiskPressure alert -- the issue is fixed"

For READ operations (checking alerts, listing silences), MEHO executes immediately. For WRITE operations (creating/expiring silences), MEHO presents the action for operator approval first.

## Topology

Alertmanager is a **query-only connector** -- it does not discover topology entities. Alerts are ephemeral states about infrastructure components that other connectors (Kubernetes, Prometheus) already track as topology entities.

The connection between Alertmanager alerts and infrastructure happens through alert labels: the `namespace`, `pod`, `instance`, and `node` labels in alerts match the entities discovered by Kubernetes and Prometheus connectors.

## Cross-System Observability

Alertmanager is the action layer of the observability stack:

- **Alertmanager + Prometheus:** Alerts originate from Prometheus alert rules. Use Prometheus to check the current metric values behind an alert, and Alertmanager to manage the alert lifecycle (investigate, silence, acknowledge). The `generatorURL` in alert details links back to the Prometheus expression that triggered the alert.
- **Alertmanager + Loki:** When an alert fires, use Loki to investigate the root cause by searching for error logs in the affected namespace and service during the alert's time window (`startsAt` from alert details).
- **Alertmanager + Tempo:** For latency-related alerts, use Tempo to find slow or error traces from the affected service during the alert window. The service name from the alert's labels maps directly to Tempo's `service.name` tag.

A typical alert investigation flow: Alertmanager surfaces the firing alert -> Prometheus shows the metric trend -> Loki reveals error logs -> Tempo traces the failing requests -> Alertmanager silences the alert while the fix is deployed.

## WRITE Operations and Trust Model

Alertmanager is unique among observability connectors because it supports **WRITE operations** that modify system state:

### How Silence Operations Work

1. **Operator asks:** "Silence the HighCPU alert on node-3 for 2 hours"
2. **MEHO proposes:** Creates a silence request with matchers derived from the alert's labels
3. **Trust modal appears:** Shows the operator exactly what will be silenced, the duration, and the matchers
4. **Operator approves:** MEHO creates the silence via the Alertmanager API
5. **Audit trail:** The silence is created with `created_by: MEHO (operator: username)` for traceability

### Safety Properties

- **Silences are time-bounded:** Default 2 hours, always require an explicit duration. No permanent silences.
- **Expire is safe:** Expiring a silence re-enables notifications -- it can never suppress alerts.
- **All WRITE operations logged:** Every silence creation and expiration is tracked in MEHO's audit trail with the operator who approved it.
- **No alert deletion:** MEHO cannot delete or resolve alerts -- only Alertmanager's internal logic resolves alerts when conditions clear.

## Troubleshooting

### Alert Routing Confusion

**Symptom:** Alerts are going to unexpected receivers or not being received at all.
**Cause:** Alertmanager routing rules can be complex with nested routes, matchers, and inhibition rules.
**Fix:** Use `list_receivers` to see configured receivers, and `list_alerts` with the `receiver` filter to check which alerts route to which receiver. Check the Alertmanager routing tree configuration for mismatched matchers.

### Silence Not Taking Effect

**Symptom:** A silence was created but the alert is still showing as firing.
**Cause:** The silence matchers don't exactly match the alert's labels. Label matching in Alertmanager is exact (unless regex matchers are used).
**Fix:** Use `get_alert_detail` to see the alert's full labels, then ensure the silence matchers match those labels exactly. Use `silence_alert` (which auto-builds matchers from the alert) instead of `create_silence` for exact matching.

### Silence Duration Confusion

**Symptom:** Silences expire sooner or later than expected.
**Cause:** Duration is relative to creation time. If `starts_at` is set to a future time, the silence won't take effect until then.
**Fix:** For immediate silences, omit `starts_at` and only specify `duration`. For maintenance windows, use explicit `starts_at` and `ends_at` timestamps (ISO8601 format).

### Cluster Status Shows Unhealthy Peers

**Symptom:** `get_cluster_status` shows peers with non-ready state.
**Cause:** Alertmanager HA cluster requires mesh communication between peers. Network issues or peer failures can cause degraded cluster state.
**Fix:** Check network connectivity between Alertmanager peers. Verify that all Alertmanager instances can reach each other on the cluster port (default 9094). Restart unhealthy peers if necessary.

### Too Many Alerts to Investigate

**Symptom:** `list_alerts` returns hundreds of alerts, making investigation overwhelming.
**Cause:** Alert storms from cascading failures or noisy alerting rules.
**Fix:** Use `get_firing_alerts(severity='critical')` to focus on the most important alerts first. Use `list_alerts(state='active')` to exclude already-silenced alerts. Investigate the root cause of the storm rather than individual alerts.

---

*Connector type: Observability (ObservabilityHTTPConnector)*
*Operations: 9 (6 READ, 3 WRITE)*
*Topology entities: None (query-only, alerts are ephemeral)*
