# Fire agent runs from external events

The [scheduler](scheduler.md) fires agent runs on a clock. Its third
trigger kind, **event**, fires them on something happening instead — a
monitoring alert, a registry push, a completed pipeline. This guide
covers the other half of that story: the authenticated inbound webhook
surface that turns an external event into a governed agent run, on the
same policy, approval, and audit path as any other operation.

The shape is: register the sender as an **event source**, point it at the
ingest URL, and author a scheduler `event` trigger whose filter selects
the events you care about. A matching event fires an agent run — nothing
about that run is less governed for having started from a webhook.

!!! note "Prerequisites, roles, and maturity"

    - A running backplane the sender can reach, and a scheduler
      [`event` trigger](scheduler.md) to fire.
    - Registering an event source and authoring an `event` trigger both
      need **tenant_admin**; listing sources is operator-level.
    - The `event` trigger rides the scheduler, which is **Beta** — see
      the [feature-maturity index](../reference/maturity.md#scheduler).

## Register an event source

A webhook sender carries no login token, so the **event source** row is
the whole trust context of an inbound request: it names the tenant the
event belongs to, how the sender authenticates, and the secret to check
it against. Registration is an operator action — **REST, CLI, and the
console, with no MCP tool**.

```bash
# The slug is the positional argument; the secret is piped in, never
# passed on the command line.
printf '%s' "$SHARED_SECRET" | meho event-source add am-prod \
  --name alertmanager-prod \
  --kind alertmanager \
  --auth-strategy static-header \
  --secret-stdin
```

Two identifiers matter:

- **`name`** is unique within your tenant — how you refer to the source.
- The **slug** (`am-prod` above) is unique **globally** and is the
  routing key in the ingest URL, `POST /api/v1/events/ingest/<slug>`.
  That URL is JWT-less by design; the slug is a high-entropy routing
  token, and the sender proves itself with the source's secret, not a
  login. The secret is held in your secret store; rotating it later is an
  update with no downtime.

A source registered without a secret has no credential yet, so ingest
**fails closed** until you set one with a later update. A source can also
be **paused**: a paused or non-existent slug both return the same `404`,
so an outsider cannot probe which slugs are real.

## Webhook authentication modes

Each source declares one `auth_strategy`, checked in constant time on
every delivery. Header names are configurable per source, so a vendor
whose signature header differs is a configuration detail, not a special
case.

| Mode | How the sender proves itself |
|---|---|
| `hmac-sha256` | An HMAC of the raw request body, in a signature header — optionally binding a timestamp that is checked against a replay window. |
| `static-header` | A shared secret presented verbatim in a named header. |
| `basic` | HTTP Basic — the username is configuration, the password is the source's secret. |

Every rejection returns the **same** uniform `401`; the specific reason
is logged server-side, never in the response, so a probing sender learns
nothing.

## The shipped source kinds

Five source kinds ship with a per-vendor normalizer (below). Each covers
a family of senders; the "typical auth" column is the vendor's usual
story, but auth is per-source configuration, not welded to the kind.

| Kind | Covers | Typical auth |
|---|---|---|
| `alertmanager` | Prometheus Alertmanager, the Loki ruler, anything Alertmanager-compatible | `static-header` / `basic` |
| `grafana` | Grafana Alerting | `hmac-sha256` |
| `vcf-operations` | vCenter / NSX / vSAN alerts via the VCF Operations outbound webhook plugin | `static-header` |
| `harbor` | Registry automation — artifact push/pull/delete, scan complete/failed, quota, replication | `static-header` |
| `generic-json` | Templated senders — Argo CD, Proxmox VE, a Keycloak events plugin, custom scripts | `hmac-sha256` |

## What one delivery does

The ingest endpoint runs a fixed, fail-closed sequence for every
delivery, and the order is load-bearing:

1. **Resolve** the source by slug (uniform `404` if missing or paused).
2. **Body cap** (`413`) — enforced before the whole body is buffered, so
   an understated `Content-Length` cannot smuggle an oversize payload
   past it. A source may lower the cap, never raise it.
3. **Authenticate** (`401`) per the source's `auth_strategy`.
4. **Rate-limit** (`429` with `Retry-After`) per tenant and source.
5. **De-duplicate** — a delivery id (or a hash of the body) makes a
   redelivery collide at insert and return an idempotent `200`, so a
   retry-storming sender never double-fires a subscriber.
6. **Publish and audit in one transaction.** The event is written to the
   durable event log and a synchronous audit row is committed in the
   *same* transaction. As everywhere in MEHO, there is no success without
   a committed audit row — the ingest itself is on the ledger, recorded
   under a synthetic ingest identity because the sender has no operator
   login.

The event log is a transactional outbox: the event row is written in the
same commit as the state it records, and a background drain claims and
dispatches it. A pod restart loses nothing — the row is on disk and the
next drain picks it up.

### Normalization

Before an event is stored, its raw body is reduced to a normalized
envelope so filters are simple to write. The per-kind normalizer lifts
the fields you filter on to the **top level** and keeps the full sender
body verbatim under `raw`:

```json
{
  "status": "firing",
  "labels": {"severity": "critical", "alertname": "TargetDown"},
  "source":     {"slug": "am-prod", "kind": "alertmanager", "id": "<uuid>"},
  "event_type": "firing",
  "received_at": "2026-08-18T10:00:00+00:00",
  "raw":        { "...": "the full sender body, verbatim" }
}
```

Untrusted input is handled defensively throughout: a non-JSON body is a
`400`, a malformed or partially-shaped payload degrades to an empty match
set rather than crashing, and the raw body always survives under `raw`. A
bad payload fails closed, never `500`.

## Matching: `payload @> event_filter`

A scheduler `event` trigger carries an **event filter** — a small JSON
object. An event fires the trigger when the event **payload contains**
every key and value the filter names. The filter is a *subset* test, and
the direction is fixed: a filter is matched against the payload, not the
other way round.

```bash
# Fire the incident-triage agent on any critical, firing alert.
meho scheduler create --kind event \
  --agent-definition incident-triage \
  --event-filter '{"status": "firing", "labels": {"severity": "critical"}}'
```

Author filters against the lifted top-level fields:

| Kind | Example `--event-filter` | Matches |
|---|---|---|
| `alertmanager` | `{"status": "firing", "labels": {"severity": "critical"}}` | critical alerts, firing |
| `grafana` | `{"state": "alerting", "labels": {"severity": "warning"}}` | warning alerts still alerting |
| `vcf-operations` | `{"status": "active", "criticality": "critical"}` | active critical VMware alerts |
| `vcf-operations` | `{"resource_kind": "VirtualMachine"}` | any alert on a VM |
| `harbor` | `{"type": "SCANNING_COMPLETED"}` | a completed image scan |
| `generic-json` | `{"severity": "high"}` | any body with a top-level `severity: high` |

!!! warning "An empty filter matches every event of the tenant"

    `{}` names no constraints, so it matches everything — every source in
    the tenant shares one event stream. Always scope a filter to the
    fields (and the source's `kind`) you actually mean, or an unrelated
    alert will wake your agent.

## What fires, and how it stays governed

A match reuses the scheduler's own fire recipe, so an event-fired run is
identical to a scheduled one in every way that matters: the bound agent
definition supplies the identity and credentials, and the run dispatches
through the same policy, approval, and audit seam. A write it attempts
still parks for a human if the operation requires approval; every call it
makes still writes its own audit row.

- **Prompt.** If the trigger carries an `--inputs` prompt, the run uses
  it. If it does not, the matcher synthesizes a prompt from the matched
  event — and because the event came from outside, its text is wrapped as
  untrusted input before the agent sees it.
- **At-least-once, de-duplicated.** Delivery is at-least-once, but each
  fired run is keyed so a redelivery of the same event does not fire a
  second run.
- **Loop and storm guards.** An `event` trigger never fires from the
  completion event of the very agent it spawned, one misbehaving
  subscription never stalls the drain for others, and a run refused by
  its budget is logged once rather than retried into a storm.

The result is a closed loop: your monitoring stack raises an alert, MEHO
authenticates and records it, a filter selects it, and a governed agent
run investigates or remediates — with the whole chain, ingest through
dispatch, on the audit ledger.

## What can go wrong here

| Symptom | What it means | Fix |
|---|---|---|
| Every delivery returns `404` | The slug is wrong, or the source is paused — the two are deliberately indistinguishable. | Confirm the slug and that the source's status is active. |
| Deliveries return `401` | The signature or secret did not verify; the reason is never returned. | Re-check the sender's secret and signature header against the source's `auth_strategy`. |
| Ingest returns `200` but no agent run fires | The event was accepted but no `event` trigger's filter matched it. | Compare the trigger's `--event-filter` against the lifted top-level fields; test with a broader filter first. |
| One alert fires many agent runs | A filter is too broad (in the limit, `{}` matches everything). | Tighten the filter to the fields and source `kind` you mean. |
| A source registered but ingest still fails closed | It was registered without a secret. | Set the secret with an update before pointing the sender at it. |

**Next:** [Satellite gateway](satellite-gateway.md).
