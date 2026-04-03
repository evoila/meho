# Events System

> Last verified: v2.3

!!! info "Renamed in v2.3"
    The webhook system was renamed to "events" in v2.3. If upgrading from a previous version, see the [Migration from Webhooks](#migration-from-webhooks) section below.

MEHO's events system allows external systems to trigger investigations and receive results. Events can come from CI/CD pipelines, monitoring alerts, ticketing systems, or any HTTP source. When an event arrives, MEHO matches it to a registered event pattern, creates an investigation session, and optionally posts results back via a response channel.

## Overview

The events system provides a generalized ingress for external triggers:

1. **Receive** -- An external system sends an HTTP POST to `/api/events/{event_id}`.
2. **Verify** -- HMAC-SHA256 signature verification ensures the event is authentic.
3. **Deduplicate** -- Redis-based deduplication prevents processing the same event twice (5-minute window).
4. **Rate limit** -- Per-registration rate limiting (10 sessions per registration per hour) prevents runaway loops.
5. **Render** -- The event payload is rendered into an investigation prompt using Jinja2 templates.
6. **Investigate** -- A new agent session is created and the investigation runs automatically.
7. **Respond** -- If a response channel is configured, investigation results are posted back to the source system.

## Event Registration

Register an event source through the connector operations API:

```
POST /api/connectors/{connector_id}/operations/events
```

Each registration defines:

- **Name** -- Human-readable identifier for the event source.
- **Secret** -- Shared secret for HMAC-SHA256 signature verification.
- **Prompt template** -- Jinja2 template that renders the event payload into an investigation prompt. Available variables: `{{payload.*}}` (any field from the incoming JSON).
- **Response config** -- Optional configuration for posting results back (see Response Channels below).

### Prompt Template Example

```jinja2
A new Jira issue has been created:

**Issue:** {{payload.issue.key}} - {{payload.issue.fields.summary}}
**Priority:** {{payload.issue.fields.priority.name}}
**Reporter:** {{payload.issue.fields.reporter.displayName}}

Investigate this issue and provide a root cause analysis.
```

## Response Channels

New in v2.3, response channels allow MEHO to post investigation results back to the system that triggered the event. This is configured via the `response_config` field on an event registration.

### Configuration

The `response_config` is a JSON object with three fields:

| Field | Description |
|-------|-------------|
| `connector_id` | Which connector to use for the response (can be the same or different connector) |
| `operation_id` | Which operation to invoke (e.g., `add_comment` for Jira, `post_message` for Slack) |
| `parameter_mapping` | Jinja2-rendered dict mapping event and result data to operation parameters |

### Parameter Mapping

The `parameter_mapping` uses Jinja2 templates with these available variables:

| Variable | Description |
|----------|-------------|
| `{{payload.*}}` | Any field from the original event payload |
| `{{result}}` | The formatted investigation result (markdown) |
| `{{session_id}}` | The investigation session UUID |
| `{{session_title}}` | The auto-generated session title |

### Example: Jira Comment Response

```json
{
  "connector_id": "jira-prod",
  "operation_id": "add_comment",
  "parameter_mapping": {
    "issue_key": "{{payload.issue.key}}",
    "body": "## MEHO Investigation Result\n\n{{result}}\n\n---\n*Session: {{session_id}}*"
  }
}
```

### Result Formatting

Investigation results are formatted per connector type:

- **Jira** -- Markdown passed through as-is (Jira renders markdown natively).
- **Slack** -- Converted to mrkdwn format (`**bold**` becomes `*bold*`, etc.).
- **Other** -- Raw markdown used as plain text fallback.

Response execution is best-effort -- a failed response never blocks or fails the investigation itself. Failures are logged for troubleshooting.

## Feature Flag

The events system is controlled by `MEHO_FEATURE_EVENTS`. When set to `false`:

- The `/api/events/` routes are not registered.
- Existing event registrations are preserved in the database but inactive.
- No events are processed.

```bash
# Disable the events system
MEHO_FEATURE_EVENTS=false
```

## Migration from Webhooks

If upgrading from a version prior to v2.3, the following changes apply:

| What Changed | Before (< v2.3) | After (v2.3+) |
|--------------|------------------|---------------|
| Database tables | `webhook_registrations` | `event_registrations` |
| | `webhook_events` | `event_history` |
| API routes | `POST /api/webhooks/{webhook_id}` | `POST /api/events/{event_id}` |
| | `/api/connectors/{id}/operations/webhooks` | `/api/connectors/{id}/operations/events` |
| Feature flag | `MEHO_FEATURE_WEBHOOKS` | `MEHO_FEATURE_EVENTS` |
| Frontend tab | "Webhooks" | "Events" |
| Internal classes | `WebhookRegistrationModel` | `EventRegistrationModel` |
| | `WebhookEventModel` | `EventHistoryModel` |
| | `WebhookExecutor` | `EventExecutor` |

The database migration renames tables automatically. Existing registrations, secrets, and event history are preserved. External systems that POST to the old `/api/webhooks/` URL must be updated to use `/api/events/`.

## Troubleshooting

**Events not triggering investigations:**

- Verify the event registration exists and is active.
- Check the HMAC signature -- the `X-Webhook-Signature` header must contain a valid SHA256 HMAC of the request body using the registration's secret.
- Check rate limits -- each registration is limited to 10 sessions per hour.
- Verify `MEHO_FEATURE_EVENTS=true` (the default).

**Response channel not posting results:**

- Verify the `response_config` connector and operation exist and are accessible.
- Check that the response connector has valid credentials.
- Response failures are logged but do not raise errors -- check application logs.
- Verify Jinja2 templates in `parameter_mapping` render correctly with the payload fields.

**Duplicate events being processed:**

- Deduplication uses a 5-minute Redis window based on payload hash. Events with identical payloads within 5 minutes are dropped.
- If Redis is unavailable, deduplication is skipped and events may be processed multiple times.
