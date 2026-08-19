# Event-subscription trigger — transactional outbox + drain (G11.3-T3 #824)

The third scheduler trigger shape: an agent run fires in response to a
MEHO-internal event (an audit predicate match, a connector alert, or
another agent run reaching a terminal state). The scheduler's cron +
one-off paths fire on a *clock* boundary ([scheduler.md](scheduler.md));
this path fires on an *event* boundary, which needs a different
substrate.

## Why a transactional outbox, not raw `LISTEN`/`NOTIFY`

The research note on #824 settled this before any code landed:
PostgreSQL `LISTEN`/`NOTIFY` is **not durable**. A `NOTIFY` fired while
no listener is connected is silently lost — the PG docs are explicit
([sql-notify.html](https://www.postgresql.org/docs/current/sql-notify.html)).
A multi-replica deployment that rolls a pod or evicts during a deploy
has a window with zero listeners; relying on `NOTIFY` for delivery
guarantees would silently drop escalations every release.

The PG-docs-recommended pattern is the textbook transactional outbox:
the producer writes an `event_outbox` row in the **same DB
transaction** as the event-producing state change. A separate drain
loop scans the outbox via `FOR UPDATE SKIP LOCKED`, claims unprocessed
rows, dispatches them, marks them processed. A pod restart loses
nothing: the rows are on disk and the next poll picks them up.
Multi-replica safety follows from `SKIP LOCKED` — two concurrent
drains never see the same row.

`LISTEN`/`NOTIFY` is layered on top as a **latency hint only**: the
producer's commit triggers a `NOTIFY` that wakes the drain's sleep
early, dropping per-event latency from the next 5-10s tick to
sub-second. A dropped notification is benign — the next polled tick
picks the row up anyway. The correctness primitive is the row on
disk; `NOTIFY` is a tail-latency optimisation.

## What's in the box

```
backend/src/meho_backplane/events/
├── __init__.py        # re-exports publish, start_event_drain, stop_event_drain
├── outbox.py          # producer-side publish() + post-commit NOTIFY hint
├── drain.py           # background drain loop + advisory-lock + claim + dispatch
├── matcher.py         # subscription matcher: payload @> event_filter -> fire triggers
├── ingest/            # inbound webhook path (#2881): auth, config, rate-limit, service
└── normalizers/       # per-kind payload normalisers + prompt synthesis (#2882)
    ├── __init__.py    #   registry + normalize_event() + synthesize_event_prompt()
    ├── base.py        #   NormalizedEvent + shared defensive accessors
    ├── alertmanager.py, grafana.py, vcf_operations.py, harbor.py, generic_json.py
```

Plus the persistence shape in
[backend/src/meho_backplane/db/models.py](backend/src/meho_backplane/db/models.py):

- `EventOutbox` — one row per emitted event. Columns: `event_id` (PG
  `BIGSERIAL` / SQLite `Integer`), `tenant_id` (FK), `event_kind`
  (free-text discriminator), `payload` (JSONB / JSON), `created_at`,
  `claimed_at` (observability), `claimed_by` (observability),
  `processed_at` (`NULL` until claimed and dispatched), `dedupe_key`
  (`NULL` for internal producers; a producer-supplied idempotency token
  for at-least-once external ingest), and `origin` (`NULL` = internal
  producer, else the external event-source id). Two partial indexes:
  `(processed_at, event_id)` on `processed_at IS NULL` drives the
  drain's claim query, and the **unique** `event_outbox_tenant_dedupe_idx`
  on `(tenant_id, dedupe_key)` `WHERE dedupe_key IS NOT NULL` makes a
  duplicate external delivery collide at insert.
- `EVENT_OUTBOX_NOTIFY_CHANNEL` — the PG channel name the producer
  side `NOTIFY`s on and the drain side `LISTEN`s on.

Migrations: `backend/alembic/versions/0027_create_event_outbox.py`
(revises `0026`, #1125's agent-run lease/reaper columns) created the
table; `0073_event_outbox_dedupe_key_origin.py` (#2879) adds the
`dedupe_key` / `origin` columns and the partial unique dedupe index.

## The producer side — `publish()`

The single function `events.publish` is the producer-side surface.
Call it inside the producer's *open* `AsyncSession` so the outbox
INSERT shares the producer's commit:

```python
from meho_backplane.events import publish as publish_event

async def succeed_run(session, run, output):
    # ... mutate the agent_run row ...
    await publish_event(
        session,
        tenant_id=run.tenant_id,
        event_kind="agent_run.completed",
        payload={
            "run_id": str(run.id),
            "agent_definition_id": str(run.agent_definition_id),
            "status": "succeeded",
            "tenant_id": str(run.tenant_id),
        },
    )
    # caller commits; both the agent_run UPDATE and the outbox
    # INSERT land in the same transaction
```

Same-transaction discipline is the **load-bearing invariant** of the
whole feature:

- If the producer's transaction commits, both the state change and
  the outbox row are durable; the drain picks the row up on the next
  tick (or sooner via the `NOTIFY` hint).
- If the producer's transaction rolls back, the outbox row rolls back
  with it; no orphan event is ever dispatched for state that didn't
  land.

`publish` flushes (so `event_id` is populated) but does not commit —
the caller's transaction owns the commit. Two tests pin this
contract:

- `test_publish_rolls_back_with_producer` — explicit rollback on the
  shared session discards both the producer's INSERT and the outbox
  row.
- `test_terminal_event_rolls_back_with_run_transition` — exercises
  the same invariant at the real call site (`operations/agent_run.py`
  terminal transition).

### Post-commit `NOTIFY` hint

`publish` attaches a one-shot `after_commit` listener via SQLAlchemy
events. When the producer's transaction commits, the listener opens a
*fresh* short-lived connection through the engine and fires
`NOTIFY <channel>`. Notes:

- **Why a fresh connection, not the producer's:** the producer's
  connection may already be returned to the pool by the time
  `after_commit` fires (FastAPI's request-scoped session dependency
  shape). `NOTIFY` is fire-and-forget so the cost of `engine.connect()`
  + immediate close is acceptable.
- **`once=True` de-duplication:** a batched producer that publishes
  N events in one transaction notifies *once*, not N times. Cheap
  and avoids notify-storms.
- **Dialect-gated:** the listener body checks `dialect.name ==
  "postgresql"` and silently skips on SQLite (the unit-test path) —
  same gate the scheduler's advisory-lock path uses.
- **Failure is silent:** if the `NOTIFY` `engine.connect()` raises,
  the listener swallows it. The durable channel is the outbox row;
  `NOTIFY` is a latency hint and never blocks producer commit
  success.

## The drain side — the loop

`drain.py`'s background `asyncio` task is wired into the FastAPI
lifespan via `start_event_drain` / `stop_event_drain` in
[backend/src/meho_backplane/main.py](backend/src/meho_backplane/main.py).
It is gated on `EVENT_DRAIN_ENABLED=true` (default), the same shape as
the scheduler / topology-refresh / memory-expiry / grant-expiry
sweepers.

On each tick (default 10s, settable via
`EVENT_DRAIN_TICK_INTERVAL_SECONDS`):

1. **Claim the process-wide advisory lock.**
   `pg_try_advisory_lock(0x4D45_484F_4556_5442)` — see
   [Advisory-lock keys](#advisory-lock-keys) below. Only one
   replica's drain runs the tick body at a time. A second replica's
   tick is a no-op until the holder releases. The lock is hosted on a
   dedicated pinned connection
   (`meho_backplane/db/advisory.py::advisory_lock`, #3010 — advisory
   lock and unlock must run on the same connection, and the drain's
   mid-tick commits would strand a lock taken on the work session).

2. **Scan + claim unprocessed rows** via:

   ```sql
   SELECT * FROM event_outbox
   WHERE processed_at IS NULL
   ORDER BY event_id
   LIMIT :batch
   FOR UPDATE SKIP LOCKED
   ```

   `SKIP LOCKED` is the belt-and-braces guarantee: even with the
   advisory-lock guard removed, two concurrent claimers never receive
   the same row. The partial index
   `event_outbox_unprocessed_idx ON (processed_at, event_id) WHERE
   processed_at IS NULL` keeps the claim query O(log unprocessed)
   rather than O(total rows).

3. **Stamp `claimed_at` + `claimed_by`** (observability — the
   `claimed_by` column carries `pod_name` so a stuck poll can be
   traced to the holding replica). The claim is a separate UPDATE on
   the same open session as the SELECT; the FOR UPDATE row-lock
   carries through to the UPDATE.

4. **Dispatch each row** through the subscription matcher
   ([The subscription matcher](#the-subscription-matcher) below): fire
   every active `kind='event'` trigger whose `event_filter` the payload
   contains, then stamp `processed_at`. An event that matches no
   subscriber is still durably consumed (stamped, no fire, no log
   noise). The claim stamps are committed *before* this fire step so
   the drain holds no open write across a subscriber's independently-
   committed run row (mirrors the scheduler-fire ordering).

5. **Mark each row processed** via a conditional UPDATE
   (`processed_at IS NULL` predicate). The conditional UPDATE is the
   single-processing invariant on SQLite where SKIP LOCKED is a
   no-op; on PG it's defensive belt-and-braces. Per-row failure is
   isolated by an inner `try`/`except` so one bad row never stalls
   the rest of the tick.

6. **`LISTEN` for `NOTIFY`** (concurrent with the polling sleep).
   The drain holds a long-lived engine connection on which it
   `LISTEN <channel>`s; a producer's post-commit `NOTIFY` lands as a
   wake-up that cuts the sleep short. The next iteration starts
   immediately rather than waiting for the next tick boundary. The
   listener is **not durable** — a NOTIFY missed during a drain
   restart is benign because the polling cadence picks the row up
   anyway. Reconnect-with-backoff for the listen connection is a
   v0.3 polish item; the current shape degrades to polling-only on
   listen-connection failure with no retry.

## Advisory-lock keys

Two PG advisory locks coexist on the same DB; each has a distinct
63-bit signed-int key so the locks never collide:

| Key | Hex | ASCII | Owner |
|---|---|---|---|
| `_SCHEDULER_ADVISORY_LOCK_KEY` | `0x4D45_484F_5343_4844` | `MEHOSCHD` | `scheduler/loop.py` (cron + one-off, T2 #1065) |
| `_EVENT_DRAIN_ADVISORY_LOCK_KEY` | `0x4D45_484F_4556_5442` | `MEHOEVTB` | `events/drain.py` (event outbox, this Task) |

Distinct keys mean both loops can run concurrently without starving
each other — the scheduler's lock holder does not block the drain's
claim, and vice versa. The ASCII spellings make the keys easy to
recognise in `pg_locks` during operator triage.

## The subscription matcher

`backend/src/meho_backplane/events/matcher.py` is the junction between a
drained event and the agent runs it fires. The drain calls
`fire_matching_triggers(event, invoker)` for each claimed row (#2878).

### Match semantics — `payload @> event_filter` (direction is load-bearing)

An event matches a trigger when the event **`payload` contains** every
key/value the trigger's **`event_filter`** names — the Postgres jsonb
`@>` direction, the same one the retrieval metadata-filter path uses
(`doc_metadata @> :filters`). Concretely:

- A filter `{"status": "succeeded"}` matches a payload
  `{"run_id": "…", "status": "succeeded", …}` — the filter is a subset
  of the payload.
- The **reverse never matches**: a payload that is a subset of the
  filter is not a match (unless they are equal). Inverting the
  operands would match nothing useful, so the direction is pinned by a
  test.
- An **empty filter `{}` matches every event** of the tenant (it names
  no constraints). `event_filter` is matched, `event_kind` is not — a
  subscriber filters on payload fields (for `agent_run.completed`:
  `run_id` / `tenant_id` / `status` / `agent_definition_id` /
  `work_ref`).

### Why one pure-Python predicate on both dialects (not native `@>` on PG)

`_payload_contains` is a single pure-Python containment predicate
(recursive: object ⊇ object, array ⊇ array subset, scalar equality)
used on **both** PG and SQLite, rather than the native jsonb `@>` on PG
with a portable fallback on SQLite. One code path is deliberate:

- The drain's tests run on SQLite, so a single predicate means those
  tests exercise the **exact** code that also runs on Postgres. Two
  implementations (native `@>` + a fallback) could silently diverge on
  nested/array shapes — precisely the "works on PG **and** SQLite"
  contract this Task must hold.
- There is **no GIN index** on `scheduled_trigger.event_filter`, so a
  native `@>` gives no index-backed speed-up over loading the tenant's
  (dozens of) event triggers and filtering them in Python — which the
  drain already does row-by-row.

### The fire recipe — reuse of the scheduler seam

The matcher reuses the cron/one-off fire recipe verbatim
(`scheduler/loop.py`): `_prepare_invocation` resolves the definition +
credentials (the agent's `client_credentials` secret stays a
`SecretStr` end-to-end, CWE-532), and `_dispatch_invocation` calls
`AgentInvoker.run_scheduled` and treats `BudgetExceededError` as a
do-not-retry single-log refusal (`scheduler_invoke_refused` with the
`budget_reason` tag). An event storm therefore never blasts through a
budget kill switch — the refusal is logged once per matched trigger and
the drain tick completes. Only two values are overridden on the
prepared invocation:

- **`work_ref` = `event:{event_id}:{trigger_id}`** — the fire-dedupe
  key (below). The dispatched run inherits it through the trigger→run
  `work_ref_var` seam `run_scheduled` already plumbs.
- **`inputs`** — a `kind=event` trigger is exempt from the create-time
  non-empty-inputs rule (`scheduler/schemas.py`), so an input-less
  trigger reaches the matcher with no operator prompt. Rather than let
  the fire fail typed at the no-input guard, the matcher **synthesises
  a prompt from the matched event**. The event kind + payload are
  untrusted, so the composed body is wrapped with
  `wrap_untrusted_text` (`untrusted_text.py`). A trigger that *does*
  carry a usable `inputs` prompt uses that instead.

### Redelivery dedupe — the `work_ref`

Delivery is **at-least-once**: a fired subscriber's `agent_run` commits
in its own transaction, and the drain stamps `processed_at` in a
separate one. A crash between the two leaves the event unprocessed and
the next tick re-matches it. `_run_exists_for_work_ref` makes that
idempotent: before firing, the matcher checks
`agent_run_tenant_work_ref_idx` for **any** run (terminal or not)
carrying `event:{event_id}:{trigger_id}` — a hit means the first
delivery already fired, so the redelivery is skipped. A single drain
replica runs at a time (the drain advisory lock), so this check-then-
fire never races itself in production. (This overloads `work_ref` — a
change-ticket field for scheduled triggers — as the dedupe key; a
sibling Task, #2879, adds a first-class `dedupe_key` column, out of
scope here.)

### Storm controls

- **Self-trigger guard**: a `kind=event` trigger never fires from the
  `agent_run.completed` event of the agent it spawns (the direct
  self-loop), suppressed in `_fire_event_trigger` and logged once as
  `event_trigger_self_fire_suppressed` (see below).
- **Budget gate**: `BudgetExceededError` refusals do not retry and log
  once — an event storm cannot repeatedly blast a kill switch.
- **Zero-match quiet**: an event matching no subscriber is stamped with
  no per-event log line.
- **Per-trigger isolation**: one subscription raising never stalls the
  others or the drain tick.

### Avoiding self-triggering loops

A fired agent run reaches a terminal state, which the producer emits as
its own `agent_run.completed` event. A subscription with a filter broad
enough to match *that* event (e.g. `{"status": "succeeded"}` — every
successful run) would fire again on the run it just spawned, and so on:
a feedback loop that, on a default install with no budget row, is
bounded only by the manual kill switch. Two controls contain it:

- **Matcher self-trigger guard (defence in depth).**
  `_fire_event_trigger` refuses to fire a trigger from the
  `agent_run.completed` event of the very agent that trigger spawns: it
  skips when `event_kind == agent_run.completed` and the event payload's
  `agent_definition_id` equals the trigger's `agent_definition_id`,
  logging `event_trigger_self_fire_suppressed` once. This breaks the
  *direct* one-hop self-loop with no schema change, while leaving
  legitimate cross-agent chains intact — a trigger that spawns a
  *different* definition Y off X's completion still fires because the
  ids differ.
- **Subscription filter (the recommended discipline).** The guard only
  covers the one-hop self-loop; a longer cycle (X → Y → X) slips past
  it. Subscribe to a **specific upstream** by including
  `agent_definition_id` (or a `run_id`) in the `event_filter`, which the
  payload carries for exactly this reason, so a subscription only wakes
  on the intended producer.

Work_ref dedupe does **not** help here — each distinct event has a
distinct `event_id`, hence a distinct work_ref.

## Inbound external event ingest (#2881)

The producer side above is MEHO-internal (an agent run completing).
`POST /api/v1/events/ingest/{source_slug}`
(`api/v1/events_ingest.py` + `events/ingest/`) is the **external**
producer: a JWT-less webhook a registered `event_source` (#2880)
delivers to, which lands one `event_outbox` row the same drain + matcher
route to `kind=event` triggers. It is the only `/api/v1` route with **no
operator dependency** — the sender is authenticated per-source against
the source's Vault-custodied secret, not by a Keycloak JWT.

### Request path (the order is load-bearing)

1. **Resolve** the source by slug via
   `resolve_event_source_by_slug` (global — the webhook carries no
   tenant). A missing *or* paused source returns the identical uniform
   `404`, so the two are indistinguishable (no paused-vs-missing oracle).
2. **Body cap** (`413`) is enforced *before* the whole body is buffered:
   a `Content-Length` fast-reject plus a streaming running-total guard
   (`_read_capped_body`) so an absent/understated length header cannot
   smuggle an oversize body past it (the `ui/csrf.py` DoS lesson). The
   cap defaults to 256 KiB; a source's `extras["max_body_bytes"]` can
   only *lower* it, never raise it above the ceiling.
3. **Auth** (`401`) per the source's `auth_strategy`, **constant-time**
   (`events/ingest/auth.py`, every secret comparison through
   `hmac.compare_digest`): `hmac-sha256` over the raw body (with an
   optional timestamp bound into the signature and gated by a replay
   window), `static-header`, or `basic` (username from `extras`,
   password is the secret). Header names are `extras`-configurable, so a
   vendor whose headers differ (Grafana's `X-Grafana-Alerting-Signature`)
   is config, not a code branch — richer per-vendor normalisation is
   #2882. The failure reason is logged server-side, never returned, so
   every rejection is the same uniform `401`.
4. **Rate limit** (`429` + `Retry-After`) — the broadcast fixed-window
   `INCR`/`EXPIRE` pattern under a new key namespace
   `meho:ratelimit:ingest:{tenant}:{source}` (`events/ingest/rate_limit.py`).
   Fail-loud on a Valkey outage (`503`; a webhook sender retries).
5. **Dedupe** — the sender's delivery id where one exists, else a SHA-256
   of the raw body, namespaced by source id, written as `event_outbox.dedupe_key`
   (#2879). A `(tenant_id, dedupe_key)` collision (the partial unique
   index) is caught as an `IntegrityError` and mapped to an idempotent
   `200` — never a second outbox row, never a second audit row, so a
   retry-storming sender never double-fires a subscriber.
6. **Publish + audit in one transaction.** `events.publish()` (now
   carrying `origin` = source id and the `dedupe_key`) inserts the
   outbox row in the **same** `session.begin()` block as a synchronous
   audit row under the synthetic `__ingest__` identity (the `__sensor__`
   / `__scheduler__` sentinel precedent; `audit_log.operator_sub` is
   plain `Text`). No success without a committed audit row (postulate 7).
   A **fail-open** broadcast follows the commit.

### The `__ingest__` audit + Vault-read identities

Ingest is authenticated per-source, not per-operator, so there is no JWT
to derive an `operator_sub` from — the chassis `AuditMiddleware` (which
keys on the JWT-bound contextvar) writes no row for it, and the ingest
service writes its own `__ingest__` row instead. Reading the source's
secret still needs a Vault client, which needs an operator: the
`__ingest__` operator reuses the backplane's existing
background-dispatch service-principal token (`check_runner_jwt`, the
same "MEHO's own in-process dispatch, outward" identity the sensor
check-runner uses) with `check_runner_dispatch=False` so the Vault login
uses the standard `vault_oidc_role`. When that principal is unconfigured
the token is empty, the Vault read fails, and ingest fails closed — the
correct posture. A dedicated ingest principal (separately bounded Vault
role) is a future refinement.

### Reconciliation note (404 vs 401)

The #2880 resolver docstring suggests mapping a missing slug to the
*same* response as an auth failure. This endpoint instead follows the
#2881 contract: missing/paused → `404`, bad signature → `401`. The
residual "a 404 vs 401 distinguishes a real slug" oracle is acceptable
because a slug is a high-entropy routing token (not enumerable), while
the uniform 404 still hides the operationally sensitive paused-vs-missing
distinction.

## First-wave payload normalisers (#2882)

#2881 lands one `event_outbox` row per delivery, but its normalisation was
**lite** — the whole body under a single `data` key. #2882 replaces that with
a **per-kind normaliser** selected by the resolved source's
`EventSourceKind`, plus per-vendor **prompt synthesis** for the fired agent.
The code is `events/normalizers/`; the two seams it plugs into are
`events/ingest/service.py::_normalise` (ingest time) and
`events/matcher.py::_synthesize_event_prompt` (fire time).

### The normalised envelope

Every normaliser reduces the raw body to a `NormalizedEvent(event_type,
match_fields, raw)`, and the ingest service builds the outbox `payload` from
it:

```json
{
  "<canonical match fields, lifted to the top level>": "...",
  "source":     {"slug": "...", "kind": "alertmanager", "id": "<uuid>"},
  "event_type": "firing",
  "received_at": "2026-08-18T10:00:00+00:00",
  "raw":        { "...": "full sender body, verbatim" }
}
```

- **`event_kind`** (the outbox column, not a payload key) is
  `external.{kind}.{event_type}`, e.g. `external.alertmanager.firing`.
- **Match fields are lifted to the top level** so `event_filter` authoring is
  simple: a filter names the field directly (`{"status": "firing"}`), no
  `raw.` prefix, and the matcher's `payload @> event_filter` containment does
  the rest.
- **The four reserved keys** (`source` / `event_type` / `received_at` / `raw`)
  are written **last**, so a vendor match field can never shadow them. A body
  whose own top-level key collides with a reserved name (possible only for
  `generic-json`) is shadowed at the top level but stays filterable under
  `raw` (`{"raw": {"source": "..."}}`).
- **Untrusted input is defensive throughout.** A wrong-typed / missing field
  degrades to absent (pruned), and a top-level **array or scalar** body (legal
  JSON that is not an object) normalises to an empty match set with the body
  under `raw` — never a crash. Combined with the #2881 parse boundary (a
  non-JSON body → `400`), a malformed or partial vendor payload always
  **fails closed**, never `500`.

### Source-kind matrix

| Kind | Covers | Typical sender auth | `event_type` from | Lifted match fields |
|---|---|---|---|---|
| `alertmanager` | Prometheus, Loki ruler, anything AM-compatible | bearer / basic (`static-header` / `basic`; no native HMAC) | group `status` (`firing`/`resolved`) | `status`, `labels` (commonLabels), `annotations` (commonAnnotations), `alertname`, `receiver`, `num_alerts` |
| `grafana` | Grafana Alerting ≥ 12.0 | `hmac-sha256` (`X-Grafana-Alerting-Signature` + timestamp) | group `status` | AM core + `state` (`alerting`/`ok`), `title` |
| `vcf-operations` | vCenter / NSX / vSAN alerts via the VCF Operations outbound webhook plugin — the only viable vSphere push path in 2026 | `static-header` (Auth header) | `status` | `alert_name`, `status`, `criticality`, `alert_type`, `sub_type`, `resource_name`, `resource_kind`, `adapter_kind`, `alert_id` |
| `harbor` | Registry automation: push/pull/delete artifact, scan complete/failed, quota, replication, tag-retention | `static-header` (Auth Header) | `type` (`PUSH_ARTIFACT`, `SCANNING_COMPLETED`, …) | `type`, `operator`, `repository`, `namespace` |
| `generic-json` | Templated senders: ArgoCD, Proxmox VE ≥ 8.3, Keycloak events SPI, custom scripts | `hmac-sha256` (`X-Meho-Signature` + timestamp) | top-level `type` | **every** top-level key |

Auth is per-source config (`event_source.extras`, #2881), not welded to the
kind — the column above is the vendor's usual story. Header names
(`X-Grafana-Alerting-Signature`, `X-Meho-Signature`) are `extras` overrides,
so vendor-specific signing stays config, never a normaliser branch.

### Filter recipes

Author `event_filter` against the lifted top-level fields:

| Kind | Example `event_filter` | Matches |
|---|---|---|
| `alertmanager` | `{"status": "firing", "labels": {"severity": "critical"}}` | critical firing alerts |
| `grafana` | `{"state": "alerting", "labels": {"severity": "warning"}}` | warning alerts still alerting |
| `vcf-operations` | `{"status": "active", "criticality": "critical"}` | active critical VMware alerts |
| `vcf-operations` | `{"resource_kind": "VirtualMachine"}` | any alert on a VM |
| `harbor` | `{"type": "SCANNING_COMPLETED", "repository": {"namespace": "prod"}}` | scans finishing in the `prod` project |
| `generic-json` | `{"severity": "high"}` | any body with a top-level `severity: high` |

`{}` (empty filter) matches every event of the tenant — scope with the source
`kind` in mind, since a tenant's sources share the outbox.

### VCF Operations: the recommended payload template

The VCF Operations Webhook notification plugin's body is **operator-templated**
(Configure → Alerts → Payload Templates), so there is no vendor-fixed schema.
Paste a template that emits the top-level keys the normaliser lifts, mapping
each from the plugin's parameter browser (the confirmed parameters
`${ALERT_DEFINITION}` and `${RESOURCE_NAME}` are shown; select the rest from
the same browser):

```json
{
  "alert_name":    "${ALERT_DEFINITION}",
  "status":        "<alert status parameter>",
  "criticality":   "<alert criticality parameter>",
  "alert_type":    "<alert type parameter>",
  "sub_type":      "<alert sub-type parameter>",
  "resource_name": "${RESOURCE_NAME}",
  "resource_kind": "<object type parameter>",
  "adapter_kind":  "<adapter kind parameter>",
  "alert_id":      "<alert id parameter>"
}
```

An operator who keeps a different template still ingests fine — their fields
are absent from the match set but preserved verbatim under `raw`, filterable
as `{"raw": {...}}`.

### Prompt synthesis (fire time)

When a matched `kind=event` trigger carries no operator `inputs`, the matcher
synthesises the fired agent's prompt from the event via
`normalizers.synthesize_event_prompt(event_kind, payload)`. It builds a
**per-vendor triage body** for an `external.{kind}.*` event (e.g. an
Alertmanager alert → "alert group '<name>' is firing … Triage this alert …")
and the generic structured render for an internal producer event
(`agent_run.completed`). Either way the composed body is **always** wrapped
with `wrap_untrusted_text` (`untrusted_text.py`): the inbound payload is
untrusted external input, so the fired agent sees it as data to act on, not a
directive channel — and a body that forges the envelope terminator cannot
escape the block (the wrapper emits the terminator positionally). The trusted
lead-in sentence sits *outside* the envelope; every interpolated sender value
sits *inside* it.

### Events (push) vs. Sensors (pull) — operator guidance

The events plane is **push**: a vendor delivers a webhook the instant
something happens, and a subscribed agent fires with sub-second latency. The
Sensors / Checks plane ([sensor.md](sensor.md)) is **pull**: it polls an
operation on a cadence and asserts `degraded` / `critical` thresholds, rolling
state up into dashboards. They are complementary, not alternatives:

- **Reach for events** when the vendor *can* push and you want low-latency
  agent triage off a discrete alert (an Alertmanager critical → an agent run).
- **Reach for sensors** when you need MEHO to actively *watch* a metric the
  vendor will not push, or you want threshold roll-ups, dashboards, and the
  diagnose-only investigator.

**Pull stays the metrics-paging plane; push is the low-latency escalation
plane.** A vendor with no webhook (bare vCenter) is a sensor target; a vendor
that aggregates and pushes (VCF Operations) is an event source.

## Deferred work

### Reconnect-with-backoff for the `LISTEN` connection

`_listen_for_notify` holds one engine connection for the lifetime of
the drain task. On any connection failure it silently degrades to
polling-only with no retry. Acceptable for v0.2 (the outbox is
durable via polling; tail latency reverts to the tick cadence rather
than sub-second) but a reconnect-with-backoff loop would protect
tail latency across PG restarts.

### Per-second `NOTIFY` cost at high producer throughput

Every `publish` registers a fresh `after_commit` listener that opens
a fresh engine connection. The `once=True` listener dedups per-commit,
but N producer transactions per second still cost N `engine.connect()`
round-trips. v0.2's anticipated "dozens per minute" volume keeps this
comfortable; revisit if a connector emits bursts.

## Settings

Both gated via env vars (defaults shown in parens):

- `EVENT_DRAIN_ENABLED` (`true`) — start the drain task at lifespan
  startup. `false` opts out cleanly (the start helper returns `None`
  and the lifespan shutdown tolerates that shape).
- `EVENT_DRAIN_TICK_INTERVAL_SECONDS` (`10`, range `[1, 3600]`) —
  bound between consecutive polled scans. The `NOTIFY` hint can cut
  the actual wake-up shorter than this interval; this bounds the
  *worst-case* poll latency when no producers fire and no listeners
  are connected.
- `EVENTS_INGEST_RATE_PER_MINUTE` (`60`, `0` disables) — the default
  per-source fixed-window cap for the inbound ingest endpoint (#2881). A
  source overrides it via `event_source.extras["rate_per_minute"]`
  (`0` there disables the limit for that one source).

## Test coverage

- [backend/tests/test_event_outbox.py](backend/tests/test_event_outbox.py)
  — producer + drain unit tests:
  - `test_publish_inserts_outbox_row_in_caller_transaction`
  - `test_publish_rolls_back_with_producer`
  - `test_succeed_run_publishes_outbox_event_in_same_tx`
  - `test_fail_run_publishes_outbox_event`
  - `test_terminal_event_rolls_back_with_run_transition`
  - `test_drain_no_double_process_under_concurrency` — two concurrent
    ticks, sum of processed == N exactly
  - `test_restart_durability_drains_unprocessed_rows` — publish →
    simulate kill → restart → tick drains the row
- [backend/tests/test_event_matcher.py](backend/tests/test_event_matcher.py)
  — subscription-matcher unit tests (#2878): containment direction,
  end-to-end `agent_run.completed` → matching trigger → agent run with
  work_ref, non-matching filter fires nothing, redelivery dedupe,
  `BudgetExceededError` no-retry, input-less trigger fires with a
  synthesised untrusted-enveloped prompt.
- [backend/tests/integration/test_event_matcher_pg.py](backend/tests/integration/test_event_matcher_pg.py)
  — the same end-to-end chain against a real Postgres container (#2878).
- [backend/tests/migrations/test_migration_0027_event_outbox.py](backend/tests/migrations/test_migration_0027_event_outbox.py)
  — schema + index migration round-trip
- [backend/tests/migrations/test_migration_0073_event_outbox_dedupe.py](backend/tests/migrations/test_migration_0073_event_outbox_dedupe.py)
  — `dedupe_key` / `origin` additive columns + partial unique dedupe
  index (per-tenant uniqueness, NULL-unconstrained, SQLite partial-index
  parity, downgrade round-trip)
- Inbound ingest (#2881):
  - [backend/tests/test_events_ingest_auth.py](backend/tests/test_events_ingest_auth.py)
    — per-strategy constant-time auth: bad signature, replay window,
    static-header, basic, and the `compare_digest`-for-every-secret guard
  - [backend/tests/test_events_ingest_rate_limit.py](backend/tests/test_events_ingest_rate_limit.py)
    — 429 + `Retry-After`, per-source isolation, `0` disables, Valkey
    fail-loud
  - [backend/tests/test_events_ingest_config.py](backend/tests/test_events_ingest_config.py)
    — per-source `extras` config resolution + security clamps
  - [backend/tests/test_api_v1_events_ingest.py](backend/tests/test_api_v1_events_ingest.py)
    — endpoint: uniform 404, 413-before-buffer, 401, 202 + outbox
    `origin`/`dedupe_key` + `__ingest__` audit row, idempotent 200,
    broadcast fail-open, 429/400 mapping
  - [backend/tests/test_events_ingest_e2e.py](backend/tests/test_events_ingest_e2e.py)
    — POSTed event → drain tick → matched `kind=event` trigger → agent run
- First-wave normalisers (#2882):
  - [backend/tests/test_event_normalizers.py](backend/tests/test_event_normalizers.py)
    — golden per-vendor payloads (Alertmanager v4, Grafana ≥ 12,
    VCF Operations template, Harbor default + CloudEvents, generic-json):
    `event_type` + match-field extraction, the top-level lift + filter-recipe
    containment (`payload @> event_filter`), reserved-key precedence,
    non-object-body fail-closed, the registry-covers-every-kind drift guard,
    and per-vendor untrusted-enveloped prompt synthesis (incl. the
    forged-terminator no-escape assertion)

## References

- Goal #800 (G11 Agentic ops runtime)
- Initiative #804 (G11.3 Scheduler P2)
- This Task #824 — research note (transactional-outbox vs
  LISTEN/NOTIFY durability)
- Sibling Tasks: T1 #822 (substrate-decision spike), T2 #1065 (cron +
  one-off, merged), T4 #825 (in-flight resume), T5 #826 (admin surface)
- Task #2878 — the subscription matcher + `kind=event` dispatch
  (`events/matcher.py`); Initiative #2877 (inbound event ingestion)
- Task #2879 — `event_outbox.dedupe_key` / `origin` columns the ingest
  path populates; Task #2880 — the `event_source` registry + Vault
  secret custody the ingest path resolves against; Task #2881 — this
  inbound ingest endpoint (`api/v1/events_ingest.py`, `events/ingest/`);
  Task #2882 — the first-wave per-vendor payload normalisers + prompt
  synthesis (`events/normalizers/`)
- Companion: [scheduler.md](scheduler.md) — the cron + one-off
  substrate this layer joins on
- PG `LISTEN`/`NOTIFY` durability:
  https://www.postgresql.org/docs/current/sql-notify.html
- `pg_try_advisory_lock`:
  https://www.postgresql.org/docs/current/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS
- `FOR UPDATE SKIP LOCKED`:
  https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE
